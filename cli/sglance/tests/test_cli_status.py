from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from sglance import CommandResult, TransportError
from sglance.models import ClusterEntry
from sglance.runner import AllowlistedRunner, AllowlistError, validate_allowlist
from tests.fakes import (
    FIXED_NOW,
    MINE_ME_ARGV,
    QUEUE_ARGV,
    WAIT_SQUEUE_ARGV,
    FakeRunner,
    make_test_env,
    run_cli,
    write_config,
)

SINFO_FIELDS = "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|"


class StatusCliTests(unittest.TestCase):
    def test_allowlist_rejects_disallowed_argv_and_propagates_out_of_main(self) -> None:
        entry = ClusterEntry(name="cluster-a", ssh="login.example.edu")
        rejected_vectors: tuple[tuple[str, list[str]], ...] = (
            ("scancel", ["env", "SLURM_TIME_FORMAT=%s", "scancel", "1001"]),
            ("sbatch", ["env", "SLURM_TIME_FORMAT=%s", "sbatch", "job.sh"]),
            ("rm", ["env", "SLURM_TIME_FORMAT=%s", "rm", "-rf", "/tmp/work"]),
            ("missing-env", ["sinfo", "-h", "-N"]),
            ("wrong-time-fmt", ["env", "SLURM_TIME_FORMAT=standard", "sinfo", "-h"]),
            ("scontrol-node", ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "show", "node"]),
            ("scontrol-update", ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "update", "State=DRAIN"]),
            ("dash-m-flag", ["env", "SLURM_TIME_FORMAT=%s", "sinfo", "-M", "-bad", "-h"]),
            ("dash-m-missing", ["env", "SLURM_TIME_FORMAT=%s", "sacct", "-M"]),
        )
        for label, bad_argv in rejected_vectors:
            with self.subTest(case=label):
                inner = FakeRunner()
                guarded = AllowlistedRunner(inner)
                with self.assertRaises(AllowlistError):
                    validate_allowlist(bad_argv)
                with self.assertRaises(AllowlistError):
                    guarded.run(entry, bad_argv)
                self.assertEqual(inner.calls, [])

        # AllowlistError raised during cluster execution must propagate out of main(),
        # never get swallowed into a per-cluster error string.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\n[[clusters]]\nname = "cluster-b"\nlocal = true\n',
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): AllowlistError("disallowed sinfo command"),
                    ("cluster-b", "sinfo"): "cpu|node01|idle|0/64/0/64|(null)|(null)|\n",
                }
            )
            with self.assertRaises(AllowlistError):
                run_cli(["status", "--json"], root=root, runner=runner)

    def test_ssh_runner_invokes_exact_ssh_argv_and_25s_timeout(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root, '[[clusters]]\nname = "cluster-a"\nssh = "cluster-a.example"\n')
            fake_proc = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="cpu|node01|idle|0/64/0/64|(null)|(null)|\n",
                stderr="",
            )
            with mock.patch("sglance.runner.subprocess.run", return_value=fake_proc) as mock_run:
                rc, out, err = run_cli(["status", "--json"], root=root)

            self.assertEqual((rc, err), (0, ""))
            mock_run.assert_called_once()
            call_args = mock_run.call_args
            expected_remote = f"env SLURM_TIME_FORMAT=%s sinfo -h -N -O '{SINFO_FIELDS}'"
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
                        expected_remote,
                    ],
                ),
            )
            self.assertEqual(call_args.kwargs["timeout"], 25.0)
            self.assertEqual(call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(call_args.kwargs["capture_output"])
            self.assertFalse(call_args.kwargs["check"])
            self.assertEqual(json.loads(out)["clusters"][0]["partitions"][0]["cpus_total"], 64)

    def test_error_mapping_for_transport_ssh_255_and_nonzero_stderr(self) -> None:
        cases: tuple[tuple[str, CommandResult | Exception, str], ...] = (
            (
                "transport-error",
                TransportError("timed out after 25s"),
                "unreachable (timed out after 25s)",
            ),
            (
                "ssh-exit-255",
                CommandResult(255, "", "Warning: host key\nssh: Connection timed out\n"),
                "unreachable (ssh: Connection timed out)",
            ),
            (
                "nonzero-strips-tool-prefix-on-last-line",
                CommandResult(
                    1,
                    "",
                    "sinfo: warning: old config key\nsinfo: error: controller down\n  \n",
                ),
                "sinfo exited 1: error: controller down",
            ),
            (
                "nonzero-preserves-other-prefix",
                CommandResult(2, "", "sinfo_wrapper: bad auth\n"),
                "sinfo exited 2: sinfo_wrapper: bad auth",
            ),
            ("nonzero-empty-stderr", CommandResult(7, "", "   \n"), "sinfo exited 7"),
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            for label, outcome, expected_error in cases:
                with self.subTest(case=label):
                    runner = FakeRunner({("cluster-a", "sinfo"): outcome})
                    rc, out, err = run_cli(["status", "--json"], root=root, runner=runner)
                    self.assertEqual((rc, err), (0, ""))
                    self.assertEqual(json.loads(out)["clusters"][0]["error"], expected_error)

    def test_status_exact_argv_gres_parsing_banner_and_deduplication(self) -> None:
        sinfo_a = (
            "CLUSTER: prod\n"
            # Duplicated node row: typed + untyped GPU sum (2+2=4 total, 1+2=3 alloc); shard/gpumem/gpuutil skipped.
            "gpu|gpu01|mixed|32/16/16/64|"
            "gpu:a100:2(S:0),gpu:2,shard:16(S:0),gpumem:80G|"
            "gpu:a100:1(IDX:0),gpu:(null):2(IDX:2-3),shard:4,gpuutil:90|\n"
            "gpu|gpu01|mixed|32/16/16/64|"
            "gpu:a100:2(S:0),gpu:2,shard:16(S:0),gpumem:80G|"
            "gpu:a100:1(IDX:0),gpu:(null):2(IDX:2-3),shard:4,gpuutil:90|\n"
            # Typed & untyped no_consume (4+2=6 total, 2+1=3 alloc) + non-GPU GRES skipped.
            "gpu|gpu02|mixed|16/48/0/64|"
            "gpu:a100:no_consume:4(S:0-1),gpu:no_consume:2,fpga,bandwidth:lustre:no_consume:4G|"
            "gpu:a100:2(IDX:0-1),gpu:(null):1(IDX:0),fpga:(null):1|\n"
            # Bare gpu:type and bare gpu (1+1=2 total, 0 alloc).
            "gpu|gpu03|idle|0/32/0/32|gpu:v100,gpu|(null)|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\nslurm_cluster = "prod"\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): sinfo_a,
                    ("cluster-b", "sinfo"): "gpu|gpu01|idle|0/64/0/64|gpu:a100:four|(null)|\n",
                }
            )
            rc, out, err = run_cli(["status", "--json"], root=root, runner=runner)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(
                sorted(runner.calls, key=lambda c: c[0]),
                [
                    (
                        "cluster-a",
                        "ssh",
                        "login.example.edu",
                        ["env", "SLURM_TIME_FORMAT=%s", "sinfo", "-M", "prod", "-h", "-N", "-O", SINFO_FIELDS],
                    ),
                    (
                        "cluster-b",
                        "local",
                        None,
                        ["env", "SLURM_TIME_FORMAT=%s", "sinfo", "-h", "-N", "-O", SINFO_FIELDS],
                    ),
                ],
            )
            clusters = json.loads(out)["clusters"]
            self.assertEqual(
                clusters[0],
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
                            "cpus_alloc": 48,
                            "cpus_total": 160,
                            "cpus_utilization": 0.3,
                            "gpus_alloc": 6,
                            "gpus_total": 12,
                            "gpus_utilization": 0.5,
                            "nodes_alloc": 0,
                            "nodes_mixed": 2,
                            "nodes_idle": 1,
                            "nodes_down": 0,
                            "nodes_drained": 0,
                            "nodes_draining": 0,
                            "nodes_other": 0,
                            "nodes_total": 3,
                        }
                    ],
                },
            )
            self.assertIn("malformed sinfo GRES", clusters[1]["error"])

    def test_status_strips_node_state_suffixes_and_renders_table(self) -> None:
        sinfo_rows = (
            "cpu*|node01|allocated#+|64/0/0/64|(null)|(null)|\n"
            "cpu*|node02|mixed-|32/32/0/64|(null)|(null)|\n"
            "cpu*|node03|idle~?|0/64/0/64|(null)|none|\n"
            "cpu*|node04|down*|0/0/64/64|none|(null)|\n"
            "cpu*|node05|fail!|0/0/64/64|(null)|(null)|\n"
            "cpu*|node06|failing%|64/0/0/64|(null)|(null)|\n"
            "cpu*|node07|drained$|0/0/64/64|(null)|(null)|\n"
            "cpu*|node08|draining@?|0/0/64/64|(null)|(null)|\n"
            "cpu*|node09|maint^|0/0/64/64|(null)|(null)|\n"
            "cpu*|node10|power_down+|0/0/64/64|(null)|(null)|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            runner = FakeRunner({("cluster-a", "sinfo"): sinfo_rows})
            rc, out, err = run_cli(["status"], root=root, runner=runner)
            self.assertEqual((rc, err), (0, ""))
            self.assertEqual(
                out,
                "cluster-a\n"
                "  PARTITION  CPUS           GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu*       160/640 (25%)  -     10     1      1      1     3     1        1         2\n",
            )

    def test_json_flag_works_before_or_after_subcommand_and_help_exits_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n",
                    ("cluster-a", *QUEUE_ARGV): "1|alice|cpu|R|None|\n",
                    ("cluster-a", "sacct"): "2|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n",
                    ("cluster-a", *WAIT_SQUEUE_ARGV): "",
                    ("cluster-a", *MINE_ME_ARGV): (
                        "3|alice|proj-1|cpu|R|10:00|01:00:00|1|16|1|cpu=16|N/A|N/A|N/A|1794513000|None|j|\n"
                    ),
                }
            )
            for subcmd in ("status", "queue", "wait", "mine"):
                with self.subTest(subcommand=subcmd):
                    rc_pre, out_pre, err_pre = run_cli(["--json", subcmd], root=root, runner=runner)
                    rc_post, out_post, err_post = run_cli([subcmd, "--json"], root=root, runner=runner)
                    self.assertEqual((rc_pre, err_pre, rc_post, err_post), (0, "", 0, ""))
                    parsed_pre = json.loads(out_pre)
                    self.assertEqual(parsed_pre, json.loads(out_post))
                    self.assertEqual(parsed_pre["command"], subcmd)

            rc_help, out_help, err_help = run_cli(["wait", "--help"], root=root, runner=FakeRunner())
            self.assertEqual((rc_help, err_help), (0, ""))
            self.assertNotIn("\x1b", out_help)
            self.assertIn("--days", out_help)

            rc_bad, _, _ = run_cli(["--unknown-flag"], root=root, runner=FakeRunner())
            self.assertEqual(rc_bad, 2)

    def test_compact_tables_across_subcommands_cache_and_json(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
            )
            env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): (
                        "cpu*|node01|allocated|64/0/0/64|(null)|(null)|\n"
                        "cpu*|node02|mixed|16/48/0/64|(null)|(null)|\n"
                        "cpu*|node03|idle|0/64/0/64|(null)|(null)|\n"
                        "gpu|gpu01|mixed|16/48/0/64|gpu:a100:4|gpu:a100:1|\n"
                        "gpu|gpu02|idle|0/64/0/64|gpu:a100:4|(null)|\n"
                    ),
                    ("cluster-b", "sinfo"): TransportError("timed out after 25s"),
                    ("cluster-a", *QUEUE_ARGV): "1|alice|cpu|R|None|\n2|alice|gpu|PD|Resources|\n",
                    ("cluster-b", *QUEUE_ARGV): "",
                    ("cluster-a", *MINE_ME_ARGV): (
                        "1001|alice|proj-1|gpu|R|45:30|04:00:00|2|64|8|cpu=64,gres/gpu=8|N/A|N/A|N/A|1794510870|None|train|\n"
                        "1002_[1-4]|alice|proj-1|cpu|PD|0:00|01:00:00|1|16|1|cpu=16|N/A|N/A|N/A|1794517200|Resources|eval|\n"
                    ),
                    ("cluster-b", *MINE_ME_ARGV): "",
                    ("cluster-a", "sacct"): (
                        "1001|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n"
                        "1002|bob|cpu|RUNNING|1794501000|1794501600|cpu=16\n"
                        "1003|alice|cpu|TIMEOUT|1794502000|1794502120|cpu=8\n"
                        "1004|bob|cpu|FAILED|1794503000|1794503300|cpu=4\n"
                    ),
                    ("cluster-a", *WAIT_SQUEUE_ARGV): "",
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-b", "sacct"): CommandResult(1, "", "sacct: error: Slurm accounting storage is disabled\n"),
                    ("cluster-b", *WAIT_SQUEUE_ARGV): "901|alice|gpu|PD|Resources|1794512700|\n902|bob|gpu|PD|Priority|1794513300|\n",
                    ("cluster-b", "scontrol"): "PrivateData = none\n",
                }
            )
            run_cli(["status", "--cluster", "cluster-a"], root=root, env=env, runner=runner, now=FIXED_NOW)
            t_later = FIXED_NOW + timedelta(seconds=15)
            cases: tuple[tuple[list[str], str], ...] = (
                (
                    ["--compact", "status"],
                    "cluster-a [cached 15s ago]\n"
                    "  PARTITION  FREE CPUS  FREE GPUS  IDLE NODES\n"
                    "  cpu*       112/192    -          1/3\n"
                    "  gpu        112/128    7/8        1/2\n\n"
                    "cluster-b: unreachable (timed out after 25s)\n",
                ),
                (
                    ["queue", "--compact"],
                    "cluster-a [user-scoped]\n  PARTITION\n  cpu\n  gpu\n\ncluster-b\n  0 running, 0 pending\n",
                ),
                (
                    ["mine", "--compact"],
                    "cluster-a\n"
                    "  JOBID       PARTITION  STATE  ESTIMATED START\n"
                    "  1001        gpu        R      -\n"
                    "  1002_[1-4]  cpu        PD     2026-11-12T21:00:00Z\n\n"
                    "cluster-b\n  (no jobs)\n",
                ),
                (
                    ["wait", "--compact"],
                    "cluster-a\n  PARTITION  P50     P90\n  cpu        3m 30s  8m 30s\n\n"
                    "cluster-b\n  PARTITION  P50  P90\n  gpu        -    -\n",
                ),
            )
            for argv, expected in cases:
                with self.subTest(argv=argv):
                    rc, out, err = run_cli(argv, root=root, env=env, runner=runner, now=t_later)
                    self.assertEqual((rc, err, out), (0, "", expected))
            self.assertEqual([c[0] for c in runner.calls if c[3][2] == "sinfo"], ["cluster-a", "cluster-b"])
            _, json_plain, _ = run_cli(["status", "--json", "--cluster", "cluster-a"], root=root, env=env, runner=runner, now=t_later)
            _, json_compact, _ = run_cli(["status", "--compact", "--json", "--cluster", "cluster-a"], root=root, env=env, runner=runner, now=t_later)
            self.assertEqual(json_compact, json_plain)


if __name__ == "__main__":
    unittest.main()
