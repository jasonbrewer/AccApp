"""Shared logging setup.

One formatter and one console handler for the whole app, plus a helper that
attaches a per-run file handler so each backup run also produces the
human-readable .log that ships next to its manifest.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

ROOT_LOGGER_NAME = "footage_pipeline"

_configured = False


def formatter() -> logging.Formatter:
    return logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)


def configure_logging(level: int = logging.INFO, force: bool = False) -> logging.Logger:
    """Attach a console handler to the app's root logger. Idempotent."""
    global _configured
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    if _configured and not force:
        return logger

    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler()
    console.setFormatter(formatter())
    console.setLevel(level)
    logger.addHandler(console)

    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """Logger for a submodule, e.g. get_logger("backup.core")."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


@contextmanager
def run_log_file(path: Path | str, level: int = logging.INFO) -> Iterator[Path]:
    """Tee the app's logging into `path` for the duration of the block.

    Used by a backup run to produce its own readable log alongside the JSON
    manifest. The handler is always detached and closed on the way out, even
    if the run raises.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = configure_logging()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter())
    handler.setLevel(level)
    logger.addHandler(handler)
    # A run's log is only useful if it captures the run's own INFO lines.
    previous_level = logger.level
    if previous_level > level:
        logger.setLevel(level)
    try:
        yield path
    finally:
        logger.setLevel(previous_level)
        logger.removeHandler(handler)
        handler.close()
