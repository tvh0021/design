from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ClusterEntry:
    name: str
    ssh: str | None
    local: bool = False
    slurm_cluster: str | None = None


@dataclass(frozen=True)
class PartitionStatus:
    partition: str
    default: bool
    cpus_alloc: int
    cpus_total: int
    cpus_utilization: float | None
    gpus_alloc: int
    gpus_total: int
    gpus_utilization: float | None
    nodes_alloc: int
    nodes_mixed: int
    nodes_idle: int
    nodes_down: int
    nodes_drained: int
    nodes_draining: int
    nodes_other: int
    nodes_total: int


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ClusterResult:
    cluster: str
    fetched_at: str | None = None
    stale: bool = False
    user_scoped: bool = False
    error: str | None = None


@dataclass(frozen=True)
class ClusterStatus(ClusterResult):
    partitions: tuple[PartitionStatus, ...] = ()


@dataclass(frozen=True)
class PendingReasonCount:
    reason: str
    count: int


@dataclass(frozen=True)
class PartitionQueue:
    partition: str
    running_jobs: int
    pending_jobs: int
    top_reasons: tuple[PendingReasonCount, ...]


@dataclass(frozen=True)
class ClusterQueue(ClusterResult):
    partitions: tuple[PartitionQueue, ...] = ()


@dataclass(frozen=True)
class MineJob:
    job_id: str
    name: str
    user: str
    account: str
    partition: str
    state: str
    cpus: int | None
    gpus: int | None
    nodes: int | None
    elapsed_seconds: int | None
    time_limit_seconds: int | None
    reason: str | None
    estimated_start: str | None


@dataclass(frozen=True)
class ClusterMine(ClusterResult):
    slurm_user: str | None = None
    account_filter: str | None = None
    jobs: tuple[MineJob, ...] = ()


@dataclass(frozen=True)
class PartitionWait:
    partition: str
    n: int
    p50_seconds: float | None
    p90_seconds: float | None
    median_pending_seconds: float | None
    oldest_pending_seconds: int | None


@dataclass(frozen=True)
class ClusterWait(ClusterResult):
    history_error: str | None = None
    partitions: tuple[PartitionWait, ...] = ()
