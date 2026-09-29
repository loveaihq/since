"""Shared fixtures. Every test runs with SINCE_HOME pointing at a throwaway directory."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def since_home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SINCE_HOME at a tmp dir so no test can touch the real ``~/.since``.

    The directory is not created; the code under test creates it (like on first run)."""
    home = tmp_path / "since-home"
    monkeypatch.setenv("SINCE_HOME", str(home))
    return home
