from __future__ import annotations

from dataclasses import asdict
import json

from sglance.models import ClusterStatus, StatusReport

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


def format_status_table(report: StatusReport) -> str:
    sections = [_format_cluster_status_section(cluster) for cluster in report.clusters]
    return "\n\n".join(sections) + "\n"


def _format_cluster_status_section(cluster: ClusterStatus) -> str:
    if cluster.error is not None:
        return f"{cluster.cluster}: {cluster.error}"
    header = cluster.cluster
    if cluster.user_scoped:
        header = f"{header} [user-scoped]"
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
