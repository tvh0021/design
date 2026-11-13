from __future__ import annotations

from collections.abc import Callable, Mapping
import dataclasses
from datetime import UTC, datetime
import errno
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import types
from typing import Any, NoReturn, TypeVar, get_args, get_origin, get_type_hints

from sglance.config import xdg_dir
from sglance.models import ClusterEntry, ClusterResult

CACHE_TTL_SECONDS = 300
CACHE_PRUNE_AGE_SECONDS = 86400
MAX_CACHE_FILE_BYTES = 4 * 1024 * 1024
CACHE_LOCK_TIMEOUT_SECONDS = 10.0
CACHE_LOCK_POLL_SECONDS = 0.01
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_ENVELOPE_KEYS = frozenset({"cluster", "fetched_at", "stale", "user_scoped", "error"})

T = TypeVar("T", bound=ClusterResult)


def cached_cluster_fetch(
    entry: ClusterEntry,
    *,
    command: str,
    schema_version: int,
    result_type: type[T],
    fetch: Callable[[ClusterEntry], T],
    env: Mapping[str, str],
    now: datetime,
    refresh: bool = False,
    options: Mapping[str, str | int | float | bool | None] | None = None,
) -> T:
    """Fetch a per-cluster result through the 5-minute XDG response cache."""
    cache_dir = xdg_dir(env, "XDG_CACHE_HOME", ".cache")
    if cache_dir is None:
        return fetch(entry)

    digest = _cache_key_digest(
        entry,
        command=command,
        options=options,
        schema_version=schema_version,
    )
    cache_path = cache_dir / f"{digest}.json"

    if not refresh:
        cached = _read_valid_snapshot(cache_path, entry=entry, result_type=result_type, now=now)
        if cached is not None:
            return cached

    lock_fd = _acquire_cache_lock(cache_dir / f"{digest}.lock")
    try:
        if lock_fd is not None and not refresh:
            cached = _read_valid_snapshot(cache_path, entry=entry, result_type=result_type, now=now)
            if cached is not None:
                return cached

        live_result = fetch(entry)
        if live_result.error is None and live_result.fetched_at is not None:
            if _write_snapshot_atomic(cache_path, live_result):
                _prune_old_cache_files(cache_dir, now=now, keep_stem=digest)
        return live_result
    finally:
        if lock_fd is not None:
            _release_cache_lock(lock_fd)


def _ensure_cache_dir(cache_dir: Path) -> None:
    cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(cache_dir, 0o700)


def _acquire_cache_lock(lock_path: Path) -> int | None:
    if fcntl is None:
        return None
    fd: int | None = None
    try:
        _ensure_cache_dir(lock_path.parent)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + CACHE_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(CACHE_LOCK_POLL_SECONDS, remaining))
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        return None


def _release_cache_lock(lock_fd: int) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(lock_fd)
    except OSError:
        pass


def _cache_key_digest(
    entry: ClusterEntry,
    *,
    command: str,
    options: Mapping[str, str | int | float | bool | None] | None,
    schema_version: int,
) -> str:
    normalized_options = {
        key: value
        for key, value in sorted((options or {}).items())
        if value is not None
    }
    key_material = json.dumps(
        {
            "schema_version": schema_version,
            "command": command,
            "options": normalized_options,
            "cluster_name": entry.name,
            "transport_kind": "local" if entry.local else "ssh",
            "transport_target": None if entry.local else entry.ssh,
            "slurm_cluster": entry.slurm_cluster,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()


def _reject_json_constant(token: str) -> NoReturn:
    raise ValueError(f"invalid JSON constant {token!r}")


def _load_json(path: str | Path, flags: int = 0) -> Any:
    fd = os.open(path, os.O_RDONLY | flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_CACHE_FILE_BYTES:
            raise ValueError("invalid cache file")
        with open(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_CACHE_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_CACHE_FILE_BYTES:
        raise ValueError("oversized cache file")
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)


def _age_seconds(fetched_at: object, now: datetime) -> float | None:
    if not isinstance(fetched_at, str) or not fetched_at.endswith("Z"):
        return None
    dt = datetime.fromisoformat(fetched_at)
    if dt.tzinfo is None:
        return None
    return (now.astimezone(UTC) - dt.astimezone(UTC)).total_seconds()


def _read_valid_snapshot(
    cache_path: Path,
    *,
    entry: ClusterEntry,
    result_type: type[T],
    now: datetime,
) -> T | None:
    try:
        loaded = _decode_value(result_type, _load_json(cache_path))
        age = _age_seconds(loaded.fetched_at, now)
        if (
            loaded.cluster != entry.name
            or loaded.error is not None
            or age is None
            or not (0 <= age < CACHE_TTL_SECONDS)
        ):
            return None
        return dataclasses.replace(loaded, stale=True)
    except Exception:
        return None


def _write_snapshot_atomic(cache_path: Path, result: T) -> bool:
    tmp_path: str | None = None
    try:
        snapshot = dataclasses.replace(result, stale=False)
        serialized = json.dumps(dataclasses.asdict(snapshot), indent=2, allow_nan=False) + "\n"
        _ensure_cache_dir(cache_path.parent)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=cache_path.parent,
            prefix=f".{cache_path.stem}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            fh.write(serialized)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, cache_path)
        tmp_path = None
        return True
    except (OSError, ValueError, TypeError):
        return False
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _prune_expired_snapshot(snapshot_path: str, lock_path: str, *, now: datetime) -> None:
    if fcntl is None:  # pragma: no cover
        return
    lock_fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    locked = False
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            return
        payload = _load_json(snapshot_path, getattr(os, "O_NOFOLLOW", 0))
        if (
            isinstance(payload, dict)
            and _SNAPSHOT_ENVELOPE_KEYS <= payload.keys()
            and isinstance(payload.get("cluster"), str)
            and isinstance(payload.get("stale"), bool)
            and isinstance(payload.get("user_scoped"), bool)
            and (age := _age_seconds(payload.get("fetched_at"), now)) is not None
            and age > CACHE_PRUNE_AGE_SECONDS
        ):
            os.unlink(snapshot_path)
    finally:
        if locked:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
        os.close(lock_fd)


def _prune_old_cache_files(cache_dir: Path, *, now: datetime, keep_stem: str) -> None:
    try:
        with os.scandir(cache_dir) as entries:
            for dent in entries:
                try:
                    stem, dot, ext = dent.name.partition(".")
                    if (
                        dot != "."
                        or ext != "json"
                        or stem == keep_stem
                        or not _HEX_DIGEST_RE.fullmatch(stem)
                        or not dent.is_file(follow_symlinks=False)
                    ):
                        continue
                    _prune_expired_snapshot(
                        dent.path, os.path.join(cache_dir, f"{stem}.lock"), now=now
                    )
                except Exception:
                    continue
    except Exception:
        return


def _decode_value(expected_type: Any, raw: Any) -> Any:
    if dataclasses.is_dataclass(expected_type) and isinstance(expected_type, type):
        if not isinstance(raw, dict):
            raise TypeError("expected JSON object for dataclass")
        fields = dataclasses.fields(expected_type)
        if set(raw) != {f.name for f in fields}:
            raise ValueError("dataclass field mismatch")
        hints = get_type_hints(expected_type)
        return expected_type(**{f.name: _decode_value(hints[f.name], raw[f.name]) for f in fields})

    origin = get_origin(expected_type)
    args = get_args(expected_type)
    if origin is types.UnionType and len(args) == 2 and type(None) in args:
        if raw is None:
            return None
        inner = args[0] if args[1] is type(None) else args[1]
        return _decode_value(inner, raw)

    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        if not isinstance(raw, list):
            raise TypeError("expected JSON array for tuple")
        return tuple(_decode_value(args[0], item) for item in raw)

    if expected_type is str and isinstance(raw, str):
        return raw
    if expected_type is bool and isinstance(raw, bool):
        return raw
    if expected_type is int and isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    if expected_type is float and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("expected finite float")
        return value
    raise TypeError(f"invalid value for {expected_type!r}")
