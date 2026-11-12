from __future__ import annotations

from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sglance import CommandResult, TransportError, main
from tests.fakes import FakeRunner, make_test_env

FIXED_NOW = datetime(2026, 11, 12, 20, 0, 0, tzinfo=UTC)


class QueueCliTests(unittest.TestCase):
    def test_queue_json_counts_running_standalone_array_and_multipartition_pending_jobs(self) -> None:
        squeue_output = (
            "1001|alice|cpu|R|None|\n"
            "1002|bob|cpu|R|None|\n"
            "1003|alice|cpu|PD|Resources|\n"
            "1004|bob|cpu,gpu|PD|Priority|\n"
            "2001|alice|gpu|R|None|\n"
            "2002_1|bob|gpu|PD|Resources|\n"
            "2002_2|bob|gpu|PD|Resources|\n"
            "2002_3|bob|gpu|PD|Resources|\n"
            "2003|alice|gpu|PD|Priority|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): squeue_output,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["queue", "--json"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            raw_stdout = stdout.getvalue()
            self.assertNotIn("\x1b", raw_stdout)
            self.assertEqual(
                runner.calls,
                [
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
                            "-O",
                            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|",
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
                json.loads(raw_stdout),
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
                                    "pending_jobs": 2,
                                    "top_reasons": [
                                        {"reason": "Priority", "count": 1},
                                        {"reason": "Resources", "count": 1},
                                    ],
                                },
                                {
                                    "partition": "gpu",
                                    "running_jobs": 1,
                                    "pending_jobs": 4,
                                    "top_reasons": [
                                        {"reason": "Resources", "count": 3},
                                        {"reason": "Priority", "count": 1},
                                    ],
                                },
                            ],
                        }
                    ],
                },
            )

    def test_queue_ranks_top_reasons_with_alphabetical_ties_omits_running_and_caps_at_five(self) -> None:
        squeue_output = (
            "2901|alice|gpu|R|None|\n"
            "2902|bob|gpu|R|Priority|\n"
            "2903|alice|gpu|R|Resources|\n"
            "3001_1|bob|gpu|PD|Resources|\n"
            "3001_2|bob|gpu|PD|Resources|\n"
            "3001_3|bob|gpu|PD|Resources|\n"
            "3002|alice|gpu|PD|Priority|\n"
            "3003|bob|gpu|PD|Priority|\n"
            "3004|alice|gpu|PD|Priority|\n"
            "3005|bob|gpu|PD|Dependency|\n"
            "3006|alice|gpu|PD|Dependency|\n"
            "3007|bob|gpu|PD|AssocGrpNodeLimit|\n"
            "3008|alice|gpu|PD|AssocGrpNodeLimit|\n"
            "3009|bob|gpu|PD|QOSMaxGpuPerUserLimit|\n"
            "3010|alice|gpu|PD|JobHeldUser|\n"
            "3011|bob|gpu|PD|BeginTime|\n"
            "4001|alice|debug|R|None|\n"
            "4002|bob|debug|R|None|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): squeue_output,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["queue", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )

            self.assertEqual(json_rc, 0)
            self.assertEqual(json_err.getvalue(), "")
            self.assertEqual(
                json.loads(json_out.getvalue())["clusters"][0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "running_jobs": 3,
                        "pending_jobs": 13,
                        "top_reasons": [
                            {"reason": "Priority", "count": 3},
                            {"reason": "Resources", "count": 3},
                            {"reason": "AssocGrpNodeLimit", "count": 2},
                            {"reason": "Dependency", "count": 2},
                            {"reason": "BeginTime", "count": 1},
                        ],
                    },
                    {
                        "partition": "debug",
                        "running_jobs": 2,
                        "pending_jobs": 0,
                        "top_reasons": [],
                    },
                ],
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["queue"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )

            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            rendered = table_out.getvalue()
            self.assertNotIn("\x1b", rendered)
            self.assertNotIn("JobHeldUser", rendered)
            self.assertNotIn("QOSMaxGpuPerUserLimit", rendered)
            self.assertEqual(
                rendered,
                "cluster-a\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  gpu        3        13       Priority (3), Resources (3), AssocGrpNodeLimit (2), Dependency (2), BeginTime (1)\n"
                "  debug      2        0        -\n",
            )

    def test_empty_queue_renders_zero_counts_and_empty_reasons_in_table_and_json(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                    ("cluster-b", "squeue"): "   \n\n",
                    ("cluster-b", "scontrol"): "PrivateData             = none\n",
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["queue", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )

            self.assertEqual(json_rc, 0)
            self.assertEqual(json_err.getvalue(), "")
            self.assertEqual(
                json.loads(json_out.getvalue()),
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
                            "partitions": [],
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

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["queue"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )

            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            rendered = table_out.getvalue()
            self.assertNotIn("\x1b", rendered)
            self.assertEqual(
                rendered,
                "cluster-a\n"
                "  0 running, 0 pending\n"
                "\n"
                "cluster-b\n"
                "  0 running, 0 pending\n",
            )

    def test_queue_handles_slurm_cluster_flag_cluster_filter_and_transport_or_rc_errors(self) -> None:
        base_argv = [
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-h",
            "-r",
            "-O",
            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|",
        ]
        dash_m_argv = [
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-M",
            "prod",
            "-h",
            "-r",
            "-O",
            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|",
        ]
        base_scontrol_argv = [
            "env",
            "SLURM_TIME_FORMAT=%s",
            "scontrol",
            "show",
            "config",
        ]
        dash_m_scontrol_argv = [
            "env",
            "SLURM_TIME_FORMAT=%s",
            "scontrol",
            "-M",
            "prod",
            "show",
            "config",
        ]
        failure_cases = [
            (
                "ssh-timeout",
                TransportError("timed out after 25s"),
                "unreachable (timed out after 25s)",
            ),
            (
                "ssh-exit-255",
                CommandResult(255, "", "ssh: Connection timed out\n"),
                "unreachable (ssh: Connection timed out)",
            ),
            (
                "squeue-stderr",
                CommandResult(
                    1,
                    "",
                    "squeue: warning: ignoring deprecated config key\n"
                    "squeue: error: Slurm controller not responding\n",
                ),
                "squeue exited 1: squeue: error: Slurm controller not responding",
            ),
        ]

        for label, cluster_b_response, expected_b_error in failure_cases:
            with self.subTest(case=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(
                    '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                    '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\nslurm_cluster = "prod"\n\n'
                    '[[clusters]]\nname = "cluster-c"\nlocal = true\n',
                    encoding="utf-8",
                )
                runner = FakeRunner(
                    {
                        ("cluster-a", "squeue"): "100|alice|cpu|R|None|\n101|bob|cpu|PD|Resources|\n",
                        ("cluster-a", "scontrol"): "PrivateData             = none\n",
                        ("cluster-b", "squeue"): cluster_b_response,
                        ("cluster-c", "squeue"): CommandResult(5, "", "   \n"),
                    }
                )

                json_out = io.StringIO()
                json_err = io.StringIO()
                json_rc = main(
                    ["queue", "--json"],
                    env=make_test_env(root),
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=json_out,
                    stderr=json_err,
                )

                self.assertEqual(json_rc, 0)
                self.assertEqual(json_err.getvalue(), "")
                self.assertEqual(
                    sorted(runner.calls, key=lambda c: c[0]),
                    [
                        ("cluster-a", "ssh", "login.example.edu", base_argv),
                        ("cluster-a", "ssh", "login.example.edu", base_scontrol_argv),
                        ("cluster-b", "ssh", "cluster-b", dash_m_argv),
                        ("cluster-c", "local", None, base_argv),
                    ],
                )
                self.assertEqual(
                    json.loads(json_out.getvalue()),
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
                                        "running_jobs": 1,
                                        "pending_jobs": 1,
                                        "top_reasons": [
                                            {"reason": "Resources", "count": 1},
                                        ],
                                    }
                                ],
                            },
                            {
                                "cluster": "cluster-b",
                                "fetched_at": None,
                                "stale": False,
                                "user_scoped": False,
                                "error": expected_b_error,
                                "partitions": [],
                            },
                            {
                                "cluster": "cluster-c",
                                "fetched_at": None,
                                "stale": False,
                                "user_scoped": False,
                                "error": "squeue exited 5",
                                "partitions": [],
                            },
                        ],
                    },
                )

                table_out = io.StringIO()
                table_err = io.StringIO()
                table_rc = main(
                    ["queue"],
                    env=make_test_env(root),
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=table_out,
                    stderr=table_err,
                )

                self.assertEqual(table_rc, 0)
                self.assertEqual(table_err.getvalue(), "")
                self.assertEqual(
                    table_out.getvalue(),
                    "cluster-a\n"
                    "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                    "  cpu        1        1        Resources (1)\n"
                    "\n"
                    f"cluster-b: {expected_b_error}\n"
                    "\n"
                    "cluster-c: squeue exited 5\n",
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
            filtered_runner = FakeRunner(
                {
                    ("cluster-b", "squeue"): "200|alice|gpu|PD|Priority|\n",
                    ("cluster-b", "scontrol"): "PrivateData             = none\n",
                }
            )
            for argv in (["queue", "--cluster", "cluster-b"], ["--cluster", "cluster-b", "queue"]):
                out = io.StringIO()
                err = io.StringIO()
                rc = main(
                    argv,
                    env=make_test_env(root),
                    runner=filtered_runner,
                    clock=lambda: FIXED_NOW,
                    stdout=out,
                    stderr=err,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(err.getvalue(), "")
                self.assertEqual(
                    out.getvalue(),
                    "cluster-b [user-scoped]\n"
                    "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                    "  gpu        0        1        Priority (1)\n",
                )
            self.assertEqual(
                filtered_runner.calls,
                [
                    ("cluster-b", "ssh", "cluster-b", dash_m_argv),
                    ("cluster-b", "ssh", "cluster-b", dash_m_scontrol_argv),
                    ("cluster-b", "ssh", "cluster-b", dash_m_argv),
                    ("cluster-b", "ssh", "cluster-b", dash_m_scontrol_argv),
                ],
            )

            for bad_argv in (["queue", "--cluster", "cluster-unknown"], ["queue", "--unknown-flag"]):
                bad_runner = FakeRunner()
                bad_out = io.StringIO()
                bad_err = io.StringIO()
                bad_rc = main(
                    bad_argv,
                    env=make_test_env(root),
                    runner=bad_runner,
                    clock=lambda: FIXED_NOW,
                    stdout=bad_out,
                    stderr=bad_err,
                )
                self.assertEqual(bad_rc, 2)
                self.assertEqual(bad_out.getvalue(), "")
                self.assertNotEqual(bad_err.getvalue().strip(), "")
                self.assertEqual(bad_runner.calls, [])

    def test_malformed_squeue_stdout_rows_report_inline_cluster_error(self) -> None:
        malformed_cases = [
            ("wrong-field-count", "1001|alice|cpu|R|\n"),
            (
                "stray-squeue-error-on-stdout",
                "squeue: error: slurm_receive_msg: Socket timed out\n"
                "1001|alice|cpu|R|None|\n",
            ),
            ("empty-job-id", "|alice|cpu|R|None|\n"),
            ("empty-partition", "1001|alice||R|None|\n"),
            ("empty-multipartition-segment", "1001|alice|,gpu|PD|Resources|\n"),
        ]
        for label, bad_stdout in malformed_cases:
            with self.subTest(case=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(
                    '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                    '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
                    encoding="utf-8",
                )
                runner = FakeRunner(
                    {
                        ("cluster-a", "squeue"): bad_stdout,
                        ("cluster-b", "squeue"): "201|bob|gpu|R|None|\n",
                        ("cluster-b", "scontrol"): "PrivateData             = none\n",
                    }
                )

                json_out = io.StringIO()
                json_err = io.StringIO()
                json_rc = main(
                    ["queue", "--json"],
                    env=make_test_env(root),
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=json_out,
                    stderr=json_err,
                )

                self.assertEqual(json_rc, 0)
                self.assertEqual(json_err.getvalue(), "")
                cluster_a, cluster_b = json.loads(json_out.getvalue())["clusters"]
                self.assertEqual(cluster_a["cluster"], "cluster-a")
                self.assertIsNone(cluster_a["fetched_at"])
                self.assertFalse(cluster_a["stale"])
                self.assertFalse(cluster_a["user_scoped"])
                self.assertIsInstance(cluster_a["error"], str)
                self.assertNotEqual(cluster_a["error"].strip(), "")
                self.assertEqual(cluster_a["partitions"], [])

                self.assertEqual(cluster_b["cluster"], "cluster-b")
                self.assertEqual(cluster_b["fetched_at"], "2026-11-12T20:00:00Z")
                self.assertTrue(cluster_b["user_scoped"])
                self.assertIsNone(cluster_b["error"])
                self.assertEqual(
                    cluster_b["partitions"],
                    [
                        {
                            "partition": "gpu",
                            "running_jobs": 1,
                            "pending_jobs": 0,
                            "top_reasons": [],
                        }
                    ],
                )

                table_out = io.StringIO()
                table_err = io.StringIO()
                table_rc = main(
                    ["queue"],
                    env=make_test_env(root),
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=table_out,
                    stderr=table_err,
                )
                self.assertEqual(table_rc, 0)
                self.assertEqual(table_err.getvalue(), "")
                self.assertIn(
                    f"cluster-a: {cluster_a['error']}\n\ncluster-b [user-scoped]\n",
                    table_out.getvalue(),
                )

    def test_queue_tolerates_transient_slurm_compact_states_without_counting_as_pending_or_error(self) -> None:
        squeue_output = (
            "5001|alice|cpu|R|None|\n"
            "5002|bob|cpu|CG|None|\n"
            "5003|alice|cpu|CF|None|\n"
            "5004|bob|cpu|S|Priority|\n"
            "5005|alice|cpu|PD|Resources|\n"
            "6001|bob|debug|CG|None|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): squeue_output,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["queue", "--json"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            cluster = json.loads(stdout.getvalue())["clusters"][0]
            self.assertIsNone(cluster["error"])
            self.assertEqual(
                cluster["partitions"],
                [
                    {
                        "partition": "cpu",
                        "running_jobs": 2,
                        "pending_jobs": 1,
                        "top_reasons": [{"reason": "Resources", "count": 1}],
                    },
                ],
            )

    def test_queue_counts_running_and_pending_states_skips_uncounted_and_defaults_empty_reason(self) -> None:
        squeue_output = (
            "7001|alice|cpu|R|None|\n"
            "7002|bob|cpu|CF|None|\n"
            "7003|alice|cpu|EX|None|\n"
            "7004|bob|cpu|BOGUS|None|\n"
            "7005||cpu|RH|JobHeldAdmin|\n"
            "7006|alice|cpu|RQ|Resources|\n"
            "7007|bob|cpu|RD|Reservation|\n"
            "7008|alice|cpu|SE|SpecialExit|\n"
            "7009|bob|cpu|PD||\n"
            "7010|alice|cpu|PD|JobHeldAdmin|\n"
            "8001|bob|archive|EX|None|\n"
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
                    ("cluster-a", "squeue"): squeue_output,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["queue", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )
            self.assertEqual(json_rc, 0)
            self.assertEqual(json_err.getvalue(), "")
            self.assertEqual(
                json.loads(json_out.getvalue()),
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
                                    "pending_jobs": 6,
                                    "top_reasons": [
                                        {"reason": "JobHeldAdmin", "count": 2},
                                        {"reason": "None", "count": 1},
                                        {"reason": "Reservation", "count": 1},
                                        {"reason": "Resources", "count": 1},
                                        {"reason": "SpecialExit", "count": 1},
                                    ],
                                }
                            ],
                        }
                    ],
                },
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["queue"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            self.assertEqual(
                table_out.getvalue(),
                "cluster-a\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  cpu        2        6        JobHeldAdmin (2), None (1), Reservation (1), Resources (1), SpecialExit (1)\n",
            )

    def test_queue_reuses_five_minute_cache_and_refresh_refetches(self) -> None:
        initial_squeue = (
            "1001|alice|cpu|R|None|\n"
            "1002|bob|cpu|PD|Resources|\n"
            "1003|alice|cpu|PD|Resources|\n"
        )
        updated_squeue = (
            "1001|alice|cpu|R|None|\n"
            "1002|bob|cpu|R|None|\n"
            "1003|alice|cpu|R|None|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            shared_env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): initial_squeue,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )

            first_out = io.StringIO()
            first_err = io.StringIO()
            first_rc = main(
                ["queue"],
                env=shared_env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=first_out,
                stderr=first_err,
            )
            self.assertEqual(first_rc, 0)
            self.assertEqual(first_err.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(
                first_out.getvalue(),
                "cluster-a\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  cpu        1        2        Resources (2)\n",
            )

            runner._responses[("cluster-a", "squeue")] = updated_squeue
            t_cached = FIXED_NOW + timedelta(seconds=120)

            cached_out = io.StringIO()
            cached_err = io.StringIO()
            cached_rc = main(
                ["queue"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_cached,
                stdout=cached_out,
                stderr=cached_err,
            )
            self.assertEqual(cached_rc, 0)
            self.assertEqual(cached_err.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(
                cached_out.getvalue(),
                "cluster-a [cached 120s ago]\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  cpu        1        2        Resources (2)\n",
            )

            refresh_out = io.StringIO()
            refresh_err = io.StringIO()
            refresh_rc = main(
                ["queue", "--refresh"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_cached,
                stdout=refresh_out,
                stderr=refresh_err,
            )
            self.assertEqual(refresh_rc, 0)
            self.assertEqual(refresh_err.getvalue(), "")
            self.assertEqual(len(runner.calls), 4)
            self.assertEqual(
                refresh_out.getvalue(),
                "cluster-a\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  cpu        3        0        -\n",
            )

    def test_nonzero_squeue_stderr_uses_last_nonempty_line_after_warning(self) -> None:
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
                    ("cluster-a", "squeue"): CommandResult(
                        1,
                        "",
                        "squeue: warning: x\nsqueue: error: Invalid user\n",
                    )
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["queue", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )
            self.assertEqual(json_rc, 0)
            self.assertEqual(json_err.getvalue(), "")
            self.assertEqual(
                json.loads(json_out.getvalue()),
                {
                    "schema_version": 1,
                    "command": "queue",
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": None,
                            "stale": False,
                            "user_scoped": False,
                            "error": "squeue exited 1: squeue: error: Invalid user",
                            "partitions": [],
                        }
                    ],
                },
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["queue"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            self.assertNotIn("warning", table_out.getvalue())
            self.assertEqual(
                table_out.getvalue(),
                "cluster-a: squeue exited 1: squeue: error: Invalid user\n",
            )

    def test_queue_skips_dash_m_cluster_banner_line(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\nslurm_cluster = "prod"\n',
                encoding="utf-8",
            )
            squeue_fixture = (
                "CLUSTER: prod\n"
                "1001|alice|gpu|R|None|\n"
                "1002|bob|gpu|PD|Resources|\n"
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): squeue_fixture,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["queue"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            self.assertEqual(
                table_out.getvalue(),
                "cluster-a\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  gpu        1        1        Resources (1)\n",
            )

    def test_queue_groups_pending_reasons_by_prefix_before_first_comma(self) -> None:
        squeue_fixture = (
            "9001|alice|gpu|PD|ReqNodeNotAvail, UnavailableNodes:g[01-03]|\n"
            "9002|bob|gpu|PD|ReqNodeNotAvail, UnavailableNodes:g[02-03]|\n"
            "9003|alice|gpu|PD|Priority|\n"
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
                    ("cluster-a", "squeue"): squeue_fixture,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["queue", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )
            self.assertEqual(json_rc, 0)
            self.assertEqual(json_err.getvalue(), "")
            self.assertEqual(
                json.loads(json_out.getvalue())["clusters"][0]["partitions"],
                [
                    {
                        "partition": "gpu",
                        "running_jobs": 0,
                        "pending_jobs": 3,
                        "top_reasons": [
                            {"reason": "ReqNodeNotAvail", "count": 2},
                            {"reason": "Priority", "count": 1},
                        ],
                    }
                ],
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["queue"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            self.assertEqual(
                table_out.getvalue(),
                "cluster-a\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  gpu        0        3        ReqNodeNotAvail (2), Priority (1)\n",
            )

    def test_queue_allows_pipe_character_inside_last_reason_column(self) -> None:
        squeue_fixture = "1|alice|cpu|PD|Held by admin | maint|\n"
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
                    ("cluster-a", "squeue"): squeue_fixture,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["queue", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )
            self.assertEqual(json_rc, 0)
            self.assertEqual(json_err.getvalue(), "")
            self.assertEqual(
                json.loads(json_out.getvalue())["clusters"][0]["partitions"],
                [
                    {
                        "partition": "cpu",
                        "running_jobs": 0,
                        "pending_jobs": 1,
                        "top_reasons": [
                            {"reason": "Held by admin | maint", "count": 1},
                        ],
                    }
                ],
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["queue"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            self.assertEqual(
                table_out.getvalue(),
                "cluster-a [user-scoped]\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  cpu        0        1        Held by admin | maint (1)\n",
            )


if __name__ == "__main__":
    unittest.main()
