from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sglance._parse import SlurmError, iter_rows, parse_epoch
from sglance.models import PartitionWait

SACCT_WAIT_FORMAT = "JobID,User,Partition,State,Eligible,Start,AllocTRES"
SQUEUE_WAIT_FIELDS = "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|,EligibleTime:0|"

_STARTED_SACCT_STATES = frozenset({
    "COMPLETED", "CD", "FAILED", "F", "TIMEOUT", "TO", "CANCELLED", "CA",
    "OUT_OF_MEMORY", "OOM", "NODE_FAIL", "NF", "PREEMPTED", "PR", "RUNNING", "R",
    "COMPLETING", "CG", "BOOT_FAIL", "BF", "DEADLINE", "DL", "SUSPENDED", "S",
})
_CONTENTION_PENDING_REASONS = frozenset({"Resources", "Priority", "None"})
_CONTENTION_PENDING_PREFIXES = ("AssocGrp", "QOSGrp")


def _quantile_hundredths(v: Sequence[int], pct: int) -> float:
    lo, rem = divmod(pct * (len(v) - 1), 100)
    return (v[lo] * 100 + rem * (v[lo + (rem > 0)] - v[lo])) / 100


def _parse_squeue_pending_ages(squeue_stdout: str, *, now_epoch: int) -> dict[str, list[int]]:
    ages_by_partition: dict[str, list[int]] = {}
    for line, (job_id, _user, partition_field, state, reason, eligible_s) in iter_rows(
        squeue_stdout, "squeue", 6
    ):
        if not job_id or not partition_field:
            raise SlurmError(f"malformed squeue row: {line}")
        partition_parts = [part.strip().rstrip("*") for part in partition_field.split(",")]
        if not all(partition_parts):
            raise SlurmError(f"malformed squeue partition {partition_field!r}")
        if state != "PD":
            continue
        reason_code = reason.split(",", 1)[0].strip().strip("()")
        if (
            reason_code not in _CONTENTION_PENDING_REASONS
            and not reason_code.startswith(_CONTENTION_PENDING_PREFIXES)
        ):
            continue
        eligible = parse_epoch(eligible_s)
        if eligible is None or eligible > now_epoch:
            continue
        age = now_epoch - eligible
        for partition in dict.fromkeys(partition_parts):
            ages_by_partition.setdefault(partition, []).append(age)
    return ages_by_partition


def parse_wait_partitions(
    sacct_stdout: str, squeue_stdout: str, *, days: int, now: datetime
) -> tuple[tuple[PartitionWait, ...], frozenset[str]]:
    now_epoch = int(now.astimezone(UTC).timestamp())
    window_start = now_epoch - days * 86400
    collapsed: dict[tuple[str, str], tuple[int, str, int]] = {}
    users: set[str] = set()

    for row_idx, (line, (job_id, user, partition_field, state, eligible_s, start_s, alloc_tres)) in enumerate(
        iter_rows(sacct_stdout, "sacct", 7, bar=False)
    ):
        if not job_id:
            raise SlurmError(f"malformed sacct row: {line}")
        if "." in job_id:
            continue
        if user:
            users.add(user)
        partition = partition_field.split(",", 1)[0].strip().rstrip("*")
        if (
            not alloc_tres
            or not partition
            or (state.split()[0].rstrip("+").upper() if state else "") not in _STARTED_SACCT_STATES
        ):
            continue
        eligible, start = parse_epoch(eligible_s), parse_epoch(start_s)
        if eligible is None or start is None or start < eligible:
            continue

        array_id, array_sep, task_id = job_id.partition("_")
        het_id, het_sep, het_offset = job_id.partition("+")
        if array_sep and array_id and task_id:
            key = ("array", array_id)
        elif het_sep and het_id and het_offset:
            key = ("het", het_id)
        else:
            key = ("job", str(row_idx))
        existing = collapsed.get(key)
        if existing is None or start < existing[0]:
            collapsed[key] = (start, partition, start - eligible)

    samples: dict[str, list[int]] = {}
    for start, partition, wait_seconds in collapsed.values():
        if start >= window_start:
            samples.setdefault(partition, []).append(wait_seconds)

    pending_ages = _parse_squeue_pending_ages(squeue_stdout, now_epoch=now_epoch)
    partitions: list[PartitionWait] = []
    for p in dict.fromkeys([*samples, *pending_ages]):
        waits = sorted(samples.get(p, ()))
        ages = sorted(pending_ages.get(p, ()))
        partitions.append(
            PartitionWait(
                partition=p,
                n=len(waits),
                p50_seconds=_quantile_hundredths(waits, 50) if waits else None,
                p90_seconds=_quantile_hundredths(waits, 90) if waits else None,
                median_pending_seconds=_quantile_hundredths(ages, 50) if ages else None,
                oldest_pending_seconds=ages[-1] if ages else None,
            )
        )
    return tuple(partitions), frozenset(users)
