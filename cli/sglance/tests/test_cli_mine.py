from __future__ import annotations

from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sglance import TransportError, main
from tests.fakes import FakeRunner, make_test_env

FIXED_NOW = datetime(2026, 11, 12, 20, 0, 0, tzinfo=UTC)

MINE_SQUEUE_FIELDS = (
    "JobID:0|,Name:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
    "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,tres-alloc:0|,StartTime:0|,Reason:0|"
)


class MineCliTests(unittest.TestCase):
    def test_mine_json_queries_me_across_clusters_with_distinct_usernames_and_backfill_start(
        self,
    ) -> None:
        # cluster-a: caller is "alice"; running job (StartTime epoch ignored -> null)
        # plus pending job with epoch 1794536100 (2026-11-12 21:15:00 EST / 2026-11-13T02:15:00Z).
        cluster_a_squeue = (
            "1001|train_llm|alice|proj-123|gpu|R|45:30|04:00:00|2|64|"
            "cpu=64,mem=256G,node=2,gres/gpu=8|1794510870|None|\n"
            "1002_1|eval_sweep|alice|proj-123|gpu|PD|0:00|UNLIMITED|1|16|"
            "cpu=16,mem=64G,node=1,gres/gpu=2|1794536100|Resources|\n"
        )
        # cluster-b: caller is "alice_hpc" on a UTC-5 site; running multi-day job with
        # Partition_Limit plus pending jobs with N/A and Unknown StartTime.
        cluster_b_squeue = (
            "5001|preprocess|alice_hpc|bio-lab|cpu|R|1-02:30:15|Partition_Limit|1|32|"
            "cpu=32,mem=128G,node=1|1794418185|None|\n"
            "5002|postproc|alice_hpc|bio-lab|cpu|PD|0:00|2-00:00:00|1|8|"
            "cpu=8,mem=32G,node=1|N/A|Priority|\n"
            "5003| wait_dep |alice_hpc|bio-lab|debug|PD|0:00|01:00:00|1|4|"
            "(null)|Unknown|Dependency|\n"
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
                    ("cluster-a", "squeue"): cluster_a_squeue,
                    ("cluster-b", "squeue"): cluster_b_squeue,
                    ("cluster-c", "squeue"): "",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["mine", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            expected_argv = [
                "env",
                "SLURM_TIME_FORMAT=%s",
                "squeue",
                "-h",
                "-r",
                "--me",
                "-O",
                MINE_SQUEUE_FIELDS,
            ]
            self.assertEqual(
                sorted(runner.calls, key=lambda call: call[0]),
                [
                    ("cluster-a", "ssh", "login.example.edu", expected_argv),
                    ("cluster-b", "local", None, expected_argv),
                    ("cluster-c", "ssh", "cluster-c", expected_argv),
                ],
            )
            self.assertEqual(
                json.loads(stdout.getvalue()),
                {
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
                                    "job_id": "1001",
                                    "name": "train_llm",
                                    "user": "alice",
                                    "account": "proj-123",
                                    "partition": "gpu",
                                    "state": "R",
                                    "cpus": 64,
                                    "gpus": 8,
                                    "nodes": 2,
                                    "elapsed_seconds": 2730,
                                    "time_limit_seconds": 14400,
                                    "reason": None,
                                    "estimated_start": None,
                                },
                                {
                                    "job_id": "1002_1",
                                    "name": "eval_sweep",
                                    "user": "alice",
                                    "account": "proj-123",
                                    "partition": "gpu",
                                    "state": "PD",
                                    "cpus": 16,
                                    "gpus": 2,
                                    "nodes": 1,
                                    "elapsed_seconds": 0,
                                    "time_limit_seconds": None,
                                    "reason": "Resources",
                                    "estimated_start": "2026-11-13T02:15:00Z",
                                },
                            ],
                        },
                        {
                            "cluster": "cluster-b",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "slurm_user": "alice_hpc",
                            "account_filter": None,
                            "jobs": [
                                {
                                    "job_id": "5001",
                                    "name": "preprocess",
                                    "user": "alice_hpc",
                                    "account": "bio-lab",
                                    "partition": "cpu",
                                    "state": "R",
                                    "cpus": 32,
                                    "gpus": 0,
                                    "nodes": 1,
                                    "elapsed_seconds": 95415,
                                    "time_limit_seconds": None,
                                    "reason": None,
                                    "estimated_start": None,
                                },
                                {
                                    "job_id": "5002",
                                    "name": "postproc",
                                    "user": "alice_hpc",
                                    "account": "bio-lab",
                                    "partition": "cpu",
                                    "state": "PD",
                                    "cpus": 8,
                                    "gpus": 0,
                                    "nodes": 1,
                                    "elapsed_seconds": 0,
                                    "time_limit_seconds": 172800,
                                    "reason": "Priority",
                                    "estimated_start": None,
                                },
                                {
                                    "job_id": "5003",
                                    "name": "wait_dep",
                                    "user": "alice_hpc",
                                    "account": "bio-lab",
                                    "partition": "debug",
                                    "state": "PD",
                                    "cpus": 4,
                                    "gpus": 0,
                                    "nodes": 1,
                                    "elapsed_seconds": 0,
                                    "time_limit_seconds": 3600,
                                    "reason": "Dependency",
                                    "estimated_start": None,
                                },
                            ],
                        },
                        {
                            "cluster": "cluster-c",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "slurm_user": None,
                            "account_filter": None,
                            "jobs": [],
                        },
                    ],
                },
            )

    def test_mine_table_renders_aligned_jobs_and_empty_cluster_without_ansi(self) -> None:
        cluster_a_squeue = (
            "1001|train_llm|alice|proj-123|gpu|R|45:30|04:00:00|2|64|"
            "cpu=64,gres/gpu=8|1794510870|None|\n"
            "1002_1|eval_sweep|alice|proj-123|gpu|PD|0:00|UNLIMITED|1|16|"
            "cpu=16,gres/gpu=2|1794536100|Resources|\n"
            "1003|long_sim|alice|proj-123|cpu|R|1-02:05:09|2-00:00:00|2|128|"
            "cpu=128|1794419691|None|\n"
        )
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
                    ("cluster-a", "squeue"): cluster_a_squeue,
                    ("cluster-b", "squeue"): "   \n",
                }
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["mine"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            rendered = stdout.getvalue()
            self.assertNotIn("\x1b", rendered)
            self.assertEqual(
                rendered,
                "cluster-a\n"
                "  JOBID   NAME        USER   ACCOUNT   PARTITION  STATE  CPUS  GPUS  NODES  ELAPSED     LIMIT       REASON     ESTIMATED START\n"
                "  1001    train_llm   alice  proj-123  gpu        R      64    8     2      45:30       4:00:00     -          -\n"
                "  1002_1  eval_sweep  alice  proj-123  gpu        PD     16    2     1      0:00        -           Resources  2026-11-13T02:15:00Z\n"
                "  1003    long_sim    alice  proj-123  cpu        R      128   0     2      1-02:05:09  2-00:00:00  -          -\n"
                "\n"
                "cluster-b\n"
                "  (no jobs)\n",
            )

    def test_mine_counts_tres_gpus_without_double_counting_typed_entries_and_ignores_shards(
        self,
    ) -> None:
        squeue_output = (
            "2001|both_untyped_and_typed|alice|proj-123|gpu|R|10:00|02:00:00|1|32|"
            "cpu=32,mem=128G,gres/gpu=4,gres/gpu:a100=4,gres/gpumem=160G,gres/gpuutil=100,gres/shard=8|"
            "1794513000|None|\n"
            "2002|typed_only_sum|alice|proj-123|gpu|R|05:00|01:00:00|1|24|"
            "cpu=24,mem=96G,gres/gpu:a100=2,gres/gpu:v100=1|1794513300|None|\n"
            "2003|shards_and_multipart|alice|proj-123|cpu,gpu|RQ|0:00|00:30:00|1|8|"
            "cpu=8,mem=32G,gres/gpumem=40G,gres/gpuutil=50,gres/shard=2,gres/shard:a100=2|"
            "None|Resources|\n"
            "2004|configuring_job|alice|proj-123|gpu*|CF|0:05|01:00:00|1|16|"
            "cpu=16,gres/gpu=1|1794513595|None|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner({("cluster-a", "squeue"): squeue_output})
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = main(
                ["mine", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            jobs = json.loads(stdout.getvalue())["clusters"][0]["jobs"]
            self.assertEqual(
                jobs,
                [
                    {
                        "job_id": "2001",
                        "name": "both_untyped_and_typed",
                        "user": "alice",
                        "account": "proj-123",
                        "partition": "gpu",
                        "state": "R",
                        "cpus": 32,
                        "gpus": 4,
                        "nodes": 1,
                        "elapsed_seconds": 600,
                        "time_limit_seconds": 7200,
                        "reason": None,
                        "estimated_start": None,
                    },
                    {
                        "job_id": "2002",
                        "name": "typed_only_sum",
                        "user": "alice",
                        "account": "proj-123",
                        "partition": "gpu",
                        "state": "R",
                        "cpus": 24,
                        "gpus": 3,
                        "nodes": 1,
                        "elapsed_seconds": 300,
                        "time_limit_seconds": 3600,
                        "reason": None,
                        "estimated_start": None,
                    },
                    {
                        "job_id": "2003",
                        "name": "shards_and_multipart",
                        "user": "alice",
                        "account": "proj-123",
                        "partition": "cpu",
                        "state": "RQ",
                        "cpus": 8,
                        "gpus": 0,
                        "nodes": 1,
                        "elapsed_seconds": 0,
                        "time_limit_seconds": 1800,
                        "reason": "Resources",
                        "estimated_start": None,
                    },
                    {
                        "job_id": "2004",
                        "name": "configuring_job",
                        "user": "alice",
                        "account": "proj-123",
                        "partition": "gpu",
                        "state": "CF",
                        "cpus": 16,
                        "gpus": 1,
                        "nodes": 1,
                        "elapsed_seconds": 5,
                        "time_limit_seconds": 3600,
                        "reason": None,
                        "estimated_start": None,
                    },
                ],
            )

    def test_mine_account_queries_dash_a_and_detects_user_scoped_from_private_data_or_single_user(
        self,
    ) -> None:
        cluster_b_squeue = (
            "3001|sim_a|alice|proj-123|gpu|R|15:00|01:00:00|1|16|"
            "cpu=16,gres/gpu=2|1794512700|None|\n"
            "3002|sim_b|alice|proj-123|gpu|PD|0:00|01:00:00|1|16|"
            "cpu=16,gres/gpu=2|1794536100|Priority|\n"
        )
        cluster_c_squeue = (
            "4001|fit_a|alice|proj-123|cpu|R|05:00|00:30:00|1|8|cpu=8|1794513300|None|\n"
            "4002|fit_b|bob|proj-123|cpu|PD|0:00|00:30:00|1|8|cpu=8|N/A|Resources|\n"
        )
        with TemporaryDirectory() as tmp:
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
                    ("cluster-a", "squeue"): "",
                    ("cluster-a", "scontrol"): "PrivateData             = jobs,usage\n",
                    ("cluster-b", "squeue"): cluster_b_squeue,
                    ("cluster-b", "scontrol"): "PrivateData             = none\n",
                    ("cluster-c", "squeue"): cluster_c_squeue,
                    ("cluster-c", "scontrol"): "PrivateData             = none\n",
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["mine", "--account", "proj-123", "--json"],
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
            cluster_c_calls = [call for call in runner.calls if call[0] == "cluster-c"]
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
                            "-A",
                            "proj-123",
                            "-O",
                            MINE_SQUEUE_FIELDS,
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
                            "-A",
                            "proj-123",
                            "-O",
                            MINE_SQUEUE_FIELDS,
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
                cluster_c_calls,
                [
                    (
                        "cluster-c",
                        "local",
                        None,
                        [
                            "env",
                            "SLURM_TIME_FORMAT=%s",
                            "squeue",
                            "-h",
                            "-r",
                            "-A",
                            "proj-123",
                            "-O",
                            MINE_SQUEUE_FIELDS,
                        ],
                    ),
                    (
                        "cluster-c",
                        "local",
                        None,
                        ["env", "SLURM_TIME_FORMAT=%s", "scontrol", "show", "config"],
                    ),
                ],
            )
            payload = json.loads(json_out.getvalue())
            self.assertEqual(payload["command"], "mine")
            self.assertEqual(payload["account"], "proj-123")
            clusters = payload["clusters"]
            self.assertEqual(
                [
                    (c["cluster"], c["user_scoped"], c["slurm_user"], c["account_filter"], len(c["jobs"]))
                    for c in clusters
                ],
                [
                    ("cluster-a", True, None, "proj-123", 0),
                    ("cluster-b", True, None, "proj-123", 2),
                    ("cluster-c", False, None, "proj-123", 2),
                ],
            )

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["mine", "--account", "proj-123"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            rendered = table_out.getvalue()
            self.assertIn("cluster-a [user-scoped]\n  (no jobs)\n\ncluster-b [user-scoped]\n", rendered)
            self.assertIn("\n\ncluster-c\n", rendered)

    def test_mine_rejects_invalid_account_values_with_exit_two_before_runner(self) -> None:
        bad_argvs = [
            ("empty", ["mine", "--account", ""]),
            ("leading-dash-arg", ["mine", "--account", "-proj"]),
            ("leading-dash-eq", ["mine", "--account=-proj"]),
            ("leading-dash-digit", ["mine", "--account", "-1"]),
            ("space", ["mine", "--account", "proj 123"]),
            ("tab", ["mine", "--account", "proj\t123"]),
            ("newline", ["mine", "--account", "proj\n123"]),
            ("comma", ["mine", "--account", "proj,123"]),
            ("ascii-bell", ["mine", "--account", "proj\x07123"]),
            ("ascii-del", ["mine", "--account", "proj\x7f123"]),
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            for label, argv in bad_argvs:
                with self.subTest(case=label):
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
                    self.assertNotEqual(stderr.getvalue().strip(), "")
                    self.assertEqual(runner.calls, [])

    def test_mine_skips_cluster_banner_and_unusable_rows_preserves_pipe_in_reason_and_isolates_broken_output(
        self,
    ) -> None:
        cluster_a_squeue = (
            "CLUSTER: prod\n"
            "6001|admin_hold|alice|proj-123|gpu|PD|0:00|04:00:00|1|16|"
            "cpu=16,gres/gpu=1|0|Held by admin | ticket 42|\n"
            "6002|blank_reason|alice|proj-123|cpu|RH|0:00|01:00:00|1|4|cpu=4|N/A||\n"
            "|missing_id|alice|proj-123|cpu|R|01:00|02:00:00|1|8|cpu=8|1794513000|None|\n"
            "6091|missing_part|alice|proj-123| , |R|01:00|02:00:00|1|8|cpu=8|1794513000|None|\n"
            "6092|bad_cpus|alice|proj-123|cpu|R|01:00|02:00:00|1|N/A|cpu=8|1794513000|None|\n"
            "6093|bad_elapsed|alice|proj-123|cpu|R|INVALID|02:00:00|1|8|cpu=8|1794513000|None|\n"
            "6094|finished_cd|alice|proj-123|cpu|CD|10:00|02:00:00|1|8|cpu=8|1794513000|None|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\nslurm_cluster = "prod"\n\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n\n'
                '[[clusters]]\nname = "cluster-c"\nlocal = true\n',
                encoding="utf-8",
            )
            runner = FakeRunner(
                {
                    ("cluster-a", "squeue"): cluster_a_squeue,
                    ("cluster-b", "squeue"): "1001|too_few_columns|alice|\n",
                    ("cluster-c", "squeue"): "unexpected proxy banner without trailing pipe\n",
                }
            )

            json_out = io.StringIO()
            json_err = io.StringIO()
            json_rc = main(
                ["mine", "--json"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=json_out,
                stderr=json_err,
            )

            self.assertEqual(json_rc, 0)
            self.assertEqual(json_err.getvalue(), "")
            clusters = json.loads(json_out.getvalue())["clusters"]
            self.assertEqual(
                clusters[0],
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
                            "job_id": "6001",
                            "name": "admin_hold",
                            "user": "alice",
                            "account": "proj-123",
                            "partition": "gpu",
                            "state": "PD",
                            "cpus": 16,
                            "gpus": 1,
                            "nodes": 1,
                            "elapsed_seconds": 0,
                            "time_limit_seconds": 14400,
                            "reason": "Held by admin | ticket 42",
                            "estimated_start": None,
                        },
                        {
                            "job_id": "6002",
                            "name": "blank_reason",
                            "user": "alice",
                            "account": "proj-123",
                            "partition": "cpu",
                            "state": "RH",
                            "cpus": 4,
                            "gpus": 0,
                            "nodes": 1,
                            "elapsed_seconds": 0,
                            "time_limit_seconds": 3600,
                            "reason": "None",
                            "estimated_start": None,
                        },
                    ],
                },
            )
            for broken in clusters[1:]:
                self.assertIsNone(broken["fetched_at"])
                self.assertFalse(broken["stale"])
                self.assertFalse(broken["user_scoped"])
                self.assertIsNone(broken["slurm_user"])
                self.assertIsNone(broken["account_filter"])
                self.assertEqual(broken["jobs"], [])
                self.assertIsNotNone(broken["error"])
                self.assertIn("squeue", broken["error"])

            table_out = io.StringIO()
            table_err = io.StringIO()
            table_rc = main(
                ["mine"],
                env=make_test_env(root),
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=table_out,
                stderr=table_err,
            )
            self.assertEqual(table_rc, 0)
            self.assertEqual(table_err.getvalue(), "")
            rendered = table_out.getvalue()
            self.assertIn("Held by admin | ticket 42", rendered)
            self.assertIn(f"cluster-b: {clusters[1]['error']}\n\ncluster-c: {clusters[2]['error']}\n", rendered)

    def test_mine_caches_healthy_clusters_isolates_account_key_retries_errors_and_honors_refresh(
        self,
    ) -> None:
        me_initial = (
            "7001|job_me_1|alice|proj-123|gpu|R|10:00|02:00:00|1|16|"
            "cpu=16,gres/gpu=2|1794513000|None|\n"
        )
        me_refreshed = (
            "7001|job_me_1|alice|proj-123|gpu|R|12:00|02:00:00|1|16|"
            "cpu=16,gres/gpu=2|1794513000|None|\n"
            "7002|job_me_2|alice|proj-123|gpu|PD|0:00|01:00:00|1|8|"
            "cpu=8,gres/gpu=1|1794536100|Priority|\n"
        )
        account_rows = (
            "7001|job_me_1|alice|proj-123|gpu|R|11:30|02:00:00|1|16|"
            "cpu=16,gres/gpu=2|1794513000|None|\n"
            "7010|teammate_job|bob|proj-123|gpu|R|05:00|02:00:00|1|16|"
            "cpu=16,gres/gpu=2|1794513300|None|\n"
        )
        cluster_b_recovered = (
            "8001|remote_job|alice_hpc|proj-123|cpu|PD|0:00|01:00:00|1|4|cpu=4|N/A|Resources|\n"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n',
                encoding="utf-8",
            )
            shared_env = make_test_env(root)
            me_argv = (
                "env",
                "SLURM_TIME_FORMAT=%s",
                "squeue",
                "-h",
                "-r",
                "--me",
                "-O",
                MINE_SQUEUE_FIELDS,
            )
            acct_argv = (
                "env",
                "SLURM_TIME_FORMAT=%s",
                "squeue",
                "-h",
                "-r",
                "-A",
                "proj-123",
                "-O",
                MINE_SQUEUE_FIELDS,
            )
            runner = FakeRunner(
                {
                    ("cluster-a", *me_argv): me_initial,
                    ("cluster-a", *acct_argv): account_rows,
                    ("cluster-a", "scontrol"): "PrivateData             = none\n",
                    ("cluster-b", "squeue"): TransportError("ssh: Connection timed out"),
                }
            )

            # 1. t = 0s: cluster-a succeeds and caches; cluster-b fails and is not cached.
            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["mine", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            p1 = json.loads(out1.getvalue())["clusters"]
            self.assertIsNone(p1[0]["error"])
            self.assertFalse(p1[0]["stale"])
            self.assertEqual(p1[1]["error"], "unreachable (ssh: Connection timed out)")

            # 2. t = 90s: --account proj-123 must NOT reuse --me cache entry.
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["mine", "--account", "proj-123", "--cluster", "cluster-a", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: FIXED_NOW + timedelta(seconds=90),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 4)
            p2 = json.loads(out2.getvalue())
            self.assertEqual(p2["account"], "proj-123")
            self.assertEqual(len(p2["clusters"]), 1)
            self.assertFalse(p2["clusters"][0]["stale"])
            self.assertEqual(p2["clusters"][0]["account_filter"], "proj-123")
            self.assertIsNone(p2["clusters"][0]["slurm_user"])
            self.assertEqual(len(p2["clusters"][0]["jobs"]), 2)

            # 3. t = 120s: cluster-a served from cache (stale=True), cluster-b retried and succeeds.
            runner._responses[("cluster-a", *me_argv)] = me_refreshed
            runner._responses[("cluster-b", "squeue")] = cluster_b_recovered
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["mine"],
                env=shared_env,
                runner=runner,
                clock=lambda: FIXED_NOW + timedelta(seconds=120),
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            # Only cluster-b was called; cluster-a was served from cache!
            self.assertEqual(len(runner.calls), 5)
            self.assertEqual(runner.calls[-1][0], "cluster-b")
            rendered3 = out3.getvalue()
            self.assertIn("cluster-a [cached 120s ago]\n", rendered3)
            self.assertNotIn("job_me_2", rendered3)
            self.assertIn("cluster-b\n", rendered3)
            self.assertIn("remote_job", rendered3)

            # 4. t = 120s with --refresh: bypasses cache on cluster-a and fetches updated jobs.
            out4, err4 = io.StringIO(), io.StringIO()
            rc4 = main(
                ["mine", "--refresh", "--cluster", "cluster-a", "--json"],
                env=shared_env,
                runner=runner,
                clock=lambda: FIXED_NOW + timedelta(seconds=120),
                stdout=out4,
                stderr=err4,
            )
            self.assertEqual(rc4, 0)
            self.assertEqual(err4.getvalue(), "")
            self.assertEqual(len(runner.calls), 6)
            p4_cluster = json.loads(out4.getvalue())["clusters"][0]
            self.assertFalse(p4_cluster["stale"])
            self.assertEqual(len(p4_cluster["jobs"]), 2)
