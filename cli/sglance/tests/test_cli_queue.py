from __future__ import annotations

from datetime import UTC, datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sglance import CommandResult, TransportError, main
from tests.fakes import FakeRunner

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
            runner = FakeRunner({("cluster-a", "squeue"): squeue_output})
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
                    )
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
            runner = FakeRunner({("cluster-a", "squeue"): squeue_output})

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["queue", "--json"],
                env=env,
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
                env=env,
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
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): "",
                    ("cluster-b", "squeue"): "   \n\n",
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["queue", "--json"],
                env=env,
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
                env=env,
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
                    "squeue: error: Slurm controller not responding\nextra line\n",
                ),
                "squeue: error: Slurm controller not responding",
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
                env = {
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "HOME": str(root / "home"),
                }
                runner = FakeRunner(
                    {
                        ("cluster-a", "squeue"): "100|alice|cpu|R|None|\n101|bob|cpu|PD|Resources|\n",
                        ("cluster-b", "squeue"): cluster_b_response,
                        ("cluster-c", "squeue"): CommandResult(5, "", "   \n"),
                    }
                )

                json_out = io.StringIO()
                json_err = io.StringIO()
                json_rc = main(
                    ["queue", "--json"],
                    env=env,
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
                    env=env,
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
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            filtered_runner = FakeRunner(
                {("cluster-b", "squeue"): "200|alice|gpu|PD|Priority|\n"}
            )
            for argv in (["queue", "--cluster", "cluster-b"], ["--cluster", "cluster-b", "queue"]):
                out = io.StringIO()
                err = io.StringIO()
                rc = main(
                    argv,
                    env=env,
                    runner=filtered_runner,
                    clock=lambda: FIXED_NOW,
                    stdout=out,
                    stderr=err,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(err.getvalue(), "")
                self.assertEqual(
                    out.getvalue(),
                    "cluster-b\n"
                    "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                    "  gpu        0        1        Priority (1)\n",
                )
            self.assertEqual(
                filtered_runner.calls,
                [
                    ("cluster-b", "ssh", "cluster-b", dash_m_argv),
                    ("cluster-b", "ssh", "cluster-b", dash_m_argv),
                ],
            )

            for bad_argv in (["queue", "--cluster", "cluster-unknown"], ["queue", "--unknown-flag"]):
                bad_runner = FakeRunner()
                bad_out = io.StringIO()
                bad_err = io.StringIO()
                bad_rc = main(
                    bad_argv,
                    env=env,
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
            ("empty-user", "1001||cpu|R|None|\n"),
            ("empty-partition", "1001|alice||R|None|\n"),
            ("empty-multipartition-segment", "1001|alice|,gpu|PD|Resources|\n"),
            ("invalid-state-compact", "1001|alice|cpu|BOGUS|None|\n"),
            ("empty-pending-reason", "1001|alice|cpu|PD||\n"),
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
                env = {
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "HOME": str(root / "home"),
                }
                runner = FakeRunner(
                    {
                        ("cluster-a", "squeue"): bad_stdout,
                        ("cluster-b", "squeue"): "201|bob|gpu|R|None|\n",
                    }
                )

                json_out = io.StringIO()
                json_err = io.StringIO()
                json_rc = main(
                    ["queue", "--json"],
                    env=env,
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
                    env=env,
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=table_out,
                    stderr=table_err,
                )
                self.assertEqual(table_rc, 0)
                self.assertEqual(table_err.getvalue(), "")
                self.assertIn(
                    f"cluster-a: {cluster_a['error']}\n\ncluster-b\n",
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
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner({("cluster-a", "squeue"): squeue_output})
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
                        "running_jobs": 1,
                        "pending_jobs": 1,
                        "top_reasons": [{"reason": "Resources", "count": 1}],
                    },
                    {
                        "partition": "debug",
                        "running_jobs": 0,
                        "pending_jobs": 0,
                        "top_reasons": [],
                    },
                ],
            )


if __name__ == "__main__":
    unittest.main()
