from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tests.fakes import FakeRunner, run_cli, write_config

MINIMAL_SINFO = "cpu|node01|idle|0/64/0/64|(null)|(null)|\n"
SINFO_ARGV = [
    "env",
    "SLURM_TIME_FORMAT=%s",
    "sinfo",
    "-h",
    "-N",
    "-O",
    "PartitionName:0|,NodeHost:0|,StateLong:0|,CPUsState:0|,Gres:0|,GresUsed:0|",
]


class ConfigCliTests(unittest.TestCase):
    def test_valid_config_defaults_ssh_to_name_and_falls_back_to_home_dot_config(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(root, '[[clusters]]\nname = "cluster-a"\n')
            runner = FakeRunner({("cluster-a", "sinfo"): MINIMAL_SINFO})
            rc, _out, err = run_cli(["status", "--json"], root=root, runner=runner)
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertEqual(runner.calls, [("cluster-a", "ssh", "cluster-a", SINFO_ARGV)])

            home_cfg = root / "alt-home" / ".config" / "sglance"
            home_cfg.mkdir(parents=True)
            (home_cfg / "config.toml").write_text(
                '[[clusters]]\nname = "cluster-a"\nssh = "login.example.edu"\n',
                encoding="utf-8",
            )
            runner2 = FakeRunner({("cluster-a", "sinfo"): MINIMAL_SINFO})
            rc2, _out2, err2 = run_cli(
                ["status", "--json"],
                root=root,
                runner=runner2,
                env={"HOME": str(root / "alt-home"), "XDG_CACHE_HOME": str(root / "alt-cache")},
            )
            self.assertEqual(rc2, 0)
            self.assertEqual(err2, "")
            self.assertEqual(
                runner2.calls,
                [("cluster-a", "ssh", "login.example.edu", SINFO_ARGV)],
            )

    def test_invalid_configs_exit_two_with_diagnostic_before_runner(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = FakeRunner()
            rc, out, err = run_cli(["status"], root=root, runner=runner)
            self.assertEqual(rc, 2)
            self.assertEqual(out, "")
            self.assertIn("config.toml", err)
            self.assertEqual(runner.calls, [])

            rc_no_env, _, err_no_env = run_cli(["status"], root=root, runner=runner, env={})
            self.assertEqual(rc_no_env, 2)
            self.assertIn("XDG_CONFIG_HOME", err_no_env)

        bad_configs: tuple[tuple[str, str, str], ...] = (
            ("malformed-toml", '[[clusters]\nname = "cluster-a"\n', "config.toml"),
            ("empty-clusters", "", "[[clusters]]"),
            (
                "unknown-top-key",
                'timeout_seconds = 30\n\n[[clusters]]\nname = "cluster-a"\n',
                "timeout_seconds",
            ),
            (
                "unknown-entry-key",
                '[[clusters]]\nname = "cluster-a"\nhostname = "login.example.edu"\n',
                "hostname",
            ),
            ("missing-name", '[[clusters]]\nssh = "login.example.edu"\n', "name"),
            (
                "duplicate-name",
                '[[clusters]]\nname = "cluster-a"\n[[clusters]]\nname = "cluster-a"\n',
                "duplicate",
            ),
            (
                "local-ssh-conflict",
                '[[clusters]]\nname = "cluster-a"\nlocal = true\nssh = "login.example.edu"\n',
                "local = true and ssh",
            ),
            ("non-bool-local", '[[clusters]]\nname = "cluster-a"\nlocal = "yes"\n', "boolean"),
            ("unsafe-name-empty", '[[clusters]]\nname = ""\n', "clusters[0].name"),
            ("unsafe-name-dash", '[[clusters]]\nname = "-bad"\n', "must not start with '-'"),
            ("unsafe-name-comma", '[[clusters]]\nname = "a,b"\n', "commas"),
            ("unsafe-ssh-dash", '[[clusters]]\nname = "cluster-a"\nssh = "-oProxyCommand=x"\n', "'-'"),
            ("unsafe-ssh-space", '[[clusters]]\nname = "cluster-a"\nssh = "bad host"\n', "whitespace"),
            (
                "unsafe-ssh-ctrl",
                '[[clusters]]\nname = "cluster-a"\nssh = "host\\u0007"\n',
                "control",
            ),
            (
                "unsafe-slurm-cluster-dash",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = "-prod"\n',
                "'-'",
            ),
            (
                "unsafe-slurm-cluster-comma",
                '[[clusters]]\nname = "cluster-a"\nslurm_cluster = "a,b"\n',
                "commas",
            ),
        )
        for label, toml_body, expected_err in bad_configs:
            with self.subTest(case=label), TemporaryDirectory() as tmp:
                root = Path(tmp)
                write_config(root, toml_body)
                runner = FakeRunner({("cluster-a", "sinfo"): MINIMAL_SINFO})
                rc, out, err = run_cli(["status"], root=root, runner=runner)
                self.assertEqual(rc, 2)
                self.assertEqual(out, "")
                self.assertIn(expected_err, err)
                self.assertEqual(runner.calls, [])

    def test_cluster_filter_queries_selected_entry_and_rejects_unknown_cluster(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_config(
                root,
                '[[clusters]]\nname = "cluster-a"\n[[clusters]]\nname = "cluster-b"\nlocal = true\n',
            )
            runner = FakeRunner({("cluster-b", "sinfo"): MINIMAL_SINFO})
            rc, _out, err = run_cli(
                ["status", "--cluster", "cluster-b", "--json"],
                root=root,
                runner=runner,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(err, "")
            self.assertEqual(runner.calls, [("cluster-b", "local", None, SINFO_ARGV)])

            rc_bad, out_bad, err_bad = run_cli(
                ["status", "--cluster", "missing"],
                root=root,
                runner=runner,
            )
            self.assertEqual(rc_bad, 2)
            self.assertEqual(out_bad, "")
            self.assertIn("unknown cluster 'missing'", err_bad)


if __name__ == "__main__":
    unittest.main()
