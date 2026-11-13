from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.fakes import QUEUE_ARGV, FakeRunner, run_cli, write_config

SCONTROL_ARGV = ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "show", "config"]


class QueueCliTests(unittest.TestCase):
    def test_queue_exact_argv_running_pending_states_and_cluster_banner(self) -> None:
        squeue_a = (
            "1001|alice|cpu|R|None|\n"
            "1002|bob|cpu|CF|None|\n"
            "1003|alice|cpu|CG|None|\n"
            "1004|bob|cpu|CD|None|\n"
            "1005|alice|cpu,gpu|PD|Resources|\n"
            "1006|bob|gpu|PD|Priority|\n"
            "1007|alice|gpu|RQ|Priority|\n"
            "1008|bob|gpu|RH|Resources|\n"
            "1009|alice|gpu|RD|Resources|\n"
            "1010|bob|gpu|SE|   |\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\nslurm_cluster = "prod"\n',
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): squeue_a,
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-b", "squeue"): "CLUSTER: prod\n\n",
                    ("cluster-b", "scontrol"): "CLUSTER: prod\nPrivateData = none\n",
                }
            )
            rc, out, err = run_cli(["queue", "--json"], root=root, runner=runner)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertEqual(
                sorted(runner.calls, key=lambda c: c[0]),
                [
                    ("cluster-a", "ssh", "login.example.edu", list(QUEUE_ARGV)),
                    ("cluster-a", "ssh", "login.example.edu", SCONTROL_ARGV),
                    (
                        "cluster-b",
                        "ssh",
                        "cluster-b",
                        [
                            "env",
                            "SLURM_TIME_FORMAT=%s",
                            "squeue",
                            "-M",
                            "prod",
                            "-h",
                            "-r",
                            "-O",
                            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|",
                        ],
                    ),
                    (
                        "cluster-b",
                        "ssh",
                        "cluster-b",
                        ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "-M", "prod", "show", "config"],
                    ),
                ],
            )
            self.assertEqual(
                json.loads(out),
                {
                    "schema_version": 1,
                    "command": "queue",
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "running_jobs": 2,
                                    "pending_jobs": 1,
                                    "top_reasons": [{"reason": "Resources", "count": 1}],
                                },
                                {
                                    "partition": "gpu",
                                    "running_jobs": 0,
                                    "pending_jobs": 5,
                                    "top_reasons": [
                                        {"reason": "Priority", "count": 2},
                                        {"reason": "Resources", "count": 2},
                                        {"reason": "None", "count": 1},
                                    ],
                                },
                            ],
                        },
                        {
                            "cluster": "cluster-b",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "partitions": [],
                        },
                    ],
                },
            )

            rc_empty, out_empty, _ = run_cli(
                ["queue", "--cluster", "cluster-b"],
                root=root,
                runner=runner,
            )
            self.assertEqual(rc_empty, 0)
            self.assertEqual(out_empty, "cluster-b\n  0 running, 0 pending\n")

    def test_queue_groups_reasons_before_first_comma_caps_at_five_and_allows_pipe(self) -> None:
        squeue_out = (
            "2001|alice|gpu|PD|ReqNodeNotAvail, UnavailableNodes:node01|\n"
            "2002|bob|gpu|PD|ReqNodeNotAvail, Reserved for maintenance|\n"
            "2003|alice|gpu|PD|Resources|\n"
            "2004|bob|gpu|PD|Resources|\n"
            "2005|alice|gpu|PD|AssocGrpCpuLimit|\n"
            "2006|bob|gpu|PD|Dependency|\n"
            "2007|alice|gpu|PD|JobHeldAdmin|hold|note|\n"
            "2008|bob|gpu|PD|ZLastDropped|\n"
            "2009|alice|debug|R|None|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): squeue_out,
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-b", "squeue"): "broken_row_without_trailing_pipe\n",
                }
            )
            rc, out, err = run_cli(["queue"], root=root, runner=runner)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertEqual(
                out,
                "cluster-a\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  gpu        0        8        ReqNodeNotAvail (2), Resources (2), AssocGrpCpuLimit (1), Dependency (1), JobHeldAdmin|hold|note (1)\n"
                "  debug      1        0        -\n"
                "\n"
                "cluster-b: malformed squeue output: broken_row_without_trailing_pipe\n",
            )


if __name__ == "__main__":
    unittest.main()
