"""Logging setup.

A rotating file handler keeps the on-disk log bounded, and we mirror everything
to stderr at the configured level so developers running from a terminal still
see what is happening. ``configure_logging`` is idempotent - calling it twice
will not double up handlers.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Final

from sclip.paths import app_paths

_LOG_FORMAT: Final = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"
_MAX_BYTES: Final = 1_000_000  # 1 MB per log file
_BACKUP_COUNT: Final = 3
_CONFIGURED_FLAG: Final = "_sclip_logging_configured"


def configure_logging(level: int = logging.INFO, *, log_file: Path | None = None) -> None:
    """Wire up rotating-file and stderr handlers on the root logger.

    Safe to call repeatedly - the second call is a no-op.
    """
    root = logging.getLogger()
    if getattr(root, _CONFIGURED_FLAG, False):
        root.setLevel(level)
        return

    root.setLevel(level)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    target = log_file or app_paths().log_file
    target.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        target,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    setattr(root, _CONFIGURED_FLAG, True)
