from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json

from sglance.models import ClusterQueue, ClusterStatus, QueueReport, StatusReport

_STATUS_HEADERS = (
    "PARTITION",
    "CPUS",
    "GPUS",
    "NODES",
    "ALLOC",
    "MIXED",
    "IDLE",
    "DOWN",
    "DRAINED",
    "DRAINING",
    "OTHER",
)


def format_status_json(report: StatusReport) -> str:
    payload = {
        "schema_version": report.schema_version,
        "command": report.command,
        "clusters": [asdict(cluster) for cluster in report.clusters],
    }
    return json.dumps(payload, indent=2) + "\n"


def format_cluster_header(
    cluster_name: str,
    *,
    user_scoped: bool = False,
    stale: bool = False,
    fetched_at: str | None = None,
    now: datetime | None = None,
) -> str:
    header = cluster_name
    if user_scoped:
        header = f"{header} [user-scoped]"
    if stale:
        if now is not None and fetched_at is not None:
            fetched_dt = datetime.fromisoformat(fetched_at).astimezone(UTC)
            age_seconds = max(0, int((now.astimezone(UTC) - fetched_dt).total_seconds()))
            header = f"{header} [cached {age_seconds}s ago]"
        else:
            header = f"{header} [cached]"
    return header


def format_status_table(report: StatusReport, *, now: datetime | None = None) -> str:
    sections = [_format_cluster_status_section(cluster, now=now) for cluster in report.clusters]
    return "\n\n".join(sections) + "\n"


def _format_cluster_status_section(cluster: ClusterStatus, *, now: datetime | None = None) -> str:
    if cluster.error is not None:
        return f"{cluster.cluster}: {cluster.error}"
    header = format_cluster_header(
        cluster.cluster,
        user_scoped=cluster.user_scoped,
        stale=cluster.stale,
        fetched_at=cluster.fetched_at,
        now=now,
    )
    if not cluster.partitions:
        return f"{header}\n  (no partitions)"

    rows: list[tuple[str, ...]] = []
    for part in cluster.partitions:
        part_label = f"{part.partition}*" if part.default else part.partition
        if part.cpus_utilization is not None:
            cpus_label = f"{part.cpus_alloc}/{part.cpus_total} ({round(part.cpus_utilization * 100)}%)"
        else:
            cpus_label = f"{part.cpus_alloc}/{part.cpus_total} (-)"
        if part.gpus_utilization is not None:
            gpus_label = f"{part.gpus_alloc}/{part.gpus_total} ({round(part.gpus_utilization * 100)}%)"
        else:
            gpus_label = "-"
        rows.append(
            (
                part_label,
                cpus_label,
                gpus_label,
                str(part.nodes_total),
                str(part.nodes_alloc),
                str(part.nodes_mixed),
                str(part.nodes_idle),
                str(part.nodes_down),
                str(part.nodes_drained),
                str(part.nodes_draining),
                str(part.nodes_other),
            )
        )

    widths = [
        max(len(_STATUS_HEADERS[col]), *(len(row[col]) for row in rows))
        for col in range(len(_STATUS_HEADERS))
    ]

    lines = [header, _render_table_row(_STATUS_HEADERS, widths)]
    for row in rows:
        lines.append(_render_table_row(row, widths))
    return "\n".join(lines)


def _render_table_row(cells: tuple[str, ...], widths: list[int]) -> str:
    padded = [cell.ljust(widths[idx]) for idx, cell in enumerate(cells[:-1])]
    padded.append(cells[-1])
    return "  " + "  ".join(padded)


def format_queue_json(report: QueueReport) -> str:
    payload = {
        "schema_version": report.schema_version,
        "command": report.command,
        "clusters": [asdict(cluster) for cluster in report.clusters],
    }
    return json.dumps(payload, indent=2) + "\n"


_QUEUE_HEADERS = (
    "PARTITION",
    "RUNNING",
    "PENDING",
    "TOP REASONS",
)


def format_queue_table(report: QueueReport, *, now: datetime | None = None) -> str:
    sections = [_format_cluster_queue_section(cluster, now=now) for cluster in report.clusters]
    return "\n\n".join(sections) + "\n"


def _format_cluster_queue_section(cluster: ClusterQueue, *, now: datetime | None = None) -> str:
    if cluster.error is not None:
        return f"{cluster.cluster}: {cluster.error}"
    header = format_cluster_header(
        cluster.cluster,
        user_scoped=cluster.user_scoped,
        stale=cluster.stale,
        fetched_at=cluster.fetched_at,
        now=now,
    )
    if not cluster.partitions:
        return f"{header}\n  0 running, 0 pending"

    rows: list[tuple[str, ...]] = []
    for part in cluster.partitions:
        reasons_label = (
            ", ".join(f"{item.reason} ({item.count})" for item in part.top_reasons[:5])
            if part.top_reasons
            else "-"
        )
        rows.append(
            (
                part.partition,
                str(part.running_jobs),
                str(part.pending_jobs),
                reasons_label,
            )
        )

    widths = [
        max(len(_QUEUE_HEADERS[col]), *(len(row[col]) for row in rows))
        for col in range(len(_QUEUE_HEADERS))
    ]

    lines = [header, _render_table_row(_QUEUE_HEADERS, widths)]
    for row in rows:
        lines.append(_render_table_row(row, widths))
    return "\n".join(lines)


def format_wait_json(report: object) -> str:
    from sglance.models import WaitReport

    assert isinstance(report, WaitReport)
    payload = {
        "schema_version": report.schema_version,
        "command": report.command,
        "days": report.days,
        "clusters": [asdict(cluster) for cluster in report.clusters],
    }
    return json.dumps(payload, indent=2) + "\n"


_WAIT_HEADERS = (
    "PARTITION",
    "N",
    "P50",
    "P90",
    "OLDEST PENDING",
)


def _format_wait_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "-"
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        mins, secs = divmod(total, 60)
        return f"{mins}m {secs}s"
    if total < 86400:
        hours, rem = divmod(total, 3600)
        mins = rem // 60
        return f"{hours}h {mins}m"
    days, rem = divmod(total, 86400)
    hours = rem // 3600
    return f"{days}d {hours}h"


def format_wait_table(report: object, *, now: datetime | None = None) -> str:
    from sglance.models import WaitReport

    assert isinstance(report, WaitReport)
    sections = [_format_cluster_wait_section(cluster, now=now) for cluster in report.clusters]
    return "\n\n".join(sections) + "\n"


def _format_cluster_wait_section(cluster: object, *, now: datetime | None = None) -> str:
    from sglance.models import ClusterWait

    assert isinstance(cluster, ClusterWait)
    if cluster.error is not None:
        return f"{cluster.cluster}: {cluster.error}"
    header = format_cluster_header(
        cluster.cluster,
        user_scoped=cluster.user_scoped,
        stale=cluster.stale,
        fetched_at=cluster.fetched_at,
        now=now,
    )
    if not cluster.partitions:
        return f"{header}\n  (no wait data)"

    rows: list[tuple[str, ...]] = []
    for part in cluster.partitions:
        rows.append(
            (
                part.partition,
                str(part.n),
                _format_wait_duration(part.p50_seconds),
                _format_wait_duration(part.p90_seconds),
                _format_wait_duration(part.oldest_pending_seconds),
            )
        )

    widths = [
        max(len(_WAIT_HEADERS[col]), *(len(row[col]) for row in rows))
        for col in range(len(_WAIT_HEADERS))
    ]

    lines = [header, _render_table_row(_WAIT_HEADERS, widths)]
    for row in rows:
        lines.append(_render_table_row(row, widths))
    return "\n".join(lines)


