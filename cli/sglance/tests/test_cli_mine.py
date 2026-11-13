from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sglance import CommandResult
from tests.fakes import FakeRunner, run_cli, write_config

MINE_SQUEUE_FIELDS = (
    "JobArrayID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
    "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,NumTasks:0|,"
    "tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,tres-per-task:0|,"
    "StartTime:0|,Reason:0|,Name:0|"
)


class MineCliTests(unittest.TestCase):
    def test_mine_exact_argv_me_vs_account_validation_and_slurm_20_11_message(self) -> None:
        me_argv = ["env", "SLURM_TIME_FORMAT=%s", "squeue", "-h", "--me", "-O", MINE_SQUEUE_FIELDS]
        acct_argv = [
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-M",
            "prod",
            "-h",
            "-A",
            "proj-1",
            "-O",
            MINE_SQUEUE_FIELDS,
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\nslurm_cluster = "prod"\n',
            )
            runner = FakeRunner(
                {
                    ("cluster-a", *me_argv): (
                        "1001|alice|proj-1|gpu|R|45:30|04:00:00|2|64|8|"
                        "cpu=64,gres/gpu=8|N/A|N/A|N/A|1794510870|None|train|\n"
                    ),
                    ("cluster-b", "squeue"): CommandResult(
                        1,
                        "",
                        "squeue: unrecognized option '--me'\n"
                        'Try "squeue --help" for more information\n',
                    ),
                }
            )
            rc, out, err = run_cli(["mine", "--json"], root=root, runner=runner)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertTrue(MINE_SQUEUE_FIELDS.startswith("JobArrayID:0|,"))
            self.assertEqual(
                [c for c in runner.calls if c[0] == "cluster-a"],
                [("cluster-a", "ssh", "login.example.edu", me_argv)],
            )
            clusters = json.loads(out)["clusters"]
            self.assertEqual(clusters[0]["slurm_user"], "alice")
            self.assertEqual(clusters[0]["jobs"][0]["job_id"], "1001")
            self.assertEqual(
                clusters[1]["error"],
                "squeue does not support --me (needs Slurm 20.11 or newer)",
            )

            # --account uses -A <account> (never --me) and normal non-zero squeue error uses last line.
            runner_acct = FakeRunner(
                {
                    ("cluster-a", "squeue"): CommandResult(
                        1,
                        "",
                        "squeue: error: socket timed out\n"
                        "slurm_load_jobs error: Unable to contact controller\n",
                    ),
                    ("cluster-b", *acct_argv): (
                        "2001|alice|proj-1|cpu|R|05:00|01:00:00|1|8|1|"
                        "cpu=8|N/A|N/A|N/A|1794513300|None|fit|\n"
                    ),
                }
            )
            rc2, out2, err2 = run_cli(
                ["mine", "--account", "proj-1", "--json"],
                root=root,
                runner=runner_acct,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2, "")
            self.assertEqual(
                [c for c in runner_acct.calls if c[0] == "cluster-b"],
                [("cluster-b", "ssh", "cluster-b", acct_argv)],
            )
            p2 = json.loads(out2)
            self.assertEqual(p2["account"], "proj-1")
            self.assertEqual(
                p2["clusters"][0]["error"],
                "squeue exited 1: slurm_load_jobs error: Unable to contact controller",
            )
            self.assertIsNone(p2["clusters"][1]["slurm_user"])
            self.assertEqual(p2["clusters"][1]["account_filter"], "proj-1")

            for bad in ("", "-proj", "proj,1", "proj 1", "proj\x07"):
                with self.subTest(account=bad):
                    empty_runner = FakeRunner()
                    rc_bad, _, err_bad = run_cli(
                        ["mine", "--account", bad],
                        root=root,
                        runner=empty_runner,
                    )
                    self.assertEqual(rc_bad, 2)
                    self.assertNotEqual(err_bad.strip(), "")
                    self.assertEqual(empty_runner.calls, [])

    def test_mine_pipe_in_name_unparseable_numbers_and_past_start_suppression(self) -> None:
        # FIXED_NOW = 2026-11-12T20:00:00Z (1794513600).
        squeue_rows = (
            "CLUSTER: prod\n"
            # Running job: pipe in Name preserved; future StartTime ignored -> estimated_start None.
            "123_4|alice|proj-1|gpu|R|45:30|04:00:00|2|64|8|"
            "cpu=64,gres/gpu=8|N/A|N/A|N/A|1794517200|None|a|b|\n"
            # Pending array range: StartTime 1794513599 (1s in past) -> estimated_start None.
            "123_[5-20%4]|alice|proj-1|gpu*|PD|0:00|UNLIMITED|1|16|2|"
            "cpu=16|gres/gpu:2|N/A|N/A|1794513599|Priority|past_start|\n"
            # Pending het job: StartTime 1794517200 (+1h) -> ISO UTC; blank reason -> "None".
            "126+0|alice|proj-1|cpu|PD|0:00|1-00:00:00|1|8|1|"
            "cpu=8|N/A|N/A|N/A|1794517200||future_start|\n"
            # Unparseable numbers -> null in JSON / "-" in table while keeping the row.
            "2001|alice|proj-1|cpu|R|INVALID|INVALID|2-4|N/A|N/A|"
            "cpu=8,gres/gpu=INVALID|N/A|N/A|N/A|N/A|None|bad_nums|\n"
            # Terminal state row skipped.
            "9999|alice|proj-1|cpu|CD|10:00|01:00:00|1|8|1|cpu=8|N/A|N/A|N/A|N/A|None|done|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = "prod"\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): squeue_rows,
                    ("cluster-b", "squeue"): "1001|too_few_fields|\n",
                }
            )
            rc, out, err = run_cli(["mine", "--json"], root=root, runner=runner)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            clusters = json.loads(out)["clusters"]
            self.assertEqual(
                clusters[0]["jobs"],
                [
                    {
                        "job_id": "123_4",
                        "name": "a|b",
                        "user": "alice",
                        "account": "proj-1",
                        "partition": "gpu",
                        "state": "R",
                        "cpus": 64,
                        "gpus": 8,
                        "nodes": 2,
                        "elapsed_seconds": 2730,
                        "time_limit_seconds": 14400,
                        "reason": None,
                        "estimated_start": None,
                    },
                    {
                        "job_id": "123_[5-20%4]",
                        "name": "past_start",
                        "user": "alice",
                        "account": "proj-1",
                        "partition": "gpu",
                        "state": "PD",
                        "cpus": 16,
                        "gpus": 2,
                        "nodes": 1,
                        "elapsed_seconds": 0,
                        "time_limit_seconds": None,
                        "reason": "Priority",
                        "estimated_start": None,
                    },
                    {
                        "job_id": "126+0",
                        "name": "future_start",
                        "user": "alice",
                        "account": "proj-1",
                        "partition": "cpu",
                        "state": "PD",
                        "cpus": 8,
                        "gpus": 0,
                        "nodes": 1,
                        "elapsed_seconds": 0,
                        "time_limit_seconds": 86400,
                        "reason": "None",
                        "estimated_start": "2026-11-12T21:00:00Z",
                    },
                    {
                        "job_id": "2001",
                        "name": "bad_nums",
                        "user": "alice",
                        "account": "proj-1",
                        "partition": "cpu",
                        "state": "R",
                        "cpus": None,
                        "gpus": None,
                        "nodes": None,
                        "elapsed_seconds": None,
                        "time_limit_seconds": None,
                        "reason": None,
                        "estimated_start": None,
                    },
                ],
            )
            self.assertIn("malformed squeue output", clusters[1]["error"])

            rc_tbl, out_tbl, _ = run_cli(["mine", "--cluster", "cluster-a"], root=root, runner=runner)
            self.assertEqual(rc_tbl, 0)
            self.assertIn("123_4         a|b", out_tbl)
            self.assertIn("2001          bad_nums", out_tbl)

    def test_mine_gpu_fallback_order_and_tres_deduplication(self) -> None:
        # Order: tres-alloc -> tres-per-job -> tres-per-node * nodes -> tres-per-task * tasks -> 0.
        squeue_rows = (
            # 1. tres-alloc wins over per-node; untyped+typed in TRES not double-counted; shards ignored -> 4.
            "3001|alice|proj-1|gpu|R|10:00|01:00:00|2|32|4|"
            "cpu=32,gres/gpu=4,gres/gpu:a100=4,gres/gpumem=80G,gres/shard=8|gres/gpu:8|N/A|N/A|1794513000|None|alloc_dedup|\n"
            # 2. tres-alloc typed-only sum -> 2 + 1 = 3.
            "3002|alice|proj-1|gpu|R|05:00|01:00:00|1|24|3|"
            "cpu=24,gres/gpu:a100=2,gres/gpu:v100=1|N/A|N/A|N/A|1794513300|None|alloc_typed|\n"
            # 3. tres-per-job (2) beats tres-per-node (3*2=6) and tres-per-task (1*4=4), not multiplied by nodes -> 2.
            "3003|alice|proj-1|gpu|PD|0:00|01:00:00|2|16|4|"
            "cpu=16|gres/gpu:3|gres/gpu:a100:2|gres/gpu:1|N/A|Resources|per_job|\n"
            # 4. tres-per-node (2*3=6) beats tres-per-task (1*4=4) -> 6.
            "3004|alice|proj-1|gpu|PD|0:00|01:00:00|3|24|4|"
            "cpu=24|gres:gpu:2|N/A|gres/gpu:1|N/A|Resources|per_node|\n"
            # 5. tres-per-node with unknown nodes (2-4) -> null without falling through to tres-per-task.
            "3005|alice|proj-1|gpu|PD|0:00|01:00:00|2-4|16|4|"
            "cpu=16|gres/gpu:2|N/A|gres/gpu:1|N/A|Resources|per_node_unknown|\n"
            # 6. tres-per-task * tasks (2*4=8) -> 8.
            "3006|alice|proj-1|gpu|PD|0:00|01:00:00|2|32|4|"
            "cpu=32|N/A|N/A|gres/gpu:a100:2|N/A|Resources|per_task|\n"
            # 7. tres-per-task with unknown tasks (N/A) -> null.
            "3007|alice|proj-1|gpu|PD|0:00|01:00:00|1|16|N/A|"
            "cpu=16|N/A|N/A|gpu:1|N/A|Resources|per_task_unknown|\n"
            # 8. No GPU TRES anywhere (shards only) -> 0.
            "3008|alice|proj-1|cpu|PD|0:00|01:00:00|1|8|1|"
            "cpu=8,gres/shard=2|N/A|N/A|N/A|N/A|Priority|no_gpu|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root, '[[clusters]]\nname = "cluster-a"\nlocal = true\n')
            runner = FakeRunner({("cluster-a", "squeue"): squeue_rows})
            rc, out, err = run_cli(["mine", "--json"], root=root, runner=runner)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            got = [(j["name"], j["gpus"]) for j in json.loads(out)["clusters"][0]["jobs"]]
            self.assertEqual(
                got,
                [
                    ("alloc_dedup", 4),
                    ("alloc_typed", 3),
                    ("per_job", 2),
                    ("per_node", 6),
                    ("per_node_unknown", None),
                    ("per_task", 8),
                    ("per_task_unknown", None),
                    ("no_gpu", 0),
                ],
            )


if __name__ == "__main__":
    unittest.main()
