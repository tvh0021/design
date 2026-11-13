from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sglance import CommandResult
from tests.fakes import FakeRunner, run_cli, write_config

SACCT_FORMAT = "JobID,User,Partition,State,Eligible,Start,AllocTRES"
SQUEUE_WAIT_FIELDS = "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|,EligibleTime:0|"


class WaitCliTests(unittest.TestCase):
    def test_wait_exact_slurm_argv_percentile_interpolation_and_days_validation(self) -> None:
        # Waits on cpu: [60, 120, 300, 600] -> n=4,
        # p50 = 120 + 0.5*(300-120) = 210.0, p90 = 300 + 0.7*(600-300) = 510.0.
        sacct_a = (
            "1001|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n"
            "1002|bob|cpu|RUNNING|1794501000|1794501600|cpu=16\n"
            "1003|alice|cpu|TIMEOUT|1794502000|1794502120|cpu=8\n"
            "1004|bob|cpu|FAILED|1794503000|1794503300|cpu=4\n"
        )
        # Waits on gpu: [0, 10, 13] -> n=3, p50 = 10.0, p90 = 10 + 0.8*3 = 12.4.
        sacct_b = (
            "CLUSTER: prod\n"
            "3001|alice|gpu|COMPLETED|1794000000|1794000000|gres/gpu=1\n"
            "3002|bob|gpu|NODE_FAIL|1794001000|1794001013|gres/gpu=1\n"
            "3003|alice|gpu|CANCELLED by 1001|1794003000|1794003010|gres/gpu=1\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\nslurm_cluster = "prod"\n',
            )
            runner_a = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_a,
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            rc_a, out_a, err_a = run_cli(
                ["wait", "--cluster", "cluster-a", "--json"],
                root=root,
                runner=runner_a,
            )
            self.assertEqual((rc_a, err_a), (0, ""))
            self.assertEqual(
                runner_a.calls,
                [
                    (
                        "cluster-a",
                        "ssh",
                        "login.example.edu",
                        [
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
                            f"--format={SACCT_FORMAT}",
                        ],
                    ),
                    (
                        "cluster-a",
                        "ssh",
                        "login.example.edu",
                        ["env", "SLURM_TIME_FORMAT=%s", "squeue", "-h", "-r", "-t", "PD", "-O", SQUEUE_WAIT_FIELDS],
                    ),
                    (
                        "cluster-a",
                        "ssh",
                        "login.example.edu",
                        ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "show", "config"],
                    ),
                ],
            )
            self.assertEqual(
                json.loads(out_a)["clusters"][0]["partitions"],
                [
                    {
                        "partition": "cpu",
                        "n": 4,
                        "p50_seconds": 210.0,
                        "p90_seconds": 510.0,
                        "median_pending_seconds": None,
                        "oldest_pending_seconds": None,
                    }
                ],
            )

            runner_b = FakeRunner(
                {
                    ("cluster-b", "sacct"): sacct_b,
                    ("cluster-b", "squeue"): "",
                    ("cluster-b", "scontrol"): "PrivateData = none\n",
                }
            )
            rc_b, out_b, err_b = run_cli(
                ["wait", "--cluster", "cluster-b", "--days", "14", "--json"],
                root=root,
                runner=runner_b,
            )
            self.assertEqual((rc_b, err_b), (0, ""))
            self.assertEqual(
                runner_b.calls[0][3],
                [
                    "env",
                    "SLURM_TIME_FORMAT=%s",
                    "sacct",
                    "-M",
                    "prod",
                    "-X",
                    "-P",
                    "-n",
                    "-a",
                    "-S",
                    "now-14days",
                    "-E",
                    "now",
                    "--state=CD,F,TO,CA,OOM,NF,PR,R",
                    f"--format={SACCT_FORMAT}",
                ],
            )
            parsed_b = json.loads(out_b)
            self.assertEqual(parsed_b["days"], 14)
            self.assertEqual(
                parsed_b["clusters"][0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "n": 3,
                        "p50_seconds": 10.0,
                        "p90_seconds": 12.4,
                        "median_pending_seconds": None,
                        "oldest_pending_seconds": None,
                    }
                ],
            )

            for bad_days in ("0", "-1", "abc", "2.5"):
                with self.subTest(days=bad_days):
                    empty_runner = FakeRunner()
                    rc_bad, _, err_bad = run_cli(
                        ["wait", "--days", bad_days],
                        root=root,
                        runner=empty_runner,
                    )
                    self.assertEqual(rc_bad, 2)
                    self.assertIn("--days", err_bad)
                    self.assertEqual(empty_runner.calls, [])

    def test_wait_skips_cancelled_pending_and_empty_alloc_tres_rows(self) -> None:
        sacct_rows = (
            "4101|alice|gpu|CANCELLED by 1001|1794500000|1794500900|\n"
            "4102|bob|gpu|COMPLETED|1794502000|1794502400|\n"
            "4200_1|bob|gpu|CANCELLED|1794503000|1794503010|\n"
            "4200_2|bob|gpu|COMPLETED|1794503000|1794503180|gres/gpu=1\n"
            "4104|bob|gpu|CANCELLED by 1001|1794504000|1794504060|gres/gpu=1\n"
            "4301|alice|cpu|CANCELLED|1794505000|1794505600|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root, '[[clusters]]\nname = "cluster-a"\nlocal = true\n')
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_rows,
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            rc, out, err = run_cli(["wait", "--json"], root=root, runner=runner)
            self.assertEqual((rc, err), (0, ""))
            # Valid samples on gpu: [60, 180] -> p50 = 120.0, p90 = 168.0; cpu omitted.
            self.assertEqual(
                json.loads(out)["clusters"][0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "n": 2,
                        "p50_seconds": 120.0,
                        "p90_seconds": 168.0,
                        "median_pending_seconds": None,
                        "oldest_pending_seconds": None,
                    }
                ],
            )

    def test_wait_collapses_array_before_window_cutoff_and_collapses_het_jobs(self) -> None:
        # FIXED_NOW = 1794513600; 7-day window_start = 1793908800.
        sacct_rows = (
            # Array 6000: in-window task appears before pre-window earliest task -> collapsed Start < window_start -> dropped.
            "6000_50|bob|gpu|COMPLETED|1793908700|1793995200|gres/gpu=1\n"
            "6000_1|bob|gpu|COMPLETED|1793908700|1793908750|gres/gpu=1\n"
            # In-window array 6100: .batch step ignored; collapses to earliest task 6100_1 (wait = 90s).
            "6100_1.batch||cpu|COMPLETED|1794500000|1794500000|cpu=8\n"
            "6100_1|alice|cpu|COMPLETED|1794500000|1794500090|cpu=8\n"
            "6100_2|alice|cpu|COMPLETED|1794500000|1794500300|cpu=8\n"
            # Hetjob 7600: +1 on gpu starts earlier than +0 on cpu -> 1 sample on gpu (wait = 40s).
            "7600+0|bob|cpu|COMPLETED|1794501000|1794501090|cpu=8\n"
            "7600+1|bob|gpu|COMPLETED|1794501000|1794501040|gres/gpu=1\n"
            # Requeued and non-positive epochs skipped.
            "7700|alice|cpu|REQUEUED|1794502000|1794502050|cpu=4\n"
            "7701|alice|cpu|COMPLETED|0|1794502050|cpu=4\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root, '[[clusters]]\nname = "cluster-a"\nlocal = true\n')
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_rows,
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            rc, out, err = run_cli(["wait", "--json"], root=root, runner=runner)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(
                json.loads(out)["clusters"][0]["partitions"],
                [
                    {
                        "partition": "cpu",
                        "n": 1,
                        "p50_seconds": 90.0,
                        "p90_seconds": 90.0,
                        "median_pending_seconds": None,
                        "oldest_pending_seconds": None,
                    },
                    {
                        "partition": "gpu",
                        "n": 1,
                        "p50_seconds": 40.0,
                        "p90_seconds": 40.0,
                        "median_pending_seconds": None,
                        "oldest_pending_seconds": None,
                    },
                ],
            )

    def test_wait_contention_whitelist_multipartition_pending_and_median_pending(self) -> None:
        # FIXED_NOW = 1794513600.
        sacct_rows = (
            "1001|alice|gpu|COMPLETED|1794500000|1794501800|gres/gpu=1\n"
            "1002|bob|gpu|CANCELLED|1794500000|1794500100|\n"
        )
        squeue_rows = (
            # Non-contention reasons and invalid/future EligibleTime excluded:
            "8101|alice|gpu|PD|JobHeldUser|1789000000|\n"
            "8102|bob|gpu|PD|Dependency|1789100000|\n"
            "8103|alice|gpu|PD|BeginTime|1789200000|\n"
            "8104|bob|gpu|PD|ReqNodeNotAvail, UnavailableNodes:node01|1789300000|\n"
            "8105|alice|gpu|PD|JobArrayTaskLimit|1789400000|\n"
            "8106|bob|gpu|PD|QOSMaxJobsPerUserLimit|1789500000|\n"
            "8107|alice|gpu|PD|AssocMaxJobsLimit|1789600000|\n"
            "8108|bob|gpu|PD|PartitionDown|1789700000|\n"
            "8109|alice|gpu|PD|Resources|Unknown|\n"
            "8110|bob|gpu|PD|Priority|1794513605|\n"
            "8111|alice|holdonly|PD|Dependency|1789000000|\n"
            # Whitelisted on gpu: [600, 3600, 7200, 4450000] -> median 5400.0 (1h 30m), oldest 4450000 (51d 12h).
            "8201|alice|gpu|PD|Priority|1794506400|\n"
            "8202|bob|gpu|PD|Resources|1790063600|\n"
            "8203|alice|gpu|PD|AssocGrpGpuLimit|1794513000|\n"
            "8204|bob|gpu|PD|QOSGrpBillingMinutes|1794510000|\n"
            # Multi-partition pending updates both debug (3600s) and cpu ([1800, 3600] -> median 2700.0, oldest 3600).
            "8301|alice|debug*,cpu|PD|Resources|1794510000|\n"
            "8302|bob|cpu|PD|None|1794511800|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root, '[[clusters]]\nname = "cluster-a"\nlocal = true\n')
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_rows,
                    ("cluster-a", "squeue"): squeue_rows,
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            rc, out_json, err = run_cli(["wait", "--json"], root=root, runner=runner)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(
                json.loads(out_json)["clusters"][0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "n": 1,
                        "p50_seconds": 1800.0,
                        "p90_seconds": 1800.0,
                        "median_pending_seconds": 5400.0,
                        "oldest_pending_seconds": 4450000,
                    },
                    {
                        "partition": "debug",
                        "n": 0,
                        "p50_seconds": None,
                        "p90_seconds": None,
                        "median_pending_seconds": 3600.0,
                        "oldest_pending_seconds": 3600,
                    },
                    {
                        "partition": "cpu",
                        "n": 0,
                        "p50_seconds": None,
                        "p90_seconds": None,
                        "median_pending_seconds": 2700.0,
                        "oldest_pending_seconds": 3600,
                    },
                ],
            )

            rc_tbl, out_tbl, _ = run_cli(["wait"], root=root, runner=runner)
            self.assertEqual(rc_tbl, 0)
            self.assertEqual(
                out_tbl,
                "cluster-a\n"
                "  PARTITION  N  P50     P90     MEDIAN PENDING  OLDEST PENDING\n"
                "  gpu        1  30m 0s  30m 0s  1h 30m          51d 12h\n"
                "  debug      0  -       -       1h 0m           1h 0m\n"
                "  cpu        0  -       -       45m 0s          1h 0m\n",
            )

    def test_wait_history_unavailable_fallback_with_history_error(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): CommandResult(
                        1, "", "sacct: error: Slurm accounting storage is disabled\n"
                    ),
                    ("cluster-a", "squeue"): (
                        "901|alice|gpu|PD|Resources|1794512700|\n"
                        "902|bob|gpu|PD|Priority|1794513300|\n"
                    ),
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-b", "sacct"): CommandResult(
                        1, "", "sacct: error: Slurm accounting storage is disabled\n"
                    ),
                    ("cluster-b", "squeue"): CommandResult(1, "", "squeue: error: controller down\n"),
                }
            )
            rc, out_json, err = run_cli(["wait", "--json"], root=root, runner=runner)
            self.assertEqual((rc, err), (0, ""))
            clusters = json.loads(out_json)["clusters"]
            self.assertEqual(
                clusters[0],
                {
                    "cluster": "cluster-a",
                    "fetched_at": "2026-11-12T20:00:00Z",
                    "stale": False,
                    "user_scoped": False,
                    "error": None,
                    "history_error": "sacct exited 1: error: Slurm accounting storage is disabled",
                    "partitions": [
                        {
                            "partition": "gpu",
                            "n": 0,
                            "p50_seconds": None,
                            "p90_seconds": None,
                            "median_pending_seconds": 600.0,
                            "oldest_pending_seconds": 900,
                        }
                    ],
                },
            )
            self.assertEqual(clusters[1]["error"], "squeue exited 1: error: controller down")
            self.assertIsNone(clusters[1]["history_error"])

            rc_tbl, out_tbl, _ = run_cli(["wait", "--cluster", "cluster-a"], root=root, runner=runner)
            self.assertEqual(rc_tbl, 0)
            self.assertEqual(
                out_tbl,
                "cluster-a\n"
                "  history unavailable: sacct exited 1: error: Slurm accounting storage is disabled\n"
                "  PARTITION  N  P50  P90  MEDIAN PENDING  OLDEST PENDING\n"
                "  gpu        0  -    -    10m 0s          15m 0s\n",
            )


if __name__ == "__main__":
    unittest.main()
