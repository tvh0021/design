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


class PrivateDataScopingCliTests(unittest.TestCase):
    def test_queue_marks_user_scoped_when_private_data_has_jobs_or_all_even_when_empty(self) -> None:
        scontrol_jobs = (
            "Configuration data as of 2026-11-12T20:00:00\n"
            "AccountingStorageEnforce = associations,limits,qos\n"
            "PrivateData             = jobs,usage\n"
            "SlurmctldPort           = 6817\n"
        )
        scontrol_all = (
            "CLUSTER: prod\n"
            "Configuration data as of 2026-11-12T20:00:00\n"
            "PrivateData             = all\n"
        )
        squeue_cluster_b = (
            "CLUSTER: prod\n"
            "1001|alice|gpu|R|None|\n"
            "1002|bob|gpu|PD|Resources|\n"
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
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): scontrol_jobs,
                    ("cluster-b", "squeue"): squeue_cluster_b,
                    ("cluster-b", "scontrol"): scontrol_all,
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
            cluster_a_calls = [call for call in runner.calls if call[0] == "cluster-a"]
            cluster_b_calls = [call for call in runner.calls if call[0] == "cluster-b"]
            self.assertEqual(
                cluster_a_calls,
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
                        ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "show", "config"],
                    ),
                ],
            )
            self.assertEqual(
                cluster_b_calls,
                [
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
                json.loads(json_out.getvalue()),
                {
                    "schema_version": 1,
                    "command": "queue",
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": True,
                            "error": None,
                            "partitions": [],
                        },
                        {
                            "cluster": "cluster-b",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": True,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "gpu",
                                    "running_jobs": 1,
                                    "pending_jobs": 1,
                                    "top_reasons": [
                                        {"reason": "Resources", "count": 1},
                                    ],
                                }
                            ],
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
                "cluster-a [user-scoped]\n"
                "  0 running, 0 pending\n"
                "\n"
                "cluster-b [user-scoped]\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  gpu        1        1        Resources (1)\n",
            )

    def test_queue_falls_back_to_distinct_user_count_when_scontrol_fails_or_omits_jobs_and_all(self) -> None:
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
                    ("cluster-a", "squeue"): (
                        "1001|alice|cpu|R|None|\n"
                        "1002_1|alice|cpu|PD|Resources|\n"
                        "1002_2|alice|cpu|PD|Resources|\n"
                    ),
                    ("cluster-a", "scontrol"): (
                        "PrivateData             = usage,accounts,users,nodes,partitions,reservations\n"
                    ),
                    ("cluster-b", "squeue"): "2001|alice|gpu|R|None|\n",
                    ("cluster-b", "scontrol"): CommandResult(
                        1, "", "scontrol: error: Permission denied\n"
                    ),
                    ("cluster-c", "squeue"): "3001|bob|debug|PD|Priority|\n",
                    ("cluster-c", "scontrol"): (
                        "Configuration data as of 2026-11-12T20:00:00\n"
                        "SlurmctldPort           = 6817\n"
                    ),
                    ("cluster-d", "squeue"): (
                        "4001|alice|cpu|R|None|\n"
                        "4002|bob|cpu|PD|Priority|\n"
                    ),
                    ("cluster-d", "scontrol"): TransportError("timed out after 25s"),
                    ("cluster-e", "squeue"): "",
                    ("cluster-e", "scontrol"): "PrivateData             = usage,accounts\n",
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
                            "user_scoped": True,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "running_jobs": 1,
                                    "pending_jobs": 2,
                                    "top_reasons": [
                                        {"reason": "Resources", "count": 2},
                                    ],
                                }
                            ],
                        },
                        {
                            "cluster": "cluster-b",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": True,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "gpu",
                                    "running_jobs": 1,
                                    "pending_jobs": 0,
                                    "top_reasons": [],
                                }
                            ],
                        },
                        {
                            "cluster": "cluster-c",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": True,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "debug",
                                    "running_jobs": 0,
                                    "pending_jobs": 1,
                                    "top_reasons": [
                                        {"reason": "Priority", "count": 1},
                                    ],
                                }
                            ],
                        },
                        {
                            "cluster": "cluster-d",
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
                                        {"reason": "Priority", "count": 1},
                                    ],
                                }
                            ],
                        },
                        {
                            "cluster": "cluster-e",
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
            self.assertEqual(
                table_out.getvalue(),
                "cluster-a [user-scoped]\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  cpu        1        2        Resources (2)\n"
                "\n"
                "cluster-b [user-scoped]\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  gpu        1        0        -\n"
                "\n"
                "cluster-c [user-scoped]\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  debug      0        1        Priority (1)\n"
                "\n"
                "cluster-d\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  cpu        1        1        Priority (1)\n"
                "\n"
                "cluster-e\n"
                "  0 running, 0 pending\n",
            )

    def test_queue_counts_distinct_users_across_transient_squeue_rows_and_survives_scontrol_ssh_255(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): (
                        "1001|alice|cpu|R|None|\n"
                        "1002|bob|cpu|CG|None|\n"
                    ),
                    ("cluster-a", "scontrol"): CommandResult(
                        255, "", "ssh: Connection reset by peer\n"
                    ),
                    ("cluster-b", "squeue"): "2001|alice|cpu|CG|None|\n",
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
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "running_jobs": 1,
                                    "pending_jobs": 0,
                                    "top_reasons": [],
                                }
                            ],
                        },
                        {
                            "cluster": "cluster-b",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": True,
                            "error": None,
                            "partitions": [],
                        },
                    ],
                },
            )

    def test_queue_caches_user_scoped_flag_without_extra_scontrol_calls_and_refresh_updates_it(self) -> None:
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
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData             = jobs\n",
                }
            )

            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["queue"],
                env=shared_env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(
                out1.getvalue(),
                "cluster-a [user-scoped]\n"
                "  0 running, 0 pending\n",
            )

            # Mutate fake responses before cache hit to prove neither squeue nor scontrol runs.
            runner._responses[("cluster-a", "squeue")] = (
                "1001|alice|cpu|R|None|\n"
                "1002|bob|cpu|R|None|\n"
            )
            runner._responses[("cluster-a", "scontrol")] = "PrivateData             = none\n"

            t_hit = FIXED_NOW + timedelta(seconds=90)
            out2_table, err2_table = io.StringIO(), io.StringIO()
            rc2_table = main(
                ["queue"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_hit,
                stdout=out2_table,
                stderr=err2_table,
            )
            self.assertEqual(rc2_table, 0)
            self.assertEqual(err2_table.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(
                out2_table.getvalue(),
                "cluster-a [user-scoped] [cached 90s ago]\n"
                "  0 running, 0 pending\n",
            )

            out2_json, err2_json = io.StringIO(), io.StringIO()
            rc2_json = main(
                ["queue", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_hit,
                stdout=out2_json,
                stderr=err2_json,
            )
            self.assertEqual(rc2_json, 0)
            self.assertEqual(err2_json.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(
                json.loads(out2_json.getvalue())["clusters"][0],
                {
                    "cluster": "cluster-a",
                    "fetched_at": "2026-11-12T20:00:00Z",
                    "stale": True,
                    "user_scoped": True,
                    "error": None,
                    "partitions": [],
                },
            )

            # --refresh bypasses cache, calls squeue + scontrol, and clears user_scoped.
            t_refresh = FIXED_NOW + timedelta(seconds=120)
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["queue", "--refresh"],
                env=shared_env,
                runner=runner,
                clock=lambda: t_refresh,
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertEqual(len(runner.calls), 4)
            self.assertEqual(
                out3.getvalue(),
                "cluster-a\n"
                "  PARTITION  RUNNING  PENDING  TOP REASONS\n"
                "  cpu        2        0        -\n",
            )

    def test_status_never_queries_scontrol_and_always_reports_user_scoped_false(self) -> None:
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
                    ("cluster-a", "sinfo"): "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n",
                }
            )

            json_out, json_err = io.StringIO(), io.StringIO()
            json_rc = main(
                ["status", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )
            self.assertEqual(json_rc, 0)
            self.assertEqual(json_err.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(runner.calls[0][3][2], "sinfo")
            self.assertFalse(json.loads(json_out.getvalue())["clusters"][0]["user_scoped"])

            table_out, table_err = io.StringIO(), io.StringIO()
            table_rc = main(
                ["status"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertNotIn("[user-scoped]", table_out.getvalue())


if __name__ == "__main__":
    unittest.main()
