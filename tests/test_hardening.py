"""Round 13: ignored files, ownership patterns and the checks environment, against real git repos."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import config, contracts, gittools
from brahma.models import Plan, Task


def make_repo(root: Path, ignore: str = ".env\n*.log\n.venv/\n__pycache__/\nbuild/\n*.sh\n") -> Path:
    def run(*args):
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       check=True, capture_output=True)
    run("init", "-q")
    (root / ".gitignore").write_text(ignore)
    (root / "src").mkdir()
    (root / "src" / "keep.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return root


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name))

    def write(self, name: str, text: str = "x\n"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


class IgnoredFiles(Fixture):
    def test_gitignored_env_is_seen_and_blocked_even_when_owned(self):
        self.write(".env", "TOKEN=abc\n")
        self.assertNotIn(".env", subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                                capture_output=True, text=True).stdout)  # the old blind spot
        self.assertIn(".env", gittools.changed_paths(self.repo))
        ok, message = gittools.ownership_changes(self.repo, ["**"])
        self.assertFalse(ok)
        self.assertIn(".env", message)

    def test_other_sensitive_names_always_block(self):
        for name in ("credentials.json", "nested/.credentials", "key.pem", "id.key", ".env.local"):
            with self.subTest(name=name):
                self.write(name)
                ok, message = gittools.ownership_changes(self.repo, ["**"])
                self.assertFalse(ok)
                self.assertIn(name, message)
                (self.repo / name).unlink()

    def test_allowlisted_build_caches_are_tolerated(self):
        self.write(".venv/lib/site.py")
        self.write("src/__pycache__/m.cpython-314.pyc")
        self.write("build/out.js")
        ok, _ = gittools.ownership_changes(self.repo, ["src/**"], ignored_allow=[".venv", "__pycache__", "build"])
        self.assertTrue(ok)

    def test_default_allowlist_does_not_hide_other_ignored_dirs(self):
        self.write("build/out.js")  # `build` is ignored by .gitignore but not in the default allowlist
        ok, message = gittools.ownership_changes(self.repo, ["src/**"])
        self.assertFalse(ok)
        self.assertIn("build/out.js", message)

    def test_planted_ignored_script_is_outside_ownership(self):
        self.write("tools/plant.sh", "echo owned\n")
        ok, message = gittools.ownership_changes(self.repo, ["src/**"])
        self.assertFalse(ok)
        self.assertIn("tools/plant.sh", message)
        ok, _ = gittools.ownership_changes(self.repo, ["src/**", "tools/"])
        self.assertTrue(ok)

    def test_baseline_ignores_preexisting_ignored_files(self):
        self.write("pre.log")
        baseline = gittools.snapshot_changes(self.repo)
        self.assertIn("pre.log", baseline)
        ok, _ = gittools.ownership_changes(self.repo, ["src/**"], baseline=baseline)
        self.assertTrue(ok)
        self.write("pre.log", "changed\n")
        ok, message = gittools.ownership_changes(self.repo, ["src/**"], baseline=baseline)
        self.assertFalse(ok)
        self.assertIn("pre.log", message)


class CommitWithIgnoredFiles(Fixture):
    def test_owned_ignored_file_does_not_break_the_commit_and_is_not_staged(self):
        self.write("src/new.py", "y = 2\n")
        self.write("tools/helper.sh", "echo hi\n")  # ignored by *.sh, owned via the contract
        self.assertIn("tools/helper.sh", gittools.changed_paths(self.repo))      # reviewed for ownership
        self.assertNotIn("tools/helper.sh", gittools.status_paths(self.repo))    # but not stageable
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "-b", "brahma/abc"], check=True)
        head = gittools.commit_changes(self.repo, "add new")
        files = subprocess.run(["git", "-C", str(self.repo), "show", "--name-only", "--format=", head],
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(files, ["src/new.py"])

    def test_commit_refuses_a_branch_that_is_not_brahmas(self):
        # Review round 8, finding 4: no coordinator commit unless the worktree is still on its own branch.
        self.write("src/new.py", "y = 2\n")
        with self.assertRaises(ValueError):
            gittools.commit_changes(self.repo, "x")

    def test_sensitive_ignored_file_still_blocks_the_commit(self):
        self.write("src/new.py")
        self.write(".env", "TOKEN=1\n")
        with self.assertRaises(ValueError):
            gittools.commit_changes(self.repo, "x")


class AllowlistAbuse(Fixture):
    """Independent review M1: an allowlisted name must not hide a planted secret or a disguised file."""
    IGNORE = ".env\n*.log\n.venv/\n__pycache__/\nbuild/\n*.sh\ndist/\nnode_modules/\n*.egg-info/\n.venv\n"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name), self.IGNORE)

    def test_env_file_planted_inside_an_allowlisted_directory_is_still_seen(self):
        for name in ("dist/.env", "src/dist/.env.local", "node_modules/x/.env", "pkg.egg-info/.envrc"):
            with self.subTest(name=name):
                self.write(name)
                ok, message = gittools.ownership_changes(self.repo, ["**"])
                self.assertFalse(ok, name)
                self.assertIn(name, message)
                (self.repo / name).unlink()

    def test_ordinary_package_files_inside_caches_are_not_flagged(self):
        # certifi ships cacert.pem and botocore ships credentials.py; a venv must not trip the gate.
        self.write(".venv/lib/site-packages/certifi/cacert.pem")
        self.write(".venv/lib/site-packages/botocore/credentials.py")
        self.write("node_modules/x/server.key.js")
        self.assertTrue(gittools.ownership_changes(self.repo, ["src/**"])[0])

    def test_a_symlink_or_file_that_merely_carries_an_allowlisted_name_is_not_tolerated(self):
        outside = Path(self.tmp.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        (self.repo / ".venv").symlink_to(outside)           # a symlink named like the cache directory
        ok, message = gittools.ownership_changes(self.repo, ["src/**"])
        self.assertFalse(ok)
        self.assertIn(".venv", message)
        (self.repo / ".venv").unlink()
        self.write("dist", "not a directory\n")            # a *file* named dist
        ok, message = gittools.ownership_changes(self.repo, ["src/**"])
        self.assertFalse(ok)
        self.assertIn("dist", message)


class CheckEnvSurvivors(unittest.TestCase):
    """Independent review M5: names the first denylist missed."""
    def test_additional_secret_names_are_removed_and_tools_still_work(self):
        secrets = {"NPM_CONFIG_//registry.npmjs.org/:_authtoken": "t", "STRIPE_KEY": "k", "SECRET_KEY": "k",
                   "PRIVATE_KEY": "k", "DB_PASS": "p", "CLIENT_SECRET_VALUE": "s", "SENTRY_DSN": "d",
                   "SLACK_WEBHOOK_URL": "u", "GOOGLE_APPLICATION_CREDENTIALS": "/x", "KUBECONFIG": "/k",
                   "AWS_SHARED_CREDENTIALS_FILE": "/c", "NETRC": "/n", "DATABASE_URL": "postgres://u:p@h/d",
                   "GH_TOKEN": "t", "HF_TOKEN": "t", "NPM_TOKEN": "t", "DOCKER_CONFIG": "/d", "AWS_PROFILE": "p"}
        keep = {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "en_US.UTF-8", "VIRTUAL_ENV": "/v", "TMPDIR": "/t",
                "PYTHONPATH": "/src", "JAVA_HOME": "/j", "XDG_CACHE_HOME": "/c", "UV_CACHE_DIR": "/u"}
        with patch.dict(os.environ, {**secrets, **keep}, clear=True):
            env = gittools.check_env()
        self.assertEqual([name for name in secrets if name in env], [])
        for name, value in keep.items():
            self.assertEqual(env.get(name), value, name)


class AllowlistConfigGuard(unittest.TestCase):
    def test_wildcard_only_entries_that_would_allow_everything_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for bad in ('["*"]', '["**"]', '["?"]', '["[*]"]', '[".venv", "*"]'):
                with self.subTest(bad=bad):
                    (Path(directory) / "brahma.toml").write_text(f"[brahma]\nignored_allowlist = {bad}\n")
                    with self.assertRaises(ValueError):
                        config.Settings.load(Path(directory))


class InstructionsForDirectoryOwnership(Fixture):
    def test_bare_directory_owner_pulls_in_its_nested_instructions(self):
        self.write("src/AGENTS.md", "rules for src\n")
        for owned in ("src", "src/", "src/**"):
            with self.subTest(owned=owned):
                plan = Plan("g", [Task("T", "i", [owned], ["ok"])], checks=[["true"]])
                payload = contracts.prepare_contract(plan, self.repo, [["true"]])["payload"]
                self.assertIn("src/AGENTS.md", payload["instructions"])


class LargeIgnoredFiles(Fixture):
    def test_large_files_are_content_hashed_even_when_size_and_mtime_are_preserved(self):
        # Review round 11, finding 16: size + mtime stood in for contents above 64 MiB, and both can be preserved.
        import os
        self.write("tools/big.sh", "aaaa")
        path = self.repo / "tools/big.sh"
        with patch.object(gittools, "_HASH_CACHE_FROM", 0):            # treat it as large: the cached path
            first = gittools.snapshot_changes(self.repo)["tools/big.sh"]
            info = path.stat()
            path.write_text("bbbb")                                     # same size
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))     # modification time put back
            self.assertNotEqual(first, gittools.snapshot_changes(self.repo)["tools/big.sh"])


class OwnershipSemantics(Fixture):
    def test_single_star_pattern_does_not_own_nested_files(self):
        self.write("src/a/b.py")
        ok, message = gittools.ownership_changes(self.repo, ["src/*.py"])
        self.assertFalse(ok)
        self.assertIn("src/a/b.py", message)

    def test_bare_directory_owns_its_subtree(self):
        self.write("src/a/b.py")
        for pattern in ("src", "src/", "src/**"):
            with self.subTest(pattern=pattern):
                self.assertTrue(gittools.ownership_changes(self.repo, [pattern])[0])


class ChecksEnvironment(unittest.TestCase):
    def test_check_env_removes_credentials_and_agent_sockets(self):
        secrets = {"GITHUB_TOKEN": "t", "MY_SECRET_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "OPENAI_API_KEY": "k",
                   "AWS_SECRET_ACCESS_KEY": "s", "SSH_AUTH_SOCK": "/tmp/agent", "GIT_ASKPASS": "x"}
        with patch.dict(os.environ, secrets):
            env = gittools.check_env()
        for name in secrets:
            self.assertNotIn(name, env)
        self.assertIn("PATH", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_run_checks_executes_worker_code_without_credentials(self):
        script = ("import os, sys\n"
                  "bad = [k for k in ('MY_SECRET_TOKEN', 'ANTHROPIC_API_KEY', 'SSH_AUTH_SOCK') if k in os.environ]\n"
                  "print('LEAK:' + ','.join(bad) if bad else 'CLEAN'); sys.exit(1 if bad else 0)\n")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"MY_SECRET_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "SSH_AUTH_SOCK": "/tmp/a"}):
            make_repo(Path(directory))
            events = []
            ok, output = asyncio.run(gittools.run_checks(
                Path(directory), [[sys.executable, "-c", script]], asyncio.Event(), 60, events.append))
        self.assertTrue(ok, output)
        self.assertIn("CLEAN", output)


class IgnoredAllowlistConfig(unittest.TestCase):
    def test_default_matches_gittools_default(self):
        self.assertEqual(tuple(config.Settings().ignored_allowlist), gittools.DEFAULT_IGNORED_ALLOWLIST)

    def test_configurable_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "brahma.toml").write_text('[brahma]\nignored_allowlist = [".venv", "target"]\n')
            self.assertEqual(config.Settings.load(path).ignored_allowlist, [".venv", "target"])
            for bad in ('["a/b"]', '[""]', '"build"', '[".."]', '[1]'):
                with self.subTest(bad=bad):
                    (path / "brahma.toml").write_text(f"[brahma]\nignored_allowlist = {bad}\n")
                    with self.assertRaises(ValueError):
                        config.Settings.load(path)


class GitMetadataCannotHideChanges(Fixture):
    """Review round 8, findings 4 and 6: ownership is measured against the Git identity taken when the run started."""

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        self.git("checkout", "-q", "-b", "brahma/abc")
        self.identity = gittools.git_identity(self.repo)

    def test_a_clean_run_passes(self):
        self.write("src/keep.py", "x = 2\n")
        ok, _ = gittools.ownership_changes(self.repo, ["src/**"], baseline={}, identity=self.identity)
        self.assertTrue(ok)

    def test_an_unowned_change_committed_during_the_run_is_caught(self):
        self.write("other/unowned.py", "evil = 1\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "worker commit")        # git status is now clean
        self.assertEqual(gittools.status_paths(self.repo), [])
        ok, reason = gittools.ownership_changes(self.repo, ["src/**"], baseline={}, identity=self.identity)
        self.assertFalse(ok)
        self.assertIn("HEAD", reason)

    def test_assume_unchanged_cannot_hide_an_edit(self):
        self.write("src/keep.py", "x = 'tampered'\n")
        self.git("update-index", "--assume-unchanged", "src/keep.py")
        self.assertEqual(gittools.status_paths(self.repo), [])
        ok, reason = gittools.ownership_changes(self.repo, ["other/**"], baseline={}, identity=self.identity)
        self.assertFalse(ok)
        self.assertIn("index flags", reason)

    def test_skip_worktree_cannot_hide_an_edit(self):
        self.git("update-index", "--skip-worktree", "src/keep.py")
        ok, reason = gittools.ownership_changes(self.repo, ["other/**"], baseline={}, identity=self.identity)
        self.assertFalse(ok)
        self.assertIn("src/keep.py", reason)

    def test_flags_left_by_an_earlier_run_are_cleared_not_trusted(self):
        # Automated security review: an identity re-taken on resume must not accept flags set by an interrupted run.
        self.write("src/keep.py", "x = 'hidden by an earlier run'\n")
        self.git("update-index", "--assume-unchanged", "src/keep.py")
        stale = gittools.git_identity(self.repo)
        ok, reason = gittools.ownership_changes(self.repo, ["other/**"], baseline={}, identity=stale)
        self.assertFalse(ok)                      # a flag is a violation even if it was present when the identity was taken
        self.assertEqual(gittools.clear_index_flags(self.repo), ["src/keep.py"])
        self.assertIn("src/keep.py", gittools.status_paths(self.repo))     # the hidden edit is visible again
        self.assertEqual(gittools.git_identity(self.repo)["hidden"], [])

    def test_a_branch_switch_is_caught_and_blocks_the_commit(self):
        self.git("checkout", "-q", "-b", "brahma/other")
        ok, reason = gittools.ownership_changes(self.repo, ["src/**"], baseline={}, identity=self.identity)
        self.assertFalse(ok)
        self.assertIn("branch", reason)
        with self.assertRaises(ValueError):
            gittools.commit_changes(self.repo, "x", self.identity)

    def test_a_repointed_git_directory_is_caught(self):
        changed = dict(self.identity, git_dir="/elsewhere/.git")
        ok, reason = gittools.ownership_changes(self.repo, ["src/**"], baseline={}, identity=changed)
        self.assertFalse(ok)
        self.assertIn("Git directory", reason)

    def test_publication_pushes_only_the_verified_commit_from_a_clean_tree(self):
        # Review round 8, finding 9: /publish pushed whatever the branch held, even commits added after readiness.
        self.write("src/keep.py", "x = 3\n")
        verified = gittools.commit_changes(self.repo, "ready", gittools.git_identity(self.repo))
        pushed = []
        with patch("shutil.which", return_value="/usr/bin/gh"), \
                patch.object(gittools, "git", side_effect=lambda repo, *args: pushed.append(args) if args[0] == "push"
                             else subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()):
            self.write("src/keep.py", "x = 'after readiness'\n")
            with self.assertRaisesRegex(ValueError, "not part of the verified"):
                gittools.publish(self.repo, "s", "b", verified)
            self.git("add", "-A")
            self.git("commit", "-q", "-m", "added after readiness")
            with self.assertRaisesRegex(ValueError, "moved since it was verified"):
                gittools.publish(self.repo, "s", "b", verified)
        self.assertEqual(pushed, [])

    def test_repository_fsmonitor_config_is_never_run(self):
        marker = Path(self.tmp.name) / "ran"
        hook = Path(self.tmp.name) / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        self.git("config", "core.fsmonitor", str(hook))
        gittools.status_paths(self.repo)
        gittools.git_identity(self.repo)
        self.assertFalse(marker.exists())

    def test_env_inside_an_allowlisted_cache_nested_in_another_ignored_directory_is_seen(self):
        (self.repo / ".gitignore").write_text(".env\nscratch/\n.venv/\n")
        self.write("scratch/.venv/.env", "TOKEN=1\n")
        self.write("scratch/.venv/lib/site.py", "pass\n")
        found = gittools.ignored_paths(self.repo)
        self.assertIn("scratch/.venv/.env", found)
        self.assertNotIn("scratch/.venv/lib/site.py", found)


if __name__ == "__main__":
    unittest.main()
