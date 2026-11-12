from __future__ import annotations

from datetime import UTC, datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest

import sglance
from sglance import CommandResult, TransportError, main
from tests.fakes import FakeRunner

FIXED_NOW = datetime(2026, 11, 12, 20, 0, 0, tzinfo=UTC)

SYNTHETIC_SINFO_SINGLE_PARTITION = (
    "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n"
    "cpu|node02|mixed|48/16/0/64|(null)|(null)|\n"
    "cpu|node03|idle|0/64/0/64|(null)|(null)|\n"
    "cpu|node04|idle|0/64/0/64|(null)|(null)|\n"
)


class StatusCliTests(unittest.TestCase):
    def test_public_package_omits_test_doubles(self) -> None:
        self.assertEqual(
            set(sglance.__all__),
            {"CommandResult", "TransportError", "main"},
        )
        self.assertFalse(hasattr(sglance, "FakeRunner"))

    def test_status_json_reports_single_partition_cpu_and_node_utilization(self) -> None:
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
            runner = FakeRunner({("cluster-a", "sinfo"): SYNTHETIC_SINFO_SINGLE_PARTITION})
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["status", "--json"],
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
                            "sinfo",
                            "-h",
                            "-N",
                            "-O",
                            "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|",
                        ],
                    )
                ],
            )
            self.assertEqual(
                json.loads(raw_stdout),
                {
                    "schema_version": 1,
                    "command": "status",
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
                                    "default": False,
                                    "cpus_alloc": 112,
                                    "cpus_total": 256,
                                    "cpus_utilization": 0.4375,
                                    "gpus_alloc": 0,
                                    "gpus_total": 0,
                                    "gpus_utilization": None,
                                    "nodes_alloc": 1,
                                    "nodes_mixed": 1,
                                    "nodes_idle": 2,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 4,
                                }
                            ],
                        }
                    ],
                },
            )

    def test_status_table_renders_plain_text_cpu_and_node_counts_without_ansi(self) -> None:
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
            runner = FakeRunner({("cluster-a", "sinfo"): SYNTHETIC_SINFO_SINGLE_PARTITION})
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            rendered = stdout.getvalue()
            self.assertNotIn("\x1b", rendered)
            expected_table = (
                "cluster-a\n"
                "  PARTITION  CPUS           GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu        112/256 (44%)  -     4      1      1      2     0     0        0         0\n"
            )
            self.assertEqual(rendered, expected_table)

    def test_unknown_cli_flag_exits_two_with_stderr_diagnostic(self) -> None:
        for bad_argv in (["status", "--unknown-flag"], ["--unknown-flag"], ["unknown-subcommand"], []):
            with self.subTest(argv=bad_argv), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(
                    '[[clusters]]\nname = "cluster-a"\n',
                    encoding="utf-8",
                )
                env = {
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "HOME": str(root / "home"),
                }
                runner = FakeRunner()
                stdout = io.StringIO()
                stderr = io.StringIO()

                exit_code = main(
                    bad_argv,
                    env=env,
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=stdout,
                    stderr=stderr,
                )

                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotEqual(stderr.getvalue().strip(), "")
                self.assertEqual(runner.calls, [])

    def test_status_deduplicates_repeated_partition_node_rows(self) -> None:
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
            duplicate_sinfo = (
                "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n"
                "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n"
                "cpu|node02|idle|0/64/0/64|(null)|(null)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): duplicate_sinfo})
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            parsed = json.loads(stdout.getvalue())
            partition = parsed["clusters"][0]["partitions"][0]
            self.assertEqual(partition["cpus_alloc"], 64)
            self.assertEqual(partition["cpus_total"], 128)
            self.assertEqual(partition["cpus_utilization"], 0.5)
            self.assertEqual(partition["nodes_alloc"], 1)
            self.assertEqual(partition["nodes_idle"], 1)
            self.assertEqual(partition["nodes_total"], 2)

    def test_cli_commands_only_dispatch_allowlisted_readonly_slurm_argv(self) -> None:
        # AllowlistedRunner wrapped in main() is the single production allowlist
        # enforcement point. Because no CLI subcommand constructs a disallowed
        # argv, rejection of a disallowed argv cannot be triggered through the
        # main(argv, ...) seam without reaching into internals; here we assert
        # that every CLI invocation reaches the injected runner only with
        # allowlisted read-only Slurm command vectors.
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
            runner = FakeRunner({("cluster-a", "sinfo"): SYNTHETIC_SINFO_SINGLE_PARTITION})

            for idx, cli_argv in enumerate((["status"], ["status", "--json"])):
                exit_code = main(
                    cli_argv,
                    env={**env, "XDG_CACHE_HOME": str(root / f"cache-{idx}")},
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
                self.assertEqual(exit_code, 0)

            self.assertEqual(len(runner.calls), 2)
            for cluster_name, transport_kind, target, argv in runner.calls:
                self.assertEqual(cluster_name, "cluster-a")
                self.assertEqual(transport_kind, "local")
                self.assertIsNone(target)
                self.assertEqual(argv[:2], ["env", "SLURM_TIME_FORMAT=%s"])
                self.assertIn(argv[2], {"sinfo", "squeue", "sacct", "scontrol"})
                if argv[2] == "scontrol":
                    rest = argv[3:]
                    if len(rest) >= 2 and rest[0] == "-M":
                        rest = rest[2:]
                    self.assertEqual(rest, ["show", "config"])

    def test_nonzero_sinfo_returncode_reports_inline_cluster_error(self) -> None:
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
                    ("cluster-a", "sinfo"): CommandResult(
                        1,
                        "",
                        "sinfo: error: Unable to contact slurm controller\nmore details\n",
                    ),
                    ("cluster-b", "sinfo"): CommandResult(7, "", "   \n"),
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["status", "--json"],
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
                    "command": "status",
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": None,
                            "stale": False,
                            "user_scoped": False,
                            "error": "sinfo: error: Unable to contact slurm controller",
                            "partitions": [],
                        },
                        {
                            "cluster": "cluster-b",
                            "fetched_at": None,
                            "stale": False,
                            "user_scoped": False,
                            "error": "sinfo exited 7",
                            "partitions": [],
                        },
                    ],
                },
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["status"],
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
                "cluster-a: sinfo: error: Unable to contact slurm controller\n\n"
                "cluster-b: sinfo exited 7\n",
            )

    def test_malformed_sinfo_stdout_rows_report_inline_cluster_error(self) -> None:
        malformed_cases = [
            ("wrong-field-count", "cpu|node01|idle|0/64/0/64|\n"),
            (
                "stray-sinfo-error-on-stdout",
                "sinfo: error: slurm_receive_msg: Socket timed out\n"
                "cpu|node01|idle|0/64/0/64|(null)|(null)|\n",
            ),
            ("bad-cpus-state-arity", "cpu|node01|idle|0/64|(null)|(null)|\n"),
            ("bad-cpus-state-int", "cpu|node01|idle|0/na/0/64|(null)|(null)|\n"),
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
                        ("cluster-a", "sinfo"): bad_stdout,
                        ("cluster-b", "sinfo"): "cpu|node01|idle|0/64/0/64|(null)|(null)|\n",
                    }
                )

                json_out = io.StringIO()
                json_err = io.StringIO()
                json_rc = main(
                    ["status", "--json"],
                    env=env,
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=json_out,
                    stderr=json_err,
                )

                self.assertEqual(json_rc, 0)
                self.assertEqual(json_err.getvalue(), "")
                payload = json.loads(json_out.getvalue())
                cluster_a, cluster_b = payload["clusters"]
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
                self.assertEqual(len(cluster_b["partitions"]), 1)
                self.assertEqual(cluster_b["partitions"][0]["cpus_total"], 64)

                table_out = io.StringIO()
                table_err = io.StringIO()
                table_rc = main(
                    ["status"],
                    env={**env, "XDG_CACHE_HOME": str(root / "cache-table")},
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=table_out,
                    stderr=table_err,
                )
                self.assertEqual(table_rc, 0)
                self.assertEqual(table_err.getvalue(), "")
                self.assertIn(f"cluster-a: {cluster_a['error']}\n\ncluster-b\n", table_out.getvalue())

    def test_status_uses_total_cpus_with_other_state_and_deduplicates_per_partition(self) -> None:
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
            # node01 has CPUsState 16/32/16/64 (O=16 != 0, so T=64 != A+I=48),
            # is duplicated inside cpu, and also belongs to debug.
            sinfo_fixture = (
                "cpu|node01|mixed|16/32/16/64|(null)|(null)|\n"
                "cpu|node01|mixed|16/32/16/64|(null)|(null)|\n"
                "cpu|node02|allocated|32/0/0/32|(null)|(null)|\n"
                "debug|node01|mixed|16/32/16/64|(null)|(null)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_fixture})
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            partitions = json.loads(stdout.getvalue())["clusters"][0]["partitions"]
            self.assertEqual(
                partitions,
                [
                    {
                        "partition": "cpu",
                        "default": False,
                        "cpus_alloc": 48,
                        "cpus_total": 96,
                        "cpus_utilization": 0.5,
                        "gpus_alloc": 0,
                        "gpus_total": 0,
                        "gpus_utilization": None,
                        "nodes_alloc": 1,
                        "nodes_mixed": 1,
                        "nodes_idle": 0,
                        "nodes_down": 0,
                        "nodes_drained": 0,
                        "nodes_draining": 0,
                        "nodes_other": 0,
                        "nodes_total": 2,
                    },
                    {
                        "partition": "debug",
                        "default": False,
                        "cpus_alloc": 16,
                        "cpus_total": 64,
                        "cpus_utilization": 0.25,
                        "gpus_alloc": 0,
                        "gpus_total": 0,
                        "gpus_utilization": None,
                        "nodes_alloc": 0,
                        "nodes_mixed": 1,
                        "nodes_idle": 0,
                        "nodes_down": 0,
                        "nodes_drained": 0,
                        "nodes_draining": 0,
                        "nodes_other": 0,
                        "nodes_total": 1,
                    },
                ],
            )

    def test_fake_runner_rejects_bare_cluster_key_and_unmatched_requests(self) -> None:
        with self.assertRaises(ValueError):
            FakeRunner({"cluster-a": SYNTHETIC_SINFO_SINGLE_PARTITION})  # type: ignore[dict-item]

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
            runner = FakeRunner({("cluster-a", "squeue"): ""})
            with self.assertRaises(KeyError) as ctx:
                main(
                    ["status"],
                    env=env,
                    runner=runner,
                    clock=lambda: FIXED_NOW,
                    stdout=io.StringIO(),
                    stderr=io.StringIO(),
                )
            self.assertIn("cluster-a", str(ctx.exception))
            self.assertIn("sinfo", str(ctx.exception))

    def test_slurm_cluster_entry_passes_dash_m_immediately_after_binary(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-b"\nssh = "login.example.edu"\nslurm_cluster = "prod"\n',
                encoding="utf-8",
            )
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner({("cluster-b", "sinfo"): SYNTHETIC_SINFO_SINGLE_PARTITION})
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(
                runner.calls,
                [
                    (
                        "cluster-b",
                        "ssh",
                        "login.example.edu",
                        [
                            "env",
                            "SLURM_TIME_FORMAT=%s",
                            "sinfo",
                            "-M",
                            "prod",
                            "-h",
                            "-N",
                            "-O",
                            "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|",
                        ],
                    )
                ],
            )

    def test_cluster_filter_queries_only_selected_entry_and_rejects_unknown_name(self) -> None:
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
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner({("cluster-b", "sinfo"): SYNTHETIC_SINFO_SINGLE_PARTITION})
            json_out = io.StringIO()
            json_err = io.StringIO()

            exit_code = main(
                ["status", "--cluster", "cluster-b", "--json"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json_err.getvalue(), "")
            self.assertEqual(
                runner.calls,
                [
                    (
                        "cluster-b",
                        "local",
                        None,
                        [
                            "env",
                            "SLURM_TIME_FORMAT=%s",
                            "sinfo",
                            "-h",
                            "-N",
                            "-O",
                            "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|",
                        ],
                    )
                ],
            )
            payload = json.loads(json_out.getvalue())
            self.assertEqual([c["cluster"] for c in payload["clusters"]], ["cluster-b"])

            table_out = io.StringIO()
            table_err = io.StringIO()
            prefix_rc = main(
                ["--cluster", "cluster-b", "status"],
                env={**env, "XDG_CACHE_HOME": str(root / "cache-table")},
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(prefix_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            self.assertEqual(
                table_out.getvalue(),
                "cluster-b\n"
                "  PARTITION  CPUS           GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu        112/256 (44%)  -     4      1      1      2     0     0        0         0\n",
            )

            unknown_runner = FakeRunner()
            unknown_out = io.StringIO()
            unknown_err = io.StringIO()
            unknown_rc = main(
                ["status", "--cluster", "cluster-unknown"],
                env=env,
                runner=unknown_runner,
                clock=lambda: FIXED_NOW,
                stdout=unknown_out,
                stderr=unknown_err,
            )

            self.assertEqual(unknown_rc, 2)
            self.assertEqual(unknown_out.getvalue(), "")
            self.assertIn("cluster-unknown", unknown_err.getvalue())
            self.assertEqual(unknown_runner.calls, [])

    def test_three_clusters_isolate_ssh_timeout_and_exit_255_preserving_config_order(self) -> None:
        sinfo_cluster_a = (
            "cpu*|node01|allocated|64/0/0/64|(null)|(null)|\n"
            "cpu*|node02|idle|0/64/0/64|(null)|(null)|\n"
        )
        sinfo_cluster_c = "debug|node10|mixed|16/16/0/32|(null)|(null)|\n"
        expected_sinfo_argv = [
            "env",
            "SLURM_TIME_FORMAT=%s",
            "sinfo",
            "-h",
            "-N",
            "-O",
            "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|",
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
        ]

        for label, cluster_b_response, expected_error in failure_cases:
            with self.subTest(case=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(
                    '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                    '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n\n'
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
                        ("cluster-a", "sinfo"): sinfo_cluster_a,
                        ("cluster-b", "sinfo"): cluster_b_response,
                        ("cluster-c", "sinfo"): sinfo_cluster_c,
                    }
                )

                json_out = io.StringIO()
                json_err = io.StringIO()
                json_rc = main(
                    ["status", "--json"],
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
                        ("cluster-a", "ssh", "login.example.edu", expected_sinfo_argv),
                        ("cluster-b", "ssh", "cluster-b", expected_sinfo_argv),
                        ("cluster-c", "local", None, expected_sinfo_argv),
                    ],
                )
                self.assertEqual(
                    json.loads(json_out.getvalue()),
                    {
                        "schema_version": 1,
                        "command": "status",
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
                                        "default": True,
                                        "cpus_alloc": 64,
                                        "cpus_total": 128,
                                        "cpus_utilization": 0.5,
                                        "gpus_alloc": 0,
                                        "gpus_total": 0,
                                        "gpus_utilization": None,
                                        "nodes_alloc": 1,
                                        "nodes_mixed": 0,
                                        "nodes_idle": 1,
                                        "nodes_down": 0,
                                        "nodes_drained": 0,
                                        "nodes_draining": 0,
                                        "nodes_other": 0,
                                        "nodes_total": 2,
                                    }
                                ],
                            },
                            {
                                "cluster": "cluster-b",
                                "fetched_at": None,
                                "stale": False,
                                "user_scoped": False,
                                "error": expected_error,
                                "partitions": [],
                            },
                            {
                                "cluster": "cluster-c",
                                "fetched_at": "2026-11-12T20:00:00Z",
                                "stale": False,
                                "user_scoped": False,
                                "error": None,
                                "partitions": [
                                    {
                                        "partition": "debug",
                                        "default": False,
                                        "cpus_alloc": 16,
                                        "cpus_total": 32,
                                        "cpus_utilization": 0.5,
                                        "gpus_alloc": 0,
                                        "gpus_total": 0,
                                        "gpus_utilization": None,
                                        "nodes_alloc": 0,
                                        "nodes_mixed": 1,
                                        "nodes_idle": 0,
                                        "nodes_down": 0,
                                        "nodes_drained": 0,
                                        "nodes_draining": 0,
                                        "nodes_other": 0,
                                        "nodes_total": 1,
                                    }
                                ],
                            },
                        ],
                    },
                )

                table_out = io.StringIO()
                table_err = io.StringIO()
                table_rc = main(
                    ["status"],
                    env={**env, "XDG_CACHE_HOME": str(root / "cache-table")},
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
                    "  PARTITION  CPUS          GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                    "  cpu*       64/128 (50%)  -     2      1      0      1     0     0        0         0\n"
                    "\n"
                    f"cluster-b: {expected_error}\n"
                    "\n"
                    "cluster-c\n"
                    "  PARTITION  CPUS         GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                    "  debug      16/32 (50%)  -     1      0      1      0     0     0        0         0\n",
                )

    def test_multi_cluster_queries_execute_in_parallel_and_preserve_config_order(self) -> None:
        barrier = threading.Barrier(3, timeout=0.5)

        class _ConcurrentFakeRunner(FakeRunner):
            def run(self, cluster_entry: object, argv: list[str]) -> CommandResult:  # type: ignore[override]
                try:
                    barrier.wait()
                except threading.BrokenBarrierError as exc:
                    raise TransportError("clusters were not queried concurrently") from exc
                if getattr(cluster_entry, "name", "") == "cluster-c":
                    time.sleep(0.02)
                return super().run(cluster_entry, argv)  # type: ignore[arg-type]

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-c"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n',
                encoding="utf-8",
            )
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = _ConcurrentFakeRunner(
                {
                    ("cluster-c", "sinfo"): "cpu|node01|allocated|32/0/0/32|(null)|(null)|\n",
                    ("cluster-a", "sinfo"): "cpu|node02|idle|0/64/0/64|(null)|(null)|\n",
                    ("cluster-b", "sinfo"): "cpu|node03|mixed|16/16/0/32|(null)|(null)|\n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(len(runner.calls), 3)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                [(c["cluster"], c["error"], c["partitions"][0]["cpus_alloc"]) for c in payload["clusters"]],
                [
                    ("cluster-c", None, 32),
                    ("cluster-a", None, 0),
                    ("cluster-b", None, 16),
                ],
            )


if __name__ == "__main__":
    unittest.main()





