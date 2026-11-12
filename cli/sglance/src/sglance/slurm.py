from __future__ import annotations

from datetime import UTC, datetime
import re

from sglance.models import ClusterEntry, ClusterStatus, PartitionStatus
from sglance.runner import CommandRunner

_SINFO_FIELDS = "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|"


class _SlurmParseError(Exception):
    """Raised when Slurm command output cannot be parsed."""


class SlurmAdapter:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def query_status(self, cluster_entry: ClusterEntry, *, now: datetime) -> ClusterStatus:
        argv = [
            "env",
            "SLURM_TIME_FORMAT=%s",
            "sinfo",
            "-h",
            "-N",
            "-O",
            _SINFO_FIELDS,
        ]
        result = self._runner.run(cluster_entry, argv)
        if result.returncode != 0:
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


def _parse_gres_gpus(raw: str) -> int:
    stripped = raw.strip()
    if stripped.lower() in {"", "(null)", "none"}:
        return 0
    cleaned = re.sub(r"\((?!null\))[^()]*\)", "", stripped, flags=re.IGNORECASE)
    without_null_type = re.sub(r":\(null\):", "::", cleaned, flags=re.IGNORECASE)
    if "(" in without_null_type or ")" in without_null_type:
        raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
    untyped_total = 0
    has_untyped = False
    typed_total = 0
    for token in cleaned.split(","):
        item = token.strip()
        if not item:
            raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
        parts = [part.strip() for part in item.split(":")]
        if len(parts) < 2 or any(not part for part in parts):
            raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
        if parts[0].lower() != "gpu":
            continue
        count_str = parts[-1]
        if len(parts) not in (2, 3) or not (count_str.isascii() and count_str.isdigit()):
            raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
        count = int(count_str)
        if len(parts) == 2 or parts[1].lower() == "(null)":
            has_untyped = True
            untyped_total += count
        else:
            typed_total += count
    return untyped_total if has_untyped else typed_total


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
        partition_name, node_host, state_long, cpus_state, gres, gres_used = cells
        is_default = partition_name.endswith("*")
        if is_default:
            partition_name = partition_name[:-1]
        normalized_state = state_long.rstrip("*~#!%$@^-").lower()
        if not partition_name or not node_host or not normalized_state:
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
        gpus_total = _parse_gres_gpus(gres)
        gpus_alloc = _parse_gres_gpus(gres_used)

        acc = by_partition.setdefault(
            partition_name,
            {
                "default": False,
                "cpus_alloc": 0,
                "cpus_total": 0,
                "gpus_alloc": 0,
                "gpus_total": 0,
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
        acc["gpus_alloc"] = int(acc["gpus_alloc"]) + gpus_alloc
        acc["gpus_total"] = int(acc["gpus_total"]) + gpus_total
        acc["nodes_total"] = int(acc["nodes_total"]) + 1

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
        gpus_alloc = int(acc["gpus_alloc"])
        gpus_total = int(acc["gpus_total"])
        gpus_utilization = (gpus_alloc / gpus_total) if gpus_total > 0 else None
        partitions.append(
            PartitionStatus(
                partition=name,
                default=bool(acc["default"]),
                cpus_alloc=cpus_alloc,
                cpus_total=cpus_total,
                cpus_utilization=cpus_utilization,
                gpus_alloc=gpus_alloc,
                gpus_total=gpus_total,
                gpus_utilization=gpus_utilization,
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

