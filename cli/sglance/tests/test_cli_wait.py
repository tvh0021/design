from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sglance import CommandResult, TransportError, main
from tests.fakes import FakeRunner, make_test_env

# 2026-11-12T20:00:00Z == 1794513600 Unix epoch seconds.
FIXED_NOW = datetime(2026, 11, 12, 20, 0, 0, tzinfo=UTC)


class WaitCliTests(unittest.TestCase):
    def test_wait_rejects_non_positive_or_non_integer_days_with_exit_two_before_runner_calls(
        self,
    ) -> None:
        bad_values = ("0", "-1", "-14", "abc", "2.5", "", "1_0")
        for bad in bad_values:
            for argv in (["wait", "--days", bad], [f"--days={bad}", "wait"]):
                if argv[0].startswith("--days="):
                    argv = ["wait", f"--days={bad}"]
                with self.subTest(argv=argv), TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    config_dir = root / "config" / "sglance"
                    config_dir.mkdir(parents=True)
                    (config_dir / "config.toml").write_text(
                        '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                        encoding="utf-8",
                    )
                    runner = FakeRunner()
                    stdout = io.StringIO()
                    stderr = io.StringIO()

                    exit_code = main(
                        argv,
                        env=make_test_env(root),
                        runner=runner,
                        clock=lambda: FIXED_NOW,
                        stdout=stdout,
                        stderr=stderr,
                    )

                    self.assertEqual(exit_code, 2)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("--days", stderr.getvalue())
                    self.assertEqual(runner.calls, [])

    def test_wait_records_exact_slurm_argv_and_computes_standalone_percentiles_in_json(
        self,
    ) -> None:
        sacct_cluster_a = (
            "1001|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n"
            "1002|bob|cpu|RUNNING|1794501000|1794501600|cpu=16\n"
            "1003|alice|cpu|TIMEOUT|1794502000|1794502120|cpu=8\n"
            "1004|bob|cpu|FAILED|1794503000|1794503300|cpu=4\n"
            "2001|alice|gpu|OUT_OF_MEMORY|1794505000|1794505045|cpu=8,gres/gpu=1\n"
        )
        sacct_cluster_b = (
            "3001|alice|gpu|COMPLETED|1794000000|1794000000|gres/gpu=1\n"
            "3002|bob|gpu|NODE_FAIL|1794001000|1794001013|gres/gpu=1\n"
            "3003|alice|gpu|PREEMPTED|1794002000|1794002000|gres/gpu=1\n"
            "3004|bob|gpu|CANCELLED by 1001|1794003000|1794003010|gres/gpu=1\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\nslurm_cluster = "prod"\n',
                encoding="utf-8",
            )

            runner_a = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_cluster_a,
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )
            out_a = io.StringIO()
            err_a = io.StringIO()
            rc_a = main(
                ["wait", "--cluster", "cluster-a", "--json"],
                env=make_test_env(root),
                runner=runner_a,
                clock=lambda: FIXED_NOW,
                stdout=out_a,
                stderr=err_a,
            )

            self.assertEqual(rc_a, 0)
            self.assertEqual(err_a.getvalue(), "")
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
                            "--format=JobID,User,Partition,State,Eligible,Start,AllocTRES",
                        ],
                    ),
                    (
                        "cluster-a",
                        "ssh",
                        "login.example.edu",
                        [
                            "env",
                            "SLURM_TIME_FORMAT=%s",
                            "squeue",
                            "-h",
                            "-r",
                            "-t",
                            "PD",
                            "-O",
                            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|,EligibleTime:0|",
                        ],
                    ),
                    (
                        "cluster-a",
                        "ssh",
                        "login.example.edu",
                        [
                            "env",
                            "SLURM_TIME_FORMAT=%s",
                            "scontrol",
                            "show",
                            "config",
                        ],
                    ),
                ],
            )
            self.assertEqual(
                json.loads(out_a.getvalue()),
                {
                    "schema_version": 1,
                    "command": "wait",
                    "days": 7,
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "history_error": None,
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "n": 4,
                                    "p50_seconds": 210.0,
                                    "p90_seconds": 510.0,
                                    "oldest_pending_seconds": None,
                                },
                                {
                                    "partition": "gpu",
                                    "n": 1,
                                    "p50_seconds": 45.0,
                                    "p90_seconds": 45.0,
                                    "oldest_pending_seconds": None,
                                },
                            ],
                        }
                    ],
                },
            )

            runner_b = FakeRunner(
                {
                    ("cluster-b", "sacct"): sacct_cluster_b,
                    ("cluster-b", "squeue"): "",
                    ("cluster-b", "scontrol"): "PrivateData             = none\n",
                }
            )
            out_b = io.StringIO()
            err_b = io.StringIO()
            rc_b = main(
                ["wait", "--cluster", "cluster-b", "--days", "14", "--json"],
                env=make_test_env(root),
                runner=runner_b,
                clock=lambda: FIXED_NOW,
                stdout=out_b,
                stderr=err_b,
            )

            self.assertEqual(rc_b, 0)
            self.assertEqual(err_b.getvalue(), "")
            self.assertEqual(
                runner_b.calls,
                [
                    (
                        "cluster-b",
                        "ssh",
                        "cluster-b",
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
                            "--format=JobID,User,Partition,State,Eligible,Start,AllocTRES",
                        ],
                    ),
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
                            "-t",
                            "PD",
                            "-O",
                            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|,EligibleTime:0|",
                        ],
                    ),
                    (
                        "cluster-b",
                        "ssh",
                        "cluster-b",
                        [
                            "env",
                            "SLURM_TIME_FORMAT=%s",
                            "scontrol",
                            "-M",
                            "prod",
                            "show",
                            "config",
                        ],
                    ),
                ],
            )
            self.assertEqual(
                json.loads(out_b.getvalue()),
                {
                    "schema_version": 1,
                    "command": "wait",
                    "days": 14,
                    "clusters": [
                        {
                            "cluster": "cluster-b",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "history_error": None,
                            "partitions": [
                                {
                                    "partition": "gpu",
                                    "n": 4,
                                    "p50_seconds": 5.0,
                                    "p90_seconds": 12.1,
                                    "oldest_pending_seconds": None,
                                }
                            ],
                        }
                    ],
                },
            )

    def test_wait_rejects_sacct_rows_not_matching_seven_field_format(self) -> None:
        for bad_row in (
            "1001|alice|cpu|COMPLETED|1794500000|1794500060\n",
            "1001|1001|alice|cpu|COMPLETED|1794500000|1794500000|1794500060|1|8|cpu=8|cpu=8|01:00:00\n",
        ):
            with self.subTest(bad_row=bad_row), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(
                    '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                    encoding="utf-8",
                )
                runner = FakeRunner(
                    {
                        ("cluster-a", "sacct"): bad_row,
                        ("cluster-a", "squeue"): "",
                    }
                )
                stdout = io.StringIO()
                stderr = io.StringIO()
                rc = main(
                    ["wait", "--json"],
                    env=make_test_env(root),
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=stdout,
                    stderr=stderr,
                )
                self.assertEqual(rc, 0)
                cluster_payload = json.loads(stdout.getvalue())["clusters"][0]
                self.assertIn("malformed sacct output", cluster_payload["error"] or "")
                self.assertEqual(cluster_payload["partitions"], [])

    def test_wait_collapses_twenty_task_array_and_filters_prewindow_cancelled_requeued_and_hold_time(
        self,
    ) -> None:
        # FIXED_NOW = 1794513600; 7-day window cutoff = 1794513600 - 604800 = 1793908800.
        array_rows = []
        for idx in range(1, 21):
            start_epoch = 1794200120 if idx == 7 else 1794200300 + idx * 60
            array_rows.append(
                f"6000_{idx}|bob|gpu|COMPLETED|1794200000|{start_epoch}|gres/gpu=1"
            )
        sacct_lines = [
            # Pre-window job: started 1s before cutoff (1793908799), ended inside window -> discard.
            "4001|alice|gpu|COMPLETED|1793900000|1793908799|gres/gpu=1",
            # Pre-window job on retired partition -> must not emit a phantom partition row.
            "40010|alice|legacy|COMPLETED|1793800000|1793850000|cpu=8",
            # Cancelled while pending (numeric Start = cancel time, empty AllocTRES) -> discard.
            "4002|bob|gpu|CANCELLED by 1002|1794100000|1794100500|",
            "4003|alice|gpu|CANCELLED|1794100100|1794100250|",
            # Unknown / None Start or Eligible -> discard.
            "40030|bob|gpu|COMPLETED|1794100150|Unknown|gres/gpu=1",
            "40031|alice|gpu|COMPLETED|1794100160|None|gres/gpu=1",
            "4004|bob|gpu|CANCELLED|Unknown|1794100200|",
            # Clock skew / invalid Start < Eligible -> discard.
            "4005|alice|gpu|COMPLETED|1794100500|1794100499|gres/gpu=1",
            # Requeued records (standalone + array task with earlier Start) -> discard.
            "4006|bob|gpu|REQUEUED|1794101000|1794109999|gres/gpu=1",
            "6000_21|bob|gpu|REQUEUED|1794200000|1794200010|gres/gpu=1",
            "4007|alice|gpu|REQUEUE_HOLD|1794110000|1794119999|",
            "4008|bob|gpu|PENDING|1794115000|Unknown|",
            # Step row (if present) -> skip leniently.
            "5001.batch||gpu|COMPLETED|1793908800|1793908800|cpu=8",
            # Sample 1: started at exact cutoff (1793908800), wait = 30s.
            "5001|alice|gpu|COMPLETED|1793908770|1793908800|gres/gpu=1",
            # Sample 2: 20-task array 6000_1..6000_20, earliest Start is 6000_7 -> wait = 120s.
            *array_rows,
            # Sample 3: held job, Start - Eligible = 90s.
            "7001|alice|gpu|COMPLETED|1794307200|1794307290|gres/gpu=1",
            # Sample 4: held/dependency job, Start - Eligible = 240s.
            "7002|bob|gpu|COMPLETED|1794353600|1794353840|gres/gpu=1",
            # Sample 5: standalone running job, Start - Eligible = 600s.
            "7003|alice|gpu|RUNNING|1794400000|1794400600|gres/gpu=1",
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): "\n".join(sacct_lines) + "\n",
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            # Sorted waits: [30, 90, 120, 240, 600], n = 5
            # p50: pos = 0.50 * 4 = 2.0 -> 120.0
            # p90: pos = 0.90 * 4 = 3.6 -> 240 + 0.6 * (600 - 240) = 456.0
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "schema_version": 1,
                    "command": "wait",
                    "days": 7,
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "history_error": None,
                            "partitions": [
                                {
                                    "partition": "gpu",
                                    "n": 5,
                                    "p50_seconds": 120.0,
                                    "p90_seconds": 456.0,
                                    "oldest_pending_seconds": None,
                                }
                            ],
                        }
                    ],
                },
            )

    def test_wait_computes_oldest_pending_age_excluding_non_runnable_reasons_and_invalid_eligible_times(
        self,
    ) -> None:
        # Cluster in UTC+9 (2026-11-13T05:00:00+09:00 == 2026-11-12T20:00:00Z == 1794513600).
        jst = timezone(timedelta(hours=9))
        now_jst = datetime(2026, 11, 13, 5, 0, 0, tzinfo=jst)

        sacct_stdout = (
            "7101|alice|gpu|COMPLETED|1794500000|1794500100|gres/gpu=1\n"
            "7102|bob|gpu|COMPLETED|1794501000|1794501300|gres/gpu=1\n"
            "7201|alice|cpu|COMPLETED|1794502000|1794502060|cpu=4\n"
        )
        squeue_stdout = (
            # Excluded reasons on gpu (all much older than 5400s):
            "8001|alice|gpu|PD|JobHeldUser|1794400000|\n"
            "8002|bob|gpu|PD|JobHeldAdmin|1794410000|\n"
            "8003|alice|gpu|PD|BeginTime|1794420000|\n"
            "8004|bob|gpu|PD|Dependency|1794430000|\n"
            "8005|alice|gpu|PD|DependencyNeverSatisfied|1794440000|\n"
            "8006|bob|gpu|PD|ReqNodeNotAvail|1794450000|\n"
            "8007|alice|gpu|PD|ReqNodeNotAvail, UnavailableNodes:node04|1794460000|\n"
            # Excluded EligibleTime values on gpu:
            "8008|bob|gpu|PD|Resources|Unknown|\n"
            "8009|alice|gpu|PD|Priority|N/A|\n"
            "8010|bob|gpu|PD|Resources|None|\n"
            "8011|alice|gpu|PD|Priority|1794513601|\n"
            # Runnable pending jobs on gpu (03:30+09:00 = 1794508200 -> 5400s old; 1800s; 0s):
            "8012|alice|gpu|PD|Resources|1794508200|\n"
            "8013|bob|gpu|PD|Priority|1794511800|\n"
            "8014|alice|gpu|PD|AssocGrpGpuLimit|1794513600|\n"
            # cpu's single-partition pending jobs are excluded, while debug,cpu below updates both debug and cpu:
            "8020|bob|cpu|PD|JobHeldUser|1794490000|\n"
            "8021|alice|cpu|PD|Resources|1794514000|\n"
            # Multi-partition target debug,cpu -> 3600s on both debug (n=0 in sacct) and cpu:
            "8030_1|bob|debug,cpu|PD|Resources|1794510000|\n"
            "8030_2|bob|debug,cpu|PD|Priority|1794512000|\n"
            # Hold-only partition with no sacct samples -> must not emit phantom all-null row:
            "8040|alice|holdonly|PD|Dependency|1794480000|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_stdout,
                    ("cluster-a", "squeue"): squeue_stdout,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: now_jst,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
                    "schema_version": 1,
                    "command": "wait",
                    "days": 7,
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "history_error": None,
                            "partitions": [
                                {
                                    "partition": "gpu",
                                    "n": 2,
                                    "p50_seconds": 200.0,
                                    "p90_seconds": 280.0,
                                    "oldest_pending_seconds": 5400,
                                },
                                {
                                    "partition": "cpu",
                                    "n": 1,
                                    "p50_seconds": 60.0,
                                    "p90_seconds": 60.0,
                                    "oldest_pending_seconds": 3600,
                                },
                                {
                                    "partition": "debug",
                                    "n": 0,
                                    "p50_seconds": None,
                                    "p90_seconds": None,
                                    "oldest_pending_seconds": 3600,
                                },
                            ],
                        }
                    ],
                },
            )

    def test_wait_sets_user_scoped_from_sacct_distinct_users_and_scontrol_private_data(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n\n'
                '[[clusters]]\nname = "cluster-c"\nlocal = true\n\n'
                '[[clusters]]\nname = "cluster-d"\nssh = "cluster-d"\n\n'
                '[[clusters]]\nname = "cluster-e"\nssh = "cluster-e"\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    # cluster-a: slurmdbd restricts sacct to alice while squeue shows bob too -> 1 distinct sacct user -> True without scontrol.
                    ("cluster-a", "sacct"): (
                        "9001|alice|gpu|COMPLETED|1794500000|1794500060|gres/gpu=1\n"
                        "9002|alice|gpu|RUNNING|1794501000|1794501180|gres/gpu=1\n"
                    ),
                    ("cluster-a", "squeue"): (
                        "9010|alice|gpu|PD|Resources|1794513000|\n"
                        "9011|bob|gpu|PD|Priority|1794513300|\n"
                    ),
                    # cluster-b: sacct returns alice (started) and bob (cancelled while pending) -> 2 distinct sacct users -> False.
                    ("cluster-b", "sacct"): (
                        "9101|alice|cpu|COMPLETED|1794500000|1794500030|cpu=4\n"
                        "9102|bob|cpu|CANCELLED by 1002|1794501000|1794501200|\n"
                    ),
                    ("cluster-b", "squeue"): "",
                    ("cluster-b", "scontrol"): "PrivateData             = none\n",
                    # cluster-c: sacct has only alice on a cancelled-pending job row -> 1 distinct sacct user -> True without scontrol.
                    ("cluster-c", "sacct"): (
                        "9201|alice|cpu|CANCELLED|1794500000|1794500300|\n"
                    ),
                    ("cluster-c", "squeue"): "",
                    # cluster-d: empty sacct, scontrol has PrivateData = jobs -> True.
                    ("cluster-d", "sacct"): "",
                    ("cluster-d", "squeue"): "",
                    ("cluster-d", "scontrol"): "PrivateData             = jobs\n",
                    # cluster-e: empty sacct, scontrol omits jobs/all -> False.
                    ("cluster-e", "sacct"): "",
                    ("cluster-e", "squeue"): "",
                    ("cluster-e", "scontrol"): "PrivateData             = usage\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                sorted(call[0] for call in runner.calls if call[3][2] == "scontrol"),
                ["cluster-b", "cluster-d", "cluster-e"],
            )
            clusters = json.loads(stdout.getvalue())["clusters"]
            by_name = {item["cluster"]: item for item in clusters}
            scoping = {name: item["user_scoped"] for name, item in by_name.items()}
            self.assertEqual(
                scoping,
                {
                    "cluster-a": True,
                    "cluster-b": False,
                    "cluster-c": True,
                    "cluster-d": True,
                    "cluster-e": False,
                },
            )
            self.assertEqual(by_name["cluster-b"]["partitions"][0]["n"], 1)
            self.assertEqual(by_name["cluster-c"]["partitions"], [])

    def test_wait_skips_cancelled_pending_and_empty_alloc_tres_rows_with_numeric_start(
        self,
    ) -> None:
        sacct_stdout = (
            # Cancelled while pending: Slurm sets numeric Start = End = cancel time, AllocTRES empty.
            "4101|alice|gpu|CANCELLED by 1001|1794500000|1794500900|\n"
            "4102|bob|gpu|CA|1794501000|1794501300|\n"
            # Any row with empty AllocTRES must not count even if State is COMPLETED.
            "4103|alice|gpu|COMPLETED|1794502000|1794502400|\n"
            # Array task cancelled while pending before sibling started must not become array Start.
            "4200_1|bob|gpu|CANCELLED|1794503000|1794503010|\n"
            "4200_2|bob|gpu|COMPLETED|1794503000|1794503180|gres/gpu=1\n"
            # Cancelled after starting (non-empty AllocTRES) still counts: wait = 60s.
            "4104|bob|gpu|CANCELLED by 1001|1794504000|1794504060|gres/gpu=1\n"
            # Partition with only cancelled-pending row must not emit a partition entry.
            "4301|alice|cpu|CANCELLED|1794505000|1794505600|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_stdout,
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            # Valid samples on gpu: [60, 180], n = 2
            # p50: 60 + 0.5 * 120 = 120.0; p90: 60 + 0.9 * 120 = 168.0
            self.assertEqual(
                json.loads(stdout.getvalue())["clusters"][0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "n": 2,
                        "p50_seconds": 120.0,
                        "p90_seconds": 168.0,
                        "oldest_pending_seconds": None,
                    }
                ],
            )

    def test_wait_oldest_pending_whitelists_only_contention_reasons(self) -> None:
        # FIXED_NOW = 1794513600.
        squeue_stdout = (
            # Excluded non-contention reasons with much older eligible times:
            "8101|alice|gpu|PD|JobArrayTaskLimit|1794000000|\n"
            "8102|bob|gpu|PD|QOSMaxJobsPerUserLimit|1794100000|\n"
            "8103|alice|gpu|PD|AssocMaxJobsLimit|1794200000|\n"
            "8104|bob|gpu|PD|PartitionDown|1794300000|\n"
            "8105|alice|gpu|PD|Reservation|1794350000|\n"
            "8106|bob|gpu|PD|JobHeldUser|1794400000|\n"
            "8107|alice|gpu|PD|Dependency|1794410000|\n"
            "8108|bob|gpu|PD|BeginTime|1794420000|\n"
            "8109|alice|gpu|PD|ReqNodeNotAvail|1794430000|\n"
            # Whitelisted contention reasons on gpu (oldest is QOSGrpGpuLimit at 7200s):
            "8110|bob|gpu|PD|QOSGrpGpuLimit|1794506400|\n"
            "8111|alice|gpu|PD|AssocGrpCpuLimit|1794510000|\n"
            "8112|bob|gpu|PD|Resources|1794511800|\n"
            "8113|alice|gpu|PD|Priority|1794513000|\n"
            # cpu: JobArrayTaskLimit excluded; Reason=None whitelisted (900s old):
            "8201|alice|cpu|PD|JobArrayTaskLimit|1794000000|\n"
            "8202|bob|cpu|PD|None|1794512700|\n"
            # bigmem: only PartitionDown -> must not emit a partition row:
            "8301|alice|bigmem|PD|PartitionDown|1794000000|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): "",
                    ("cluster-a", "squeue"): squeue_stdout,
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue())["clusters"][0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "n": 0,
                        "p50_seconds": None,
                        "p90_seconds": None,
                        "oldest_pending_seconds": 7200,
                    },
                    {
                        "partition": "cpu",
                        "n": 0,
                        "p50_seconds": None,
                        "p90_seconds": None,
                        "oldest_pending_seconds": 900,
                    },
                ],
            )

    def test_wait_collapses_array_to_earliest_start_before_applying_window_cutoff(
        self,
    ) -> None:
        # FIXED_NOW = 1794513600; 7-day window_start = 1793908800.
        # Eligible 100s before window = 1793908700;
        # 6000_1 starts 50s before window = 1793908750;
        # 6000_50 starts 1 day into window = 1793995200.
        sacct_only_array = (
            "6000_1|alice|gpu|COMPLETED|1793908700|1793908750|gres/gpu=1\n"
            "6000_50|alice|gpu|COMPLETED|1793908700|1793995200|gres/gpu=1\n"
        )
        # Also verify reverse sacct row order (in-window task before pre-window earliest task).
        sacct_reversed_plus_valid = (
            "6001_50|bob|gpu|COMPLETED|1793908700|1793995200|gres/gpu=1\n"
            "6001_1|bob|gpu|COMPLETED|1793908700|1793908750|gres/gpu=1\n"
            "6002|alice|gpu|COMPLETED|1794500000|1794500090|gres/gpu=1\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_only_array,
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-b", "sacct"): sacct_reversed_plus_valid,
                    ("cluster-b", "squeue"): "",
                    ("cluster-b", "scontrol"): "PrivateData = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            clusters = {
                item["cluster"]: item["partitions"]
                for item in json.loads(stdout.getvalue())["clusters"]
            }
            self.assertEqual(clusters["cluster-a"], [])
            self.assertEqual(
                clusters["cluster-b"],
                [
                    {
                        "partition": "gpu",
                        "n": 1,
                        "p50_seconds": 90.0,
                        "p90_seconds": 90.0,
                        "oldest_pending_seconds": None,
                    }
                ],
            )

    def test_wait_collapses_heterogeneous_jobs_on_base_id_using_earliest_start_partition(
        self,
    ) -> None:
        # FIXED_NOW = 1794513600; window_start = 1793908800.
        sacct_stdout = (
            # Hetjob 7500: +0 and +1 tie at Start=1794500120 -> keeps +0 on cpu (wait=120s); +2 on bigmem ignored.
            "7500+0.batch||cpu|COMPLETED|1794500000|1794500000|cpu=16\n"
            "7500+0|alice|cpu|COMPLETED|1794500000|1794500120|cpu=16\n"
            "7500+1|alice|gpu|COMPLETED|1794500000|1794500120|gres/gpu=2\n"
            "7500+2|alice|bigmem|COMPLETED|1794500000|1794500125|cpu=32\n"
            # Hetjob 7600: +1 has earlier Start (1794501040 < 1794501090) -> uses +1's partition gpu (wait=40s).
            "7600+0|bob|cpu|COMPLETED|1794501000|1794501090|cpu=8\n"
            "7600+1|bob|gpu|COMPLETED|1794501000|1794501040|gres/gpu=1\n"
            # Hetjob 7700: +0 starts pre-window (1793908750), +1 in-window -> collapsed Start < window_start -> dropped.
            "7700+0|alice|bigmem|COMPLETED|1793908700|1793908750|cpu=32\n"
            "7700+1|alice|bigmem|COMPLETED|1793908700|1793908810|cpu=32\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_stdout,
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue())["clusters"][0]["partitions"],
                [
                    {
                        "partition": "cpu",
                        "n": 1,
                        "p50_seconds": 120.0,
                        "p90_seconds": 120.0,
                        "oldest_pending_seconds": None,
                    },
                    {
                        "partition": "gpu",
                        "n": 1,
                        "p50_seconds": 40.0,
                        "p90_seconds": 40.0,
                        "oldest_pending_seconds": None,
                    },
                ],
            )

    def test_wait_multipartition_pending_job_updates_every_listed_partition(self) -> None:
        # FIXED_NOW = 1794513600.
        squeue_stdout = (
            # 3600s old across debug* (default marker stripped), cpu, and bigmem:
            "9501|alice|debug*,cpu,bigmem|PD|Resources|1794510000|\n"
            # 7200s old on gpu:
            "9502|bob|gpu|PD|Priority|1794506400|\n"
            # 1800s old across cpu,gpu -> must not overwrite older 3600s on cpu or 7200s on gpu:
            "9503|alice|cpu,gpu|PD|Priority|1794511800|\n"
            # 600s old on bigmem -> must keep 3600s from 9501:
            "9504|bob|bigmem|PD|Resources|1794513000|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): "",
                    ("cluster-a", "squeue"): squeue_stdout,
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue())["clusters"][0]["partitions"],
                [
                    {
                        "partition": "debug",
                        "n": 0,
                        "p50_seconds": None,
                        "p90_seconds": None,
                        "oldest_pending_seconds": 3600,
                    },
                    {
                        "partition": "cpu",
                        "n": 0,
                        "p50_seconds": None,
                        "p90_seconds": None,
                        "oldest_pending_seconds": 3600,
                    },
                    {
                        "partition": "bigmem",
                        "n": 0,
                        "p50_seconds": None,
                        "p90_seconds": None,
                        "oldest_pending_seconds": 3600,
                    },
                    {
                        "partition": "gpu",
                        "n": 0,
                        "p50_seconds": None,
                        "p90_seconds": None,
                        "oldest_pending_seconds": 7200,
                    },
                ],
            )

    def test_wait_treats_non_positive_epoch_timestamps_as_unknown(self) -> None:
        # FIXED_NOW = 1794513600.
        sacct_stdout = (
            "9601|alice|gpu|COMPLETED|0|1794500100|gres/gpu=1\n"
            "9602|bob|gpu|COMPLETED|-1|1794500200|gres/gpu=1\n"
            "9603|alice|gpu|COMPLETED|1794500000|0|gres/gpu=1\n"
            "9604|bob|gpu|COMPLETED|0|0|gres/gpu=1\n"
            "9605|alice|gpu|COMPLETED|1794500000|1794500075|gres/gpu=1\n"
        )
        squeue_stdout = (
            "9701|alice|gpu|PD|Resources|0|\n"
            "9702|bob|gpu|PD|Priority|-5|\n"
            "9703|alice|gpu|PD|Resources|1794512400|\n"
            "9704|bob|cpu|PD|Resources|0|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_stdout,
                    ("cluster-a", "squeue"): squeue_stdout,
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                json.loads(stdout.getvalue())["clusters"][0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "n": 1,
                        "p50_seconds": 75.0,
                        "p90_seconds": 75.0,
                        "oldest_pending_seconds": 1200,
                    }
                ],
            )

    def test_wait_table_renders_aligned_durations_dashes_headers_and_empty_or_error_states(
        self,
    ) -> None:
        sacct_a = (
            "101|alice|gpu|COMPLETED|1794500000|1794500015|gres/gpu=1\n"
            "102|alice|gpu|COMPLETED|1794501000|1794501045|gres/gpu=1\n"
            "103|alice|gpu|COMPLETED|1794502000|1794502170|gres/gpu=1\n"
            "201|alice|cpu|COMPLETED|1794503000|1794506600|cpu=4\n"
        )
        squeue_a = (
            "301|alice|gpu|PD|Resources|1794508200|\n"
            "302|alice|bigmem|PD|Priority|1794423600|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n\n'
                '[[clusters]]\nname = "cluster-c"\nssh = "cluster-c"\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_a,
                    ("cluster-a", "squeue"): squeue_a,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                    ("cluster-b", "sacct"): "",
                    ("cluster-b", "squeue"): "",
                    ("cluster-b", "scontrol"): "PrivateData             = none\n",
                    ("cluster-c", "sacct"): TransportError("timed out after 25s"),
                    ("cluster-c", "squeue"): TransportError("timed out after 25s"),
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            rc = main(
                ["wait"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(stderr.getvalue(), "")
            rendered = stdout.getvalue()
            self.assertNotIn("\x1b", rendered)
            self.assertEqual(
                rendered,
                "cluster-a [user-scoped]\n"
                "  PARTITION  N  P50    P90     OLDEST PENDING\n"
                "  gpu        3  45s    2m 25s  1h 30m\n"
                "  cpu        1  1h 0m  1h 0m   -\n"
                "  bigmem     0  -      -       1d 1h\n"
                "\n"
                "cluster-b\n"
                "  (no wait data)\n"
                "\n"
                "cluster-c: unreachable (timed out after 25s)\n",
            )

    def test_wait_isolates_cache_by_days_skips_cluster_banners_and_never_caches_errors(
        self,
    ) -> None:
        sacct_with_banner = (
            "CLUSTER: prod\n"
            "501|alice|gpu|COMPLETED|1794500000|1794500120|gres/gpu=1\n"
            "502|bob|gpu|COMPLETED|1794501000|1794501120|gres/gpu=1\n"
        )
        squeue_with_banner = (
            "CLUSTER: prod\n"
            "510|alice|gpu|PD|Resources|1794513300|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\nslurm_cluster = "prod"\n\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
                encoding="utf-8",
            )
            shared_env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): sacct_with_banner,
                    ("cluster-a", "squeue"): squeue_with_banner,
                    ("cluster-a", "scontrol"): "CLUSTER: prod\nPrivateData = none\n",
                    (
                        "cluster-b",
                        "sacct",
                    ): "sacct: error: Problem talking to the database\n",
                    ("cluster-b", "squeue"): "",
                }
            )

            # Run 1 at t = 0s (default days=7): cluster-a succeeds (3 calls), cluster-b fails sacct parse (2 calls).
            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["wait", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 5)
            clusters1 = json.loads(out1.getvalue())["clusters"]
            self.assertIsNone(clusters1[0]["error"])
            self.assertFalse(clusters1[0]["stale"])
            self.assertEqual(
                clusters1[0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "n": 2,
                        "p50_seconds": 120.0,
                        "p90_seconds": 120.0,
                        "oldest_pending_seconds": 300,
                    }
                ],
            )
            self.assertIsNone(clusters1[1]["fetched_at"])
            self.assertFalse(clusters1[1]["stale"])
            self.assertIsNotNone(clusters1[1]["error"])
            self.assertEqual(clusters1[1]["partitions"], [])

            # Run 2 at t = 75s with explicit --days 7: cluster-a served from cache (0 calls),
            # cluster-b retried because errors are never cached (now fails squeue rc=1 -> 2 calls).
            runner._responses[("cluster-b", "sacct")] = (
                "601|alice|cpu|COMPLETED|1794500000|1794500030|cpu=4\n"
            )
            runner._responses[("cluster-b", "squeue")] = CommandResult(
                1, "", "squeue: error: controller busy\n"
            )
            t_hit = FIXED_NOW + timedelta(seconds=75)
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["wait", "--days", "7"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_hit,
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 7)
            self.assertEqual(
                out2.getvalue(),
                "cluster-a [cached 75s ago]\n"
                "  PARTITION  N  P50    P90    OLDEST PENDING\n"
                "  gpu        2  2m 0s  2m 0s  5m 0s\n"
                "\n"
                "cluster-b: squeue exited 1: error: controller busy\n",
            )

            # Run 3 at t = 90s with --days 3 on cluster-a: distinct options={"days": 3} misses cache!
            t_days3 = FIXED_NOW + timedelta(seconds=90)
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["wait", "--cluster", "cluster-a", "--days", "3", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_days3,
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertEqual(len(runner.calls), 10)
            payload3 = json.loads(out3.getvalue())
            self.assertEqual(payload3["days"], 3)
            self.assertFalse(payload3["clusters"][0]["stale"])

            # Run 4 at t = 120s after cluster-b recovers: cluster-a hits 7d cache, cluster-b succeeds (1 user -> skips scontrol).
            runner._responses[("cluster-b", "squeue")] = ""
            t_rec = FIXED_NOW + timedelta(seconds=120)
            out4, err4 = io.StringIO(), io.StringIO()
            rc4 = main(
                ["wait", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_rec,
                stdout=out4,
                stderr=err4,
            )
            self.assertEqual(rc4, 0)
            self.assertEqual(len(runner.calls), 12)
            clusters4 = json.loads(out4.getvalue())["clusters"]
            self.assertTrue(clusters4[0]["stale"])
            self.assertFalse(clusters4[1]["stale"])
            self.assertIsNone(clusters4[1]["error"])

            # Run 5 at t = 130s with --refresh on cluster-a: bypasses cache and refetches.
            t_ref = FIXED_NOW + timedelta(seconds=130)
            out5, err5 = io.StringIO(), io.StringIO()
            rc5 = main(
                ["--refresh", "wait", "--cluster", "cluster-a", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_ref,
                stdout=out5,
                stderr=err5,
            )
            self.assertEqual(rc5, 0)
            self.assertEqual(len(runner.calls), 15)
            self.assertFalse(json.loads(out5.getvalue())["clusters"][0]["stale"])

    def test_wait_falls_back_to_squeue_when_sacct_fails_and_caches_history_only_error(
        self,
    ) -> None:
        # FIXED_NOW = 1794513600 (2026-11-12T20:00:00Z).
        squeue_cluster_a = (
            "901|alice|gpu|PD|Resources|1794512700|\n"
            "902|bob|gpu|PD|Priority|1794513300|\n"
            "903|alice|gpu|PD|JobHeldUser|1794400000|\n"
            "904|bob|cpu|PD|Priority|1794506400|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.cluster-a.example.org"\n\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n\n'
                '[[clusters]]\nname = "cluster-c"\nssh = "login.cluster-c.example.org"\n',
                encoding="utf-8",
            )
            shared_env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "sacct"): CommandResult(
                        1, "", "sacct: error: Slurm accounting storage is disabled\n"
                    ),
                    ("cluster-a", "squeue"): squeue_cluster_a,
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-b", "sacct"): CommandResult(
                        1, "", "sacct: Slurm accounting storage is disabled\n"
                    ),
                    ("cluster-b", "squeue"): "",
                    ("cluster-b", "scontrol"): "PrivateData = jobs\n",
                    ("cluster-c", "sacct"): CommandResult(
                        1, "", "sacct: error: Slurm accounting storage is disabled\n"
                    ),
                    ("cluster-c", "squeue"): CommandResult(
                        1, "", "squeue: error: slurm_receive_msg: Socket timed out\n"
                    ),
                }
            )

            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["wait", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 8)
            self.assertEqual(
                json.loads(out1.getvalue()),
                {
                    "schema_version": 1,
                    "command": "wait",
                    "days": 7,
                    "clusters": [
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
                                    "oldest_pending_seconds": 900,
                                },
                                {
                                    "partition": "cpu",
                                    "n": 0,
                                    "p50_seconds": None,
                                    "p90_seconds": None,
                                    "oldest_pending_seconds": 7200,
                                },
                            ],
                        },
                        {
                            "cluster": "cluster-b",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": True,
                            "error": None,
                            "history_error": "sacct exited 1: Slurm accounting storage is disabled",
                            "partitions": [],
                        },
                        {
                            "cluster": "cluster-c",
                            "fetched_at": None,
                            "stale": False,
                            "user_scoped": False,
                            "error": "squeue exited 1: error: slurm_receive_msg: Socket timed out",
                            "history_error": None,
                            "partitions": [],
                        },
                    ],
                },
            )

            # At t = 45s (1794513645): cluster-a and cluster-b hit cache (0 calls);
            # cluster-c was not cached because it errored, so it retries and recovers squeue.
            runner._responses[("cluster-c", "squeue")] = (
                "950|alice|debug|PD|Resources|1794513545|\n"
            )
            runner._responses[("cluster-c", "scontrol")] = "PrivateData = none\n"
            t_hit = FIXED_NOW + timedelta(seconds=45)
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["wait"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_hit,
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 11)
            self.assertEqual(
                out2.getvalue(),
                "cluster-a [cached 45s ago]\n"
                "  history unavailable: sacct exited 1: error: Slurm accounting storage is disabled\n"
                "  PARTITION  N  P50  P90  OLDEST PENDING\n"
                "  gpu        0  -    -    15m 0s\n"
                "  cpu        0  -    -    2h 0m\n"
                "\n"
                "cluster-b [user-scoped] [cached 45s ago]\n"
                "  history unavailable: sacct exited 1: Slurm accounting storage is disabled\n"
                "  (no wait data)\n"
                "\n"
                "cluster-c\n"
                "  history unavailable: sacct exited 1: error: Slurm accounting storage is disabled\n"
                "  PARTITION  N  P50  P90  OLDEST PENDING\n"
                "  debug      0  -    -    1m 40s\n",
            )

            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["wait", "--cluster", "cluster-a", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_hit,
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertEqual(len(runner.calls), 11)
            self.assertEqual(
                json.loads(out3.getvalue())["clusters"][0],
                {
                    "cluster": "cluster-a",
                    "fetched_at": "2026-11-12T20:00:00Z",
                    "stale": True,
                    "user_scoped": False,
                    "error": None,
                    "history_error": "sacct exited 1: error: Slurm accounting storage is disabled",
                    "partitions": [
                        {
                            "partition": "gpu",
                            "n": 0,
                            "p50_seconds": None,
                            "p90_seconds": None,
                            "oldest_pending_seconds": 900,
                        },
                        {
                            "partition": "cpu",
                            "n": 0,
                            "p50_seconds": None,
                            "p90_seconds": None,
                            "oldest_pending_seconds": 7200,
                        },
                    ],
                },
            )


if __name__ == "__main__":
    unittest.main()
