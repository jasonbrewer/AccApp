"""FastAPI app: endpoints + static frontend.

This layer is deliberately thin. It picks folders, holds settings, starts a
background thread, and reports progress. Every byte of copy/verify/manifest
logic lives in ``footage_pipeline.backup.core`` — nothing here duplicates it.
"""

from __future__ import annotations

import copy
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..config import Settings, load_settings, save_settings
from ..logging_setup import configure_logging, get_logger
from ..backup.core import (
    BackupEngine,
    BackupError,
    InsufficientSpaceError,
    Progress,
    human_bytes,
)

log = get_logger("web")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Footage Pipeline", version="0.1.0")


# --------------------------------------------------------------------------
# Run state (one backup at a time)
# --------------------------------------------------------------------------

@dataclass
class RunState:
    """Snapshot of the current or most recent run, polled by the frontend."""

    running: bool = False
    source: str | None = None
    backup_root: str | None = None
    progress: dict = field(default_factory=lambda: Progress().to_dict())
    report: dict | None = None
    error: str | None = None


class RunManager:
    """Owns the background thread and the state the UI polls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = RunState()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._state.running,
                "source": self._state.source,
                "backup_root": self._state.backup_root,
                "progress": copy.deepcopy(self._state.progress),
                "report": copy.deepcopy(self._state.report),
                "error": self._state.error,
            }

    def start(self, source: str, backup_root: str) -> None:
        with self._lock:
            if self._state.running:
                raise RuntimeError("A backup is already running.")
            self._state = RunState(
                running=True, source=source, backup_root=backup_root
            )
        self._thread = threading.Thread(
            target=self._run, args=(source, backup_root), daemon=True,
            name="backup-run",
        )
        self._thread.start()

    def _on_progress(self, progress: Progress) -> None:
        with self._lock:
            self._state.progress = progress.to_dict()

    def _run(self, source: str, backup_root: str) -> None:
        try:
            engine = BackupEngine(
                source_root=source,
                backup_root=backup_root,
                progress_cb=self._on_progress,
            )
            result = engine.run()
            report = {
                "outcome": result.outcome.value,
                "passed": result.passed,
                "run_id": result.manifest.run_id,
                "manifest_path": str(result.manifest_path),
                "log_path": str(result.log_path),
                "totals": result.totals.to_dict(),
                "bytes_copied_human": human_bytes(result.totals.bytes_copied),
                "bytes_total_human": human_bytes(result.totals.bytes_total),
                "skipped_symlinks": [s.to_dict() for s in result.manifest.skipped_symlinks],
                "conflicts": [
                    f.rel_path for f in result.manifest.files if f.status.value == "CONFLICT"
                ],
                "failures": [
                    {"rel_path": f.rel_path, "error": f.error}
                    for f in result.manifest.files if f.status.value == "FAILED"
                ],
                "notes": list(result.manifest.notes),
            }
            with self._lock:
                self._state.report = report
                self._state.error = None
        except InsufficientSpaceError as err:
            log.error("Backup refused: %s", err)
            with self._lock:
                self._state.error = str(err)
        except BackupError as err:
            log.error("Backup refused: %s", err)
            with self._lock:
                self._state.error = str(err)
        except Exception as err:  # noqa: BLE001 - surface anything else to the UI
            log.exception("Backup run crashed")
            with self._lock:
                self._state.error = f"{type(err).__name__}: {err}"
        finally:
            with self._lock:
                self._state.running = False
                self._state.progress["phase"] = "finished"


runs = RunManager()


# --------------------------------------------------------------------------
# Native folder picker
# --------------------------------------------------------------------------

def choose_folder_native(prompt: str = "Choose a folder") -> str | None:
    """Open the macOS folder chooser and return an absolute POSIX path.

    A browser <input webkitdirectory> cannot give us a real filesystem path,
    which is why this goes through AppleScript instead. Returns None if the
    user cancels.
    """
    if sys.platform != "darwin":
        raise HTTPException(
            status_code=501,
            detail=(
                "The native folder picker requires macOS. On this platform, type or "
                "paste an absolute path instead."
            ),
        )

    script = f'POSIX path of (choose folder with prompt "{prompt}")'
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise HTTPException(status_code=500, detail=f"Folder picker failed: {err}") from err

    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        if "User canceled" in stderr or "-128" in stderr:
            return None
        raise HTTPException(status_code=500, detail=f"Folder picker failed: {stderr}")

    path = completed.stdout.strip()
    if not path:
        return None
    # AppleScript hands back a trailing slash; normalise it away.
    return str(Path(path))


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class PickRequest(BaseModel):
    prompt: str = "Choose a folder"


class BackupRootRequest(BaseModel):
    backup_root: str


class StartRequest(BaseModel):
    source: str


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings() -> dict:
    settings = load_settings()
    return {
        "backup_root": settings.backup_root,
        "last_source": settings.last_source,
        "backup_root_exists": bool(settings.backup_root)
        and Path(settings.backup_root).is_dir(),
        "native_picker": sys.platform == "darwin",
    }


@app.post("/api/settings/backup-root")
def set_backup_root(body: BackupRootRequest) -> dict:
    path = Path(body.backup_root).expanduser()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a folder: {path}")
    settings = load_settings()
    settings.backup_root = str(path.resolve())
    save_settings(settings)
    return {"backup_root": settings.backup_root}


@app.post("/api/pick-folder")
def pick_folder(body: PickRequest) -> dict:
    path = choose_folder_native(body.prompt)
    return {"path": path, "cancelled": path is None}


@app.post("/api/backup/start")
def start_backup(body: StartRequest) -> dict:
    settings = load_settings()
    if not settings.backup_root:
        raise HTTPException(
            status_code=400,
            detail="No backup root configured. Set one in settings first.",
        )

    source = Path(body.source).expanduser()
    if not source.is_dir():
        raise HTTPException(status_code=400, detail=f"Source folder not found: {source}")

    # Fail fast on bad roots so the user gets a 400, not a dead background thread.
    try:
        BackupEngine(source_root=source, backup_root=settings.backup_root)
    except BackupError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    settings.last_source = str(source.resolve())
    save_settings(settings)

    try:
        runs.start(str(source.resolve()), settings.backup_root)
    except RuntimeError as err:
        raise HTTPException(status_code=409, detail=str(err)) from err

    return {"started": True, "source": str(source.resolve()), "backup_root": settings.backup_root}


@app.get("/api/backup/status")
def backup_status() -> dict:
    return runs.snapshot()


@app.get("/api/backup/preflight")
def backup_preflight(source: str) -> dict:
    """Size a prospective run without writing anything."""
    settings = load_settings()
    if not settings.backup_root:
        raise HTTPException(status_code=400, detail="No backup root configured.")
    try:
        engine = BackupEngine(source_root=source, backup_root=settings.backup_root)
        result = engine.preflight()
    except BackupError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err
    return {
        "files_total": result.files_total,
        "bytes_total": result.bytes_total,
        "bytes_to_copy": result.bytes_to_copy,
        "bytes_free": result.bytes_free,
        "ok": result.ok,
        "message": result.message,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    """Console entry point: run the local server."""
    import uvicorn

    configure_logging()
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
