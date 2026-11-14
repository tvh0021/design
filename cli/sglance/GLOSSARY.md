# sglance

Read-only inspection of job queues, node capacity, and historical scheduling delays across one or more Slurm clusters.

## Language

**Cluster**:
An independent Slurm installation with its own controller, compute nodes, and job queue.
_Avoid_: Host, server, site, machine

**Cluster entry**:
A named target in the user's configuration that pairs a display name with how a cluster is reached, either on the local login node or over SSH, plus an optional Slurm cluster name.
_Avoid_: Host entry, profile, remote, connection

**Partition**:
A named group of nodes on a cluster governed by shared scheduling limits, access rules, and hardware properties.
_Avoid_: Queue, pool, lane, slice

**Node state**:
The scheduling availability of a compute node: `allocated` when all CPUs are assigned, `idle` when no CPUs are assigned, `mixed` when some CPUs are assigned and others remain free, `down` when unreachable or failed, `drained` when offline to new jobs with none running, or `draining` when finishing running jobs before going offline.
_Avoid_: Host health, machine status

**Utilization**:
The fraction of configured CPUs, GPUs, or nodes in a partition that the scheduler has allocated to running jobs, regardless of how busy the hardware actually is inside those jobs.
_Avoid_: Load, efficiency, occupancy

**Pending reason**:
The scheduler's stated cause keeping a pending job from starting, such as insufficient resources, lower priority, a group limit, an unmet dependency, a hold, or a deferred start time.
_Avoid_: Block reason, wait cause, status code

**Eligible time**:
The moment a submitted job clears all holds, dependencies, and deferred start constraints and becomes a candidate for scheduling.
_Avoid_: Submit time, arrival time

**Job array**:
A single submission of indexed tasks that share a job definition while being scheduled as individual tasks.
_Avoid_: Task group, batch bundle

**Wait time**:
The elapsed time from a job's eligible time to its start time for jobs that started within a lookback window, counting each job array once using its earliest started task and excluding jobs cancelled before starting.
_Avoid_: Queue time, turnaround time, submit delay

**Estimated start**:
The scheduler's own backfill forecast of when a currently pending job will begin running.
_Avoid_: Predicted start, ETA, wait estimate

**Slurm user**:
The login identity that owns a caller's jobs on a specific cluster, which can differ across clusters.
_Avoid_: Account, local user, SSH user

**Slurm account**:
A shared project or group allocation on a cluster under which one or more Slurm users run jobs.
_Avoid_: User, login, billing group

**User-scoped result**:
A reading that reflects only the caller's visible jobs rather than the full cluster because site privacy policy hides other users' records.
_Avoid_: Partial view, personal mode, filtered output
