"""UI-agnostic copy + verify + manifest engine.

This module knows nothing about FastAPI, HTTP, or the browser. It takes two
paths and an optional progress callback, and it returns a result object. Later
pipeline stages reuse it as-is; the web layer is a thin caller.

Guarantees this code is built around
------------------------------------
* **Straight mirror.** A file's destination is ``backup_root`` joined with the
  file's path *relative to the chosen source root*. Nothing else.
* **The source is read-only to us.** Nothing here opens a source file for
  writing, renames it, or unlinks it.
* **Existing destination data is never overwritten or deleted.** New files are
  created with ``open(..., "xb")`` — an exclusive create that fails rather than
  clobbering. A file whose destination already exists is either a SKIP (hashes
  match) or a CONFLICT (hashes differ, destination left exactly as it was).
  The single ``unlink`` in this file removes only a partial or failed-verify
  copy *that this run itself created moments earlier*.
* **Decisions come from hashing, never from a manifest.** Manifests are output.
  This module never reads one back.
* **Streamed hashing.** Files are read in chunks (4 MB by default) and never
  loaded whole — these are video files.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import xxhash

from ..logging_setup import get_logger, run_log_file
from .manifest import (
    FileRecord,
    FileStatus,
    RunManifest,
    RunOutcome,
    RunTotals,
    SkippedSymlink,
    log_path,
    manifest_path,
    run_dir,
    write_manifest,
)

log = get_logger("backup.core")

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class BackupError(Exception):
    """Base class for refusals raised before a run starts."""


class InvalidRootsError(BackupError):
    """Source or destination root is unusable (missing, not a dir, overlapping)."""


class InsufficientSpaceError(BackupError):
    """Pre-flight found less free space at the destination than the run needs."""

    def __init__(self, bytes_needed: int, bytes_free: int, backup_root: Path):
        self.bytes_needed = bytes_needed
        self.bytes_free = bytes_free
        self.backup_root = backup_root
        super().__init__(
            f"Not enough free space at {backup_root}: "
            f"need {human_bytes(bytes_needed)}, only {human_bytes(bytes_free)} available "
            f"(short by {human_bytes(bytes_needed - bytes_free)}). Nothing was copied."
        )


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedFile:
    """A source file the run intends to consider."""

    rel_path: str
    size_bytes: int

    def source(self, source_root: Path) -> Path:
        return source_root / self.rel_path

    def dest(self, backup_root: Path) -> Path:
        # The straight-mirror rule, in one place.
        return backup_root / self.rel_path


@dataclass
class ScanResult:
    files: list[PlannedFile] = field(default_factory=list)
    skipped_symlinks: list[SkippedSymlink] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)


@dataclass
class PreflightResult:
    files_total: int
    bytes_total: int
    bytes_to_copy: int
    bytes_free: int
    ok: bool
    message: str


@dataclass
class Progress:
    """A snapshot handed to the progress callback. Plain data, safe to copy."""

    phase: str = "idle"  # scanning | comparing | copying | finished
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    current_file: str | None = None
    copied: int = 0
    skipped: int = 0
    conflicts: int = 0
    failed: int = 0

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "files_total": self.files_total,
            "files_done": self.files_done,
            "bytes_total": self.bytes_total,
            "bytes_done": self.bytes_done,
            "current_file": self.current_file,
            "copied": self.copied,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "failed": self.failed,
        }


@dataclass
class BackupResult:
    manifest: RunManifest
    manifest_path: Path
    log_path: Path

    @property
    def outcome(self) -> RunOutcome:
        return self.manifest.outcome

    @property
    def passed(self) -> bool:
        return self.manifest.outcome is RunOutcome.PASS

    @property
    def totals(self) -> RunTotals:
        return self.manifest.totals


ProgressCallback = Callable[[Progress], None]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def human_bytes(n: int) -> str:
    step = 1024.0
    value = float(abs(n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            sign = "-" if n < 0 else ""
            return f"{sign}{value:.1f} {unit}" if unit != "B" else f"{sign}{int(value)} B"
        value /= step
    return f"{n} B"  # unreachable, keeps type checkers happy


def hash_file(path: Path | str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """xxHash64 of a file, streamed. Never loads the whole file."""
    digest = xxhash.xxh64()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def copy_and_hash(
    src: Path,
    dst: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    on_bytes: Callable[[int], None] | None = None,
) -> str:
    """Copy src -> dst in one streaming pass, returning the source xxHash64.

    `dst` is created exclusively ("xb"): if anything already exists at that
    path this raises FileExistsError rather than overwriting it. Modification
    time and mode are preserved via copystat, matching copy2 semantics.
    """
    digest = xxhash.xxh64()
    with open(src, "rb") as reader, open(dst, "xb") as writer:
        while True:
            chunk = reader.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            writer.write(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
        writer.flush()
        os.fsync(writer.fileno())
    shutil.copystat(src, dst)  # mtime/atime/mode, like copy2
    return digest.hexdigest()


def default_free_space(path: Path | str) -> int:
    return shutil.disk_usage(str(path)).free


def _new_run_id(backup_root: Path) -> str:
    """A filesystem-safe UTC timestamp, made unique if a run dir already exists."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = stamp
    counter = 1
    while run_dir(backup_root, candidate).exists():
        counter += 1
        candidate = f"{stamp}-{counter}"
    return candidate


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class BackupEngine:
    """Mirror `source_root` into `backup_root`, verifying every byte by hash.

    Typical use::

        engine = BackupEngine(source, backup_root, progress_cb=my_callback)
        result = engine.run()          # raises InsufficientSpaceError to refuse
        print(result.outcome, result.manifest_path)
    """

    def __init__(
        self,
        source_root: Path | str,
        backup_root: Path | str,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        progress_cb: ProgressCallback | None = None,
        free_space_fn: Callable[[Path], int] = default_free_space,
    ):
        self.source_root = Path(source_root).expanduser().resolve()
        self.backup_root = Path(backup_root).expanduser().resolve()
        self.chunk_size = chunk_size
        self.progress_cb = progress_cb
        self.free_space_fn = free_space_fn
        self.progress = Progress()
        self._validate_roots()

    # -- setup ------------------------------------------------------------

    def _validate_roots(self) -> None:
        if not self.source_root.is_dir():
            raise InvalidRootsError(f"Source folder does not exist: {self.source_root}")
        if not self.backup_root.is_dir():
            raise InvalidRootsError(f"Backup root does not exist: {self.backup_root}")
        if self.source_root == self.backup_root:
            raise InvalidRootsError("Source and backup root are the same folder.")
        if self.source_root in self.backup_root.parents:
            # Allowed, but we must not walk into the backup while writing it.
            log.warning(
                "Backup root %s sits inside the source %s; that subtree will be skipped.",
                self.backup_root, self.source_root,
            )
        if self.backup_root in self.source_root.parents:
            raise InvalidRootsError(
                "Source folder is inside the backup root; that would mirror the backup "
                "into itself."
            )

    def _emit(self) -> None:
        if self.progress_cb is not None:
            self.progress_cb(self.progress)

    def _reset_progress(self) -> None:
        """Start counters from zero so preflight() and run() never double-count."""
        self.progress = Progress()

    # -- phase 1: scan ----------------------------------------------------

    def scan(self) -> ScanResult:
        """Walk the source tree. Symlinks are recorded and never followed."""
        result = ScanResult()
        self.progress.phase = "scanning"
        self._emit()

        for dirpath, dirnames, filenames in os.walk(self.source_root, followlinks=False):
            current = Path(dirpath)

            # Deterministic order, so manifests are diffable between runs.
            dirnames.sort()
            filenames.sort()

            kept_dirs = []
            for name in dirnames:
                child = current / name
                if child.is_symlink():
                    result.skipped_symlinks.append(
                        SkippedSymlink(
                            rel_path=str(child.relative_to(self.source_root)),
                            target=_readlink(child),
                            kind="dir",
                        )
                    )
                    continue  # do not descend — this is the loop guard
                if child.resolve() == self.backup_root:
                    note = f"Skipped {child} — it is the backup root inside the source tree."
                    log.warning(note)
                    result.notes.append(note)
                    continue
                kept_dirs.append(name)
            dirnames[:] = kept_dirs

            for name in filenames:
                child = current / name
                rel = str(child.relative_to(self.source_root))
                if child.is_symlink():
                    result.skipped_symlinks.append(
                        SkippedSymlink(rel_path=rel, target=_readlink(child), kind="file")
                    )
                    continue
                try:
                    size = child.stat().st_size
                except OSError as err:
                    # Can't even stat it; record as a failure candidate with size 0
                    # so the run still reports it rather than silently dropping it.
                    note = f"Could not stat {rel}: {err}"
                    log.warning(note)
                    result.notes.append(note)
                    size = 0
                result.files.append(PlannedFile(rel_path=rel, size_bytes=size))

        self.progress.files_total = len(result.files)
        self.progress.bytes_total = result.total_bytes
        self._emit()
        log.info(
            "Scanned %s: %d files, %s%s",
            self.source_root, len(result.files), human_bytes(result.total_bytes),
            f", {len(result.skipped_symlinks)} symlinks skipped" if result.skipped_symlinks else "",
        )
        return result

    # -- phase 2: compare -------------------------------------------------

    def _compare(self, files: Iterable[PlannedFile]) -> tuple[dict[str, FileRecord], list[PlannedFile]]:
        """Hash-decide each file that already exists at the destination.

        Returns (records so far, files that still need copying). This is where
        SKIP and CONFLICT are decided — by hashing both sides, never by reading
        a previous manifest.
        """
        records: dict[str, FileRecord] = {}
        to_copy: list[PlannedFile] = []

        self.progress.phase = "comparing"
        self._emit()

        for planned in files:
            src = planned.source(self.source_root)
            dst = planned.dest(self.backup_root)
            self.progress.current_file = planned.rel_path
            self._emit()

            if not dst.exists():
                to_copy.append(planned)
                continue

            if dst.is_dir():
                records[planned.rel_path] = self._fail(
                    planned, f"Destination path exists and is a directory: {dst}"
                )
                continue

            try:
                source_hash = hash_file(src, self.chunk_size)
                dest_hash = hash_file(dst, self.chunk_size)
            except OSError as err:
                records[planned.rel_path] = self._fail(planned, f"Could not hash: {err}")
                continue

            if source_hash == dest_hash:
                records[planned.rel_path] = FileRecord(
                    rel_path=planned.rel_path,
                    size_bytes=planned.size_bytes,
                    status=FileStatus.SKIPPED,
                    source_hash=source_hash,
                    dest_hash=dest_hash,
                )
                self.progress.skipped += 1
                log.debug("SKIPPED %s (destination already matches)", planned.rel_path)
            else:
                # Destination differs. Leave it exactly as it is and move on.
                records[planned.rel_path] = FileRecord(
                    rel_path=planned.rel_path,
                    size_bytes=planned.size_bytes,
                    status=FileStatus.CONFLICT,
                    source_hash=source_hash,
                    dest_hash=dest_hash,
                    error="Destination exists with different content; left untouched.",
                )
                self.progress.conflicts += 1
                log.warning(
                    "CONFLICT %s (source %s != destination %s) — destination left untouched",
                    planned.rel_path, source_hash, dest_hash,
                )

            self.progress.files_done += 1
            self.progress.bytes_done += planned.size_bytes
            self._emit()

        return records, to_copy

    def _fail(self, planned: PlannedFile, message: str) -> FileRecord:
        log.error("FAILED %s: %s", planned.rel_path, message)
        self.progress.failed += 1
        self.progress.files_done += 1
        self.progress.bytes_done += planned.size_bytes
        self._emit()
        return FileRecord(
            rel_path=planned.rel_path,
            size_bytes=planned.size_bytes,
            status=FileStatus.FAILED,
            error=message,
        )

    # -- phase 3: pre-flight ----------------------------------------------

    def preflight(self, scan: ScanResult | None = None) -> PreflightResult:
        """Size the run and check destination free space (no writes happen)."""
        self._reset_progress()
        scan = scan or self.scan()
        _records, to_copy = self._compare(scan.files)
        bytes_to_copy = sum(f.size_bytes for f in to_copy)
        bytes_free = self.free_space_fn(self.backup_root)
        ok = bytes_free >= bytes_to_copy
        message = (
            f"{len(to_copy)} file(s) to copy, {human_bytes(bytes_to_copy)}; "
            f"{human_bytes(bytes_free)} free at {self.backup_root}."
        )
        if not ok:
            message = (
                f"Not enough free space: need {human_bytes(bytes_to_copy)}, "
                f"only {human_bytes(bytes_free)} available at {self.backup_root}."
            )
        return PreflightResult(
            files_total=len(scan.files),
            bytes_total=scan.total_bytes,
            bytes_to_copy=bytes_to_copy,
            bytes_free=bytes_free,
            ok=ok,
            message=message,
        )

    # -- phase 4: run -----------------------------------------------------

    def run(self) -> BackupResult:
        """Scan, compare, pre-flight, copy, verify, and write the manifest.

        Raises InsufficientSpaceError before copying anything if the
        destination cannot hold the run.
        """
        self._reset_progress()
        started_at = datetime.now(timezone.utc).isoformat()
        run_id = _new_run_id(self.backup_root)
        this_run_dir = run_dir(self.backup_root, run_id)
        this_run_dir.mkdir(parents=True, exist_ok=True)
        mpath = manifest_path(self.backup_root, run_id)
        lpath = log_path(self.backup_root, run_id)

        with run_log_file(lpath):
            log.info("Backup run %s starting", run_id)
            log.info("  source:      %s", self.source_root)
            log.info("  backup root: %s", self.backup_root)

            scan = self.scan()
            records, to_copy = self._compare(scan.files)

            bytes_to_copy = sum(f.size_bytes for f in to_copy)
            bytes_free = self.free_space_fn(self.backup_root)
            log.info(
                "Pre-flight: %d to copy (%s), %s free at destination",
                len(to_copy), human_bytes(bytes_to_copy), human_bytes(bytes_free),
            )
            if bytes_free < bytes_to_copy:
                log.error(
                    "Refusing to start: need %s, only %s free",
                    human_bytes(bytes_to_copy), human_bytes(bytes_free),
                )
                raise InsufficientSpaceError(bytes_to_copy, bytes_free, self.backup_root)

            self.progress.phase = "copying"
            self._emit()
            for planned in to_copy:
                records[planned.rel_path] = self._copy_one(planned)

            manifest = self._build_manifest(run_id, started_at, scan, records)
            write_manifest(mpath, manifest)

            log.info(
                "Backup run %s finished: %s — %d copied, %d skipped, %d conflicts, %d failed",
                run_id, manifest.outcome.value, manifest.totals.copied,
                manifest.totals.skipped, manifest.totals.conflicts, manifest.totals.failed,
            )
            log.info("Manifest: %s", mpath)

        self.progress.phase = "finished"
        self.progress.current_file = None
        self._emit()
        return BackupResult(manifest=manifest, manifest_path=mpath, log_path=lpath)

    def _copy_one(self, planned: PlannedFile) -> FileRecord:
        """Copy one file and verify it by re-hashing the destination."""
        src = planned.source(self.source_root)
        dst = planned.dest(self.backup_root)
        self.progress.current_file = planned.rel_path
        self._emit()

        created = False
        bytes_seen = 0
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)

            def on_bytes(n: int) -> None:
                nonlocal bytes_seen
                bytes_seen += n
                self.progress.bytes_done += n
                self._emit()

            source_hash = copy_and_hash(src, dst, self.chunk_size, on_bytes)
            created = True

            dest_hash = hash_file(dst, self.chunk_size)
            if dest_hash != source_hash:
                # We wrote this file moments ago and it did not verify. Remove
                # our own bad copy so a re-run retries it cleanly; this is the
                # only path that deletes anything, and only ever a file this
                # run created.
                _remove_our_partial(dst)
                created = False
                raise OSError(
                    f"verification failed: wrote {source_hash}, read back {dest_hash}"
                )

            self.progress.copied += 1
            self.progress.files_done += 1
            self._emit()
            log.info("COPIED %s (%s, %s)", planned.rel_path, human_bytes(planned.size_bytes), source_hash)
            return FileRecord(
                rel_path=planned.rel_path,
                size_bytes=planned.size_bytes,
                status=FileStatus.COPIED,
                source_hash=source_hash,
                dest_hash=dest_hash,
            )

        except OSError as err:
            if created:
                _remove_our_partial(dst)
            # Roll the byte counter back to a whole-file boundary; _fail then
            # adds the file's full size, so progress stays honest either way.
            self.progress.bytes_done = max(0, self.progress.bytes_done - bytes_seen)
            return self._fail(planned, str(err))

    # -- manifest ---------------------------------------------------------

    def _build_manifest(
        self,
        run_id: str,
        started_at: str,
        scan: ScanResult,
        records: dict[str, FileRecord],
    ) -> RunManifest:
        # Emit rows in scan order so the manifest reads like the tree.
        ordered = [records[f.rel_path] for f in scan.files if f.rel_path in records]

        totals = RunTotals(
            files=len(ordered),
            copied=sum(1 for r in ordered if r.status is FileStatus.COPIED),
            skipped=sum(1 for r in ordered if r.status is FileStatus.SKIPPED),
            conflicts=sum(1 for r in ordered if r.status is FileStatus.CONFLICT),
            failed=sum(1 for r in ordered if r.status is FileStatus.FAILED),
            bytes_total=sum(r.size_bytes for r in ordered),
            bytes_copied=sum(r.size_bytes for r in ordered if r.status is FileStatus.COPIED),
        )
        outcome = (
            RunOutcome.PASS
            if totals.conflicts == 0 and totals.failed == 0
            else RunOutcome.FAIL
        )
        return RunManifest(
            run_id=run_id,
            source_root=str(self.source_root),
            backup_root=str(self.backup_root),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
            outcome=outcome,
            totals=totals,
            files=ordered,
            skipped_symlinks=scan.skipped_symlinks,
            notes=scan.notes,
        )


def _readlink(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return "<unreadable>"


def _remove_our_partial(path: Path) -> None:
    """Delete a file this run just created and could not finish or verify.

    Deliberately narrow: called only from the copy path, only for a destination
    that `open(..., "xb")` created in this run. Pre-existing destination data
    never reaches this function.
    """
    try:
        os.unlink(path)
        log.debug("Removed incomplete copy %s", path)
    except OSError as err:
        log.warning("Could not remove incomplete copy %s: %s", path, err)
