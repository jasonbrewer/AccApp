"""Backup manifest: the on-disk record of one run.

One JSON manifest per run under ``<backup_root>/_backup_manifests/<timestamp>/``,
next to the human-readable ``run.log`` for the same run.

The manifest is a *record*, never an input to a decision. Skip/conflict calls
are always made by hashing the files that are actually on disk — see
``backup.core``. Nothing here is ever read back to decide whether to copy.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

MANIFESTS_DIR_NAME = "_backup_manifests"
MANIFEST_FILENAME = "manifest.json"
LOG_FILENAME = "run.log"
MANIFEST_VERSION = 1


class FileStatus(str, Enum):
    """Per-file outcome. Values are what land in the JSON."""

    COPIED = "COPIED"
    SKIPPED = "SKIPPED"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


class RunOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass
class FileRecord:
    """One row per source file considered by the run."""

    rel_path: str
    size_bytes: int
    status: FileStatus
    source_hash: str | None = None
    dest_hash: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "FileRecord":
        return cls(
            rel_path=data["rel_path"],
            size_bytes=int(data["size_bytes"]),
            status=FileStatus(data["status"]),
            source_hash=data.get("source_hash"),
            dest_hash=data.get("dest_hash"),
            error=data.get("error"),
        )


@dataclass
class SkippedSymlink:
    """A symlink we refused to follow, kept so the report can surface it."""

    rel_path: str
    target: str
    kind: str  # "file" | "dir"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SkippedSymlink":
        return cls(rel_path=data["rel_path"], target=data["target"], kind=data["kind"])


@dataclass
class RunTotals:
    files: int = 0
    copied: int = 0
    skipped: int = 0
    conflicts: int = 0
    failed: int = 0
    bytes_total: int = 0
    bytes_copied: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RunTotals":
        return cls(**{k: int(v) for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class RunManifest:
    run_id: str
    source_root: str
    backup_root: str
    started_at: str
    finished_at: str | None = None
    outcome: RunOutcome = RunOutcome.FAIL
    totals: RunTotals = field(default_factory=RunTotals)
    files: list[FileRecord] = field(default_factory=list)
    skipped_symlinks: list[SkippedSymlink] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    manifest_version: int = MANIFEST_VERSION

    def to_dict(self) -> dict:
        return {
            "manifest_version": self.manifest_version,
            "run_id": self.run_id,
            "source_root": self.source_root,
            "backup_root": self.backup_root,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome.value,
            "totals": self.totals.to_dict(),
            "skipped_symlinks": [s.to_dict() for s in self.skipped_symlinks],
            "notes": list(self.notes),
            "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunManifest":
        return cls(
            manifest_version=int(data.get("manifest_version", MANIFEST_VERSION)),
            run_id=data["run_id"],
            source_root=data["source_root"],
            backup_root=data["backup_root"],
            started_at=data["started_at"],
            finished_at=data.get("finished_at"),
            outcome=RunOutcome(data.get("outcome", RunOutcome.FAIL.value)),
            totals=RunTotals.from_dict(data.get("totals", {})),
            skipped_symlinks=[SkippedSymlink.from_dict(s) for s in data.get("skipped_symlinks", [])],
            notes=list(data.get("notes", [])),
            files=[FileRecord.from_dict(f) for f in data.get("files", [])],
        )


def run_dir(backup_root: Path | str, run_id: str) -> Path:
    """Directory holding this run's manifest and log."""
    return Path(backup_root) / MANIFESTS_DIR_NAME / run_id


def manifest_path(backup_root: Path | str, run_id: str) -> Path:
    return run_dir(backup_root, run_id) / MANIFEST_FILENAME


def log_path(backup_root: Path | str, run_id: str) -> Path:
    return run_dir(backup_root, run_id) / LOG_FILENAME


def write_manifest(path: Path | str, manifest: RunManifest) -> Path:
    """Serialize a manifest to JSON. Creates the run directory if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return path


def read_manifest(path: Path | str) -> RunManifest:
    """Load a manifest written by `write_manifest` — for reporting only."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return RunManifest.from_dict(data)
