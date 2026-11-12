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


@dataclass(frozen=True)
class ClusterStatus:
    cluster: str
    fetched_at: str | None
    stale: bool
    user_scoped: bool
    error: str | None
    partitions: tuple[PartitionStatus, ...]


@dataclass(frozen=True)
class StatusReport:
    clusters: tuple[ClusterStatus, ...]
    schema_version: int = 1
    command: str = "status"
