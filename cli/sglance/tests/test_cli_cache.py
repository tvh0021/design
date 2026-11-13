from __future__ import annotations

from datetime import timedelta
try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from sglance import TransportError
from tests.fakes import FIXED_NOW, FakeRunner, make_test_env, run_cli, write_config

SINFO_INITIAL = (
    "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n"
    "cpu|node02|idle|0/64/0/64|(null)|(null)|\n"
)
SINFO_UPDATED = (
    "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n"
    "cpu|node02|allocated|64/0/0/64|(null)|(null)|\n"
)
MINE_FIELDS = (
    "JobArrayID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
    "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,NumTasks:0|,"
    "tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,tres-per-task:0|,"
    "StartTime:0|,Reason:0|,Name:0|"
)


class CacheCliTests(unittest.TestCase):
    def test_cache_hit_within_5m_expiry_refresh_bypass_and_errors_not_cached(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n',
            )
            env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): SINFO_INITIAL,
                    ("cluster-b", "sinfo"): TransportError("timed out after 25s"),
                }
            )

            # t = 0s: cluster-a cached (stale=False), cluster-b errors and is NOT cached.
            rc1, out1, err1 = run_cli(["status", "--json"], root=root, env=env, runner=runner)
            self.assertEqual((rc1, err1, len(runner.calls)), (0, "", 2))
            c1 = json.loads(out1)["clusters"]
            self.assertFalse(c1[0]["stale"])
            self.assertEqual(c1[0]["partitions"][0]["cpus_alloc"], 64)
            self.assertEqual(c1[1]["error"], "unreachable (timed out after 25s)")

            # Change responses: at t = 299s, cluster-a hits cache; cluster-b retries live and recovers.
            runner.set_response(("cluster-a", "sinfo"), SINFO_UPDATED)
            runner.set_response(("cluster-b", "sinfo"), SINFO_INITIAL)
            rc2, out2, _ = run_cli(
                ["status"],
                root=root,
                env=env,
                runner=runner,
                now=FIXED_NOW + timedelta(seconds=299),
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(runner.calls[-1][0], "cluster-b")
            self.assertIn("cluster-a [cached 299s ago]\n", out2)
            self.assertIn("64/128 (50%)", out2)

            # At t = 299s, --refresh bypasses cache and overwrites snapshot on cluster-a.
            rc3, out3, _ = run_cli(
                ["status", "--refresh", "--cluster", "cluster-a", "--json"],
                root=root,
                env=env,
                runner=runner,
                now=FIXED_NOW + timedelta(seconds=299),
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(len(runner.calls), 4)
            c3 = json.loads(out3)["clusters"][0]
            self.assertFalse(c3["stale"])
            self.assertEqual(c3["partitions"][0]["cpus_alloc"], 128)

            # At t = 299 + 301 = 600s (> 5m after refresh), snapshot expires and refetches live.
            runner.set_response(("cluster-a", "sinfo"), SINFO_INITIAL)
            rc4, out4, _ = run_cli(
                ["status", "--cluster", "cluster-a", "--json"],
                root=root,
                env=env,
                runner=runner,
                now=FIXED_NOW + timedelta(seconds=600),
            )
            self.assertEqual(rc4, 0)
            self.assertEqual(len(runner.calls), 5)
            c4 = json.loads(out4)["clusters"][0]
            self.assertFalse(c4["stale"])
            self.assertEqual(c4["partitions"][0]["cpus_alloc"], 64)

    def test_cache_keys_isolated_across_commands_and_options_and_permissions_0700_0600(
        self,
    ) -> None:
        queue_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-h",
            "-r",
            "-O",
            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|",
        )
        mine_me_argv = ("env", "SLURM_TIME_FORMAT=%s", "squeue", "-h", "--me", "-O", MINE_FIELDS)
        mine_acct_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-h",
            "-A",
            "proj-1",
            "-O",
            MINE_FIELDS,
        )
        wait_7d_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "sacct",
            "-X",
            "-P",
            "-n",
            "-a",
            "-S",
            "now-7days",
            "-E",
            "now",
            "--state=CD,F,TO,CA,OOM,NF,PR,R",
            "--format=JobID,User,Partition,State,Eligible,Start,AllocTRES",
        )
        wait_14d_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "sacct",
            "-X",
            "-P",
            "-n",
            "-a",
            "-S",
            "now-14days",
            "-E",
            "now",
            "--state=CD,F,TO,CA,OOM,NF,PR,R",
            "--format=JobID,User,Partition,State,Eligible,Start,AllocTRES",
        )
        wait_sq_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-h",
            "-r",
            "-t",
            "PD",
            "-O",
            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|,EligibleTime:0|",
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): SINFO_INITIAL,
                    ("cluster-a", *queue_argv): "1|alice|cpu|R|None|\n2|bob|cpu|PD|Resources|\n",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-a", *mine_me_argv): (
                        "10|alice|proj-1|cpu|R|10:00|01:00:00|1|16|1|cpu=16|N/A|N/A|N/A|1794513000|None|j1|\n"
                    ),
                    ("cluster-a", *mine_acct_argv): (
                        "10|alice|proj-1|cpu|R|10:00|01:00:00|1|16|1|cpu=16|N/A|N/A|N/A|1794513000|None|j1|\n"
                        "11|bob|proj-1|cpu|PD|0:00|01:00:00|1|8|1|cpu=8|N/A|N/A|N/A|N/A|Priority|j2|\n"
                    ),
                    ("cluster-a", *wait_7d_argv): "20|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n",
                    ("cluster-a", *wait_14d_argv): (
                        "20|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n"
                        "21|bob|cpu|COMPLETED|1793600000|1793600180|cpu=8\n"
                    ),
                    ("cluster-a", *wait_sq_argv): "",
                }
            )
            distinct_invocations = (
                ["status", "--json"],
                ["queue", "--json"],
                ["mine", "--json"],
                ["mine", "--account", "proj-1", "--json"],
                ["wait", "--days", "7", "--json"],
                ["wait", "--days", "14", "--json"],
            )
            for idx, argv in enumerate(distinct_invocations):
                rc, out, _ = run_cli(
                    argv,
                    root=root,
                    env=env,
                    runner=runner,
                    now=FIXED_NOW + timedelta(seconds=idx),
                )
                self.assertEqual(rc, 0)
                self.assertFalse(json.loads(out)["clusters"][0]["stale"])

            calls_after_seed = len(runner.calls)
            for idx, argv in enumerate(distinct_invocations):
                rc, out, _ = run_cli(
                    argv,
                    root=root,
                    env=env,
                    runner=runner,
                    now=FIXED_NOW + timedelta(seconds=30 + idx),
                )
                self.assertEqual(rc, 0)
                self.assertTrue(json.loads(out)["clusters"][0]["stale"])
            self.assertEqual(len(runner.calls), calls_after_seed)

            if os.name == "posix":
                cache_dir = Path(env["XDG_CACHE_HOME"]) / "sglance"
                self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
                entries = list(cache_dir.iterdir())
                self.assertEqual(len([p for p in entries if p.suffix == ".json"]), 6)
                for path in entries:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

                # Widened permissions are restored to 0o700 / 0o600 on refresh.
                cache_dir.chmod(0o755)
                for path in entries:
                    path.chmod(0o644)
                run_cli(
                    ["status", "--refresh", "--json"],
                    root=root,
                    env=env,
                    runner=runner,
                    now=FIXED_NOW + timedelta(seconds=60),
                )
                self.assertEqual(stat.S_IMODE(cache_dir.stat().st_mode), 0o700)
                for path in cache_dir.iterdir():
                    if path.stem == entries[0].stem or path.suffix == ".json":
                        if path.stat().st_mtime >= (FIXED_NOW + timedelta(seconds=60)).timestamp():
                            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipIf(fcntl is None, "POSIX flock is required")
    def test_old_snapshot_pruning_respects_held_locks_and_symlinks(self) -> None:
        assert fcntl is not None
        mine_me_argv = ("env", "SLURM_TIME_FORMAT=%s", "squeue", "-h", "--me", "-O", MINE_FIELDS)
        queue_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-h",
            "-r",
            "-O",
            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|",
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): SINFO_INITIAL,
                    ("cluster-a", *queue_argv): "1|alice|cpu|R|None|\n",
                    ("cluster-a", *mine_me_argv): (
                        "10|alice|proj-1|cpu|R|10:00|01:00:00|1|16|1|cpu=16|N/A|N/A|N/A|1794513000|None|j1|\n"
                    ),
                }
            )

            # Seed status (will be unlocked) and queue (will hold flock) at t = 0.
            run_cli(["status", "--json"], root=root, env=env, runner=runner, now=FIXED_NOW)
            cache_dir = Path(env["XDG_CACHE_HOME"]) / "sglance"
            status_json = next(p for p in cache_dir.iterdir() if p.suffix == ".json")
            status_lock = status_json.with_suffix(".lock")
            old_text = status_json.read_text(encoding="utf-8")

            run_cli(["queue", "--json"], root=root, env=env, runner=runner, now=FIXED_NOW)
            queue_json = next(
                p for p in cache_dir.iterdir() if p.suffix == ".json" and p != status_json
            )
            queue_lock = queue_json.with_suffix(".lock")

            # Add a <64-hex>.json symlink to an outside file (+ unlocked .lock), missing-lock json, and foreign file.
            outside_target = root / "outside_snapshot.json"
            outside_target.write_text(old_text, encoding="utf-8")
            symlink_stem = "d" * 64
            (cache_dir / f"{symlink_stem}.lock").write_text("", encoding="utf-8")
            symlink_json = cache_dir / f"{symlink_stem}.json"
            symlink_json.symlink_to(outside_target)
            missing_lock_json = cache_dir / ("e" * 64 + ".json")
            missing_lock_json.write_text(old_text, encoding="utf-8")
            foreign_file = cache_dir / "notes.txt"
            foreign_file.write_text("keep\n", encoding="utf-8")

            t_over_1d = FIXED_NOW + timedelta(days=1, seconds=1)
            held_fd = os.open(queue_lock, os.O_RDWR)
            try:
                fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                rc, _, err = run_cli(
                    ["mine", "--json"],
                    root=root,
                    env=env,
                    runner=runner,
                    now=t_over_1d,
                )
                self.assertEqual((rc, err), (0, ""))
                self.assertFalse(status_json.exists())
                self.assertTrue(status_lock.is_file())
                self.assertTrue(queue_json.is_file())
                self.assertTrue(symlink_json.is_symlink())
                self.assertTrue(outside_target.is_file())
                self.assertTrue(missing_lock_json.is_file())
                self.assertTrue(foreign_file.is_file())
            finally:
                fcntl.flock(held_fd, fcntl.LOCK_UN)
                os.close(held_fd)

            # Once queue_lock is released, the next cache write prunes queue_json.
            run_cli(
                ["status", "--refresh", "--json"],
                root=root,
                env=env,
                runner=runner,
                now=t_over_1d + timedelta(seconds=5),
            )
            self.assertFalse(queue_json.exists())
            self.assertTrue(queue_lock.is_file())


if __name__ == "__main__":
    unittest.main()
