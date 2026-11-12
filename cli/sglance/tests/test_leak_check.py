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
    merged_env = clean_env(**(env or {}))
    return subprocess.run(
        [sys.executable, str(LEAK_CHECK_SCRIPT), *args],
        cwd=str(cwd or SGLANCE_ROOT),
        env=merged_env,
        capture_output=True,
        text=True,
        check=False,
    )


class LeakCheckCliTests(unittest.TestCase):
    def test_clean_directory_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sample.py").write_text(
                'CLUSTER = "cluster-a"\nHOST = "login.example.edu"\n',
                encoding="utf-8",
            )
            result = run_leak_check("--root", str(root))
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")

    def test_denylist_file_reports_token_index_without_echoing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tok_one = "alpha" + "vault"
            tok_two = "beta" + "cluster"
            (root / ".leak-denylist").write_text(
                f"\n# local private identifiers\n{tok_one}\n\n  # another comment\n  {tok_two}  \n",
                encoding="utf-8",
            )
            sub = root / "nested"
            sub.mkdir()
            (sub / "config.toml").write_text(
                f'name = "cluster-a"\nssh = "{tok_two.upper()}.example.org"\n',
                encoding="utf-8",
            )
            (root / "notes.txt").write_text(
                f"connect to {tok_one} login\n",
                encoding="utf-8",
            )

            result = run_leak_check("--root", str(root))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "nested/config.toml:2: denylist token #2",
                    "notes.txt:1: denylist token #1",
                ],
            )
            self.assertNotIn(tok_one, result.stdout + result.stderr)
            self.assertNotIn(tok_two, result.stdout.lower() + result.stderr.lower())

    def test_denylist_matches_only_on_non_alphanumeric_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".leak-denylist").write_text(
                "pu\nri\nsub.net\n",
                encoding="utf-8",
            )
            (root / "code.py").write_text(
                'partition = "gpu"  # write results to mysub.net2\n'
                'node = "node-pu-01"  # host_ri_login\n'
                'endpoint = "https://sub.net:8443"\n',
                encoding="utf-8",
            )

            result = run_leak_check("--root", str(root))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "code.py:2: denylist token #1",
                    "code.py:2: denylist token #2",
                    "code.py:3: denylist token #3",
                ],
            )

    def test_env_var_denylist_supports_comma_and_newline_separators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tok_a = "acct" + "secret"
            tok_b = "host" + "internal"
            tok_c = "proj" + "private"
            (root / "doc.md").write_text(
                f"clean header\naccount = {tok_b}\nproject = {tok_c}\n",
                encoding="utf-8",
            )

            result = run_leak_check(
                "--root",
                str(root),
                env={
                    "SGLANCE_LEAK_DENYLIST": f"{tok_a}, {tok_b}\n# comment\n\n{tok_c} ,"
                },
            )

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "doc.md:2: denylist token #2",
                    "doc.md:3: denylist token #3",
                ],
            )
            self.assertNotIn(tok_b, result.stdout + result.stderr)
            self.assertNotIn(tok_c, result.stdout + result.stderr)

    def test_require_denylist_fails_closed_when_absent_and_passes_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clean.py").write_text('NAME = "cluster-a"\n', encoding="utf-8")

            missing = run_leak_check("--require-denylist", "--root", str(root))
            self.assertEqual(missing.returncode, 1)
            self.assertIn(".leak-denylist", missing.stderr)
            self.assertIn("SGLANCE_LEAK_DENYLIST", missing.stderr)

            via_env = run_leak_check(
                "--require-denylist",
                "--root",
                str(root),
                env={"SGLANCE_LEAK_DENYLIST": "secret" + "cluster"},
            )
            self.assertEqual(via_env.returncode, 0, msg=via_env.stderr)

            (root / ".leak-denylist").write_text("secret" + "cluster\n", encoding="utf-8")
            via_file = run_leak_check("--require-denylist", "--root", str(root))
            self.assertEqual(via_file.returncode, 0, msg=via_file.stderr)

    def test_always_on_email_check_flags_emails_without_decorator_false_positives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            addr_one = "alice" + "@" + "example.org"
            addr_two = "ops.team+hpc" + "@" + "login.university.edu"
            (root / "module.py").write_text(
                '@unittest.mock.patch("os.environ")\n'
                'STATE_FLAGS = "*~#!%$@^-"  # Slurm flags\n'
                f'CONTACT = "{addr_one}"\n'
                f"# maintainer: <{addr_two}>\n",
                encoding="utf-8",
            )

            result = run_leak_check("--root", str(root))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "module.py:3: email address",
                    "module.py:4: email address",
                ],
            )
            self.assertNotIn(addr_one, result.stdout + result.stderr)
            self.assertNotIn(addr_two, result.stdout + result.stderr)

    def test_always_on_ipv4_check_allows_rfc5737_and_loopback_and_rejects_others(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            priv_ten = "10." + "1.2.3"
            priv_lan = "192.168." + "1.50"
            pub_dns = "8.8." + "8.8"
            unspec = "0.0." + "0.0"
            sent_ip = "10.20." + "30.40"
            paren_ip = "172.16." + "0.9"
            (root / "net.txt").write_text(
                "docs = 192.0.2.1., 198.51.100.42, 203.0.113.254, 192.0.2.0/24\n"
                "loopback = 127.0.0.1.\n"
                "versions = v1.2.3.4 and 1.2.3.4.5 and 999.1.2.3 and 1.2.3.4.5.6\n"
                f"internal = {priv_ten}\n"
                f"lan = {priv_lan}\n"
                f"public = {pub_dns}\n"
                f"bind = {unspec}\n"
                f"Connect to {sent_ip}.\n"
                f"Fallback ({paren_ip}).\n",
                encoding="utf-8",
            )

            result = run_leak_check("--root", str(root))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "net.txt:4: non-RFC-5737 IPv4 address",
                    "net.txt:5: non-RFC-5737 IPv4 address",
                    "net.txt:6: non-RFC-5737 IPv4 address",
                    "net.txt:7: non-RFC-5737 IPv4 address",
                    "net.txt:8: non-RFC-5737 IPv4 address",
                    "net.txt:9: non-RFC-5737 IPv4 address",
                ],
            )
            for forbidden_ip in (priv_ten, priv_lan, pub_dns, unspec, sent_ip, paren_ip):
                self.assertNotIn(forbidden_ip, result.stdout + result.stderr)

    def test_git_repo_scans_tracked_and_staged_files_and_ignores_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git_env = clean_env()
            subprocess.run(
                ["git", "init", "-q"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            pkg = repo / "cli" / "sglance"
            pkg.mkdir(parents=True)
            (pkg / "tracked.py").write_text('NAME = "cluster-a"\n', encoding="utf-8")
            leak_email = "bob" + "@" + "example.org"
            (pkg / "staged_new.py").write_text(
                f'MAINTAINER = "{leak_email}"\n',
                encoding="utf-8",
            )
            untracked_ip = "10." + "9.8.7"
            (pkg / "untracked_notes.txt").write_text(
                f"scratch = {untracked_ip}\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "add", "cli/sglance/tracked.py", "cli/sglance/staged_new.py"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )

            result = run_leak_check("--root", str(pkg))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                ["staged_new.py:1: email address"],
            )

            bad_flag = run_leak_check("--unknown-option")
            self.assertEqual(bad_flag.returncode, 2)
            missing_root = run_leak_check("--root", str(repo / "does-not-exist"))
            self.assertEqual(missing_root.returncode, 2)

    def test_staged_flag_reads_git_index_blobs_under_root_only(self) -> None:
        with tempfile.TemporaryDirectory() as non_git_tmp:
            non_git = run_leak_check("--staged", "--root", non_git_tmp)
            self.assertEqual(non_git.returncode, 2)
            self.assertNotEqual(non_git.stderr, "")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            local_ident = "author" + "@" + "localhost"
            git_env = clean_env(
                GIT_AUTHOR_NAME="Test Author",
                GIT_AUTHOR_EMAIL=local_ident,
                GIT_COMMITTER_NAME="Test Author",
                GIT_COMMITTER_EMAIL=local_ident,
            )
            subprocess.run(["git", "init", "-q"], cwd=str(repo), env=git_env, check=True)
            pkg = repo / "cli" / "sglance"
            other = repo / "other"
            pkg.mkdir(parents=True)
            other.mkdir(parents=True)

            tracked = pkg / "tracked.py"
            tracked.write_text('CLEAN = "cluster-a"\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "cli/sglance/tracked.py"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "initial"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )

            # Unstaged working-tree leak in tracked file should be ignored by --staged
            unstaged_ip = "10." + "5.5.5"
            tracked.write_text(f'DIRTY = "{unstaged_ip}"\n', encoding="utf-8")

            # Staged leak outside cli/sglance should be ignored when --root points to cli/sglance
            outside_ip = "10." + "8.8.8"
            (other / "outside.py").write_text(f'OUT = "{outside_ip}"\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "other/outside.py"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )

            # Staged leak inside cli/sglance must be caught from git index even if working tree changes
            staged_email = "carol" + "@" + "example.org"
            staged_file = pkg / "staged_only.py"
            staged_file.write_text(f'OWNER = "{staged_email}"\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "cli/sglance/staged_only.py"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            staged_file.write_text('OWNER = "placeholder"\n', encoding="utf-8")

            result = run_leak_check("--staged", "--root", str(pkg))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                ["staged_only.py:1: email address"],
            )

    def test_staged_handles_typechange_gitlink_and_cat_file_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            local_ident = "author" + "@" + "localhost"
            git_env = clean_env(
                GIT_AUTHOR_NAME="Test Author",
                GIT_AUTHOR_EMAIL=local_ident,
                GIT_COMMITTER_NAME="Test Author",
                GIT_COMMITTER_EMAIL=local_ident,
            )
            subprocess.run(["git", "init", "-q"], cwd=str(repo), env=git_env, check=True)
            pkg = repo / "cli" / "sglance"
            pkg.mkdir(parents=True)

            secret_tok = "gamma" + "vault"
            (pkg / ".leak-denylist").write_text(f"{secret_tok}\n", encoding="utf-8")
            (pkg / "target.toml").write_text('name = "cluster-a"\n', encoding="utf-8")
            link_path = pkg / "active.toml"
            link_path.symlink_to("target.toml")
            subprocess.run(
                ["git", "add", "cli/sglance/target.toml", "cli/sglance/active.toml"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "commit symlink"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )

            # Replace committed symlink with regular file containing denylist token (status T)
            link_path.unlink()
            link_path.write_text(f'ssh = "{secret_tok}.example.edu"\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "cli/sglance/active.toml"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )

            typechange_res = run_leak_check(
                "--staged",
                "--require-denylist",
                "--root",
                str(pkg),
            )
            self.assertNotEqual(typechange_res.returncode, 0)
            self.assertEqual(
                typechange_res.stdout.splitlines(),
                ["active.toml:1: denylist token #1"],
            )
            self.assertNotIn(secret_tok, typechange_res.stdout + typechange_res.stderr)

            # Clean active.toml and stage a gitlink (mode 160000): gitlink cat-file failure is skipped
            link_path.write_text('ssh = "login.example.edu"\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "cli/sglance/active.toml"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    "160000,1111111111111111111111111111111111111111,cli/sglance/vendor/submod",
                ],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            gitlink_res = run_leak_check(
                "--staged",
                "--require-denylist",
                "--root",
                str(pkg),
            )
            self.assertEqual(gitlink_res.returncode, 0, msg=gitlink_res.stdout + gitlink_res.stderr)

            # Corrupt/remove loose object for a staged regular file (mode 100644) -> must report finding
            broken_file = pkg / "broken.py"
            broken_file.write_text("X = 'missing_blob_test'\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "cli/sglance/broken.py"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            sha_proc = subprocess.run(
                ["git", "rev-parse", ":cli/sglance/broken.py"],
                cwd=str(repo),
                env=git_env,
                capture_output=True,
                text=True,
                check=True,
            )
            blob_sha = sha_proc.stdout.strip()
            obj_path = repo / ".git" / "objects" / blob_sha[:2] / blob_sha[2:]
            obj_path.chmod(0o644)
            obj_path.unlink()

            broken_res = run_leak_check(
                "--staged",
                "--require-denylist",
                "--root",
                str(pkg),
            )
            self.assertEqual(broken_res.returncode, 1)
            self.assertEqual(len(broken_res.stdout.splitlines()), 1)
            self.assertTrue(broken_res.stdout.startswith("broken.py:0: "))

    def test_pre_commit_hook_and_gitignore_in_temp_git_repo(self) -> None:
        hook_src = SGLANCE_ROOT / "hooks" / "pre-commit"
        gitignore_src = SGLANCE_ROOT / ".gitignore"
        self.assertTrue(hook_src.is_file(), "hooks/pre-commit must exist")
        self.assertTrue(os.access(hook_src, os.X_OK), "hooks/pre-commit must be executable")
        self.assertTrue(gitignore_src.is_file(), ".gitignore must exist")

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            local_ident = "author" + "@" + "localhost"
            git_env = clean_env(
                GIT_AUTHOR_NAME="Test Author",
                GIT_AUTHOR_EMAIL=local_ident,
                GIT_COMMITTER_NAME="Test Author",
                GIT_COMMITTER_EMAIL=local_ident,
            )
            subprocess.run(["git", "init", "-q"], cwd=str(repo), env=git_env, check=True)
            pkg = repo / "cli" / "sglance"
            (pkg / "scripts").mkdir(parents=True)
            (pkg / "hooks").mkdir(parents=True)

            (pkg / "scripts" / "leak_check.py").write_bytes(LEAK_CHECK_SCRIPT.read_bytes())
            (pkg / "hooks" / "pre-commit").write_bytes(hook_src.read_bytes())
            (pkg / "hooks" / "pre-commit").chmod(0o755)
            (pkg / ".gitignore").write_bytes(gitignore_src.read_bytes())

            # Install hook inside temporary repo's .git/hooks/pre-commit
            installed_hook = repo / ".git" / "hooks" / "pre-commit"
            installed_hook.write_bytes(hook_src.read_bytes())
            installed_hook.chmod(0o755)

            ignored_check = subprocess.run(
                [
                    "git",
                    "check-ignore",
                    "cli/sglance/.leak-denylist",
                    "cli/sglance/__pycache__/mod.pyc",
                    "cli/sglance/sglance.egg-info/PKG-INFO",
                    "cli/sglance/.venv/pyvenv.cfg",
                    "cli/sglance/build/out",
                    "cli/sglance/dist/pkg.whl",
                ],
                cwd=str(repo),
                env=git_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(ignored_check.returncode, 0, msg=ignored_check.stderr)
            self.assertEqual(len(ignored_check.stdout.splitlines()), 6)

            # Commits staging nothing under cli/sglance exit 0 without requiring .leak-denylist
            outside_dir = repo / "other"
            outside_dir.mkdir()
            (outside_dir / "readme.txt").write_text("unrelated change\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "other/readme.txt"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            outside_commit = subprocess.run(
                ["git", "commit", "-m", "unrelated commit without denylist"],
                cwd=str(repo),
                env=git_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                outside_commit.returncode,
                0,
                msg=outside_commit.stdout + outside_commit.stderr,
            )

            app_file = pkg / "app.py"
            app_file.write_text('CLUSTER = "cluster-a"\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "cli/sglance/.gitignore", "cli/sglance/app.py"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )

            # Fails closed without .leak-denylist when staging under cli/sglance
            no_denylist_commit = subprocess.run(
                ["git", "commit", "-m", "should fail closed"],
                cwd=str(repo),
                env=git_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(no_denylist_commit.returncode, 0)

            secret_tok = "vault" + "host"
            (pkg / ".leak-denylist").write_text(f"{secret_tok}\n", encoding="utf-8")

            # Staged leak is blocked by pre-commit without echoing secret token
            app_file.write_text(f'SSH = "{secret_tok}.example.edu"\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "cli/sglance/app.py"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            leaked_commit = subprocess.run(
                ["git", "commit", "-m", "should block leak"],
                cwd=str(repo),
                env=git_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(leaked_commit.returncode, 0)
            combined_out = leaked_commit.stdout + leaked_commit.stderr
            self.assertIn("app.py:1: denylist token #1", combined_out)
            self.assertNotIn(secret_tok, combined_out)

            # Clean staged content commits cleanly while .leak-denylist stays untracked/ignored
            app_file.write_text('SSH = "login.example.edu"\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "cli/sglance/app.py"],
                cwd=str(repo),
                env=git_env,
                check=True,
            )
            clean_commit = subprocess.run(
                ["git", "commit", "-m", "clean commit"],
                cwd=str(repo),
                env=git_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                clean_commit.returncode,
                0,
                msg=clean_commit.stdout + clean_commit.stderr,
            )

    def test_untracked_directory_inside_git_repo_scans_non_ignored_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git_env = clean_env()
            subprocess.run(["git", "init", "-q"], cwd=str(repo), env=git_env, check=True)
            pkg = repo / "cli" / "sglance"
            pkg.mkdir(parents=True)
            (pkg / ".gitignore").write_bytes((SGLANCE_ROOT / ".gitignore").read_bytes())
            secret_tok = "unborn" + "cluster"
            (pkg / ".leak-denylist").write_text(f"{secret_tok}\n", encoding="utf-8")
            (pkg / "draft.py").write_text(
                f'HOST = "{secret_tok}.example.edu"\n',
                encoding="utf-8",
            )

            result = run_leak_check("--root", str(pkg))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                ["draft.py:1: denylist token #1"],
            )

    def test_relative_paths_are_scanned_without_echoing_matched_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_tok = "alpha" + "vault"
            leak_email = "alice" + "@" + "example.org"
            leak_ip = "10.20." + "30.40"
            (root / ".leak-denylist").write_text(f"{secret_tok}\n", encoding="utf-8")
            sub = root / "configs"
            sub.mkdir()
            (sub / f"notes_{secret_tok.upper()}.txt").write_text("clean\n", encoding="utf-8")
            (sub / f"owner_{leak_email}.txt").write_text("clean\n", encoding="utf-8")
            (sub / f"node_{leak_ip}.toml").write_text("clean\n", encoding="utf-8")

            result = run_leak_check("--root", str(root))

            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "configs/node_<redacted>.toml:0: non-RFC-5737 IPv4 address in path",
                    "configs/notes_<redacted>.txt:0: denylist token #1 in path",
                    "configs/<redacted>:0: email address in path",
                ],
            )
            combined = (result.stdout + result.stderr).lower()
            self.assertNotIn(secret_tok.lower(), combined)
            self.assertNotIn(leak_email.lower(), combined)
            self.assertNotIn(leak_ip, combined)

    def test_real_sglance_tree_has_no_leaks(self) -> None:
        result = subprocess.run(
            [sys.executable, str(LEAK_CHECK_SCRIPT)],
            cwd=str(SGLANCE_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
