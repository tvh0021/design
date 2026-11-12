from __future__ import annotations

from collections.abc import Callable, Mapping
import dataclasses
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import types
from typing import Any, NoReturn, Protocol, TypeVar, Union, get_args, get_origin, get_type_hints

from sglance.models import ClusterEntry

CACHE_TTL_SECONDS = 300


class CacheableClusterResult(Protocol):
    @property
    def cluster(self) -> str: ...

    @property
    def fetched_at(self) -> str | None: ...

    @property
    def stale(self) -> bool: ...

    @property
    def error(self) -> str | None: ...


T = TypeVar("T", bound=CacheableClusterResult)


def cached_cluster_fetch(
    entry: ClusterEntry,
    *,
    command: str,
    result_type: type[T],
    fetch: Callable[[ClusterEntry], T],
    env: Mapping[str, str],
    now: datetime,
    refresh: bool = False,
    options: Mapping[str, str | int | float | bool | None] | None = None,
    schema_version: int = 1,
) -> T:
    """Fetch a per-cluster result through the 5-minute XDG response cache."""
    cache_dir = _resolve_cache_dir(env)
    cache_path = (
        cache_dir / f"{_cache_key_digest(entry, command=command, options=options, schema_version=schema_version)}.json"
        if cache_dir is not None
        else None
    )

    if not refresh and cache_path is not None:
        cached = _read_valid_snapshot(cache_path, entry=entry, result_type=result_type, now=now)
        if cached is not None:
            return cached

    live_result = fetch(entry)
    if cache_path is not None and live_result.error is None and live_result.fetched_at is not None:
        _write_snapshot_atomic(cache_path, live_result)
    return live_result


def _resolve_cache_dir(env: Mapping[str, str]) -> Path | None:
    xdg_cache = env.get("XDG_CACHE_HOME", "")
    if xdg_cache:
        return Path(xdg_cache) / "sglance"
    home_dir = env.get("HOME", "")
    if home_dir:
        return Path(home_dir) / ".cache" / "sglance"
    return None


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


def _read_valid_snapshot(
    cache_path: Path,
    *,
    entry: ClusterEntry,
    result_type: type[T],
    now: datetime,
) -> T | None:
    try:
        raw_text = cache_path.read_text(encoding="utf-8")
        raw_payload = json.loads(raw_text, parse_constant=_reject_json_constant)
        loaded = _decode_value(result_type, raw_payload)
        if loaded.cluster != entry.name or loaded.error is not None or loaded.fetched_at is None:
            return None
        if not loaded.fetched_at.endswith("Z"):
            return None
        fetched_dt = datetime.fromisoformat(loaded.fetched_at)
        if fetched_dt.tzinfo is None:
            return None
        age_seconds = (now.astimezone(UTC) - fetched_dt.astimezone(UTC)).total_seconds()
        if not (0 <= age_seconds < CACHE_TTL_SECONDS):
            return None
        return dataclasses.replace(loaded, stale=True)  # type: ignore[type-var]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        return None


def _write_snapshot_atomic(cache_path: Path, result: T) -> None:
    snapshot = dataclasses.replace(result, stale=False)  # type: ignore[type-var]
    serialized = json.dumps(dataclasses.asdict(snapshot), indent=2, allow_nan=False) + "\n"
    tmp_path: str | None = None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
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
        os.replace(tmp_path, cache_path)
        tmp_path = None
    except OSError:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _decode_value(expected_type: Any, raw: Any) -> Any:
    if dataclasses.is_dataclass(expected_type) and isinstance(expected_type, type):
        if not isinstance(raw, dict):
            raise TypeError("expected JSON object for dataclass")
        fields = dataclasses.fields(expected_type)
        if set(raw.keys()) != {f.name for f in fields}:
            raise ValueError("dataclass field mismatch")
        hints = get_type_hints(expected_type)
        kwargs = {f.name: _decode_value(hints[f.name], raw[f.name]) for f in fields}
        return expected_type(**kwargs)

    origin = get_origin(expected_type)
    if origin in (types.UnionType, Union):
        args = get_args(expected_type)
        if raw is None:
            if type(None) in args:
                return None
            raise TypeError("unexpected null")
        ordered_candidates = sorted(
            (arg for arg in args if arg is not type(None)),
            key=lambda arg: 0 if arg is type(raw) else 1,
        )
        for candidate in ordered_candidates:
            try:
                return _decode_value(candidate, raw)
            except (TypeError, ValueError):
                continue
        raise TypeError("value did not match any union member")

    if origin is tuple:
        if not isinstance(raw, list):
            raise TypeError("expected JSON array for tuple")
        args = get_args(expected_type)
        if not args:
            return tuple(raw)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_value(args[0], item) for item in raw)
        if len(raw) != len(args):
            raise ValueError("tuple length mismatch")
        return tuple(_decode_value(arg, item) for arg, item in zip(args, raw, strict=True))

    if origin is list:
        if not isinstance(raw, list):
            raise TypeError("expected JSON array for list")
        args = get_args(expected_type)
        if not args:
            return list(raw)
        return [_decode_value(args[0], item) for item in raw]

    if origin is dict:
        if not isinstance(raw, dict):
            raise TypeError("expected JSON object for dict")
        args = get_args(expected_type)
        if len(args) != 2:
            return dict(raw)
        return {_decode_value(args[0], k): _decode_value(args[1], v) for k, v in raw.items()}

    if expected_type is str:
        if not isinstance(raw, str):
            raise TypeError("expected str")
        return raw
    if expected_type is bool:
        if not isinstance(raw, bool):
            raise TypeError("expected bool")
        return raw
    if expected_type is int:
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise TypeError("expected int")
        return raw
    if expected_type is float:
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise TypeError("expected float")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("expected finite float")
        return value
    if expected_type in (Any, object):
        return raw
    raise TypeError(f"unsupported schema type {expected_type!r}")
