from __future__ import annotations

from datetime import UTC, datetime
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from sglance import main
from tests.fakes import FakeRunner

FIXED_NOW = datetime(2026, 11, 12, 20, 0, 0, tzinfo=UTC)
MINIMAL_SINFO = "cpu|node01|idle|0/64/0/64|(null)|(null)|\n"


class ConfigCliTests(unittest.TestCase):
    def test_cluster_entry_with_only_name_defaults_ssh_to_name(self) -> None:
        with TemporaryDirectory() as tmp:
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
            runner = FakeRunner({("cluster-a", "sinfo"): MINIMAL_SINFO})
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
                        "cluster-a",
                        "ssh",
                        "cluster-a",
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

    def test_xdg_config_falls_back_to_home_dot_config_when_xdg_config_home_unset(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            home_config_dir = root / "home" / ".config" / "sglance"
            home_config_dir.mkdir(parents=True)
            (home_config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            env = {
                "HOME": str(root / "home"),
                "XDG_CACHE_HOME": str(root / "cache"),
            }
            runner = FakeRunner({("cluster-a", "sinfo"): MINIMAL_SINFO})
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

    def test_missing_config_file_exits_two_with_stderr_diagnostic(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "XDG_CONFIG_HOME": str(root / "empty-config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner()
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

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("config.toml", stderr.getvalue())
            self.assertEqual(runner.calls, [])

    def test_malformed_toml_exits_two_with_stderr_diagnostic(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]\nname = "cluster-a"\n',
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
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("config.toml", stderr.getvalue())
            self.assertEqual(runner.calls, [])

    def test_unknown_top_level_key_in_config_exits_two(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                'timeout_seconds = 30\n\n[[clusters]]\nname = "cluster-a"\n',
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
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("timeout_seconds", stderr.getvalue())
            self.assertEqual(runner.calls, [])

    def test_unknown_cluster_entry_key_in_config_exits_two(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nhostname = "login.example.edu"\n',
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
                ["status"],
                env=env,
                runner=runner,
                clock=lambda: FIXED_NOW,
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("hostname", stderr.getvalue())
            self.assertEqual(runner.calls, [])

    def test_invalid_cluster_names_exit_two_with_stderr_diagnostic(self) -> None:
        bad_configs = [
            ("empty", '[[clusters]]\nname = ""\n'),
            ("leading-dash", '[[clusters]]\nname = "-cluster"\n'),
            ("space", '[[clusters]]\nname = "cluster a"\n'),
            ("tab", '[[clusters]]\nname = "cluster\\ta"\n'),
            ("comma", '[[clusters]]\nname = "cluster,a"\n'),
            ("ascii-bell", '[[clusters]]\nname = "cluster\\u0007a"\n'),
            ("ascii-del", '[[clusters]]\nname = "cluster\\u007fa"\n'),
            ("missing-name", '[[clusters]]\nssh = "login.example.edu"\n'),
            ("non-string-name", "[[clusters]]\nname = 42\n"),
            ("missing-clusters", ""),
        ]
        for label, toml_body in bad_configs:
            with self.subTest(case=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(toml_body, encoding="utf-8")
                env = {
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "HOME": str(root / "home"),
                }
                runner = FakeRunner()
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

                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotEqual(stderr.getvalue().strip(), "")
                self.assertEqual(runner.calls, [])

    def test_duplicate_cluster_names_exit_two_with_stderr_diagnostic(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / "config" / "sglance"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n\n'
                '[[clusters]]\nname = "cluster-a"\nlocal = true\n',
                encoding="utf-8",
            )
            env = {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "HOME": str(root / "home"),
            }
            runner = FakeRunner({("cluster-a", "sinfo"): MINIMAL_SINFO})
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

            self.assertEqual(exit_code, 2)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("cluster-a", stderr.getvalue())
            self.assertEqual(runner.calls, [])

    def test_invalid_cluster_transport_and_slurm_cluster_configs_exit_two(self) -> None:
        bad_configs = [
            (
                "local-with-ssh",
                '[[clusters]]\nname = "cluster-a"\nlocal = true\nssh = "login.example.edu"\n',
            ),
            (
                "non-bool-local",
                '[[clusters]]\nname = "cluster-a"\nlocal = "true"\n',
            ),
            ("ssh-empty", '[[clusters]]\nname = "cluster-a"\nssh = ""\n'),
            ("ssh-non-string", '[[clusters]]\nname = "cluster-a"\nssh = 123\n'),
            (
                "ssh-leading-dash",
                '[[clusters]]\nname = "cluster-a"\nssh = "-oProxyCommand=evil"\n',
            ),
            (
                "ssh-space",
                '[[clusters]]\nname = "cluster-a"\nssh = "login example.edu"\n',
            ),
            (
                "ssh-tab",
                '[[clusters]]\nname = "cluster-a"\nssh = "login\\texample.edu"\n',
            ),
            (
                "ssh-newline",
                '[[clusters]]\nname = "cluster-a"\nssh = "login\\nexample.edu"\n',
            ),
            (
                "ssh-comma",
                '[[clusters]]\nname = "cluster-a"\nssh = "login,example.edu"\n',
            ),
            (
                "ssh-control-bell",
                '[[clusters]]\nname = "cluster-a"\nssh = "login\\u0007.example.edu"\n',
            ),
            (
                "ssh-control-del",
                '[[clusters]]\nname = "cluster-a"\nssh = "login\\u007f.example.edu"\n',
            ),
            (
                "slurm-cluster-empty",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = ""\n',
            ),
            (
                "slurm-cluster-non-string",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = true\n',
            ),
            (
                "slurm-cluster-leading-dash",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = "-prod"\n',
            ),
            (
                "slurm-cluster-space",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = "prod dev"\n',
            ),
            (
                "slurm-cluster-comma",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = "prod,dev"\n',
            ),
            (
                "slurm-cluster-control-bell",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = "prod\\u0007"\n',
            ),
            (
                "slurm-cluster-control-del",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = "prod\\u007f"\n',
            ),
        ]
        for label, toml_body in bad_configs:
            with self.subTest(case=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                config_dir = root / "config" / "sglance"
                config_dir.mkdir(parents=True)
                (config_dir / "config.toml").write_text(toml_body, encoding="utf-8")
                env = {
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_CACHE_HOME": str(root / "cache"),
                    "HOME": str(root / "home"),
                }
                runner = FakeRunner({("cluster-a", "sinfo"): MINIMAL_SINFO})
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

                self.assertEqual(exit_code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotEqual(stderr.getvalue().strip(), "")
                self.assertEqual(runner.calls, [])


