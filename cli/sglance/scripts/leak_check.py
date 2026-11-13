#!/usr/bin/env python3
"""Privacy leak check for sglance."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping, Sequence, Set as AbstractSet
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
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9](?:[A-Za-z0-9._%+-]*[A-Za-z0-9])?"
    r"@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}(?![A-Za-z0-9])"
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


def iter_forbidden_ipv4(text: str) -> Iterator[re.Match[str]]:
    for match in IPV4_PATTERN.finditer(text):
        addr = ipaddress.IPv4Address(match.group(0))
        if not any(addr in net for net in ALLOWED_IPV4_NETWORKS):
            yield match


def has_forbidden_ipv4(line: str) -> bool:
    return any(iter_forbidden_ipv4(line))


def redact_sensitive_spans(text: str, token_patterns: Sequence[re.Pattern[str]]) -> str:
    spans = [
        *(m.span() for pat in token_patterns for m in pat.finditer(text)),
        *(m.span() for m in EMAIL_PATTERN.finditer(text)),
        *(m.span() for m in iter_forbidden_ipv4(text)),
    ]
    pieces: list[str] = []
    cur_end = -1
    for start, end in sorted(spans):
        if start > cur_end:
            pieces.extend((text[max(0, cur_end) : start], "<redacted>"))
            cur_end = end
        elif end > cur_end:
            cur_end = end
    pieces.append(text[max(0, cur_end) :])
    return "".join(pieces)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="leak_check.py",
        description="Scan files under a root directory for privacy leaks.",
    )
    parser.add_argument(
        "positional_root",
        nargs="?",
        help="Optional root directory to scan (defaults to cli/sglance).",
    )
    parser.add_argument(
        "--root",
        dest="flag_root",
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


def parse_denylist(raw_text: str, *, split_commas: bool = False) -> list[str]:
    tokens: list[str] = []
    for raw_line in raw_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(",") if split_commas else (stripped,)
        tokens.extend(tok for part in parts if (tok := part.strip()))
    return tokens


def merge_tokens(*token_lists: Sequence[str]) -> list[str]:
    seen: dict[str, str] = {}
    for items in token_lists:
        for tok in items:
            seen.setdefault(tok.lower(), tok)
    return list(seen.values())


def load_plain_targets(root: Path, skip_paths: AbstractSet[Path]) -> list[tuple[str, bytes]]:
    targets: list[tuple[str, bytes]] = []
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
            if candidate not in skip_paths and candidate.is_file():
                targets.append((candidate.relative_to(root).as_posix(), candidate.read_bytes()))
    targets.sort(key=lambda item: item[0])
    return targets


def find_git_toplevel(root: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout.strip() if proc.returncode == 0 else ""
    return Path(out).resolve() if out else None


def load_git_ls_targets(
    root: Path,
    *flags: str,
    skip_paths: AbstractSet[Path] = frozenset(),
) -> list[tuple[str, bytes]]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", *flags, "--", "."],
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
        if full_path.is_symlink() or not full_path.is_file():
            continue
        if skip_paths and full_path.resolve() in skip_paths:
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
        rel_posix = PurePosixPath(repo_rel_posix).relative_to(root_rel).as_posix()

        blob_proc = subprocess.run(
            ["git", "-C", str(git_toplevel), "cat-file", "blob", f":{repo_rel_posix}"],
            capture_output=True,
            check=False,
        )
        if blob_proc.returncode != 0:
            targets.append((rel_posix, b"" if dst_mode == "160000" else None))
        else:
            targets.append((rel_posix, blob_proc.stdout))
    targets.sort(key=lambda item: item[0])
    return targets


def scan_labels(text: str, token_patterns: Sequence[re.Pattern[str]]) -> Iterator[str]:
    for idx, pattern in enumerate(token_patterns, 1):
        if pattern.search(text):
            yield f"denylist token #{idx}"
    if EMAIL_PATTERN.search(text):
        yield "email address"
    if has_forbidden_ipv4(text):
        yield "non-RFC-5737 IPv4 address"


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
        print(f"leak_check.py: error: --staged requires a git worktree: {root}", file=sys.stderr)
        return 2

    denylist_path = (root / DENYLIST_FILENAME).resolve()
    file_tokens = (
        parse_denylist(denylist_path.read_text(encoding="utf-8"))
        if denylist_path.is_file()
        else []
    )
    env_tokens = parse_denylist(effective_env.get(DENYLIST_ENV_VAR, ""), split_commas=True)
    tokens = merge_tokens(file_tokens, env_tokens)
    if args.require_denylist and not tokens:
        print(
            f"leak_check.py: denylist required (--require-denylist), "
            f"but neither {denylist_path} nor {DENYLIST_ENV_VAR} provided any tokens",
            file=sys.stderr,
        )
        return 1
    token_patterns = [
        re.compile(rf"(?<![A-Za-z0-9]){re.escape(tok)}(?![A-Za-z0-9])", re.IGNORECASE)
        for tok in tokens
    ]

    targets: Sequence[tuple[str, bytes | None]]
    if args.staged:
        assert git_toplevel is not None
        targets = load_git_staged_targets(git_toplevel, root)
    elif git_toplevel is not None:
        targets = load_git_ls_targets(root, "--cached") or load_git_ls_targets(
            root, "--others", "--exclude-standard", skip_paths={denylist_path}
        )
    else:
        targets = load_plain_targets(root, skip_paths={denylist_path})

    findings: list[str] = []
    for rel_posix, raw_bytes in targets:
        display_path = redact_sensitive_spans(rel_posix, token_patterns)
        findings.extend(
            f"{display_path}:0: {label} in path" for label in scan_labels(rel_posix, token_patterns)
        )
        if raw_bytes is None:
            findings.append(f"{display_path}:0: unable to read staged blob")
            continue
        for line_no, line in enumerate(raw_bytes.decode("utf-8", errors="replace").splitlines(), 1):
            findings.extend(
                f"{display_path}:{line_no}: {label}" for label in scan_labels(line, token_patterns)
            )

    for item in findings:
        print(item)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
