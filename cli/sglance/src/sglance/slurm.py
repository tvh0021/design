from __future__ import annotations

from datetime import UTC, datetime

from sglance.models import ClusterEntry, ClusterStatus, PartitionStatus
from sglance.runner import (
    CommandRunner,
    TransportError,
    format_ssh_exit_255,
    format_transport_error,
)

_SINFO_FIELDS = "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|"


class _SlurmParseError(Exception):
    """Raised when Slurm command output cannot be parsed."""


class SlurmAdapter:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def query_status(self, cluster_entry: ClusterEntry, *, now: datetime) -> ClusterStatus:
        cluster_flag = (
            ["-M", cluster_entry.slurm_cluster]
            if cluster_entry.slurm_cluster is not None
            else []
        )
        argv = [
            "env",
            "SLURM_TIME_FORMAT=%s",
            "sinfo",
            *cluster_flag,
            "-h",
            "-N",
            "-O",
            _SINFO_FIELDS,
        ]
        try:
            result = self._runner.run(cluster_entry, argv)
        except (TransportError, OSError) as exc:
            return ClusterStatus(
                cluster=cluster_entry.name,
                fetched_at=None,
                stale=False,
                user_scoped=False,
                error=format_transport_error(exc),
                partitions=(),
            )
        if result.returncode != 0:
            if not cluster_entry.local and result.returncode == 255:
                error_msg = format_ssh_exit_255(result.stderr)
            else:
                first_stderr = next(
                    (line.strip() for line in result.stderr.splitlines() if line.strip()),
                    "",
                )
                error_msg = first_stderr or f"sinfo exited {result.returncode}"
            return ClusterStatus(
                cluster=cluster_entry.name,
                fetched_at=None,
                stale=False,
                user_scoped=False,
                error=error_msg,
                partitions=(),
            )
        try:
            partitions = _parse_sinfo_status(result.stdout)
        except _SlurmParseError as exc:
            return ClusterStatus(
                cluster=cluster_entry.name,
                fetched_at=None,
                stale=False,
                user_scoped=False,
                error=str(exc),
                partitions=(),
            )
        fetched_at = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return ClusterStatus(
            cluster=cluster_entry.name,
            fetched_at=fetched_at,
            stale=False,
            user_scoped=False,
            error=None,
            partitions=partitions,
        )


def _parse_sinfo_status(stdout: str) -> tuple[PartitionStatus, ...]:
    by_partition: dict[str, dict[str, int | bool]] = {}
    seen_nodes: set[tuple[str, str]] = set()
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        line = stripped[:-1] if stripped.endswith("|") else stripped
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != 6:
            raise _SlurmParseError(f"malformed sinfo output: {stripped}")
        partition_name, node_host, state_long, cpus_state, _gres, _gres_used = cells
        is_default = partition_name.endswith("*")
        if is_default:
            partition_name = partition_name[:-1]
        if not partition_name or not node_host or not state_long:
            raise _SlurmParseError(f"malformed sinfo row: {stripped}")

        cpu_parts = cpus_state.split("/")
        if len(cpu_parts) != 4:
            raise _SlurmParseError(f"malformed sinfo CPUsState {cpus_state!r}")
        try:
            parsed_cpus = [int(part) for part in cpu_parts]
        except ValueError as exc:
            raise _SlurmParseError(f"malformed sinfo CPUsState {cpus_state!r}") from exc
        if any(val < 0 for val in parsed_cpus):
            raise _SlurmParseError(f"malformed sinfo CPUsState {cpus_state!r}")
        cpus_alloc = parsed_cpus[0]
        cpus_total = parsed_cpus[3]

        acc = by_partition.setdefault(
            partition_name,
            {
                "default": False,
                "cpus_alloc": 0,
                "cpus_total": 0,
                "nodes_alloc": 0,
                "nodes_mixed": 0,
                "nodes_idle": 0,
                "nodes_down": 0,
                "nodes_drained": 0,
                "nodes_draining": 0,
                "nodes_other": 0,
                "nodes_total": 0,
            },
        )
        if is_default:
            acc["default"] = True

        node_key = (partition_name, node_host)
        if node_key in seen_nodes:
            continue
        seen_nodes.add(node_key)

        acc["cpus_alloc"] = int(acc["cpus_alloc"]) + cpus_alloc
        acc["cpus_total"] = int(acc["cpus_total"]) + cpus_total
        acc["nodes_total"] = int(acc["nodes_total"]) + 1

        normalized_state = state_long.lower()
        if normalized_state == "allocated":
            acc["nodes_alloc"] = int(acc["nodes_alloc"]) + 1
        elif normalized_state == "mixed":
            acc["nodes_mixed"] = int(acc["nodes_mixed"]) + 1
        elif normalized_state == "idle":
            acc["nodes_idle"] = int(acc["nodes_idle"]) + 1
        elif normalized_state in {"down", "fail", "failing"}:
            acc["nodes_down"] = int(acc["nodes_down"]) + 1
        elif normalized_state == "drained":
            acc["nodes_drained"] = int(acc["nodes_drained"]) + 1
        elif normalized_state == "draining":
            acc["nodes_draining"] = int(acc["nodes_draining"]) + 1
        else:
            acc["nodes_other"] = int(acc["nodes_other"]) + 1

    partitions: list[PartitionStatus] = []
    for name, acc in by_partition.items():
        cpus_alloc = int(acc["cpus_alloc"])
        cpus_total = int(acc["cpus_total"])
        cpus_utilization = (cpus_alloc / cpus_total) if cpus_total > 0 else None
        partitions.append(
            PartitionStatus(
                partition=name,
                default=bool(acc["default"]),
                cpus_alloc=cpus_alloc,
                cpus_total=cpus_total,
                cpus_utilization=cpus_utilization,
                gpus_alloc=0,
                gpus_total=0,
                gpus_utilization=None,
                nodes_alloc=int(acc["nodes_alloc"]),
                nodes_mixed=int(acc["nodes_mixed"]),
                nodes_idle=int(acc["nodes_idle"]),
                nodes_down=int(acc["nodes_down"]),
                nodes_drained=int(acc["nodes_drained"]),
                nodes_draining=int(acc["nodes_draining"]),
                nodes_other=int(acc["nodes_other"]),
                nodes_total=int(acc["nodes_total"]),
            )
        )
    return tuple(partitions)
