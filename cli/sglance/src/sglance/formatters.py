from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
import json
from typing import Any

from sglance.models import (
    SCHEMA_VERSION,
    ClusterMine,
    ClusterQueue,
    ClusterResult,
    ClusterStatus,
    ClusterWait,
)


def format_json(
    command: str, clusters: Sequence[ClusterResult], extra: Mapping[str, Any]
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        **extra,
        "clusters": [asdict(c) for c in clusters],
    }
    return json.dumps(payload, indent=2) + "\n"


def _format_duration_seconds(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _format_wait_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, round(seconds))
    for unit, size, sub, subsize in (("d", 86400, "h", 3600), ("h", 3600, "m", 60), ("m", 60, "s", 1)):
        if total >= size:
            return f"{total // size}{unit} {total % size // subsize}{sub}"
    return f"{total}s"


def _status_rows(cluster: ClusterStatus) -> list[tuple[str, ...]]:
    return [
        (
            f"{p.partition}*" if p.default else p.partition,
            f"{p.cpus_alloc}/{p.cpus_total} ({round(p.cpus_utilization * 100)}%)"
            if p.cpus_utilization is not None
            else f"{p.cpus_alloc}/{p.cpus_total} (-)",
            f"{p.gpus_alloc}/{p.gpus_total} ({round(p.gpus_utilization * 100)}%)"
            if p.gpus_utilization is not None
            else "-",
            str(p.nodes_total),
            str(p.nodes_alloc),
            str(p.nodes_mixed),
            str(p.nodes_idle),
            str(p.nodes_down),
            str(p.nodes_drained),
            str(p.nodes_draining),
            str(p.nodes_other),
        )
        for p in cluster.partitions
    ]


def _queue_rows(cluster: ClusterQueue) -> list[tuple[str, ...]]:
    return [
        (
            p.partition,
            str(p.running_jobs),
            str(p.pending_jobs),
            ", ".join(f"{r.reason} ({r.count})" for r in p.top_reasons[:5]) if p.top_reasons else "-",
        )
        for p in cluster.partitions
    ]


def _mine_rows(cluster: ClusterMine) -> list[tuple[str, ...]]:
    return [
        (
            j.job_id,
            j.name or "-",
            j.user or "-",
            j.account or "-",
            j.partition,
            j.state,
            "-" if j.cpus is None else str(j.cpus),
            "-" if j.gpus is None else str(j.gpus),
            "-" if j.nodes is None else str(j.nodes),
            "-" if j.elapsed_seconds is None else _format_duration_seconds(j.elapsed_seconds),
            "-" if j.time_limit_seconds is None else _format_duration_seconds(j.time_limit_seconds),
            "-" if j.reason is None else j.reason,
            "-" if j.estimated_start is None else j.estimated_start,
        )
        for j in cluster.jobs
    ]


def _wait_rows(cluster: ClusterWait) -> list[tuple[str, ...]]:
    return [
        (
            p.partition,
            str(p.n),
            _format_wait_duration(p.p50_seconds),
            _format_wait_duration(p.p90_seconds),
            _format_wait_duration(p.median_pending_seconds),
            _format_wait_duration(p.oldest_pending_seconds),
        )
        for p in cluster.partitions
    ]


def _status_compact_rows(cluster: ClusterStatus) -> list[tuple[str, ...]]:
    return [
        (
            f"{p.partition}*" if p.default else p.partition,
            f"{p.cpus_total - p.cpus_alloc}/{p.cpus_total}",
            f"{p.gpus_total - p.gpus_alloc}/{p.gpus_total}" if p.gpus_total else "-",
            f"{p.nodes_idle}/{p.nodes_total}",
        )
        for p in cluster.partitions
    ]


_TABLES: dict[str, tuple[tuple[str, ...], Callable[[Any], list[tuple[str, ...]]], str]] = {
    "status": (
        ("PARTITION", "CPUS", "GPUS", "NODES", "ALLOC", "MIXED", "IDLE", "DOWN", "DRAINED", "DRAINING", "OTHER"),
        _status_rows,
        "(no partitions)",
    ),
    "queue": (("PARTITION", "RUNNING", "PENDING", "TOP REASONS"), _queue_rows, "0 running, 0 pending"),
    "mine": (
        ("JOBID", "NAME", "USER", "ACCOUNT", "PARTITION", "STATE", "CPUS", "GPUS", "NODES", "ELAPSED", "LIMIT", "REASON", "ESTIMATED START"),
        _mine_rows,
        "(no jobs)",
    ),
    "wait": (("PARTITION", "N", "P50", "P90", "MEDIAN PENDING", "OLDEST PENDING"), _wait_rows, "(no wait data)"),
}

_COMPACT_TABLES: dict[str, tuple[tuple[str, ...], Callable[[Any], list[tuple[str, ...]]]]] = {
    "status": (("PARTITION", "FREE CPUS", "FREE GPUS", "IDLE NODES"), _status_compact_rows),
    "queue": (("PARTITION",), lambda c: [(r[0],) for r in _queue_rows(c)]),
    "mine": (
        ("JOBID", "PARTITION", "STATE", "ESTIMATED START"),
        lambda c: [(r[0], r[4], r[5], r[12]) for r in _mine_rows(c)],
    ),
    "wait": (("PARTITION", "P50", "P90"), lambda c: [(r[0], r[2], r[3]) for r in _wait_rows(c)]),
}


def _render_row(cells: tuple[str, ...], widths: list[int]) -> str:
    return "  " + "  ".join([*(c.ljust(w) for c, w in zip(cells[:-1], widths)), cells[-1]])


def _format_section(command: str, cluster: ClusterResult, now: datetime, compact: bool = False) -> str:
    if cluster.error is not None:
        return f"{cluster.cluster}: {cluster.error}"
    header = f"{cluster.cluster} [user-scoped]" if cluster.user_scoped else cluster.cluster
    if cluster.stale and cluster.fetched_at is not None:
        fetched_dt = datetime.fromisoformat(cluster.fetched_at).astimezone(UTC)
        age = max(0, int((now.astimezone(UTC) - fetched_dt).total_seconds()))
        header = f"{header} [cached {age}s ago]"
    lines = [header]
    if not compact and getattr(cluster, "history_error", None) is not None:
        lines.append(f"  history unavailable: {cluster.history_error}")  # type: ignore[attr-defined]
    headers, row_fn, empty_text = _TABLES[command]
    if compact:
        headers, row_fn = _COMPACT_TABLES[command]
    rows = row_fn(cluster)
    if not rows:
        lines.append(f"  {empty_text}")
        return "\n".join(lines)
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    lines.append(_render_row(headers, widths))
    lines.extend(_render_row(r, widths) for r in rows)
    return "\n".join(lines)


def format_table(command: str, clusters: Sequence[ClusterResult], now: datetime, compact: bool = False) -> str:
    return "\n\n".join(_format_section(command, c, now, compact=compact) for c in clusters) + "\n"
