from __future__ import annotations

from datetime import UTC, datetime
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest import mock

import sglance
from sglance import CommandResult, TransportError, main
from sglance.models import ClusterEntry
from sglance.runner import AllowlistError, AllowlistedRunner, validate_allowlist
from tests.fakes import FakeRunner, UnmatchedFakeRequest, make_test_env

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

    def test_allowlist_rejects_disallowed_binaries_missing_prefix_non_config_scontrol_and_dash_m_flags(
        self,
    ) -> None:
        cluster_entry = ClusterEntry(name="cluster-a", ssh="login.example.edu")
        rejected_vectors: list[tuple[str, list[str]]] = [
            ("disallowed-rm", ["env", "SLURM_TIME_FORMAT=%s", "rm", "-rf", "/tmp/work"]),
            ("disallowed-scancel", ["env", "SLURM_TIME_FORMAT=%s", "scancel", "1001"]),
            ("disallowed-sbatch", ["env", "SLURM_TIME_FORMAT=%s", "sbatch", "--test-only", "job.sh"]),
            ("missing-env-prefix", ["sinfo", "-h", "-N"]),
            ("bare-env-without-time-format", ["env", "sinfo", "-h"]),
            ("wrong-time-format-env", ["env", "SLURM_TIME_FORMAT=standard", "sinfo", "-h"]),
            ("too-short-argv", ["env", "SLURM_TIME_FORMAT=%s"]),
            ("scontrol-show-node", ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "show", "node"]),
            (
                "scontrol-update",
                ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "update", "NodeName=node01", "State=DRAIN"],
            ),
            (
                "scontrol-extra-arg",
                ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "show", "config", "SlurmctldHost"],
            ),
            ("scontrol-no-args", ["env", "SLURM_TIME_FORMAT=%s", "scontrol"]),
            (
                "scontrol-dash-m-leading-dash",
                ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "-M", "-cluster", "show", "config"],
            ),
            (
                "sinfo-dash-m-leading-dash",
                ["env", "SLURM_TIME_FORMAT=%s", "sinfo", "-M", "-cluster", "-h"],
            ),
            (
                "squeue-dash-m-leading-dash",
                ["env", "SLURM_TIME_FORMAT=%s", "squeue", "-M", "--all", "-h"],
            ),
            (
                "sacct-dash-m-missing-value",
                ["env", "SLURM_TIME_FORMAT=%s", "sacct", "-M"],
            ),
        ]
        for label, bad_argv in rejected_vectors:
            with self.subTest(case=label):
                fake_runner = FakeRunner()
                guarded = AllowlistedRunner(fake_runner)
                with self.assertRaises(AllowlistError):
                    validate_allowlist(bad_argv)
                with self.assertRaises(AllowlistError):
                    guarded.run(cluster_entry, bad_argv)
                self.assertEqual(fake_runner.calls, [])

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
                        "sinfo: warning: ignoring deprecated config key\n"
                        "sinfo: error: Unable to contact slurm controller\n",
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
                            "error": "sinfo exited 1: error: Unable to contact slurm controller",
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
                "cluster-a: sinfo exited 1: error: Unable to contact slurm controller\n\n"
                "cluster-b: sinfo exited 7\n",
            )

    def test_nonzero_sinfo_stderr_uses_last_nonempty_line_after_warning(self) -> None:
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
                    ("cluster-a", "sinfo"): CommandResult(
                        1,
                        "",
                        "Warning: Permanently added 'login.example.edu' (ED25519) to known hosts.\n"
                        "\n"
                        "  sinfo: error: slurm_receive_msg: Zero Bytes were transmitted or received  \n"
                        "   \n",
                    )
                }
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
                "cluster-a: sinfo exited 1: error: slurm_receive_msg: Zero Bytes were transmitted or received\n",
            )

    def test_command_failure_strips_leading_tool_prefix_without_altering_other_lines(self) -> None:
        cases = (
            (
                "sinfo-prefixed",
                ["status", "--json"],
                {("cluster-a", "sinfo"): CommandResult(1, "", "sinfo: error: controller down\n")},
                "sinfo exited 1: error: controller down",
            ),
            (
                "squeue-prefixed",
                ["queue", "--json"],
                {("cluster-a", "squeue"): CommandResult(2, "", "squeue: fatal: invalid partition\n")},
                "squeue exited 2: fatal: invalid partition",
            ),
            (
                "sacct-prefixed",
                ["wait", "--json"],
                {
                    ("cluster-a", "sacct"): CommandResult(1, "", "sacct: error: accounting disabled\n"),
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                },
                "sacct exited 1: error: accounting disabled",
            ),
            (
                "non-matching-prefix-preserved",
                ["status", "--json"],
                {("cluster-a", "sinfo"): CommandResult(1, "", "sinfo_wrapper: error: wrapped\n")},
                "sinfo exited 1: sinfo_wrapper: error: wrapped",
            ),
            (
                "other-daemon-line-preserved",
                ["status", "--json"],
                {("cluster-a", "sinfo"): CommandResult(1, "", "slurm_receive_msg: Socket timed out\n")},
                "sinfo exited 1: slurm_receive_msg: Socket timed out",
            ),
        )
        for label, argv, responses, expected_error in cases:
            with self.subTest(case=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(
                    '[[clusters]]\nname = "cluster-a"\nssh = "cluster-a.example"\n',
                    encoding="utf-8",
                )
                out, err = io.StringIO(), io.StringIO()
                rc = main(
                    argv,
                    env=make_test_env(root),
                    runner=FakeRunner(responses),
                    clock=lambda: FIXED_NOW,
                    stdout=out,
                    stderr=err,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(err.getvalue(), "")
                payload = json.loads(out.getvalue())
                if label == "sacct-prefixed":
                    self.assertIsNone(payload["clusters"][0]["error"])
                    self.assertEqual(payload["clusters"][0]["history_error"], expected_error)
                else:
                    self.assertEqual(payload["clusters"][0]["error"], expected_error)

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
                self.assertRegex(cluster_a["error"], r"\b(?:sinfo|GRES)\b")
                for exc_name in ("KeyError", "ValueError", "IndexError", "TypeError", "AttributeError"):
                    self.assertNotIn(exc_name, cluster_a["error"])
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
            with self.assertRaises(UnmatchedFakeRequest) as ctx:
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

    def test_unexpected_keyerror_or_valueerror_isolates_to_single_cluster_error(self) -> None:
        cases: list[tuple[str, Exception, str]] = [
            ("keyerror", KeyError("missing_field"), "'missing_field'"),
            ("valueerror", ValueError("unexpected token"), "unexpected token"),
        ]
        for label, raised_exc, expected_error in cases:
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
                        ("cluster-a", "sinfo"): raised_exc,
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
                                "error": expected_error,
                                "partitions": [],
                            },
                            {
                                "cluster": "cluster-b",
                                "fetched_at": "2026-11-12T20:00:00Z",
                                "stale": False,
                                "user_scoped": False,
                                "error": None,
                                "partitions": [
                                    {
                                        "partition": "cpu",
                                        "default": False,
                                        "cpus_alloc": 0,
                                        "cpus_total": 64,
                                        "cpus_utilization": 0.0,
                                        "gpus_alloc": 0,
                                        "gpus_total": 0,
                                        "gpus_utilization": None,
                                        "nodes_alloc": 0,
                                        "nodes_mixed": 0,
                                        "nodes_idle": 1,
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
                    f"cluster-a: {expected_error}\n\n"
                    "cluster-b\n"
                    "  PARTITION  CPUS       GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                    "  cpu        0/64 (0%)  -     1      0      0      1     0     0        0         0\n",
                )

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

    def test_status_strips_node_state_flags_and_classifies_health_states(self) -> None:
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
            sinfo_fixture = (
                "cpu*|node01|allocated#|64/0/0/64|(null)|(null)|\n"
                "cpu*|node02|mixed-|32/32/0/64|(null)|(null)|\n"
                "cpu*|node03|idle~|0/64/0/64|(null)|(null)|\n"
                "cpu*|node04|down*|0/0/64/64|(null)|(null)|\n"
                "cpu*|node05|fail!|0/0/64/64|(null)|(null)|\n"
                "cpu*|node06|failing%|16/0/48/64|(null)|(null)|\n"
                "cpu*|node07|drained$|0/0/64/64|(null)|(null)|\n"
                "cpu*|node08|draining@|48/0/16/64|(null)|(null)|\n"
                "cpu*|node09|maint^|0/0/64/64|(null)|(null)|\n"
                "cpu*|node10|power_down|0/0/64/64|(null)|(null)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_fixture})

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
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "default": True,
                                    "cpus_alloc": 160,
                                    "cpus_total": 640,
                                    "cpus_utilization": 0.25,
                                    "gpus_alloc": 0,
                                    "gpus_total": 0,
                                    "gpus_utilization": None,
                                    "nodes_alloc": 1,
                                    "nodes_mixed": 1,
                                    "nodes_idle": 1,
                                    "nodes_down": 3,
                                    "nodes_drained": 1,
                                    "nodes_draining": 1,
                                    "nodes_other": 2,
                                    "nodes_total": 10,
                                }
                            ],
                        }
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
            expected_table = (
                "cluster-a\n"
                "  PARTITION  CPUS           GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu*       160/640 (25%)  -     10     1      1      1     3     1        1         2\n"
            )
            self.assertEqual(table_out.getvalue(), expected_table)
            self.assertEqual(len(runner.calls), 2)
            for _cluster, _kind, _target, argv in runner.calls:
                self.assertEqual(argv[2], "sinfo")

    def test_status_counts_heterogeneous_gpus_and_ignores_shards_and_counters(self) -> None:
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
            sinfo_fixture = (
                "cpu*|node01|allocated|64/0/0/64|(null)|(null)|\n"
                "cpu*|node02|idle~|0/64/0/64|(null)|none|\n"
                "cpu*|node03|down*|0/0/64/64|none|(null)|\n"
                "cpu*|node04|idle|0/64/0/64|(null)|(null)|\n"
                "gpu|gpu01|mixed|32/32/0/64|gpu:a100:4(S:0-1),shard:16(S:0-1)|gpu:a100:2(IDX:0,2),shard:4(IDX:0-1)|\n"
                "gpu|gpu01|mixed|32/32/0/64|gpu:a100:4(S:0-1),shard:16(S:0-1)|gpu:a100:2(IDX:0,2),shard:4(IDX:0-1)|\n"
                "gpu|gpu02|allocated|64/0/0/64|gpu:v100:8(S:0-1),gpumem:256G|gpu:v100:8(IDX:0-7),gpuutil:95|\n"
                "gpu|gpu03|draining|16/48/0/64|gpu:4(S:0,1)|gpu:2(IDX:1,3)|\n"
                "gpu|gpu04|drained*|0/0/64/64|gpu:a100:4(S:0-1)|gpu:a100:0(IDX:N/A)|\n"
                "debug|gpu05|idle~|0/32/0/32|gpu:a100:2(S:0)|gpu:a100:0(IDX:N/A)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_fixture})

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
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "default": True,
                                    "cpus_alloc": 64,
                                    "cpus_total": 256,
                                    "cpus_utilization": 0.25,
                                    "gpus_alloc": 0,
                                    "gpus_total": 0,
                                    "gpus_utilization": None,
                                    "nodes_alloc": 1,
                                    "nodes_mixed": 0,
                                    "nodes_idle": 2,
                                    "nodes_down": 1,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 4,
                                },
                                {
                                    "partition": "gpu",
                                    "default": False,
                                    "cpus_alloc": 112,
                                    "cpus_total": 256,
                                    "cpus_utilization": 0.4375,
                                    "gpus_alloc": 12,
                                    "gpus_total": 20,
                                    "gpus_utilization": 0.6,
                                    "nodes_alloc": 1,
                                    "nodes_mixed": 1,
                                    "nodes_idle": 0,
                                    "nodes_down": 0,
                                    "nodes_drained": 1,
                                    "nodes_draining": 1,
                                    "nodes_other": 0,
                                    "nodes_total": 4,
                                },
                                {
                                    "partition": "debug",
                                    "default": False,
                                    "cpus_alloc": 0,
                                    "cpus_total": 32,
                                    "cpus_utilization": 0.0,
                                    "gpus_alloc": 0,
                                    "gpus_total": 2,
                                    "gpus_utilization": 0.0,
                                    "nodes_alloc": 0,
                                    "nodes_mixed": 0,
                                    "nodes_idle": 1,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 1,
                                },
                            ],
                        }
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
            expected_table = (
                "cluster-a\n"
                "  PARTITION  CPUS           GPUS         NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu*       64/256 (25%)   -            4      1      0      2     1     0        0         0\n"
                "  gpu        112/256 (44%)  12/20 (60%)  4      1      1      0     0     1        1         0\n"
                "  debug      0/32 (0%)      0/2 (0%)     1      0      0      1     0     0        0         0\n"
            )
            self.assertEqual(table_out.getvalue(), expected_table)
            self.assertEqual(len(runner.calls), 2)
            for _cluster, _kind, _target, argv in runner.calls:
                self.assertEqual(argv[2], "sinfo")

    def test_status_sums_disjoint_typed_and_untyped_gpu_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            sinfo_fixture = (
                "gpu|gpu01|mixed|32/32/0/64|gpu:a100:2,gpu:2|gpu:a100:1(IDX:0),gpu:(null):2(IDX:2-3)|\n"
                "multi|gpu02|mixed|48/80/0/128|gpu:a100:2(S:0),gpu:v100:2(S:1)|gpu:a100:1(IDX:0),gpu:v100:2(IDX:2-3)|\n"
                "multi|gpu03|mixed|16/112/0/128|gpu:6(S:0-1)|gpu:(null):3(IDX:0,2,5)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_fixture})

            json_out = io.StringIO()
            json_err = io.StringIO()
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
                                    "partition": "gpu",
                                    "default": False,
                                    "cpus_alloc": 32,
                                    "cpus_total": 64,
                                    "cpus_utilization": 0.5,
                                    "gpus_alloc": 3,
                                    "gpus_total": 4,
                                    "gpus_utilization": 0.75,
                                    "nodes_alloc": 0,
                                    "nodes_mixed": 1,
                                    "nodes_idle": 0,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 1,
                                },
                                {
                                    "partition": "multi",
                                    "default": False,
                                    "cpus_alloc": 64,
                                    "cpus_total": 256,
                                    "cpus_utilization": 0.25,
                                    "gpus_alloc": 6,
                                    "gpus_total": 10,
                                    "gpus_utilization": 0.6,
                                    "nodes_alloc": 0,
                                    "nodes_mixed": 2,
                                    "nodes_idle": 0,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 2,
                                },
                            ],
                        }
                    ],
                },
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
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
            expected_table = (
                "cluster-a\n"
                "  PARTITION  CPUS          GPUS        NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  gpu        32/64 (50%)   3/4 (75%)   1      0      1      0     0     0        0         0\n"
                "  multi      64/256 (25%)  6/10 (60%)  2      0      2      0     0     0        0         0\n"
            )
            self.assertEqual(table_out.getvalue(), expected_table)

    def test_status_skips_non_gpu_gres_entries_before_strict_format_checks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            sinfo_fixture = (
                "cpu|node01|idle|0/64/0/64|fpga|(null)|\n"
                "cpu|node02|mixed|32/32/0/64|bandwidth:lustre:no_consume:4G,tmpdisk:100G|bandwidth:lustre:1G,tmpdisk:20G|\n"
                "gpu|gpu01|mixed|16/48/0/64|gpu:a100:4(S:0-1),fpga,bandwidth:lustre:no_consume:4G,tmpdisk:100G|gpu:a100:2(IDX:0-1),fpga:(null):1(IDX:0)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_fixture})

            json_out = io.StringIO()
            json_err = io.StringIO()
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
                                    "default": False,
                                    "cpus_alloc": 32,
                                    "cpus_total": 128,
                                    "cpus_utilization": 0.25,
                                    "gpus_alloc": 0,
                                    "gpus_total": 0,
                                    "gpus_utilization": None,
                                    "nodes_alloc": 0,
                                    "nodes_mixed": 1,
                                    "nodes_idle": 1,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 2,
                                },
                                {
                                    "partition": "gpu",
                                    "default": False,
                                    "cpus_alloc": 16,
                                    "cpus_total": 64,
                                    "cpus_utilization": 0.25,
                                    "gpus_alloc": 2,
                                    "gpus_total": 4,
                                    "gpus_utilization": 0.5,
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
                        }
                    ],
                },
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
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
            expected_table = (
                "cluster-a\n"
                "  PARTITION  CPUS          GPUS       NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu        32/128 (25%)  -          2      0      1      1     0     0        0         0\n"
                "  gpu        16/64 (25%)   2/4 (50%)  1      0      1      0     0     0        0         0\n"
            )
            self.assertEqual(table_out.getvalue(), expected_table)

    def test_status_counts_bare_and_no_consume_gpu_gres_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            sinfo_fixture = (
                "bare|gpu01|idle|0/64/0/64|gpu|(null)|\n"
                "bare|gpu02|mixed|32/32/0/64|gpu:a100|gpu:a100:1(IDX:0)|\n"
                "bare|gpu03|idle|0/32/0/32|gpu:v100,gpu|(null)|\n"
                "noconsume|gpu04|mixed|32/32/0/64|gpu:a100:no_consume:4(S:0-1)|gpu:a100:2(IDX:0-1)|\n"
                "noconsume|gpu05|mixed|32/32/0/64|gpu:no_consume:4|gpu:(null):2(IDX:0-1)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_fixture})

            json_out = io.StringIO()
            json_err = io.StringIO()
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
                                    "partition": "bare",
                                    "default": False,
                                    "cpus_alloc": 32,
                                    "cpus_total": 160,
                                    "cpus_utilization": 0.2,
                                    "gpus_alloc": 1,
                                    "gpus_total": 4,
                                    "gpus_utilization": 0.25,
                                    "nodes_alloc": 0,
                                    "nodes_mixed": 1,
                                    "nodes_idle": 2,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 3,
                                },
                                {
                                    "partition": "noconsume",
                                    "default": False,
                                    "cpus_alloc": 64,
                                    "cpus_total": 128,
                                    "cpus_utilization": 0.5,
                                    "gpus_alloc": 4,
                                    "gpus_total": 8,
                                    "gpus_utilization": 0.5,
                                    "nodes_alloc": 0,
                                    "nodes_mixed": 2,
                                    "nodes_idle": 0,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 2,
                                },
                            ],
                        }
                    ],
                },
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
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
            expected_table = (
                "cluster-a\n"
                "  PARTITION  CPUS          GPUS       NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  bare       32/160 (20%)  1/4 (25%)  3      0      1      2     0     0        0         0\n"
                "  noconsume  64/128 (50%)  4/8 (50%)  2      0      2      0     0     0        0         0\n"
            )
            self.assertEqual(table_out.getvalue(), expected_table)

    def test_status_strips_question_and_plus_node_state_suffixes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            sinfo_fixture = (
                "cpu|node01|allocated+|64/0/0/64|(null)|(null)|\n"
                "cpu|node02|idle?|0/64/0/64|(null)|(null)|\n"
                "cpu|node03|mixed+|32/32/0/64|(null)|(null)|\n"
                "cpu|node04|draining?|32/0/32/64|(null)|(null)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_fixture})

            json_out = io.StringIO()
            json_err = io.StringIO()
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
                                    "default": False,
                                    "cpus_alloc": 128,
                                    "cpus_total": 256,
                                    "cpus_utilization": 0.5,
                                    "gpus_alloc": 0,
                                    "gpus_total": 0,
                                    "gpus_utilization": None,
                                    "nodes_alloc": 1,
                                    "nodes_mixed": 1,
                                    "nodes_idle": 1,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 1,
                                    "nodes_other": 0,
                                    "nodes_total": 4,
                                }
                            ],
                        }
                    ],
                },
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
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
            expected_table = (
                "cluster-a\n"
                "  PARTITION  CPUS           GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu        128/256 (50%)  -     4      1      1      1     0     0        1         0\n"
            )
            self.assertEqual(table_out.getvalue(), expected_table)

    def test_malformed_sinfo_gres_and_state_flags_report_inline_cluster_error(self) -> None:
        malformed_cases = [
            ("non-integer-gpu-count", "gpu|gpu01|idle|0/64/0/64|gpu:a100:four|(null)|\n"),
            ("negative-gpu-count", "gpu|gpu01|idle|0/64/0/64|gpu:-2|(null)|\n"),
            ("bad-gres-used-count", "gpu|gpu01|mixed|32/32/0/64|gpu:a100:4|gpu:a100:bad|\n"),
            ("unclosed-gres-paren", "gpu|gpu01|idle|0/64/0/64|gpu:a100:4(S:0-1|(null)|\n"),
            ("empty-gpu-type", "gpu|gpu01|idle|0/64/0/64|gpu::4|(null)|\n"),
            ("trailing-empty-gpu-type", "gpu|gpu01|idle|0/64/0/64|gpu:|(null)|\n"),
            ("extra-gpu-colon-fields", "gpu|gpu01|idle|0/64/0/64|gpu:a100:4:2|(null)|\n"),
            ("non-integer-typed-no-consume-count", "gpu|gpu01|idle|0/64/0/64|gpu:a100:no_consume:four|(null)|\n"),
            ("non-integer-untyped-no-consume-count", "gpu|gpu01|idle|0/64/0/64|gpu:no_consume:four|(null)|\n"),
            ("missing-count-after-no-consume", "gpu|gpu01|idle|0/64/0/64|gpu:a100:no_consume|(null)|\n"),
            ("empty-gres-name", "gpu|gpu01|idle|0/64/0/64|:4|(null)|\n"),
            ("flag-only-node-state", "cpu|node01|*~|0/64/0/64|(null)|(null)|\n"),
            ("question-plus-only-node-state", "cpu|node01|?+|0/64/0/64|(null)|(null)|\n"),
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
                        ("cluster-a", "sinfo"): bad_stdout,
                        ("cluster-b", "sinfo"): "gpu|gpu01|idle~|0/64/0/64|gpu:a100:4(S:0-1)|gpu:a100:0(IDX:N/A)|\n",
                    }
                )

                json_out = io.StringIO()
                json_err = io.StringIO()
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
                payload = json.loads(json_out.getvalue())
                cluster_a, cluster_b = payload["clusters"]
                self.assertEqual(cluster_a["cluster"], "cluster-a")
                self.assertIsNone(cluster_a["fetched_at"])
                self.assertFalse(cluster_a["stale"])
                self.assertFalse(cluster_a["user_scoped"])
                self.assertIsInstance(cluster_a["error"], str)
                self.assertRegex(cluster_a["error"], r"\b(?:sinfo|GRES)\b")
                for exc_name in ("KeyError", "ValueError", "IndexError", "TypeError", "AttributeError"):
                    self.assertNotIn(exc_name, cluster_a["error"])
                self.assertEqual(cluster_a["partitions"], [])

                self.assertEqual(cluster_b["cluster"], "cluster-b")
                self.assertIsNone(cluster_b["error"])
                self.assertEqual(cluster_b["partitions"][0]["gpus_total"], 4)
                self.assertEqual(cluster_b["partitions"][0]["nodes_idle"], 1)

                table_out = io.StringIO()
                table_err = io.StringIO()
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
                rendered_table = table_out.getvalue()
                self.assertIn(f"cluster-a: {cluster_a['error']}\n\ncluster-b\n", rendered_table)
                for exc_name in ("KeyError", "ValueError", "IndexError", "TypeError", "AttributeError"):
                    self.assertNotIn(exc_name, rendered_table)

    def test_status_skips_dash_m_cluster_banner_line(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\nslurm_cluster = "prod"\n',
                encoding="utf-8",
            )
            sinfo_fixture = (
                "CLUSTER: prod\n"
                "cpu*|node01|mixed|16/48/0/64|(null)|(null)|\n"
                "cpu*|node02|idle|0/64/0/64|(null)|(null)|\n"
            )
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_fixture})

            table_out = io.StringIO()
            table_err = io.StringIO()
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
            self.assertEqual(
                table_out.getvalue(),
                "cluster-a\n"
                "  PARTITION  CPUS          GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu*       16/128 (12%)  -     2      0      1      1     0     0        0         0\n",
            )


    def test_ssh_runner_invokes_exact_ssh_argv_and_25s_timeout(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "cluster-a.example"\n',
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            fake_completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="cpu|node01|idle|0/64/0/64|(null)|(null)|\n",
                stderr="",
            )
            with mock.patch("sglance.runner.subprocess.run", return_value=fake_completed) as mock_run:
                exit_code = main(
                    ["status", "--json"],
                    env=make_test_env(root),
                    clock=lambda: FIXED_NOW,
                    stdout=stdout,
                    stderr=stderr,
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            expected_remote_cmd = (
                "env SLURM_TIME_FORMAT=%s sinfo -h -N -O "
                "'PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|'"
            )
            self.assertEqual(
                call_args.args,
                (
                    [
                        "ssh",
                        "-T",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ConnectTimeout=5",
                        "-o",
                        "RemoteCommand=none",
                        "-o",
                        "LogLevel=ERROR",
                        "--",
                        "cluster-a.example",
                        expected_remote_cmd,
                    ],
                ),
            )
            self.assertEqual(call_args.kwargs["timeout"], 25.0)
            self.assertEqual(call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(call_args.kwargs["capture_output"])
            self.assertTrue(call_args.kwargs["text"])
            self.assertFalse(call_args.kwargs["check"])
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["clusters"][0]["partitions"][0]["cpus_total"], 64)

    def test_allowlist_error_from_runner_propagates_through_main_for_status_and_queue(self) -> None:
        cases = (
            (
                "status",
                {
                    ("cluster-a", "sinfo"): AllowlistError("disallowed sinfo command"),
                    ("cluster-b", "sinfo"): "cpu|node01|idle|0/64/0/64|(null)|(null)|\n",
                },
            ),
            (
                "queue",
                {
                    ("cluster-a", "squeue"): AllowlistError("disallowed squeue command"),
                    ("cluster-b", "squeue"): "1001|alice|cpu|R|None|\n",
                },
            ),
        )
        for subcommand, responses in cases:
            with self.subTest(subcommand=subcommand), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(
                    '[[clusters]]\nname = "cluster-a"\nssh = "cluster-a.example"\n\n'
                    '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
                    encoding="utf-8",
                )
                runner = FakeRunner(responses)
                stdout = io.StringIO()
                stderr = io.StringIO()

                with self.assertRaises(AllowlistError):
                    main(
                        [subcommand, "--json"],
                        env=make_test_env(root),
                        runner=runner,
                        clock=lambda: FIXED_NOW,
                        stdout=stdout,
                        stderr=stderr,
                    )

                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_help_exits_zero_and_mentions_all_flags_for_root_and_all_subcommands(self) -> None:
        cases: tuple[tuple[list[str], tuple[str, ...], tuple[str, ...]], ...] = (
            (
                ["--help"],
                (
                    "Read-only inspection of Slurm cluster capacity, queues, wait times, and jobs.",
                    "--help",
                    "--cluster",
                    "--refresh",
                    "--json",
                    "status",
                    "queue",
                    "wait",
                    "mine",
                    "Query only the named configured cluster entry.",
                    "Bypass the 5-minute cache and fetch fresh data.",
                    "Emit JSON output.",
                ),
                ("--days", "--account"),
            ),
            (
                ["status", "--help"],
                (
                    "Show partition CPU, GPU, and node utilization.",
                    "--help",
                    "--cluster",
                    "--refresh",
                    "--json",
                    "Query only the named configured cluster entry.",
                    "Bypass the 5-minute cache and fetch fresh data.",
                    "Emit JSON output.",
                ),
                ("--days", "--account"),
            ),
            (
                ["queue", "--help"],
                (
                    "Show running/pending job counts and top pending reasons.",
                    "--help",
                    "--cluster",
                    "--refresh",
                    "--json",
                    "Query only the named configured cluster entry.",
                    "Bypass the 5-minute cache and fetch fresh data.",
                    "Emit JSON output.",
                ),
                ("--days", "--account"),
            ),
            (
                ["wait", "--help"],
                (
                    "Show wait-time percentiles and oldest pending job age.",
                    "--help",
                    "--cluster",
                    "--refresh",
                    "--days",
                    "--json",
                    "Query only the named configured cluster entry.",
                    "Bypass the 5-minute cache and fetch fresh data.",
                    "Lookback window in days (integer >= 1, default: 7).",
                    "Emit JSON output.",
                ),
                ("--account",),
            ),
            (
                ["mine", "--help"],
                (
                    "Show your jobs or an account's jobs and start estimates.",
                    "--help",
                    "--cluster",
                    "--refresh",
                    "--account",
                    "--json",
                    "Query only the named configured cluster entry.",
                    "Bypass the 5-minute cache and fetch fresh data.",
                    "List all jobs charged to the named Slurm account.",
                    "Emit JSON output.",
                ),
                ("--days",),
            ),
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = make_test_env(root)
            runner = FakeRunner()
            for argv, expected_substrings, unexpected_flags in cases:
                with self.subTest(argv=argv):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    exit_code = main(
                        argv,
                        env=env,
                        runner=runner,
                        clock=lambda: FIXED_NOW,
                        stdout=stdout,
                        stderr=stderr,
                    )
                    rendered = stdout.getvalue()
                    self.assertEqual(exit_code, 0)
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertNotIn("\x1b", rendered)
                    for token in expected_substrings:
                        self.assertIn(token, rendered)
                    for flag in unexpected_flags:
                        self.assertNotIn(flag, rendered)
            self.assertEqual(runner.calls, [])

    def test_json_flag_works_before_or_after_subcommand_for_all_subcommands(self) -> None:
        mine_fields = (
            "JobArrayID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
            "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,NumTasks:0|,"
            "tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,tres-per-task:0|,"
            "StartTime:0|,Reason:0|,Name:0|"
        )
        queue_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-h",
            "-r",
            "-O",
            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|",
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
        mine_argv = ("env", "SLURM_TIME_FORMAT=%s", "squeue", "-h", "--me", "-O", mine_fields)

        expected_status = {
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
                            "cpus_alloc": 64,
                            "cpus_total": 64,
                            "cpus_utilization": 1.0,
                            "gpus_alloc": 0,
                            "gpus_total": 0,
                            "gpus_utilization": None,
                            "nodes_alloc": 1,
                            "nodes_mixed": 0,
                            "nodes_idle": 0,
                            "nodes_down": 0,
                            "nodes_drained": 0,
                            "nodes_draining": 0,
                            "nodes_other": 0,
                            "nodes_total": 1,
                        }
                    ],
                }
            ],
        }
        expected_queue = {
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
                            "top_reasons": [{"reason": "Resources", "count": 1}],
                        }
                    ],
                }
            ],
        }
        expected_wait = {
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
                            "n": 1,
                            "p50_seconds": 60.0,
                            "p90_seconds": 60.0,
                            "median_pending_seconds": None,
                            "oldest_pending_seconds": None,
                        }
                    ],
                }
            ],
        }
        expected_mine = {
            "schema_version": 1,
            "command": "mine",
            "account": None,
            "clusters": [
                {
                    "cluster": "cluster-a",
                    "fetched_at": "2026-11-12T20:00:00Z",
                    "stale": False,
                    "user_scoped": False,
                    "error": None,
                    "slurm_user": "alice",
                    "account_filter": None,
                    "jobs": [
                        {
                            "job_id": "2001",
                            "name": "me_job",
                            "user": "alice",
                            "account": "proj-1",
                            "partition": "cpu",
                            "state": "R",
                            "cpus": 16,
                            "gpus": 0,
                            "nodes": 1,
                            "elapsed_seconds": 600,
                            "time_limit_seconds": 3600,
                            "reason": None,
                            "estimated_start": None,
                        }
                    ],
                }
            ],
        }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "cluster-a.example"\n\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n",
                    ("cluster-a", *queue_argv): (
                        "1001|alice|cpu|R|None|\n"
                        "1002|bob|cpu|PD|Resources|\n"
                    ),
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-a", "sacct"): (
                        "3001|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n"
                        "3002|bob|cpu|CANCELLED|1794501000|None|\n"
                    ),
                    ("cluster-a", *wait_squeue_argv): "3003|bob|cpu|PD|JobHeldUser|1794510000|\n",
                    ("cluster-a", *mine_argv): (
                        "2001|alice|proj-1|cpu|R|10:00|01:00:00|1|16|1|"
                        "cpu=16|N/A|N/A|N/A|1794513000|None|me_job|\n"
                    ),
                }
            )

            cases: tuple[tuple[list[str], dict[str, object]], ...] = (
                (["--json", "--cluster", "cluster-a", "status"], expected_status),
                (["status", "--cluster", "cluster-a", "--json"], expected_status),
                (["--json", "status", "--cluster", "cluster-a", "--json"], expected_status),
                (["--json", "--cluster", "cluster-a", "queue"], expected_queue),
                (["queue", "--cluster", "cluster-a", "--json"], expected_queue),
                (["--json", "--cluster", "cluster-a", "wait", "--days", "7"], expected_wait),
                (["wait", "--days", "7", "--cluster", "cluster-a", "--json"], expected_wait),
                (["--json", "--cluster", "cluster-a", "mine"], expected_mine),
                (["mine", "--cluster", "cluster-a", "--json"], expected_mine),
            )
            for argv, expected_payload in cases:
                with self.subTest(argv=argv):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    rc = main(
                        argv,
                        env=make_test_env(root),
                        runner=runner,
                        clock=lambda: FIXED_NOW,
                        stdout=stdout,
                        stderr=stderr,
                    )
                    self.assertEqual(rc, 0)
                    self.assertEqual(stderr.getvalue(), "")
                    self.assertEqual(json.loads(stdout.getvalue()), expected_payload)

            # Without --json in either position, plain-text table remains the default.
            plain_out = io.StringIO()
            plain_err = io.StringIO()
            plain_rc = main(
                ["--cluster", "cluster-a", "status"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=plain_out,
                stderr=plain_err,
            )
            self.assertEqual(plain_rc, 0)
            self.assertEqual(plain_err.getvalue(), "")
            self.assertEqual(
                plain_out.getvalue(),
                "cluster-a\n"
                "  PARTITION  CPUS          GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu        64/64 (100%)  -     1      1      0      0     0     0        0         0\n",
            )


if __name__ == "__main__":
    unittest.main()
