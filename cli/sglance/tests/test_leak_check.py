from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SGLANCE_ROOT = Path(__file__).resolve().parents[1]
LEAK_CHECK_SCRIPT = SGLANCE_ROOT / "scripts" / "leak_check.py"


def clean_env(**overrides: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("GIT_") and k != "SGLANCE_LEAK_DENYLIST"
    }
    env.update(overrides)
    return env


def run_leak_check(
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LEAK_CHECK_SCRIPT), *args],
        cwd=str(cwd or SGLANCE_ROOT),
        env=clean_env(**(env or {})),
        capture_output=True,
        text=True,
        check=False,
    )


class LeakCheckCliTests(unittest.TestCase):
    def test_clean_directory_and_real_sglance_tree_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text(
                '@unittest.mock.patch("os.environ")\n'
                'DOCS = "192.0.2.1, 198.51.100.42, 203.0.113.254, 127.0.0.1"\n'
                'VERSIONS = "v1.2.3.4 and 1.2.3.4.5"\n',
                encoding="utf-8",
            )
            res = run_leak_check("--root", str(root))
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertEqual(res.stdout, "")

        real = subprocess.run(
            [sys.executable, str(LEAK_CHECK_SCRIPT)],
            cwd=str(SGLANCE_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(real.returncode, 0, msg=real.stdout + real.stderr)
        self.assertEqual(real.stdout, "")

    def test_finds_denylisted_tokens_emails_and_ipv4_without_echoing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tok_file = "alpha" + "vault"
            tok_env = "beta" + "secret"
            short_tok = "pu"
            leak_email = "alice.ops" + "@" + "example.org"
            leak_ip = "10.20." + "30.40"

            (root / ".leak-denylist").write_text(
                f"# comment\n{tok_file}\n{short_tok}\n",
                encoding="utf-8",
            )
            (root / "app.py").write_text(
                'partition = "gpu"\n'  # substring "pu" inside word must not match
                f'host = "{tok_file.upper()}.example.edu"\n'
                f'acct = "{tok_env}"\n'
                'node = "node-pu-01"\n'
                f'contact = "{leak_email}"\n'
                f'addr = "{leak_ip}"\n',
                encoding="utf-8",
            )
            sub = root / "sub"
            sub.mkdir()
            (sub / f"cfg_{tok_file}.toml").write_text("clean = true\n", encoding="utf-8")

            res = run_leak_check(
                "--root",
                str(root),
                env={"SGLANCE_LEAK_DENYLIST": f"unused_tok, {tok_env}"},
            )
            self.assertEqual(res.returncode, 1)
            self.assertEqual(
                res.stdout.splitlines(),
                [
                    "app.py:2: denylist token #1",
                    "app.py:3: denylist token #4",
                    "app.py:4: denylist token #2",
                    "app.py:5: email address",
                    "app.py:6: non-RFC-5737 IPv4 address",
                    "sub/cfg_<redacted>.toml:0: denylist token #1 in path",
                ],
            )
            combined = (res.stdout + res.stderr).lower()
            for secret in (tok_file, tok_env, leak_email, leak_ip):
                self.assertNotIn(secret.lower(), combined)

    def test_require_denylist_fails_when_absent_and_staged_scans_git_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clean.py").write_text('NAME = "cluster-a"\n', encoding="utf-8")

            missing = run_leak_check("--require-denylist", "--root", str(root))
            self.assertEqual(missing.returncode, 1)
            self.assertIn(".leak-denylist", missing.stderr)

            via_env = run_leak_check(
                "--require-denylist",
                "--root",
                str(root),
                env={"SGLANCE_LEAK_DENYLIST": "secret" + "token"},
            )
            self.assertEqual(via_env.returncode, 0, msg=via_env.stderr)

            (root / ".leak-denylist").write_text("secret" + "token\n", encoding="utf-8")
            via_file = run_leak_check("--require-denylist", "--root", str(root))
            self.assertEqual(via_file.returncode, 0, msg=via_file.stderr)

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            local_ident = "author" + "@" + "localhost"
            git_env = clean_env(
                GIT_AUTHOR_NAME="Test",
                GIT_AUTHOR_EMAIL=local_ident,
                GIT_COMMITTER_NAME="Test",
                GIT_COMMITTER_EMAIL=local_ident,
            )
            subprocess.run(["git", "init", "-q"], cwd=str(repo), env=git_env, check=True)
            (repo / "tracked.py").write_text('X = "clean"\n', encoding="utf-8")
            subprocess.run(["git", "add", "tracked.py"], cwd=str(repo), env=git_env, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), env=git_env, check=True)

            # Unstaged leak ignored by --staged; staged index blob caught even if working tree changes.
            (repo / "tracked.py").write_text('X = "' + "10." + '5.5.5"\n', encoding="utf-8")
            staged_email = "carol" + "@" + "example.org"
            (repo / "staged.py").write_text(f'OWNER = "{staged_email}"\n', encoding="utf-8")
            subprocess.run(["git", "add", "staged.py"], cwd=str(repo), env=git_env, check=True)
            (repo / "staged.py").write_text('OWNER = "placeholder"\n', encoding="utf-8")

            staged_res = run_leak_check("--staged", "--root", str(repo))
            self.assertEqual(staged_res.returncode, 1)
            self.assertEqual(staged_res.stdout.splitlines(), ["staged.py:1: email address"])


if __name__ == "__main__":
    unittest.main()
