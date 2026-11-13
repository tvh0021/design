from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sglance import TransportError
from tests.fakes import FakeRunner, run_cli, write_config

MINE_FIELDS = (
    "JobArrayID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
    "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,NumTasks:0|,"
    "tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,tres-per-task:0|,"
    "StartTime:0|,Reason:0|,Name:0|"
)


class PrivateDataScopingCliTests(unittest.TestCase):
    def test_user_scoped_rule_and_scontrol_skip_when_single_user(self) -> None:
        # Multi-cluster queue check: PrivateData jobs/all -> True, >=2 users + failed/usage scontrol -> False.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\n'
                '[[clusters]]\nname = "cluster-b"\nslurm_cluster = "prod"\n'
                '[[clusters]]\nname = "cluster-c"\n'
                '[[clusters]]\nname = "cluster-d"\n',
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData = jobs,usage\n",
                    ("cluster-b", "squeue"): "CLUSTER: prod\n1|alice|gpu|R|None|\n2|bob|gpu|PD|Resources|\n",
                    ("cluster-b", "scontrol"): "CLUSTER: prod\nPrivateData = all\n",
                    ("cluster-c", "squeue"): "3|alice|cpu|R|None|\n4|bob|cpu|CG|None|\n",
                    ("cluster-c", "scontrol"): TransportError("timed out after 25s"),
                    ("cluster-d", "squeue"): "",
                    ("cluster-d", "scontrol"): "PrivateData = usage,accounts\n",
                }
            )
            rc, out, err = run_cli(["queue", "--json"], root=root, runner=runner)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            scoped = {c["cluster"]: c["user_scoped"] for c in json.loads(out)["clusters"]}
            self.assertEqual(
                scoped,
                {
                    "cluster-a": True,
                    "cluster-b": True,
                    "cluster-c": False,
                    "cluster-d": False,
                },
            )
            rc_tbl, out_tbl, _ = run_cli(["queue", "--cluster", "cluster-a"], root=root, runner=runner)
            self.assertEqual(rc_tbl, 0)
            self.assertEqual(out_tbl, "cluster-a [user-scoped]\n  0 running, 0 pending\n")

        # Single-user responses: no ("cluster-a", "scontrol") entry on FakeRunner proves scontrol is skipped!
        # Also verifies status and plain mine always hardcode user_scoped=False.
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
        wait_squeue_argv = (
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
        one_mine_row = (
            "1001|alice|proj-1|cpu|R|10:00|01:00:00|1|16|1|"
            "cpu=16|N/A|N/A|N/A|1794513000|None|job_a|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            single_user_runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n",
                    ("cluster-a", *mine_me_argv): one_mine_row,
                    ("cluster-a", *mine_acct_argv): one_mine_row,
                    ("cluster-a", *queue_argv): "2001|alice|cpu|R|None|\n",
                    ("cluster-a", "sacct"): "3001|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n",
                    ("cluster-a", *wait_squeue_argv): "",
                }
            )
            cases = (
                (["status", "--json"], False),
                (["mine", "--json"], False),
                (["mine", "--account", "proj-1", "--json"], True),
                (["queue", "--json"], True),
                (["wait", "--json"], True),
            )
            for argv, expected_scoped in cases:
                rc, out, err = run_cli(argv, root=root, runner=single_user_runner)
                self.assertEqual(rc, 0)
                self.assertEqual(err, "")
                self.assertEqual(json.loads(out)["clusters"][0]["user_scoped"], expected_scoped)
            self.assertEqual(
                [call[3][2] for call in single_user_runner.calls],
                ["sinfo", "squeue", "squeue", "squeue", "sacct", "squeue"],
            )

    def test_private_data_parsing_table(self) -> None:
        two_users = "1001|alice|cpu|R|None|\n1002|bob|cpu|PD|Resources|\n"
        cases: tuple[tuple[str, str, bool], ...] = (
            ("jobs-and-usage", "PrivateData = jobs,usage\n", True),
            ("all", "PrivateData = all\n", True),
            ("case-no-spaces", "PrivateData=Jobs\n", True),
            ("banner-with-jobs", "CLUSTER: prod\nPrivateData = jobs\n", True),
            ("none", "PrivateData = none\n", False),
            ("null", "PrivateData = (null)\n", False),
            ("other-flags", "PrivateData = usage,accounts\n", False),
            ("substring-trap", "PrivateData = nojobs,alljobs\n", False),
            ("wrong-key", "PrivateDataExtra = jobs\n", False),
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            for label, scontrol_out, expected in cases:
                with self.subTest(case=label):
                    runner = FakeRunner(
                        {
                            ("cluster-a", "squeue"): two_users,
                            ("cluster-a", "scontrol"): scontrol_out,
                        }
                    )
                    rc, out, err = run_cli(["queue", "--json"], root=root, runner=runner)
                    self.assertEqual(rc, 0)
                    self.assertEqual(err, "")
                    self.assertEqual(json.loads(out)["clusters"][0]["user_scoped"], expected)


if __name__ == "__main__":
    unittest.main()
