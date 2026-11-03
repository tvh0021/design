"""Scenic route planner for New York State."""

import os
from pathlib import Path

__version__ = "0.1.0"


def data_dir() -> Path:
    """Root of raw and derived data: $SCENIC_DATA, else scenic/data/."""
    env = os.environ.get("SCENIC_DATA")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "data"
