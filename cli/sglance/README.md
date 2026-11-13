# sglance

## What it does

`sglance` is a Python 3.11+ command-line tool that inspects partition capacity, job queues, historical wait times, and active jobs across one or more Slurm clusters. It uses only the Python standard library, queries remote clusters in parallel over non-interactive OpenSSH or runs directly on a local login node, and caches successful per-cluster responses for five minutes under `$XDG_CACHE_HOME/sglance` or `~/.cache/sglance`. After each successful cache write, `sglance` deletes its own snapshot files older than one day from its cache directory; caching is off when both `XDG_CACHE_HOME` and `HOME` are unset.

`sglance` is strictly read-only. Every command passes through an internal allowlist that enforces the `env SLURM_TIME_FORMAT=%s` prefix, the four binaries `sinfo`, `squeue`, `sacct`, and `scontrol`, and that `scontrol` may only run `show config`; the listed command lines are what `sglance` actually sends:

- `env SLURM_TIME_FORMAT=%s sinfo [-M <slurm_cluster>] -h -N -O PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|`
- `env SLURM_TIME_FORMAT=%s squeue [-M <slurm_cluster>] -h -r -O JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|`
- `env SLURM_TIME_FORMAT=%s squeue [-M <slurm_cluster>] -h -r -t PD -O JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|,EligibleTime:0|`
- `env SLURM_TIME_FORMAT=%s squeue [-M <slurm_cluster>] -h --me -O JobArrayID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,NumTasks:0|,tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,tres-per-task:0|,StartTime:0|,Reason:0|,Name:0|` or the same command with `-A <account>` in place of `--me`
- `env SLURM_TIME_FORMAT=%s sacct [-M <slurm_cluster>] -X -P -n -a -S now-<days>days -E now --state=CD,F,TO,CA,OOM,NF,PR,R --format=JobID,User,Partition,State,Eligible,Start,AllocTRES`
- `env SLURM_TIME_FORMAT=%s scontrol [-M <slurm_cluster>] show config`

`sglance` never submits, alters, holds, or cancels jobs, never runs `sbatch --test-only`, and provides no configuration hook to override remote binaries.

## Install

`sglance` requires Python 3.11 or newer and OpenSSH `ssh` for remote cluster entries.

Install `pipx` first if missing with `brew install pipx` on macOS or `sudo apt install pipx` on Debian/Raspberry Pi OS, then run `pipx ensurepath` and open a new shell; Homebrew and Debian Python refuse plain `pip install` outside a virtual environment.

Install in an isolated environment with `pipx`:

```sh
pipx install "git+https://github.com/tvh0021/design#subdirectory=cli/sglance"
```

Or install with `pip`, for example inside a virtual environment:

```sh
python3 -m pip install "git+https://github.com/tvh0021/design#subdirectory=cli/sglance"
```

## Configure

`sglance` reads `$XDG_CONFIG_HOME/sglance/config.toml`, or `~/.config/sglance/config.toml` when `XDG_CONFIG_HOME` is unset or empty. The file must define at least one `[[clusters]]` table and no other top-level keys.

```toml
[[clusters]]
name = "cluster-a"
ssh = "login.cluster-a.example.org"

[[clusters]]
name = "cluster-b"
ssh = "cluster-b"
slurm_cluster = "prod"

[[clusters]]
name = "cluster-local"
local = true
```

Each `[[clusters]]` cluster entry accepts these keys:

- **`name`**. Required string. Unique display name for the cluster entry and the value matched by `--cluster NAME`. When `local` is `false` and `ssh` is omitted, `sglance` uses `name` as the `ssh` destination.
- **`ssh`**. Optional string. OpenSSH host alias or hostname passed to `ssh`. Cannot be set together with `local = true`.
- **`local`**. Optional boolean, default `false`. Runs Slurm commands directly on the local login node instead of over `ssh`. When `true`, cannot be set together with `ssh`.
- **`slurm_cluster`**. Optional string. Named Slurm cluster passed as `-M <slurm_cluster>` when one login node fronts multiple Slurm clusters.

`name`, `ssh`, and `slurm_cluster` must be non-empty strings that do not start with `-`, do not contain commas, and do not contain whitespace or control characters. Unknown keys or invalid values write `sglance: error: ...` to `stderr` and exit with code `2`.

SSH access to every remote cluster entry must already work without interactive prompts. `sglance` runs `ssh -T -o BatchMode=yes -o ConnectTimeout=5 -o RemoteCommand=none -o LogLevel=ERROR -- <ssh> <command>` with standard input closed and a 25-second timeout per command. Verify each remote target beforehand with `ssh <host> true`, which must exit `0` without asking for a password, key passphrase, or host-key confirmation.

`sglance` stores no passwords, SSH keys, tokens, or other secrets. Authentication uses your existing OpenSSH configuration and SSH agent.

## Usage

Every subcommand queries all configured cluster entries in parallel unless filtered with `--cluster NAME`.

Common options work before or after the subcommand:

- **`--cluster NAME`**. Query only the named cluster entry from `config.toml`. Accepted before or after the subcommand. Passing an unconfigured name writes `sglance: error: unknown cluster 'NAME' (configured: ...)` to `stderr` and exits with code `2`.
- **`--refresh`**. Bypass the five-minute response cache and fetch fresh Slurm data. Accepted before or after the subcommand.
- **`--compact`**. Print a narrow plain-text table with fewer columns and one line per row. Accepted before or after the subcommand, and `--json` ignores it. `status` shows `PARTITION`, `CPUS` as `free/total`, `GPUS` as `free/total` or `-`, and `NODES` as `idle/total`. `queue` shows `PARTITION`, `wait` shows `PARTITION`, `P50`, and `P90` without the `history unavailable` note, and `mine` shows `JOBID`, `PARTITION`, `STATE`, and `ESTIMATED START`.
- **`--json`**. Emit structured JSON with `"schema_version": 1` instead of plain-text tables. Accepted before or after the subcommand.

When a response comes from the five-minute cache, plain-text headers append `[cached <N>s ago]` and JSON cluster objects set `"stale": true`. CLI flag errors and `config.toml` errors exit with code `2`. Per-cluster transport, Slurm, or parse failures still exit with code `0` and report the failure inline in table mode and in the cluster object's `"error"` field in JSON mode. Unreachable hosts show `<cluster>: unreachable (...)` in table mode and `unreachable (...)` in JSON, while non-zero Slurm command exits show `<cluster>: <tool> exited <code>: <last non-empty stderr line, with a leading "<tool>: " removed>`, or `<cluster>: <tool> exited <code>` when `stderr` is empty.

### `status`

Shows partition CPU, GPU, and node utilization from `sinfo`.

```text
$ sglance status
cluster-a
  PARTITION  CPUS            GPUS         NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER
  cpu*       512/1024 (50%)  -            16     6      4      5     0     1        0         0
  gpu        192/256 (75%)   24/32 (75%)  4      2      2      0     0     0        0         0
```

Columns:

- **`PARTITION`**. Partition name, with a trailing `*` on the cluster's default partition.
- **`CPUS`**. Allocated CPUs over total CPUs across unique nodes in the partition, with rounded utilization percentage, or `(-)` if total CPUs is zero.
- **`GPUS`**. Allocated GPUs over total GPUs parsed from node `Gres` and `GresUsed` fields with rounded utilization percentage, or `-` when the partition has no configured GPUs.
- **`NODES`**. Total unique compute nodes in the partition.
- **`ALLOC`**. Nodes in the `allocated` node state with all CPUs assigned.
- **`MIXED`**. Nodes in the `mixed` node state with some CPUs assigned and others free.
- **`IDLE`**. Nodes in the `idle` node state with no CPUs assigned.
- **`DOWN`**. Nodes in `down`, `fail`, or `failing` states.
- **`DRAINED`**. Offline nodes with no running jobs.
- **`DRAINING`**. Nodes finishing running jobs before going offline.
- **`OTHER`**. Nodes in any other Slurm state.

When a cluster reports no partitions, `status` prints `  (no partitions)`.

### `queue`

Shows running and pending job counts and the top pending reasons per partition from `squeue -r`, which expands job arrays into individual tasks.

```text
$ sglance queue
cluster-a
  PARTITION  RUNNING  PENDING  TOP REASONS
  cpu        14       5        Resources (3), Priority (2)
  gpu        8        12       Resources (9), QOSMaxGpuPerUserLimit (3)
```

Columns:

- **`PARTITION`**. Partition name, using the first partition when a pending job lists multiple comma-separated partitions.
- **`RUNNING`**. Jobs or expanded job array tasks in `R` or `CF` state.
- **`PENDING`**. Jobs or expanded job array tasks in `PD`, `RQ`, `RH`, `RD`, or `SE` state.
- **`TOP REASONS`**. Up to five most common pending reasons formatted as `Reason (count)`, sorted by descending count and then alphabetically, or `-` when `PENDING` is `0`.

When no running or pending jobs exist on a cluster, `queue` prints `  0 running, 0 pending`.

### `wait`

Shows historical wait-time percentiles from `sacct` and the median and oldest ages of contention-blocked eligible pending jobs from `squeue`. A few stuck jobs can skew `OLDEST PENDING`, while `MEDIAN PENDING` shows the typical current wait.

Option:

- **`--days N`**. Lookback window in days, integer `>= 1`, default `7`.

```text
$ sglance wait --days 7
cluster-a
  PARTITION  N   P50     P90     MEDIAN PENDING  OLDEST PENDING
  cpu        84  2m 30s  38m 0s  6m 0s           15m 0s
  gpu        29  32m 0s  4h 30m  45m 0s          2h 10m
```

Columns:

- **`PARTITION`**. Partition name without trailing `*`.
- **`N`**. Started job samples in the lookback window. Wait time equals `Start - Eligible`. Each job array counts once using its earliest started task, and each heterogeneous job counts once using its earliest started component. Job steps, requeued records, and jobs cancelled before receiving an allocation are excluded.
- **`P50`**. Median wait time across the `N` samples, or `-` when `N` is `0`.
- **`P90`**. 90th-percentile wait time across the `N` samples, or `-` when `N` is `0`.
- **`MEDIAN PENDING`**. Median elapsed time since eligible time across jobs and expanded job array tasks in state `PD` whose pending reason is `Resources`, `Priority`, `None`, `AssocGrp*`, or `QOSGrp*`, interpolated at `0.5 * (n - 1)` like `P50`, or `-` when none match. Multi-partition pending jobs count in each listed partition. Emitted as `"median_pending_seconds"` in `--json`, or `null` when none match.
- **`OLDEST PENDING`**. Elapsed time since eligible time for the oldest job in that same pending set, or `-` when none match. Emitted as `"oldest_pending_seconds"` in `--json`, or `null` when none match.

When neither started samples nor eligible contention-blocked pending jobs exist, `wait` prints `  (no wait data)`.

When `sacct` fails on a cluster, such as when Slurm accounting storage is disabled, `wait` still runs the `squeue` pending query and reports per-partition `MEDIAN PENDING` and `OLDEST PENDING` with `N` set to `0` and `P50` and `P90` unset. Table mode prints `  history unavailable: <sacct error>` under the cluster header, and `--json` sets `"history_error"` on the cluster object while keeping `"error": null`. Results with only `"history_error"` still use the five-minute cache, whereas a cluster where `squeue` also fails reports `"error"` as usual and is not cached.

### `mine`

Shows active and pending jobs from `squeue` with scheduler backfill estimated start times. By default `mine` passes `squeue --me`, which matches your Slurm user on each cluster and requires Slurm 20.11 or newer; older clusters report `squeue does not support --me (needs Slurm 20.11 or newer)`.

Option:

- **`--account NAME`**. Pass `squeue -A NAME` instead of `--me` to list all active and pending jobs charged to the named Slurm account. `NAME` follows the same safe-token rules as `config.toml` identifiers.

```text
$ sglance mine
cluster-a
  JOBID       NAME       USER   ACCOUNT  PARTITION  STATE  CPUS  GPUS  NODES  ELAPSED  LIMIT    REASON     ESTIMATED START
  4102        train_run  alice  lab-ml   gpu        R      32    4     1      1:25:20  8:00:00  -          -
  4108_[1-4]  eval_grid  alice  lab-ml   gpu        PD     16    2     1      0:00     4:00:00  Resources  2026-11-12T22:30:00Z
```

Columns:

- **`JOBID`**. Slurm `JobArrayID`, keeping pending job arrays compact such as `4108_[1-4]`.
- **`NAME`**. Job name, or `-` if empty.
- **`USER`**. Slurm user owning the job.
- **`ACCOUNT`**. Slurm account charged for the job.
- **`PARTITION`**. First listed partition name without trailing `*`.
- **`STATE`**. Compact Slurm state code such as `R`, `PD`, or `CG`.
- **`CPUS`**. Allocated or requested CPUs, or `-` if unavailable.
- **`GPUS`**. Total GPUs resolved in order from `tres-alloc`, `tres-per-job`, `tres-per-node` multiplied by `NODES`, or `tres-per-task` multiplied by task count, `0` when none are requested, or `-` if undetermined.
- **`NODES`**. Node count, or `-` if unavailable.
- **`ELAPSED`**. Elapsed runtime as `M:SS`, `H:MM:SS`, or `D-HH:MM:SS`, or `-` if unavailable.
- **`LIMIT`**. Wall-clock time limit, or `-` when `UNLIMITED` or `Partition_Limit`.
- **`REASON`**. Pending reason for pending states, or `-` for active jobs.
- **`ESTIMATED START`**. Backfill forecast in UTC ISO-8601 format `YYYY-MM-DDTHH:MM:SSZ` for pending jobs with a future `StartTime`, or `-` when unavailable or already running.

When no matching jobs exist on a cluster, `mine` prints `  (no jobs)`.

## Always-on small machine setup

On an always-on Linux box such as a Raspberry Pi where interactive users or local automation invoke `sglance`:

1. Install `pipx` and `sglance`:

```sh
sudo apt update && sudo apt install -y pipx openssh-client
pipx install "git+https://github.com/tvh0021/design#subdirectory=cli/sglance"
```

2. Symlink the binary into `/usr/local/bin/sglance` so processes that do not add `~/.local/bin` to `PATH` can run it directly:

```sh
sudo ln -sf "$HOME/.local/bin/sglance" /usr/local/bin/sglance
```

3. Generate a dedicated SSH key for cluster access and add `~/.ssh/id_ed25519_slurm.pub` to `~/.ssh/authorized_keys` on each cluster login node:

```sh
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_slurm -C "slurm-readonly"
```

4. Configure `Host` aliases in `~/.ssh/config` pointing to that key:

```sshconfig
Host cluster-a
    HostName login.cluster-a.example.org
    User alice
    IdentityFile ~/.ssh/id_ed25519_slurm
    IdentitiesOnly yes
```

5. Verify non-interactive SSH access for every configured remote target before using `sglance`:

```sh
ssh cluster-a true
```

Once `ssh cluster-a true` exits `0` without prompts, add the matching `[[clusters]]` entry to `~/.config/sglance/config.toml`.

## Caveats

- **OpenSSH older than 7.6**. `sglance` passes `-o RemoteCommand=none` to `ssh`. OpenSSH clients older than 7.6 do not recognize `RemoteCommand`, exit with code `255`, and cause all remote cluster entries to report `unreachable (ssh: ...)`. Upgrade the client's OpenSSH to 7.6 or newer.
- **Non-interactive `PATH` on login nodes**. Non-interactive `ssh` commands do not start a login shell and do not run `/etc/profile`, `~/.bash_profile`, or `module load slurm`. Clusters that do not put `sinfo`, `squeue`, `sacct`, and `scontrol` on the default `PATH` fail unless those binaries are exposed via a default system path or `~/.ssh/environment` when `PermitUserEnvironment` is enabled in `sshd_config`.
- **Clusters requiring Duo or password authentication**. `BatchMode=yes` disables interactive password and multi-factor prompts. For clusters that require Duo or password login, configure `ControlMaster auto`, `ControlPath`, and `ControlPersist` in `~/.ssh/config` and complete one manual `ssh <host>` login first so `sglance` reuses the open multiplexed connection.
- **Large `wait --days` windows on busy clusters**. Every command has a fixed 25-second timeout. On busy clusters with large `slurmdbd` tables, `sglance wait --days` above roughly `14` can exceed 25 seconds on `sacct` and report `unreachable (timed out after 25s)`.
- **Hidden jobs under `PrivateData`**. When a cluster hides other users' jobs via Slurm `PrivateData` settings containing `jobs` or `all`, or when returned records for `queue`, `wait`, or `mine --account` contain only one distinct Slurm user, `sglance` labels the cluster header with `[user-scoped]` and sets `"user_scoped": true` in JSON so user-scoped results are not mistaken for whole-cluster totals. `status` queries `sinfo` node state and always sets `"user_scoped": false`.

## JSON schema policy

Every `--json` response emits a top-level object with `"schema_version": 1`, `"command"`, and `"clusters"`. `wait` also includes top-level `"days"`, and `mine` includes top-level `"account"`.

Each element of `"clusters"` shares `"cluster"`, `"fetched_at"`, `"stale"`, `"user_scoped"`, and `"error"`, plus subcommand-specific fields:

- **`status`**. `"partitions"` array of objects with `"partition"`, `"default"`, `"cpus_alloc"`, `"cpus_total"`, `"cpus_utilization"`, `"gpus_alloc"`, `"gpus_total"`, `"gpus_utilization"`, `"nodes_alloc"`, `"nodes_mixed"`, `"nodes_idle"`, `"nodes_down"`, `"nodes_drained"`, `"nodes_draining"`, `"nodes_other"`, and `"nodes_total"`.
- **`queue`**. `"partitions"` array of objects with `"partition"`, `"running_jobs"`, `"pending_jobs"`, and `"top_reasons"`, where each entry in `"top_reasons"` has `"reason"` and `"count"`.
- **`wait`**. `"history_error"`, which is a string or `null`, and `"partitions"` array of objects with `"partition"`, `"n"`, `"p50_seconds"`, `"p90_seconds"`, `"median_pending_seconds"`, and `"oldest_pending_seconds"`.
- **`mine`**. `"slurm_user"`, `"account_filter"`, and `"jobs"` array of objects with `"job_id"`, `"name"`, `"user"`, `"account"`, `"partition"`, `"state"`, `"cpus"`, `"gpus"`, `"nodes"`, `"elapsed_seconds"`, `"time_limit_seconds"`, `"reason"`, and `"estimated_start"`.

Compatibility rules:

- Additive changes, such as new fields on top-level or nested objects, may ship without incrementing `schema_version`. Consumers should ignore unknown fields.
- Breaking changes, including renaming a field, removing a field, changing a field's type, or changing a field's meaning, increment `schema_version`.

## Privacy

This repository contains no real cluster names, login hostnames, usernames, account names, email addresses, or private IP addresses. Documentation and tests use synthetic names such as `cluster-a` and `login.cluster-a.example.org` and RFC 5737 documentation IPv4 ranges.

Two local checks prevent accidental leaks before commit:

- `scripts/leak_check.py` scans files under `cli/sglance` for case-insensitive whole-word matches against tokens in `cli/sglance/.leak-denylist`, which is gitignored, or the `SGLANCE_LEAK_DENYLIST` environment variable. It also flags email addresses and non-RFC-5737, non-loopback IPv4 addresses, redacting matched text in diagnostic output.
- `hooks/pre-commit` runs `scripts/leak_check.py --require-denylist --staged` whenever files under `cli/sglance` are staged in git. Enable it from the repository root with `git config core.hooksPath cli/sglance/hooks` or symlink it into `.git/hooks/pre-commit`.

## Development

Run the unit test suite from `cli/sglance`:

```sh
cd cli/sglance
python3 -m unittest discover -s tests -t .
```

Populate `cli/sglance/.leak-denylist` with one private identifier per line, then run the leak check:

```sh
python3 cli/sglance/scripts/leak_check.py --require-denylist
```
