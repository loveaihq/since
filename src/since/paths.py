"""Filesystem locations. ``SINCE_HOME`` overrides the default ``~/.since``."""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "SINCE_HOME"


def since_home() -> Path:
    """Directory holding the database and config (``SINCE_HOME`` or ``~/.since``)."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".since"


def db_path() -> Path:
    return since_home() / "since.db"


def config_path() -> Path:
    return since_home() / "since.yaml"
