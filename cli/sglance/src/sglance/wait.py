from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sglance.models import PartitionWait

SACCT_WAIT_FORMAT = "JobID,User,Partition,State,Eligible,Start,AllocTRES"
SQUEUE_WAIT_FIELDS = (
    "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|,EligibleTime:0|"
)

_STARTED_SACCT_STATES = frozenset(
    {
        "COMPLETED",
        "CD",
        "FAILED",
        "F",
        "TIMEOUT",
        "TO",
        "CANCELLED",
        "CA",
        "OUT_OF_MEMORY",
        "OOM",
        "NODE_FAIL",
        "NF",
        "PREEMPTED",
        "PR",
        "RUNNING",
        "R",
        "COMPLETING",
        "CG",
        "BOOT_FAIL",
        "BF",
        "DEADLINE",
        "DL",
        "SUSPENDED",
        "S",
    }
)

_CONTENTION_PENDING_REASONS = frozenset({"Resources", "Priority", "None"})
_CONTENTION_PENDING_PREFIXES = ("AssocGrp", "QOSGrp")


class WaitParseError(Exception):
    """Raised when sacct or squeue output for wait is structurally malformed."""


def _parse_epoch(raw: str) -> int | None:
    stripped = raw.strip()
    if not stripped or stripped.lower() in {"unknown", "none", "n/a", "(null)"}:
        return None
    if not (stripped.isascii() and stripped.isdigit()):
        return None
    value = int(stripped, 10)
    if value <= 0:
        return None
    return value


def _quantile_hundredths(sorted_values: Sequence[int], pct: int) -> float:
    n = len(sorted_values)
    lo, rem = divmod(pct * (n - 1), 100)
    if rem == 0:
        return float(sorted_values[lo])
    numer = sorted_values[lo] * 100 + rem * (sorted_values[lo + 1] - sorted_values[lo])
    return float(Decimal(numer) / Decimal(100))


def _parse_squeue_pending_ages(squeue_stdout: str, *, now_epoch: int) -> dict[str, list[int]]:
    ages_by_partition: dict[str, list[int]] = {}
    for raw_line in squeue_stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("CLUSTER: "):
            continue
        if not stripped.endswith("|"):
            raise WaitParseError(f"malformed squeue output: {stripped}")
        cells = [cell.strip() for cell in stripped[:-1].split("|")]
        if len(cells) != 6:
            raise WaitParseError(f"malformed squeue output: {stripped}")
        job_id, _user_name, partition_field, state_compact, reason, eligible_s = cells
        if not job_id or not partition_field:
            raise WaitParseError(f"malformed squeue row: {stripped}")
        partition_parts = [part.strip().rstrip("*") for part in partition_field.split(",")]
        if any(not part for part in partition_parts):
            raise WaitParseError(f"malformed squeue partition {partition_field!r}")

        if state_compact != "PD":
            continue
        reason_code = reason.split(",", 1)[0].strip().strip("()")
        if (
            reason_code not in _CONTENTION_PENDING_REASONS
            and not reason_code.startswith(_CONTENTION_PENDING_PREFIXES)
        ):
            continue
        eligible = _parse_epoch(eligible_s)
        if eligible is None or eligible > now_epoch:
            continue
        age_seconds = now_epoch - eligible
        for partition in dict.fromkeys(partition_parts):
            ages_by_partition.setdefault(partition, []).append(age_seconds)
    return ages_by_partition


def parse_wait_partitions(
    sacct_stdout: str,
    squeue_stdout: str,
    *,
    days: int,
    now: datetime,
) -> tuple[tuple[PartitionWait, ...], frozenset[str]]:
    now_epoch = int(now.astimezone(UTC).timestamp())
    window_start = now_epoch - days * 86400

    # Key: ("array", array_id), ("het", het_id), or ("job", str(row_idx)) -> (start_epoch, partition, wait_seconds)
    collapsed: dict[tuple[str, str], tuple[int, str, int]] = {}
    distinct_users: set[str] = set()

    for row_idx, raw_line in enumerate(sacct_stdout.splitlines()):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("CLUSTER: "):
            continue
        cells = [cell.strip() for cell in stripped.split("|")]
        if len(cells) != 7:
            raise WaitParseError(f"malformed sacct output: {stripped}")
        job_id, user, partition_field, state, eligible_s, start_s, alloc_tres = cells
        if not job_id:
            raise WaitParseError(f"malformed sacct row: {stripped}")
        if "." in job_id:
            continue
        if user:
            distinct_users.add(user)
        if not alloc_tres:
            continue

        partition = partition_field.split(",", 1)[0].strip().rstrip("*")
        if not partition:
            continue

        state_token = state.split()[0].rstrip("+").upper() if state else ""
        if (
            not state_token
            or state_token.startswith("REQUEUE")
            or state_token not in _STARTED_SACCT_STATES
        ):
            continue

        eligible = _parse_epoch(eligible_s)
        start = _parse_epoch(start_s)
        if eligible is None or start is None or start < eligible:
            continue

        wait_seconds = start - eligible
        array_id, array_sep, task_id = job_id.partition("_")
        het_id, het_sep, het_offset = job_id.partition("+")
        if array_sep == "_" and array_id and task_id:
            key = ("array", array_id)
        elif het_sep == "+" and het_id and het_offset:
            key = ("het", het_id)
        else:
            key = ("job", str(row_idx))
        existing = collapsed.get(key)
        if existing is None or start < existing[0]:
            collapsed[key] = (start, partition, wait_seconds)

    samples_by_partition: dict[str, list[int]] = {}
    for start, partition, wait_seconds in collapsed.values():
        if start < window_start:
            continue
        samples_by_partition.setdefault(partition, []).append(wait_seconds)

    pending_ages_by_partition = _parse_squeue_pending_ages(squeue_stdout, now_epoch=now_epoch)
    ordered_names = list(
        dict.fromkeys([*samples_by_partition.keys(), *pending_ages_by_partition.keys()])
    )

    partitions: list[PartitionWait] = []
    for partition in ordered_names:
        waits = samples_by_partition.get(partition)
        if waits:
            sorted_waits = sorted(waits)
            n = len(sorted_waits)
            p50: float | None = _quantile_hundredths(sorted_waits, 50)
            p90: float | None = _quantile_hundredths(sorted_waits, 90)
        else:
            n = 0
            p50 = None
            p90 = None
        pending_ages = pending_ages_by_partition.get(partition)
        if pending_ages:
            sorted_pending = sorted(pending_ages)
            median_pending: float | None = _quantile_hundredths(sorted_pending, 50)
            oldest_pending: int | None = sorted_pending[-1]
        else:
            median_pending = None
            oldest_pending = None
        partitions.append(
            PartitionWait(
                partition=partition,
                n=n,
                p50_seconds=p50,
                p90_seconds=p90,
                median_pending_seconds=median_pending,
                oldest_pending_seconds=oldest_pending,
            )
        )
    return tuple(partitions), frozenset(distinct_users)
