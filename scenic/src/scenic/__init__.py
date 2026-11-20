"""Scenic route planner for New York State."""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"


def _repo_root() -> Path | None:
    """The `scenic/` checkout root in an editable/source run, else None."""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "src" / "scenic").is_dir():
        return root
    return None


def data_dir() -> Path:
    """Root of raw and derived data: $SCENIC_DATA, else scenic/data/ in a
    checkout, else ~/.local/share/scenic (or $XDG_DATA_HOME/scenic)."""
    env = os.environ.get("SCENIC_DATA")
    if env:
        return Path(env).expanduser().resolve()
    repo = _repo_root()
    if repo is not None:
        return repo / "data"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else (Path.home() / ".local" / "share")
    return (base / "scenic").resolve()


def config_dir() -> Path:
    """Directory holding score.toml and demo_routes.toml (repo config/ or wheel _config/)."""
    repo = _repo_root()
    if repo is not None and (repo / "config" / "score.toml").is_file():
        return repo / "config"
    return Path(__file__).resolve().parent / "_config"
