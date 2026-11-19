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

from brahma import config, gittools


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


if __name__ == "__main__":
    unittest.main()
