"""Requests to public servers must not carry personal identifiers.

Public OSM services ask clients to identify themselves, which tempts a
contact email into the User-Agent. Identify the project instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def test_no_email_in_any_source_file():
    files = [*ROOT.glob("src/**/*.py"), *ROOT.glob("scripts/**/*.py")]
    assert files
    for path in files:
        hits = EMAIL.findall(path.read_text())
        assert not hits, f"{path.relative_to(ROOT)} contains {hits}"
