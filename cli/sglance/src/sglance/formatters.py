from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json

from sglance.models import (
    ClusterMine,
    ClusterQueue,
    ClusterStatus,
    MineReport,
    QueueReport,
    StatusReport,
)

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


def format_mine_json(report: MineReport) -> str:
    payload = {
        "schema_version": report.schema_version,
        "command": report.command,
        "account": report.account,
        "clusters": [asdict(cluster) for cluster in report.clusters],
    }
    return json.dumps(payload, indent=2) + "\n"


_MINE_HEADERS = (
    "JOBID",
    "NAME",
    "USER",
    "ACCOUNT",
    "PARTITION",
    "STATE",
    "CPUS",
    "GPUS",
    "NODES",
    "ELAPSED",
    "LIMIT",
    "REASON",
    "ESTIMATED START",
)


def _format_duration_seconds(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}-{hours:02d}:{minutes:02d}:{secs:02d}"
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_mine_table(report: MineReport, *, now: datetime | None = None) -> str:
    sections = [_format_cluster_mine_section(cluster, now=now) for cluster in report.clusters]
    return "\n\n".join(sections) + "\n"


def _format_cluster_mine_section(cluster: ClusterMine, *, now: datetime | None = None) -> str:
    if cluster.error is not None:
        return f"{cluster.cluster}: {cluster.error}"
    header = format_cluster_header(
        cluster.cluster,
        user_scoped=cluster.user_scoped,
        stale=cluster.stale,
        fetched_at=cluster.fetched_at,
        now=now,
    )
    if not cluster.jobs:
        return f"{header}\n  (no jobs)"

    rows: list[tuple[str, ...]] = []
    for job in cluster.jobs:
        elapsed_label = (
            _format_duration_seconds(job.elapsed_seconds)
            if job.elapsed_seconds is not None
            else "-"
        )
        limit_label = (
            _format_duration_seconds(job.time_limit_seconds)
            if job.time_limit_seconds is not None
            else "-"
        )
        rows.append(
            (
                job.job_id,
                job.name or "-",
                job.user or "-",
                job.account or "-",
                job.partition,
                job.state,
                str(job.cpus) if job.cpus is not None else "-",
                str(job.gpus) if job.gpus is not None else "-",
                str(job.nodes) if job.nodes is not None else "-",
                elapsed_label,
                limit_label,
                job.reason if job.reason is not None else "-",
                job.estimated_start if job.estimated_start is not None else "-",
            )
        )

    widths = [
        max(len(_MINE_HEADERS[col]), *(len(row[col]) for row in rows))
        for col in range(len(_MINE_HEADERS))
    ]

    lines = [header, _render_table_row(_MINE_HEADERS, widths)]
    for row in rows:
        lines.append(_render_table_row(row, widths))
    return "\n".join(lines)
