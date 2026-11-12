from __future__ import annotations

from datetime import UTC, datetime
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import sglance
from sglance import CommandResult, main
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

            for cli_argv in (["status"], ["status", "--json"]):
                exit_code = main(
                    cli_argv,
                    env=env,
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

    def test_no_injected_runner_prints_stderr_error_and_exits_two(self) -> None:
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
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["status"],
                env=env,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(
                stderr.getvalue(),
                "sglance: error: no transport adapter available yet\n",
            )

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
                    env=env,
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


if __name__ == "__main__":
    unittest.main()

