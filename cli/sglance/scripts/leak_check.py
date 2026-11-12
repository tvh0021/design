#!/usr/bin/env python3
"""Privacy leak check for sglance."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DENYLIST_FILENAME = ".leak-denylist"
DENYLIST_ENV_VAR = "SGLANCE_LEAK_DENYLIST"
SKIP_DIRS = {".git", "__pycache__", ".venv", "build", "dist", ".pytest_cache"}
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9](?:[A-Za-z0-9._%+-]*[A-Za-z0-9])?"
    r"@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9])"
)
_OCTET = r"(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
IPV4_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9])(?<![0-9]\.){_OCTET}\.{_OCTET}\.{_OCTET}\.{_OCTET}(?![A-Za-z0-9]|\.[0-9])"
)
ALLOWED_IPV4_NETWORKS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("127.0.0.0/8"),
)


def has_forbidden_ipv4(line: str) -> bool:
    for match in IPV4_PATTERN.finditer(line):
        addr = ipaddress.IPv4Address(match.group(0))
        if not any(addr in net for net in ALLOWED_IPV4_NETWORKS):
            return True
    return False


def redact_sensitive_spans(
    text: str,
    token_patterns: Sequence[tuple[int, re.Pattern[str]]],
) -> str:
    spans: list[tuple[int, int]] = []
    for _idx, pattern in token_patterns:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))
    for match in EMAIL_PATTERN.finditer(text):
        spans.append((match.start(), match.end()))
    for match in IPV4_PATTERN.finditer(text):
        addr = ipaddress.IPv4Address(match.group(0))
        if not any(addr in net for net in ALLOWED_IPV4_NETWORKS):
            spans.append((match.start(), match.end()))
    if not spans:
        return text

    spans.sort(key=lambda item: (item[0], -item[1]))
    merged: list[tuple[int, int]] = []
    cur_start, cur_end = spans[0]
    for start, end in spans[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = start, end
    merged.append((cur_start, cur_end))

    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        pieces.append("<redacted>")
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leak_check.py",
        description="Scan files under a root directory for privacy leaks.",
    )
    parser.add_argument(
        "positional_root",
        nargs="?",
        default=None,
        help="Optional root directory to scan (defaults to cli/sglance).",
    )
    parser.add_argument(
        "--root",
        dest="flag_root",
        default=None,
        help="Root directory to scan (defaults to cli/sglance).",
    )
    parser.add_argument(
        "--require-denylist",
        action="store_true",
        help="Fail closed if neither .leak-denylist nor SGLANCE_LEAK_DENYLIST provides tokens.",
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="Scan only files staged in the git index under root.",
    )
    return parser


def load_denylist_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    tokens: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens.append(stripped)
    return tokens


def parse_denylist_env(raw_value: str) -> list[str]:
    tokens: list[str] = []
    for raw_line in raw_value.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for piece in stripped.split(","):
            token = piece.strip()
            if token:
                tokens.append(token)
    return tokens


def merge_tokens(*token_lists: Sequence[str]) -> list[str]:
    merged: list[str] = []
    seen_lower: set[str] = set()
    for items in token_lists:
        for tok in items:
            key = tok.lower()
            if key not in seen_lower:
                seen_lower.add(key)
                merged.append(tok)
    return merged


def iter_plain_files(root: Path, skip_paths: set[Path]) -> list[Path]:
    found: list[Path] = []
    for dirpath_str, dirnames, filenames in os.walk(root):
        dirpath = Path(dirpath_str)
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")
        )
        for filename in sorted(filenames):
            raw_candidate = dirpath / filename
            if raw_candidate.is_symlink():
                continue
            candidate = raw_candidate.resolve()
            if candidate in skip_paths:
                continue
            if candidate.is_file():
                found.append(candidate)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def find_git_toplevel(root: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return Path(out).resolve() if out else None


def load_git_tracked_targets(root: Path) -> list[tuple[str, bytes]]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--", "."],
        capture_output=True,
        check=True,
    )
    seen: set[str] = set()
    targets: list[tuple[str, bytes]] = []
    for raw_entry in proc.stdout.split(b"\x00"):
        if not raw_entry:
            continue
        rel_posix = raw_entry.decode("utf-8", errors="surrogateescape")
        if rel_posix in seen:
            continue
        seen.add(rel_posix)
        full_path = root / rel_posix
        if full_path.is_file() and not full_path.is_symlink():
            targets.append((rel_posix, full_path.read_bytes()))
    targets.sort(key=lambda item: item[0])
    return targets


def load_git_untracked_targets(
    root: Path,
    skip_paths: set[Path],
) -> list[tuple[str, bytes]]:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            "--",
            ".",
        ],
        capture_output=True,
        check=True,
    )
    targets: list[tuple[str, bytes]] = []
    for raw_entry in proc.stdout.split(b"\x00"):
        if not raw_entry:
            continue
        rel_posix = raw_entry.decode("utf-8", errors="surrogateescape")
        full_path = root / rel_posix
        if full_path.is_symlink() or not full_path.is_file():
            continue
        if full_path.resolve() in skip_paths:
            continue
        targets.append((rel_posix, full_path.read_bytes()))
    targets.sort(key=lambda item: item[0])
    return targets


def load_git_staged_targets(git_toplevel: Path, root: Path) -> list[tuple[str, bytes | None]]:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(git_toplevel),
            "diff",
            "--cached",
            "--no-renames",
            "--raw",
            "--diff-filter=d",
            "-z",
            "--",
            str(root),
        ],
        capture_output=True,
        check=True,
    )
    root_rel = root.relative_to(git_toplevel).as_posix()
    targets: list[tuple[str, bytes | None]] = []
    seen: set[str] = set()
    raw_parts = proc.stdout.split(b"\x00")
    idx = 0
    while idx + 1 < len(raw_parts):
        meta = raw_parts[idx]
        if not meta:
            idx += 1
            continue
        raw_path = raw_parts[idx + 1]
        idx += 2
        repo_rel_posix = raw_path.decode("utf-8", errors="surrogateescape")
        if repo_rel_posix in seen:
            continue
        seen.add(repo_rel_posix)

        meta_fields = meta.lstrip(b":").decode("ascii", errors="replace").split()
        dst_mode = meta_fields[1] if len(meta_fields) >= 2 else ""

        pure_path = PurePosixPath(repo_rel_posix)
        rel_posix = (
            pure_path.as_posix()
            if root_rel == "."
            else pure_path.relative_to(root_rel).as_posix()
        )

        blob_proc = subprocess.run(
            ["git", "-C", str(git_toplevel), "cat-file", "blob", f":{repo_rel_posix}"],
            capture_output=True,
            check=False,
        )
        if blob_proc.returncode != 0:
            if dst_mode == "160000":
                targets.append((rel_posix, b""))
            else:
                targets.append((rel_posix, None))
            continue
        targets.append((rel_posix, blob_proc.stdout))
    targets.sort(key=lambda item: item[0])
    return targets


def load_plain_targets(root: Path, skip_paths: set[Path]) -> list[tuple[str, bytes]]:
    return [
        (file_path.relative_to(root).as_posix(), file_path.read_bytes())
        for file_path in iter_plain_files(root, skip_paths=skip_paths)
    ]


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    effective_env = os.environ if env is None else env
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.positional_root is not None and args.flag_root is not None:
        parser.error("specify root either positionally or via --root, not both")
    raw_root = args.flag_root or args.positional_root
    root = Path(raw_root).resolve() if raw_root is not None else DEFAULT_ROOT
    if not root.is_dir():
        print(f"leak_check.py: error: root directory does not exist: {root}", file=sys.stderr)
        return 2

    git_toplevel = find_git_toplevel(root)
    if args.staged and git_toplevel is None:
        print(
            f"leak_check.py: error: --staged requires a git worktree: {root}",
            file=sys.stderr,
        )
        return 2

    denylist_path = (root / DENYLIST_FILENAME).resolve()
    file_tokens = load_denylist_file(denylist_path)
    env_tokens = parse_denylist_env(effective_env.get(DENYLIST_ENV_VAR, ""))
    tokens = merge_tokens(file_tokens, env_tokens)
    if args.require_denylist and not tokens:
        print(
            f"leak_check.py: denylist required (--require-denylist), "
            f"but neither {denylist_path} nor {DENYLIST_ENV_VAR} provided any tokens",
            file=sys.stderr,
        )
        return 1
    token_patterns = [
        (
            idx,
            re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            ),
        )
        for idx, tok in enumerate(tokens, start=1)
    ]

    targets: Sequence[tuple[str, bytes | None]]
    if args.staged:
        assert git_toplevel is not None
        targets = load_git_staged_targets(git_toplevel, root)
    elif git_toplevel is not None:
        targets = load_git_tracked_targets(root)
        if not targets:
            targets = load_git_untracked_targets(root, skip_paths={denylist_path})
    else:
        targets = load_plain_targets(root, skip_paths={denylist_path})

    findings: list[str] = []
    for rel_posix, raw_bytes in targets:
        display_path = redact_sensitive_spans(rel_posix, token_patterns)
        for idx, pattern in token_patterns:
            if pattern.search(rel_posix):
                findings.append(f"{display_path}:0: denylist token #{idx} in path")
        if EMAIL_PATTERN.search(rel_posix):
            findings.append(f"{display_path}:0: email address in path")
        if has_forbidden_ipv4(rel_posix):
            findings.append(f"{display_path}:0: non-RFC-5737 IPv4 address in path")

        if raw_bytes is None:
            findings.append(f"{display_path}:0: unable to read staged blob")
            continue

        text = raw_bytes.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for idx, pattern in token_patterns:
                if pattern.search(line):
                    findings.append(f"{display_path}:{line_no}: denylist token #{idx}")
            if EMAIL_PATTERN.search(line):
                findings.append(f"{display_path}:{line_no}: email address")
            if has_forbidden_ipv4(line):
                findings.append(f"{display_path}:{line_no}: non-RFC-5737 IPv4 address")

    if findings:
        for item in findings:
            print(item)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
