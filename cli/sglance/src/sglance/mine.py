from __future__ import annotations

from datetime import UTC, datetime

from sglance.models import MineJob

MINE_SQUEUE_FIELDS = (
    "JobID:0|,Name:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
    "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,tres-alloc:0|,StartTime:0|,Reason:0|"
)

_PENDING_STATES = frozenset({"PD", "RQ", "RH", "RD", "SE"})
_TERMINAL_STATES = frozenset({"CD", "F", "CA", "TO", "OOM", "NF", "PR", "BF", "DL", "RV", "EX"})
_UNBOUNDED_LIMITS = frozenset({"unlimited", "partition_limit"})
_UNSET_START_VALUES = frozenset({"", "n/a", "unknown", "none", "(null)"})


class MineParseError(Exception):
    """Raised when squeue output for mine is structurally malformed."""


def _parse_nonneg_int(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"invalid non-negative integer {raw!r}")
    return int(raw)


def _parse_slurm_duration_seconds(raw: str) -> int:
    text = raw.strip()
    if not text:
        raise ValueError("empty duration")
    days = 0
    if "-" in text:
        days_part, time_part = text.split("-", 1)
        days = _parse_nonneg_int(days_part)
    else:
        time_part = text
    parts = time_part.split(":")
    if len(parts) == 2 and "-" not in text:
        minutes = _parse_nonneg_int(parts[0])
        seconds = _parse_nonneg_int(parts[1])
        if seconds >= 60:
            raise ValueError(f"invalid seconds in {raw!r}")
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours = _parse_nonneg_int(parts[0])
        minutes = _parse_nonneg_int(parts[1])
        seconds = _parse_nonneg_int(parts[2])
        if minutes >= 60 or seconds >= 60:
            raise ValueError(f"invalid MM:SS in {raw!r}")
        return days * 86400 + hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"invalid duration {raw!r}")


def _parse_time_limit_seconds(raw: str) -> int | None:
    stripped = raw.strip()
    if stripped.lower() in _UNBOUNDED_LIMITS:
        return None
    return _parse_slurm_duration_seconds(stripped)


def _parse_tres_gpus(raw: str) -> int:
    stripped = raw.strip()
    if stripped.lower() in {"", "(null)", "none"}:
        return 0
    has_untyped = False
    untyped_gpus = 0
    typed_gpus = 0
    for token in stripped.split(","):
        item = token.strip()
        if not item:
            continue
        key, sep, val = item.partition("=")
        if sep != "=":
            continue
        key_lower = key.strip().lower()
        val_str = val.strip()
        if key_lower == "gres/gpu":
            untyped_gpus += _parse_nonneg_int(val_str)
            has_untyped = True
        elif key_lower.startswith("gres/gpu:"):
            gpu_type = key_lower[len("gres/gpu:") :].strip()
            if not gpu_type:
                raise ValueError(f"empty GPU type in TRES {raw!r}")
            typed_gpus += _parse_nonneg_int(val_str)
    return untyped_gpus if has_untyped else typed_gpus


def _normalize_partition(raw: str) -> str:
    first = raw.split(",", 1)[0].strip()
    if first.endswith("*"):
        first = first[:-1]
    return first


def _parse_estimated_start(raw: str, *, is_pending: bool) -> str | None:
    if not is_pending:
        return None
    stripped = raw.strip()
    if stripped.lower() in _UNSET_START_VALUES:
        return None
    if not stripped.isascii() or not stripped.isdigit():
        return None
    epoch = int(stripped)
    if epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def parse_squeue_mine(stdout: str) -> tuple[tuple[MineJob, ...], frozenset[str], str | None]:
    jobs: list[MineJob] = []
    distinct_users: set[str] = set()
    first_user: str | None = None

    for raw_line in stdout.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("CLUSTER: "):
            continue
        if not stripped.endswith("|"):
            raise MineParseError(f"malformed squeue output: {stripped}")
        cells = [cell.strip() for cell in stripped[:-1].split("|", 12)]
        if len(cells) != 13:
            raise MineParseError(f"malformed squeue output: {stripped}")

        (
            job_id,
            name,
            user_name,
            account_val,
            partition_field,
            state_compact,
            time_used_raw,
            time_limit_raw,
            num_nodes_raw,
            num_cpus_raw,
            tres_alloc_raw,
            start_time_raw,
            reason_raw,
        ) = cells

        partition_name = _normalize_partition(partition_field)
        if not job_id or not partition_name or not state_compact or state_compact in _TERMINAL_STATES:
            continue

        try:
            nodes = _parse_nonneg_int(num_nodes_raw)
            cpus = _parse_nonneg_int(num_cpus_raw)
            elapsed_seconds = _parse_slurm_duration_seconds(time_used_raw)
            time_limit_seconds = _parse_time_limit_seconds(time_limit_raw)
            gpus = _parse_tres_gpus(tres_alloc_raw)
        except ValueError:
            continue

        is_pending = state_compact in _PENDING_STATES
        reason = (reason_raw or "None") if is_pending else None
        estimated_start = _parse_estimated_start(start_time_raw, is_pending=is_pending)

        if user_name:
            distinct_users.add(user_name)
            if first_user is None:
                first_user = user_name

        jobs.append(
            MineJob(
                job_id=job_id,
                name=name,
                user=user_name,
                account=account_val,
                partition=partition_name,
                state=state_compact,
                cpus=cpus,
                gpus=gpus,
                nodes=nodes,
                elapsed_seconds=elapsed_seconds,
                time_limit_seconds=time_limit_seconds,
                reason=reason,
                estimated_start=estimated_start,
            )
        )

    return tuple(jobs), frozenset(distinct_users), first_user
