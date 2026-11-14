from __future__ import annotations

from collections.abc import Collection, Iterator
import re

_GPU_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
_MEM_TOKEN_RE = re.compile(r"\d+[kmgtp]")


class SlurmError(Exception):
    """Raised when a Slurm command fails or returns malformed output."""


def iter_rows(
    stdout: str, tool: str, ncols: int, *, bar: bool | None = True, maxsplit: int = -1
) -> Iterator[tuple[str, list[str]]]:
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("CLUSTER: "):
            continue
        if bar and not line.endswith("|"):
            raise SlurmError(f"malformed {tool} output: {line}")
        body = line[:-1] if bar is not False and line.endswith("|") else line
        cells = [cell.strip() for cell in body.split("|", maxsplit)]
        if len(cells) != ncols:
            raise SlurmError(f"malformed {tool} output: {line}")
        yield line, cells


def is_gpu_type(value: str, invalid: Collection[str]) -> bool:
    low = value.lower()
    return (
        low not in invalid
        and not _MEM_TOKEN_RE.fullmatch(low)
        and bool(_GPU_TYPE_RE.fullmatch(value))
        and any(ch.isascii() and ch.isalpha() for ch in value)
    )


def parse_epoch(raw: str) -> int | None:
    text = raw.strip()
    return (int(text) or None) if text.isascii() and text.isdigit() else None
