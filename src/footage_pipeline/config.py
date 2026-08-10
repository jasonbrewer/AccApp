"""Persisted application settings.

Persistence for this slice is a single local JSON file. There is no database
anywhere in the project, and nothing here talks to the network.

The settings file lives under the user's Application Support directory on
macOS. Set ``FOOTAGE_PIPELINE_SETTINGS`` to point it somewhere else (the tests
use this so they never touch a real user's settings).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

APP_NAME = "FootagePipeline"
SETTINGS_ENV_VAR = "FOOTAGE_PIPELINE_SETTINGS"


@dataclass
class Settings:
    """Everything the app remembers between runs.

    backup_root
        The persisted destination root. Chosen once via the native folder
        picker and changeable in the settings screen. ``None`` until set.
    last_source
        The source folder used by the most recent run, purely so the UI can
        pre-fill it. The source is still chosen fresh each run.
    """

    backup_root: str | None = None
    last_source: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        # Ignore unknown keys so a settings file written by a later version
        # doesn't crash an older one.
        return cls(
            backup_root=_clean_path(data.get("backup_root")),
            last_source=_clean_path(data.get("last_source")),
        )


def _clean_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def default_settings_path() -> Path:
    """Where settings live unless overridden by the environment."""
    override = os.environ.get(SETTINGS_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME / "settings.json"


def load_settings(path: Path | str | None = None) -> Settings:
    """Read settings, falling back to defaults for a missing or unreadable file."""
    path = Path(path) if path is not None else default_settings_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings()
    except OSError as err:
        log.warning("Could not read settings at %s (%s); using defaults", path, err)
        return Settings()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        log.warning("Settings at %s are not valid JSON (%s); using defaults", path, err)
        return Settings()

    if not isinstance(data, dict):
        log.warning("Settings at %s are not a JSON object; using defaults", path)
        return Settings()

    return Settings.from_dict(data)


def save_settings(settings: Settings, path: Path | str | None = None) -> Path:
    """Write settings atomically so an interrupted write can't corrupt the file."""
    path = Path(path) if path is not None else default_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(settings.to_dict(), indent=2, sort_keys=True) + "\n"
    # Write to a sibling temp file, then rename into place — the rename is
    # atomic, so readers see either the old file or the complete new one.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # Clean up only the temp file this call created.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    log.debug("Saved settings to %s", path)
    return path
