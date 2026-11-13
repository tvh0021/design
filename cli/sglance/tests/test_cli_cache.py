from __future__ import annotations

from datetime import UTC, datetime, timedelta
import io
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import threading
import time
import unittest

from sglance import CommandResult, TransportError, main
from tests.fakes import FakeRunner, make_test_env

BASE_TIME = datetime(2026, 11, 12, 20, 0, 0, tzinfo=UTC)

SINFO_INITIAL = (
    "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n"
    "cpu|node02|idle|0/64/0/64|(null)|(null)|\n"
)

SINFO_UPDATED = (
    "cpu|node01|allocated|64/0/0/64|(null)|(null)|\n"
    "cpu|node02|allocated|64/0/0/64|(null)|(null)|\n"
)


class _DynamicFakeRunner(FakeRunner):
    def set_response(self, key: tuple[str, ...], response: object) -> None:
        self._responses[key] = response  # type: ignore[assignment]


class CacheCliTests(unittest.TestCase):
    def test_status_reuses_xdg_cache_within_299s_and_refetches_at_301s(self) -> None:
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
            runner = _DynamicFakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})

            # Run 1 at t = 0s: fresh fetch, stale is False.
            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(
                json.loads(out1.getvalue()),
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
                        }
                    ],
                },
            )

            runner.set_response(("cluster-a", "sinfo"), SINFO_UPDATED)

            # Run 2 at t = 299s: served from cache, runner not invoked again, stale is True.
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=299),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(
                json.loads(out2.getvalue()),
                {
                    "schema_version": 1,
                    "command": "status",
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": True,
                            "user_scoped": False,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "default": False,
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
                        }
                    ],
                },
            )

            # Run 3 at t = 301s: cache expired, runner called again, stale is False.
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=301),
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(
                json.loads(out3.getvalue()),
                {
                    "schema_version": 1,
                    "command": "status",
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": "2026-11-12T20:05:01Z",
                            "stale": False,
                            "user_scoped": False,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "default": False,
                                    "cpus_alloc": 128,
                                    "cpus_total": 128,
                                    "cpus_utilization": 1.0,
                                    "gpus_alloc": 0,
                                    "gpus_total": 0,
                                    "gpus_utilization": None,
                                    "nodes_alloc": 2,
                                    "nodes_mixed": 0,
                                    "nodes_idle": 0,
                                    "nodes_down": 0,
                                    "nodes_drained": 0,
                                    "nodes_draining": 0,
                                    "nodes_other": 0,
                                    "nodes_total": 2,
                                }
                            ],
                        }
                    ],
                },
            )

    def test_status_table_marks_cached_cluster_header_with_cache_age(self) -> None:
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
            runner = FakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})

            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(
                out1.getvalue(),
                "cluster-a\n"
                "  PARTITION  CPUS          GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu        64/128 (50%)  -     2      1      0      1     0     0        0         0\n",
            )

            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=65),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(
                out2.getvalue(),
                "cluster-a [cached 65s ago]\n"
                "  PARTITION  CPUS          GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu        64/128 (50%)  -     2      1      0      1     0     0        0         0\n",
            )

    def test_refresh_flag_bypasses_cache_at_60s_and_updates_stored_snapshot(self) -> None:
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
            runner = _DynamicFakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})

            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)

            runner.set_response(("cluster-a", "sinfo"), SINFO_UPDATED)

            # At 60s, --refresh forces a live query, returns stale=False, and overwrites the cache.
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status", "--refresh", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=60),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            payload2 = json.loads(out2.getvalue())
            self.assertEqual(
                payload2["clusters"][0]["fetched_at"],
                "2026-11-12T20:01:00Z",
            )
            self.assertFalse(payload2["clusters"][0]["stale"])
            self.assertEqual(payload2["clusters"][0]["partitions"][0]["cpus_alloc"], 128)

            # At 120s without --refresh, cache hit serves the 60s refreshed snapshot.
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=120),
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            payload3 = json.loads(out3.getvalue())
            self.assertEqual(
                payload3["clusters"][0]["fetched_at"],
                "2026-11-12T20:01:00Z",
            )
            self.assertTrue(payload3["clusters"][0]["stale"])
            self.assertEqual(payload3["clusters"][0]["partitions"][0]["cpus_alloc"], 128)

            # Top-level --refresh before subcommand also forces a fresh query.
            out4, err4 = io.StringIO(), io.StringIO()
            rc4 = main(
                ["--refresh", "status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=150),
                stdout=out4,
                stderr=err4,
            )
            self.assertEqual(rc4, 0)
            self.assertEqual(err4.getvalue(), "")
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(
                out4.getvalue(),
                "cluster-a\n"
                "  PARTITION  CPUS            GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
                "  cpu        128/128 (100%)  -     2      2      0      0     0     0        0         0\n",
            )

    def test_corrupt_or_unreadable_cache_files_recover_as_miss_and_store_only_json_fields(self) -> None:
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
            runner = FakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})

            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)

            cache_sglance_dir = root / "cache" / "sglance"
            cache_files = [p for p in cache_sglance_dir.iterdir() if p.suffix == ".json"]
            self.assertEqual(len(cache_files), 1)
            cache_file = cache_files[0]

            # Cache file must not contain anything beyond what --json prints.
            cli_cluster_json = json.loads(out1.getvalue())["clusters"][0]
            stored_json = json.loads(cache_file.read_text(encoding="utf-8"))
            self.assertEqual(stored_json, cli_cluster_json)

            # 1. Truncated JSON is treated as a cache miss.
            cache_file.write_text("{corrupt-json", encoding="utf-8")
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=30),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(out2.getvalue().startswith("cluster-a\n"))

            # 2. Wrong field types in JSON are treated as a cache miss.
            bad_types = dict(cli_cluster_json)
            bad_types["partitions"] = [{**cli_cluster_json["partitions"][0], "cpus_alloc": "bad"}]
            cache_file.write_text(json.dumps(bad_types), encoding="utf-8")
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=45),
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertEqual(len(runner.calls), 3)
            self.assertTrue(out3.getvalue().startswith("cluster-a\n"))

            # 3. Non-finite NaN token in corrupt cache JSON is treated as a miss, never a crash.
            nan_text = cache_file.read_text(encoding="utf-8").replace("0.5", "NaN")
            cache_file.write_text(nan_text, encoding="utf-8")
            out4, err4 = io.StringIO(), io.StringIO()
            rc4 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=60),
                stdout=out4,
                stderr=err4,
            )
            self.assertEqual(rc4, 0)
            self.assertEqual(err4.getvalue(), "")
            self.assertEqual(len(runner.calls), 4)
            self.assertTrue(out4.getvalue().startswith("cluster-a\n"))

            # 4. Unreadable cache file (mode 0o000) is treated as a miss and atomically replaced.
            if os.geteuid() != 0:
                cache_file.chmod(0o000)
                try:
                    out5, err5 = io.StringIO(), io.StringIO()
                    rc5 = main(
                        ["status", "--json"],
                        env=env,
                        runner=runner,
                        clock=lambda: BASE_TIME + timedelta(seconds=75),
                        stdout=out5,
                        stderr=err5,
                    )
                    self.assertEqual(rc5, 0)
                    self.assertEqual(err5.getvalue(), "")
                    self.assertEqual(len(runner.calls), 5)
                    self.assertFalse(json.loads(out5.getvalue())["clusters"][0]["stale"])
                finally:
                    if cache_file.exists():
                        cache_file.chmod(0o600)

    def test_failed_cluster_is_not_cached_while_healthy_cluster_serves_from_cache(self) -> None:
        sinfo_cluster_b = "debug|node10|mixed|16/16/0/32|(null)|(null)|\n"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-b"\nssh = "cluster-b"\n',
                encoding="utf-8",
            )
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = _DynamicFakeRunner(
                {
                    ("cluster-a", "sinfo"): SINFO_INITIAL,
                    ("cluster-b", "sinfo"): TransportError("timed out after 25s"),
                }
            )

            # Run 1 at t = 0s: cluster-a succeeds and is cached; cluster-b fails and is not cached.
            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            clusters1 = json.loads(out1.getvalue())["clusters"]
            self.assertEqual(clusters1[0]["cluster"], "cluster-a")
            self.assertFalse(clusters1[0]["stale"])
            self.assertIsNone(clusters1[0]["error"])
            self.assertEqual(clusters1[1]["cluster"], "cluster-b")
            self.assertIsNone(clusters1[1]["fetched_at"])
            self.assertFalse(clusters1[1]["stale"])
            self.assertEqual(clusters1[1]["error"], "unreachable (timed out after 25s)")

            # Run 2 at t = 60s after cluster-b recovers: cluster-a served from cache, only cluster-b queried.
            runner.set_response(("cluster-b", "sinfo"), sinfo_cluster_b)
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=60),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(runner.calls[2][0], "cluster-b")
            self.assertEqual(
                json.loads(out2.getvalue()),
                {
                    "schema_version": 1,
                    "command": "status",
                    "clusters": [
                        {
                            "cluster": "cluster-a",
                            "fetched_at": "2026-11-12T20:00:00Z",
                            "stale": True,
                            "user_scoped": False,
                            "error": None,
                            "partitions": [
                                {
                                    "partition": "cpu",
                                    "default": False,
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
                            "fetched_at": "2026-11-12T20:01:00Z",
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

            # Nonzero sinfo exit code is also never cached on a third cluster or subsequent failure.
            runner.set_response(
                ("cluster-b", "sinfo"),
                CommandResult(1, "", "sinfo: error: controller busy\n"),
            )
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["status", "--refresh"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=70),
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertIn("cluster-b: sinfo exited 1: error: controller busy", out3.getvalue())

    def test_cache_falls_back_to_home_dot_cache_and_isolates_cluster_target_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            config_file = config_dir / "config.toml"
            config_file.write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
                encoding="utf-8",
            )
            # XDG_CACHE_HOME omitted: falls back to $HOME/.cache/sglance.
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): SINFO_INITIAL,
                    ("cluster-b", "sinfo"): "debug|node10|idle|0/32/0/32|(null)|(null)|\n",
                }
            )

            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertTrue((root / "home" / ".cache" / "sglance").is_dir())

            # --cluster cluster-a hits the per-cluster snapshot already stored by full status.
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status", "--cluster", "cluster-a", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=30),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(json.loads(out2.getvalue())["clusters"][0]["stale"])

            # Changing slurm_cluster on cluster-a alters its cache key and triggers a fresh runner call.
            config_file.write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\nslurm_cluster = "prod"\n\n'
                '[[clusters]]\nname = "cluster-b"\nlocal = true\n',
                encoding="utf-8",
            )
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["status", "--cluster", "cluster-a", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=45),
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertEqual(len(runner.calls), 3)
            self.assertFalse(json.loads(out3.getvalue())["clusters"][0]["stale"])

    def test_deeply_nested_and_oversized_cache_files_recover_as_miss(self) -> None:
        expected_healthy_table = (
            "cluster-a\n"
            "  PARTITION  CPUS          GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
            "  cpu        64/128 (50%)  -     2      1      0      1     0     0        0         0\n"
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
            runner = FakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})

            # First run populates the cache entry.
            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(out1.getvalue(), expected_healthy_table)

            cache_sglance_dir = root / "cache" / "sglance"
            json_files = [p for p in cache_sglance_dir.iterdir() if p.suffix == ".json"]
            self.assertEqual(len(json_files), 1)
            cache_file = json_files[0]
            valid_snapshot_text = cache_file.read_text(encoding="utf-8")

            # Deeply nested JSON triggers RecursionError in json.loads -> treated as miss.
            cache_file.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=15),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(out2.getvalue(), expected_healthy_table)

            # Valid JSON padded past 4 MB cap is skipped without parsing -> treated as miss.
            oversized_padding = " " * (4 * 1024 * 1024 + 1)
            cache_file.write_text(oversized_padding + valid_snapshot_text, encoding="utf-8")
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=30),
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertEqual(len(runner.calls), 3)
            self.assertEqual(out3.getvalue(), expected_healthy_table)

    def test_concurrent_cold_misses_coalesce_to_single_runner_call(self) -> None:
        num_threads = 6
        start_barrier = threading.Barrier(num_threads, timeout=5.0)

        class _SlowFakeRunner(FakeRunner):
            def run(self, cluster_entry: object, argv: list[str]) -> CommandResult:  # type: ignore[override]
                time.sleep(0.08)
                return super().run(cluster_entry, argv)  # type: ignore[arg-type]

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
            runner = _SlowFakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})

            results: list[tuple[int, str, str]] = [(-1, "", "")] * num_threads
            errors: list[BaseException] = []
            errors_lock = threading.Lock()

            def worker(idx: int) -> None:
                try:
                    start_barrier.wait()
                    out, err = io.StringIO(), io.StringIO()
                    rc = main(
                        ["status", "--json"],
                        env=env,
                        runner=runner,
                        clock=lambda: BASE_TIME,
                        stdout=out,
                        stderr=err,
                    )
                    results[idx] = (rc, out.getvalue(), err.getvalue())
                except BaseException as exc:  # noqa: BLE001
                    with errors_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15.0)
                self.assertFalse(t.is_alive())

            self.assertEqual(errors, [])
            self.assertEqual(len(runner.calls), 1)

            stale_flags: list[bool] = []
            for rc, raw_out, raw_err in results:
                self.assertEqual(rc, 0)
                self.assertEqual(raw_err, "")
                payload = json.loads(raw_out)
                cluster_payload = payload["clusters"][0]
                stale_flags.append(cluster_payload["stale"])
                self.assertEqual(
                    {k: v for k, v in cluster_payload.items() if k != "stale"},
                    {
                        "cluster": "cluster-a",
                        "fetched_at": "2026-11-12T20:00:00Z",
                        "user_scoped": False,
                        "error": None,
                        "partitions": [
                            {
                                "partition": "cpu",
                                "default": False,
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
                )
            # Exactly one caller performed the live fetch (stale=False); the other 5 hit the cache (stale=True).
            self.assertEqual(sorted(stale_flags), [False] + [True] * (num_threads - 1))

            # If locking fails for any reason (e.g., <digest>.lock is a directory), cold fetch still succeeds.
            cache_sglance_dir = root / "cache" / "sglance"
            for item in list(cache_sglance_dir.iterdir()):
                if item.suffix == ".json":
                    item.unlink()
                elif item.suffix == ".lock":
                    item.unlink()
                    item.mkdir()
            fallback_out, fallback_err = io.StringIO(), io.StringIO()
            fallback_rc = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=10),
                stdout=fallback_out,
                stderr=fallback_err,
            )
            self.assertEqual(fallback_rc, 0)
            self.assertEqual(fallback_err.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertFalse(json.loads(fallback_out.getvalue())["clusters"][0]["stale"])

    def test_cache_write_and_serialization_failures_preserve_live_result_and_clean_temp_files(
        self,
    ) -> None:
        from sglance.cache import cached_cluster_fetch
        from sglance.models import ClusterEntry, ClusterStatus, PartitionStatus

        expected_healthy_table = (
            "cluster-a\n"
            "  PARTITION  CPUS          GPUS  NODES  ALLOC  MIXED  IDLE  DOWN  DRAINED  DRAINING  OTHER\n"
            "  cpu        64/128 (50%)  -     2      1      0      1     0     0        0         0\n"
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
            runner = FakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})

            # Populate initial cache file to locate its exact digest path.
            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            cache_sglance_dir = root / "cache" / "sglance"
            json_files = [p for p in cache_sglance_dir.iterdir() if p.suffix == ".json"]
            self.assertEqual(len(json_files), 1)
            cache_file = json_files[0]

            # Replace <digest>.json with a directory so os.replace fails after writing .tmp.
            cache_file.unlink()
            cache_file.mkdir()

            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=15),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(out2.getvalue(), expected_healthy_table)
            tmp_files = [p for p in cache_sglance_dir.iterdir() if p.suffix == ".tmp"]
            self.assertEqual(tmp_files, [])
            cache_file.rmdir()

            # Serialization failures (ValueError on NaN, TypeError on unserializable field)
            # must also preserve the live result without raising or leaving .tmp files.
            entry = ClusterEntry(name="cluster-a", ssh="login.example.edu")
            nan_status = ClusterStatus(
                cluster="cluster-a",
                fetched_at="2026-11-12T20:00:30Z",
                stale=False,
                user_scoped=False,
                error=None,
                partitions=(
                    PartitionStatus(
                        partition="cpu",
                        default=False,
                        cpus_alloc=64,
                        cpus_total=128,
                        cpus_utilization=float("nan"),
                        gpus_alloc=0,
                        gpus_total=0,
                        gpus_utilization=None,
                        nodes_alloc=1,
                        nodes_mixed=0,
                        nodes_idle=1,
                        nodes_down=0,
                        nodes_drained=0,
                        nodes_draining=0,
                        nodes_other=0,
                        nodes_total=2,
                    ),
                ),
            )
            got_nan = cached_cluster_fetch(
                entry,
                command="status",
                result_type=ClusterStatus,
                fetch=lambda _target: nan_status,
                env=env,
                now=BASE_TIME + timedelta(seconds=30),
                schema_version=1,
            )
            self.assertEqual(got_nan.cluster, "cluster-a")
            self.assertIsNone(got_nan.error)
            self.assertEqual([p for p in cache_sglance_dir.iterdir() if p.suffix == ".tmp"], [])

            unserializable_status = ClusterStatus(
                cluster="cluster-a",
                fetched_at="2026-11-12T20:00:45Z",
                stale=False,
                user_scoped=False,
                error=None,
                partitions=(object(),),  # type: ignore[arg-type]
            )
            got_type_err = cached_cluster_fetch(
                entry,
                command="status",
                result_type=ClusterStatus,
                fetch=lambda _target: unserializable_status,
                env=env,
                now=BASE_TIME + timedelta(seconds=45),
                schema_version=1,
            )
            self.assertEqual(got_type_err.cluster, "cluster-a")
            self.assertIsNone(got_type_err.error)
            self.assertEqual([p for p in cache_sglance_dir.iterdir() if p.suffix == ".tmp"], [])

    def test_cached_cluster_fetch_requires_explicit_schema_version_and_isolates_versions(
        self,
    ) -> None:
        from sglance.cache import cached_cluster_fetch
        from sglance.models import ClusterEntry, ClusterStatus

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
            entry = ClusterEntry(name="cluster-a", ssh="login.example.edu")

            # Omitting schema_version must raise TypeError (no default allowed).
            with self.assertRaises(TypeError):
                cached_cluster_fetch(  # type: ignore[call-arg]
                    entry,
                    command="status",
                    result_type=ClusterStatus,
                    fetch=lambda _target: ClusterStatus(
                        cluster="cluster-a",
                        fetched_at="2026-11-12T20:00:00Z",
                        stale=False,
                        user_scoped=False,
                        error=None,
                        partitions=(),
                    ),
                    env=env,
                    now=BASE_TIME,
                )

            # Seed schema_version=2 for status; CLI status (schema_version=1) must miss and fetch live.
            v2_calls = 0

            def fetch_v2(_target: ClusterEntry) -> ClusterStatus:
                nonlocal v2_calls
                v2_calls += 1
                return ClusterStatus(
                    cluster="cluster-a",
                    fetched_at="2026-11-12T20:00:00Z",
                    stale=False,
                    user_scoped=False,
                    error=None,
                    partitions=(),
                )

            cached_cluster_fetch(
                entry,
                command="status",
                result_type=ClusterStatus,
                fetch=fetch_v2,
                env=env,
                now=BASE_TIME,
                schema_version=2,
            )
            self.assertEqual(v2_calls, 1)

            runner = FakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})
            out, err = io.StringIO(), io.StringIO()
            rc = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=10),
                stdout=out,
                stderr=err,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(len(runner.calls), 1)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["schema_version"], 1)
            self.assertFalse(payload["clusters"][0]["stale"])
            self.assertEqual(len(payload["clusters"][0]["partitions"]), 1)

    @unittest.skipIf(os.name != "posix", "POSIX permissions are not enforced on non-POSIX systems")
    def test_cache_directory_and_files_enforce_0700_and_0600_modes(self) -> None:
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
            runner = FakeRunner({("cluster-a", "sinfo"): SINFO_INITIAL})

            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            self.assertEqual(err1.getvalue(), "")

            cache_sglance_dir = root / "cache" / "sglance"
            self.assertEqual(stat.S_IMODE(cache_sglance_dir.stat().st_mode), 0o700)

            entries = list(cache_sglance_dir.iterdir())
            self.assertTrue(any(p.suffix == ".json" for p in entries))
            for path in entries:
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    0o600,
                    f"expected 0o600 for {path.name}",
                )

            # Even if existing permissions were widened externally, refreshing restores 0o700 / 0o600.
            cache_sglance_dir.chmod(0o755)
            for path in entries:
                path.chmod(0o644)

            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["status", "--refresh", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME + timedelta(seconds=30),
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2.getvalue(), "")
            self.assertEqual(stat.S_IMODE(cache_sglance_dir.stat().st_mode), 0o700)
            for path in cache_sglance_dir.iterdir():
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    0o600,
                    f"expected 0o600 after refresh for {path.name}",
                )

    def test_single_xdg_cache_home_isolates_subcommands_and_options_and_serves_hits_for_all_four(
        self,
    ) -> None:
        mine_fields = (
            "JobArrayID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
            "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,NumTasks:0|,"
            "tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,tres-per-task:0|,"
            "StartTime:0|,Reason:0|,Name:0|"
        )
        queue_squeue_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-h",
            "-r",
            "-O",
            "JobID:0|,UserName:0|,Partition:0|,StateCompact:0|,Reason:0|",
        )
        mine_me_argv = ("env", "SLURM_TIME_FORMAT=%s", "squeue", "-h", "--me", "-O", mine_fields)
        mine_acct_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "squeue",
            "-h",
            "-A",
            "proj-123",
            "-O",
            mine_fields,
        )
        wait_sacct_7d_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "sacct",
            "-X",
            "-P",
            "-n",
            "-a",
            "-S",
            "now-7days",
            "--state=CD,F,TO,CA,OOM,NF,PR,R",
            "--format=JobID,User,Partition,State,Eligible,Start,AllocTRES",
        )
        wait_sacct_14d_argv = (
            "env",
            "SLURM_TIME_FORMAT=%s",
            "sacct",
            "-X",
            "-P",
            "-n",
            "-a",
            "-S",
            "now-14days",
            "--state=CD,F,TO,CA,OOM,NF,PR,R",
            "--format=JobID,User,Partition,State,Eligible,Start,AllocTRES",
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

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "cluster-a.example"\n',
                encoding="utf-8",
            )
            env = make_test_env(root)
            runner = FakeRunner(
                {
                    ("cluster-a", "sinfo"): SINFO_INITIAL,
                    ("cluster-a", *queue_squeue_argv): (
                        "1001|alice|cpu|R|None|\n"
                        "1002|bob|cpu|PD|Resources|\n"
                    ),
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                    ("cluster-a", *mine_me_argv): (
                        "2001|alice|proj-123|cpu|R|10:00|01:00:00|1|16|1|"
                        "cpu=16|N/A|N/A|N/A|1794513000|None|me_job|\n"
                    ),
                    ("cluster-a", *mine_acct_argv): (
                        "2001|alice|proj-123|cpu|R|10:00|01:00:00|1|16|1|"
                        "cpu=16|N/A|N/A|N/A|1794513000|None|me_job|\n"
                        "2002|bob|proj-123|cpu|PD|0:00|01:00:00|1|8|1|"
                        "cpu=8|N/A|N/A|N/A|N/A|Priority|acct_job|\n"
                    ),
                    ("cluster-a", *wait_sacct_7d_argv): (
                        "3001|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n"
                        "3002|bob|cpu|COMPLETED|1794501000|1794501120|cpu=8\n"
                    ),
                    ("cluster-a", *wait_sacct_14d_argv): (
                        "3001|alice|cpu|COMPLETED|1794500000|1794500060|cpu=8\n"
                        "3002|bob|cpu|COMPLETED|1794501000|1794501120|cpu=8\n"
                        "3003|alice|cpu|COMPLETED|1793600000|1793600300|cpu=8\n"
                    ),
                    ("cluster-a", *wait_squeue_argv): "",
                }
            )

            def invoke(argv: list[str], offset_seconds: int) -> dict[str, object]:
                out, err = io.StringIO(), io.StringIO()
                rc = main(
                    argv,
                    env=env,
                    runner=runner,
                    clock=lambda: BASE_TIME + timedelta(seconds=offset_seconds),
                    stdout=out,
                    stderr=err,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(err.getvalue(), "")
                return json.loads(out.getvalue())

            # status then queue do not share an entry; each repeats from cache within 5 minutes.
            status_1 = invoke(["status", "--json"], 0)
            self.assertEqual(len(runner.calls), 1)
            self.assertFalse(status_1["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(status_1["clusters"][0]["partitions"][0]["cpus_alloc"], 64)  # type: ignore[index]

            queue_1 = invoke(["queue", "--json"], 10)
            self.assertEqual(len(runner.calls), 3)
            self.assertFalse(queue_1["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(
                queue_1["clusters"][0]["partitions"][0]["running_jobs"],  # type: ignore[index]
                1,
            )

            status_2 = invoke(["status", "--json"], 20)
            self.assertEqual(len(runner.calls), 3)
            self.assertTrue(status_2["clusters"][0]["stale"])  # type: ignore[index]

            queue_2 = invoke(["queue", "--json"], 30)
            self.assertEqual(len(runner.calls), 3)
            self.assertTrue(queue_2["clusters"][0]["stale"])  # type: ignore[index]

            # mine vs mine --account proj-123 do not share; each repeats from cache within 5 minutes.
            mine_1 = invoke(["mine", "--json"], 40)
            self.assertEqual(len(runner.calls), 4)
            self.assertFalse(mine_1["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(len(mine_1["clusters"][0]["jobs"]), 1)  # type: ignore[index]

            mine_acct_1 = invoke(["mine", "--account", "proj-123", "--json"], 50)
            self.assertEqual(len(runner.calls), 6)
            self.assertFalse(mine_acct_1["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(len(mine_acct_1["clusters"][0]["jobs"]), 2)  # type: ignore[index]

            mine_2 = invoke(["mine", "--json"], 60)
            self.assertEqual(len(runner.calls), 6)
            self.assertTrue(mine_2["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(len(mine_2["clusters"][0]["jobs"]), 1)  # type: ignore[index]

            mine_acct_2 = invoke(["mine", "--account", "proj-123", "--json"], 70)
            self.assertEqual(len(runner.calls), 6)
            self.assertTrue(mine_acct_2["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(len(mine_acct_2["clusters"][0]["jobs"]), 2)  # type: ignore[index]

            # wait --days 7 vs wait --days 14 do not share; each repeats from cache within 5 minutes.
            wait_7_1 = invoke(["wait", "--days", "7", "--json"], 80)
            self.assertEqual(len(runner.calls), 9)
            self.assertFalse(wait_7_1["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(wait_7_1["clusters"][0]["partitions"][0]["n"], 2)  # type: ignore[index]
            self.assertEqual(wait_7_1["clusters"][0]["partitions"][0]["p50_seconds"], 90.0)  # type: ignore[index]

            wait_14_1 = invoke(["wait", "--days", "14", "--json"], 90)
            self.assertEqual(len(runner.calls), 12)
            self.assertFalse(wait_14_1["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(wait_14_1["clusters"][0]["partitions"][0]["n"], 3)  # type: ignore[index]
            self.assertEqual(wait_14_1["clusters"][0]["partitions"][0]["p50_seconds"], 120.0)  # type: ignore[index]

            wait_7_2 = invoke(["wait", "--days", "7", "--json"], 100)
            self.assertEqual(len(runner.calls), 12)
            self.assertTrue(wait_7_2["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(wait_7_2["clusters"][0]["partitions"][0]["n"], 2)  # type: ignore[index]

            wait_14_2 = invoke(["wait", "--days", "14", "--json"], 110)
            self.assertEqual(len(runner.calls), 12)
            self.assertTrue(wait_14_2["clusters"][0]["stale"])  # type: ignore[index]
            self.assertEqual(wait_14_2["clusters"][0]["partitions"][0]["n"], 3)  # type: ignore[index]

    def test_cache_write_prunes_entries_older_than_one_day_and_ignores_foreign_files_and_errors(
        self,
    ) -> None:
        from unittest import mock

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "cluster-a.example"\n',
                encoding="utf-8",
            )
            env = make_test_env(root)
            runner = _DynamicFakeRunner(
                {
                    ("cluster-a", "sinfo"): SINFO_INITIAL,
                    ("cluster-a", "squeue"): (
                        "1001|alice|cpu|R|None|\n"
                        "1002|bob|cpu|PD|Resources|\n"
                    ),
                    ("cluster-a", "scontrol"): "PrivateData = none\n",
                }
            )

            # t = 0: write status snapshot.
            out1, err1 = io.StringIO(), io.StringIO()
            rc1 = main(
                ["status", "--json"],
                env=env,
                runner=runner,
                clock=lambda: BASE_TIME,
                stdout=out1,
                stderr=err1,
            )
            self.assertEqual(rc1, 0)
            cache_sglance_dir = Path(env["XDG_CACHE_HOME"]) / "sglance"
            initial_jsons = [p for p in cache_sglance_dir.iterdir() if p.suffix == ".json"]
            self.assertEqual(len(initial_jsons), 1)
            status_json = initial_jsons[0]

            # Add foreign files/dirs inside cache/sglance and parent XDG_CACHE_HOME with 5-day-old mtimes.
            old_ts = (BASE_TIME - timedelta(days=5)).timestamp()
            foreign_txt = cache_sglance_dir / "notes.txt"
            foreign_txt.write_text("user note\n", encoding="utf-8")
            foreign_json = cache_sglance_dir / "custom.json"
            foreign_json.write_text('{"custom": 1}\n', encoding="utf-8")
            foreign_hex_json = cache_sglance_dir / ("a" * 64 + ".json")
            foreign_hex_json.write_text('{"foreign": true}\n', encoding="utf-8")
            foreign_subdir = cache_sglance_dir / "subdir"
            foreign_subdir.mkdir()
            (foreign_subdir / "nested.json").write_text("{}\n", encoding="utf-8")
            parent_file = Path(env["XDG_CACHE_HOME"]) / "outside.json"
            parent_file.write_text("{}\n", encoding="utf-8")
            for p in (foreign_txt, foreign_json, foreign_hex_json, parent_file):
                os.utime(p, (old_ts, old_ts))

            # t = +12h (< 1 day): writing queue snapshot keeps 12h-old status snapshot.
            t_12h = BASE_TIME + timedelta(hours=12)
            out2, err2 = io.StringIO(), io.StringIO()
            rc2 = main(
                ["queue", "--json"],
                env=env,
                runner=runner,
                clock=lambda: t_12h,
                stdout=out2,
                stderr=err2,
            )
            self.assertEqual(rc2, 0)
            self.assertTrue(status_json.exists())
            sglance_jsons_at_12h = {
                p
                for p in cache_sglance_dir.iterdir()
                if p.suffix == ".json" and p not in {foreign_json, foreign_hex_json}
            }
            self.assertEqual(len(sglance_jsons_at_12h), 2)
            queue_json = next(p for p in sglance_jsons_at_12h if p != status_json)

            # t = +1d + 1s: status_json is > 1 day old (86401s); queue_json is 12h 1s old.
            t_over_1d = BASE_TIME + timedelta(days=1, seconds=1)
            mine_fields = (
                "JobArrayID:0|,UserName:0|,Account:0|,Partition:0|,StateCompact:0|,"
                "TimeUsed:0|,TimeLimit:0|,NumNodes:0|,NumCPUs:0|,NumTasks:0|,"
                "tres-alloc:0|,tres-per-node:0|,tres-per-job:0|,tres-per-task:0|,"
                "StartTime:0|,Reason:0|,Name:0|"
            )
            mine_argv = ("env", "SLURM_TIME_FORMAT=%s", "squeue", "-h", "--me", "-O", mine_fields)
            runner.set_response(
                ("cluster-a", *mine_argv),
                "2001|alice|proj-123|cpu|R|10:00|01:00:00|1|16|1|"
                "cpu=16|N/A|N/A|N/A|1794513000|None|me_job|\n",
            )
            out3, err3 = io.StringIO(), io.StringIO()
            rc3 = main(
                ["mine", "--json"],
                env=env,
                runner=runner,
                clock=lambda: t_over_1d,
                stdout=out3,
                stderr=err3,
            )
            self.assertEqual(rc3, 0)
            self.assertEqual(err3.getvalue(), "")
            self.assertFalse(status_json.exists())
            self.assertTrue(queue_json.exists())
            self.assertTrue(foreign_txt.exists())
            self.assertTrue(foreign_json.exists())
            self.assertTrue(foreign_hex_json.exists())
            self.assertTrue(foreign_subdir.is_dir())
            self.assertTrue(parent_file.exists())

            # Best-effort: unlink OSError during pruning is ignored and live write still succeeds.
            t_over_2d = BASE_TIME + timedelta(days=2, seconds=2)
            with mock.patch("sglance.cache.os.unlink", side_effect=PermissionError("denied")):
                out4, err4 = io.StringIO(), io.StringIO()
                rc4 = main(
                    ["status", "--refresh", "--json"],
                    env=env,
                    runner=runner,
                    clock=lambda: t_over_2d,
                    stdout=out4,
                    stderr=err4,
                )
            self.assertEqual(rc4, 0)
            self.assertEqual(err4.getvalue(), "")


if __name__ == "__main__":
    unittest.main()





