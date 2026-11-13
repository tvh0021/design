from __future__ import annotations

from collections import Counter
from collections.abc import Collection
from datetime import UTC, datetime
import re
from typing import Any

from sglance._parse import SlurmError, is_gpu_type, iter_rows
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
_INVALID_GRES_GPU_TYPES = frozenset({"no_consume", "(null)"})
_NODE_BUCKETS = {
    "allocated": "nodes_alloc",
    "mixed": "nodes_mixed",
    "idle": "nodes_idle",
    "down": "nodes_down",
    "fail": "nodes_down",
    "failing": "nodes_down",
    "drained": "nodes_drained",
    "draining": "nodes_draining",
}
_COUNTS = (
    "cpus_alloc", "cpus_total", "gpus_alloc", "gpus_total",
    "nodes_alloc", "nodes_mixed", "nodes_idle", "nodes_down",
    "nodes_drained", "nodes_draining", "nodes_other", "nodes_total",
)
_RUNNING_SQUEUE_STATES = frozenset({"R", "CF"})
_PENDING_SQUEUE_STATES = frozenset({"PD", "RQ", "RH", "RD", "SE"})


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
            cluster=entry.name, fetched_at=_stamp(now), partitions=_parse_sinfo_status(stdout)
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
                entry, "sacct", "-X", "-P", "-n", "-a", "-S", f"now-{days}days",
                "-E", "now", "--state=CD,F,TO,CA,OOM,NF,PR,R", f"--format={SACCT_WAIT_FORMAT}",
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


def _is_count(value: str) -> bool:
    return value.isascii() and value.isdigit()


def _parse_gres_gpus(raw: str) -> int:
    bad = SlurmError(f"malformed sinfo GRES {raw!r}")
    stripped = raw.strip()
    if stripped.lower() in {"", "(null)", "none"}:
        return 0
    cleaned = re.sub(r"\((?!null\))[^()]*\)", "", stripped, flags=re.IGNORECASE)
    if re.search(r"[()]", re.sub(r":\(null\):", "::", cleaned, flags=re.IGNORECASE)):
        raise bad
    total = 0
    for token in cleaned.split(","):
        parts = [part.strip() for part in token.split(":")]
        n = len(parts)
        if not parts[0]:
            raise bad
        if parts[0].lower() != "gpu":
            continue
        if not all(parts):
            raise bad
        if n == 1 or (n == 2 and is_gpu_type(parts[1], _INVALID_GRES_GPU_TYPES)):
            total += 1
        elif _is_count(parts[-1]) and (
            n == 2
            or (
                n == 3
                and (
                    parts[1].lower() in {"(null)", "no_consume"}
                    or is_gpu_type(parts[1], _INVALID_GRES_GPU_TYPES)
                )
            )
            or (
                n == 4
                and is_gpu_type(parts[1], _INVALID_GRES_GPU_TYPES)
                and parts[2].lower() == "no_consume"
            )
        ):
            total += int(parts[-1])
        else:
            raise bad
    return total


def _parse_sinfo_status(stdout: str) -> tuple[PartitionStatus, ...]:
    totals: dict[str, Counter[str]] = {}
    defaults: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for line, (name, host, state, cpus_state, gres, gres_used) in iter_rows(
        stdout, "sinfo", 6, bar=None
    ):
        is_default = name.endswith("*")
        name = name[:-1] if is_default else name
        state = state.rstrip("*~#!%$@^?+-").lower()
        if not name or not host or not state:
            raise SlurmError(f"malformed sinfo row: {line}")
        try:
            cpus_alloc, _idle, _other, cpus_total = cpus = [int(p) for p in cpus_state.split("/")]
            if min(cpus) < 0:
                raise ValueError
        except ValueError as exc:
            raise SlurmError(f"malformed sinfo CPUsState {cpus_state!r}") from exc
        gpus_total, gpus_alloc = _parse_gres_gpus(gres), _parse_gres_gpus(gres_used)
        acc = totals.setdefault(name, Counter())
        if is_default:
            defaults.add(name)
        if (name, host) in seen:
            continue
        seen.add((name, host))
        acc.update(
            cpus_alloc=cpus_alloc, cpus_total=cpus_total,
            gpus_alloc=gpus_alloc, gpus_total=gpus_total, nodes_total=1,
        )
        acc[_NODE_BUCKETS.get(state, "nodes_other")] += 1
    return tuple(
        PartitionStatus(
            partition=name,
            default=name in defaults,
            cpus_utilization=acc["cpus_alloc"] / acc["cpus_total"] if acc["cpus_total"] > 0 else None,
            gpus_utilization=acc["gpus_alloc"] / acc["gpus_total"] if acc["gpus_total"] > 0 else None,
            **{k: acc[k] for k in _COUNTS},
        )
        for name, acc in totals.items()
    )


def _parse_squeue_queue(stdout: str) -> tuple[tuple[PartitionQueue, ...], frozenset[str]]:
    by_partition: dict[str, list[Any]] = {}
    users: set[str] = set()
    for line, (job_id, user, partition_field, state, reason) in iter_rows(
        stdout, "squeue", 5, maxsplit=4
    ):
        if not job_id or not partition_field:
            raise SlurmError(f"malformed squeue row: {line}")
        parts = [part.strip() for part in partition_field.split(",")]
        if not all(parts):
            raise SlurmError(f"malformed squeue partition {partition_field!r}")
        if user:
            users.add(user)
        if state in _RUNNING_SQUEUE_STATES:
            by_partition.setdefault(parts[0], [0, Counter()])[0] += 1
        elif state in _PENDING_SQUEUE_STATES:
            by_partition.setdefault(parts[0], [0, Counter()])[1][
                reason.split(",", 1)[0].strip() or "None"
            ] += 1
    return (
        tuple(
            PartitionQueue(
                partition=name,
                running_jobs=running,
                pending_jobs=reasons.total(),
                top_reasons=tuple(
                    PendingReasonCount(reason=r, count=c)
                    for r, c in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:5]
                ),
            )
            for name, (running, reasons) in by_partition.items()
        ),
        frozenset(users),
    )
