from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime
import re

from sglance.config import validate_safe_token
from sglance.mine import MINE_SQUEUE_FIELDS, parse_squeue_mine
from sglance.models import (
    ClusterEntry,
    ClusterMine,
    ClusterQueue,
    ClusterStatus,
    ClusterWait,
    PartitionQueue,
    PartitionStatus,
    PendingReasonCount,
)
from sglance.runner import (
    AllowlistedRunner,
    CommandResult,
    CommandRunner,
    TransportError,
    format_ssh_exit_255,
    format_transport_error,
)
from sglance.wait import SACCT_WAIT_FORMAT, SQUEUE_WAIT_FIELDS, parse_wait_partitions

_SINFO_FIELDS = "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|"
_SQUEUE_QUEUE_FIELDS = "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|"


class SlurmError(Exception):
    """Raised when a Slurm command fails or returns malformed output."""


_SlurmParseError = SlurmError


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_command_failure(tool: str, result: CommandResult) -> str:
    last = next((s for line in reversed(result.stderr.splitlines()) if (s := line.strip())), "")
    last = last.removeprefix(f"{tool}: ")
    return f"{tool} exited {result.returncode}: {last}" if last else f"{tool} exited {result.returncode}"


def _is_unsupported_me_stderr(stderr: str) -> bool:
    lowered = stderr.lower()
    return bool(re.search(r"--\s*me\b", lowered)) and any(
        phrase in lowered
        for phrase in ("unrecognized option", "unknown option", "invalid option", "illegal option")
    )


class SlurmAdapter:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = AllowlistedRunner(runner)

    def _run(self, entry: ClusterEntry, tool: str, *args: str) -> str:
        flag = ("-M", entry.slurm_cluster) if entry.slurm_cluster is not None else ()
        argv = ["env", "SLURM_TIME_FORMAT=%s", tool, *flag, *args]
        try:
            result = self._runner.run(entry, argv)
        except (TransportError, OSError) as exc:
            raise SlurmError(format_transport_error(exc)) from exc
        if not entry.local and result.returncode == 255:
            raise SlurmError(format_ssh_exit_255(result.stderr))
        if result.returncode != 0:
            if "--me" in args and _is_unsupported_me_stderr(result.stderr):
                raise SlurmError("squeue does not support --me (needs Slurm 20.11 or newer)")
            raise SlurmError(_format_command_failure(tool, result))
        return result.stdout

    def _user_scoped(self, entry: ClusterEntry, users: Collection[str]) -> bool:
        if len(users) == 1:
            return True
        try:
            stdout = self._run(entry, "scontrol", "show", "config")
        except SlurmError:
            return False
        scoped = False
        for line in stdout.splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key.strip().lower() == "privatedata":
                scoped = bool({t.strip().lower() for t in value.split(",")} & {"jobs", "all"})
        return scoped

    def query_status(self, entry: ClusterEntry, *, now: datetime) -> ClusterStatus:
        stdout = self._run(entry, "sinfo", "-h", "-N", "-O", _SINFO_FIELDS)
        return ClusterStatus(
            cluster=entry.name,
            fetched_at=_stamp(now),
            partitions=_parse_sinfo_status(stdout),
        )

    def query_queue(self, entry: ClusterEntry, *, now: datetime) -> ClusterQueue:
        stdout = self._run(entry, "squeue", "-h", "-r", "-O", _SQUEUE_QUEUE_FIELDS)
        partitions, users = _parse_squeue_queue(stdout)
        return ClusterQueue(
            cluster=entry.name,
            fetched_at=_stamp(now),
            user_scoped=self._user_scoped(entry, users),
            partitions=partitions,
        )

    def query_mine(
        self, entry: ClusterEntry, *, now: datetime, account: str | None = None
    ) -> ClusterMine:
        if account is not None:
            validate_safe_token(account, field_name="--account")
        scope = ("-A", account) if account is not None else ("--me",)
        stdout = self._run(entry, "squeue", "-h", *scope, "-O", MINE_SQUEUE_FIELDS)
        jobs, users, first_user = parse_squeue_mine(stdout, now=now)
        return ClusterMine(
            cluster=entry.name,
            fetched_at=_stamp(now),
            user_scoped=False if account is None else self._user_scoped(entry, users),
            slurm_user=first_user if account is None else None,
            account_filter=account,
            jobs=jobs,
        )

    def query_wait(self, entry: ClusterEntry, *, days: int, now: datetime) -> ClusterWait:
        try:
            sacct_stdout = self._run(
                entry,
                "sacct",
                "-X",
                "-P",
                "-n",
                "-a",
                "-S",
                f"now-{days}days",
                "-E",
                "now",
                "--state=CD,F,TO,CA,OOM,NF,PR,R",
                f"--format={SACCT_WAIT_FORMAT}",
            )
            history_error = None
        except SlurmError as exc:
            sacct_stdout, history_error = "", str(exc)
        squeue_stdout = self._run(entry, "squeue", "-h", "-r", "-t", "PD", "-O", SQUEUE_WAIT_FIELDS)
        partitions, users = parse_wait_partitions(sacct_stdout, squeue_stdout, days=days, now=now)
        return ClusterWait(
            cluster=entry.name,
            fetched_at=_stamp(now),
            user_scoped=self._user_scoped(entry, users),
            history_error=history_error,
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
    def _is_count(value: str) -> bool:
        return value.isascii() and value.isdigit()

    def _is_gpu_type(value: str) -> bool:
        lowered = value.lower()
        if lowered in {"no_consume", "(null)"}:
            return False
        if re.fullmatch(r"\d+[kmgtp]", lowered):
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value)) and any(
            ch.isascii() and ch.isalpha() for ch in value
        )

    total_gpus = 0
    for token in cleaned.split(","):
        item = token.strip()
        if not item:
            raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
        parts = [part.strip() for part in item.split(":")]
        if not parts[0]:
            raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
        if parts[0].lower() != "gpu":
            continue
        if any(not part for part in parts):
            raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
        if len(parts) == 1:
            total_gpus += 1
        elif len(parts) == 2:
            if _is_count(parts[1]):
                total_gpus += int(parts[1])
            elif _is_gpu_type(parts[1]):
                total_gpus += 1
            else:
                raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
        elif len(parts) == 3:
            if not _is_count(parts[2]):
                raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
            if parts[1].lower() not in {"(null)", "no_consume"} and not _is_gpu_type(parts[1]):
                raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
            total_gpus += int(parts[2])
        elif len(parts) == 4:
            if (
                not _is_gpu_type(parts[1])
                or parts[2].lower() != "no_consume"
                or not _is_count(parts[3])
            ):
                raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
            total_gpus += int(parts[3])
        else:
            raise _SlurmParseError(f"malformed sinfo GRES {raw!r}")
    return total_gpus


def _parse_sinfo_status(stdout: str) -> tuple[PartitionStatus, ...]:
    by_partition: dict[str, dict[str, int | bool]] = {}
    seen_nodes: set[tuple[str, str]] = set()
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("CLUSTER: "):
            continue
        line = stripped[:-1] if stripped.endswith("|") else stripped
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != 6:
            raise _SlurmParseError(f"malformed sinfo output: {stripped}")
        partition_name, node_host, state_long, cpus_state, gres, gres_used = cells
        is_default = partition_name.endswith("*")
        if is_default:
            partition_name = partition_name[:-1]
        normalized_state = state_long.rstrip("*~#!%$@^?+-").lower()
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


_RUNNING_SQUEUE_STATES = frozenset({"R", "CF"})
_PENDING_SQUEUE_STATES = frozenset({"PD", "RQ", "RH", "RD", "SE"})


def _parse_squeue_queue(stdout: str) -> tuple[tuple[PartitionQueue, ...], frozenset[str]]:
    by_partition: dict[str, tuple[int, int, dict[str, int]]] = {}
    distinct_users: set[str] = set()
    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("CLUSTER: "):
            continue
        if not stripped.endswith("|"):
            raise _SlurmParseError(f"malformed squeue output: {stripped}")
        line = stripped[:-1]
        cells = [cell.strip() for cell in line.split("|", 4)]
        if len(cells) != 5:
            raise _SlurmParseError(f"malformed squeue output: {stripped}")
        job_id, user_name, partition_field, state_compact, reason = cells
        if not job_id or not partition_field:
            raise _SlurmParseError(f"malformed squeue row: {stripped}")
        partition_parts = [part.strip() for part in partition_field.split(",")]
        if any(not part for part in partition_parts):
            raise _SlurmParseError(f"malformed squeue partition {partition_field!r}")
        partition_name = partition_parts[0]
        if user_name:
            distinct_users.add(user_name)

        is_running = state_compact in _RUNNING_SQUEUE_STATES
        is_pending = state_compact in _PENDING_SQUEUE_STATES
        if not is_running and not is_pending:
            continue

        running_jobs, pending_jobs, reason_counts = by_partition.get(
            partition_name, (0, 0, {})
        )
        if is_running:
            running_jobs += 1
        else:
            normalized_reason = reason.split(",", 1)[0].strip() or "None"
            pending_jobs += 1
            reason_counts[normalized_reason] = reason_counts.get(normalized_reason, 0) + 1
        by_partition[partition_name] = (running_jobs, pending_jobs, reason_counts)

    partitions: list[PartitionQueue] = []
    for name, (running_jobs, pending_jobs, reason_counts) in by_partition.items():
        sorted_reasons = sorted(
            reason_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[:5]
        partitions.append(
            PartitionQueue(
                partition=name,
                running_jobs=running_jobs,
                pending_jobs=pending_jobs,
                top_reasons=tuple(
                    PendingReasonCount(reason=r_name, count=r_count)
                    for r_name, r_count in sorted_reasons
                ),
            )
        )
    return tuple(partitions), frozenset(distinct_users)
