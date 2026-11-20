"""Fixes from the second independent review (version 0.12.0)."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from brahma import providers
from brahma.config import Settings


class WorkerEnvironment(unittest.TestCase):
    def test_workers_no_longer_inherit_credentials_the_checks_already_stripped(self):
        leaked = {"PGPASSWORD": "x", "STRIPE_SECRET_KEY": "x", "SECRET_KEY": "x", "PRIVATE_KEY": "x", "DB_PASSWD": "x",
                  "GITHUB_PAT": "x", "SSH_AUTH_SOCK": "/tmp/agent", "AWS_PROFILE": "p", "SERVICE_AUTH": "x"}
        with patch.dict(os.environ, {**leaked, "PATH": "/usr/bin", "HOME": "/h", "LANG": "C"}):
            env = providers.child_env()
        for name in leaked:
            self.assertNotIn(name, env, name)
        self.assertEqual((env["PATH"], env["HOME"], env["LANG"]), ("/usr/bin", "/h", "C"))

    def test_check_environment_is_at_least_as_strict_and_disables_git_prompts(self):
        from brahma import gittools
        with patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/a", "PATH": "/usr/bin"}):
            env = gittools.check_env()
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")


class ModelNames(unittest.TestCase):
    def test_model_names_cannot_look_like_options(self):
        import tempfile
        from pathlib import Path
        for bad in ("--dangerously-skip-permissions", "-x", "a b", "a;b", "", "x" * 101, "$(id)"):
            with self.subTest(model=bad), tempfile.TemporaryDirectory() as d:
                (Path(d) / "brahma.toml").write_text(f'[brahma]\nclaude_model = "{bad}"\n')
                with self.assertRaises(ValueError):
                    Settings.load(Path(d))
        for good in ("gpt-5.5", "claude-sonnet-5-5", "opus[1m]", "org/model:tag"):
            with self.subTest(model=good), tempfile.TemporaryDirectory() as d:
                (Path(d) / "brahma.toml").write_text(f'[brahma]\nclaude_model = "{good}"\n')
                self.assertEqual(Settings.load(Path(d)).claude_model, good)


class Overload(unittest.TestCase):
    def test_a_stray_529_in_prose_is_not_a_capacity_error(self):
        self.assertIsNone(providers._OVERLOADED.search("Test 529 of 600 failed"))
        for text in ("API Error: 529 overloaded", "HTTP 529", "status: 529", "overloaded_error"):
            self.assertIsNotNone(providers._OVERLOADED.search(text), text)


class ApprovalScreen(unittest.TestCase):
    def render(self, settings, checks):
        import json
        import subprocess
        import tempfile
        from dataclasses import asdict
        from pathlib import Path
        from types import SimpleNamespace
        from brahma import contracts
        from brahma.models import Plan, Task
        from brahma.ui import BrahmaApp
        plan = Plan("Goal", [Task("One", "do", ["src/**"], ["ok"])], checks=checks)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "x")):
                subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)
            contract = contracts.prepare_contract(plan, root, checks, model_profiles=asdict(settings),
                                                  task_timeout=settings.task_timeout, repair_cycles=settings.repair_cycles)
            path = root / "c.json"
            path.write_text(json.dumps({"version": contract["version"], "payload": contract["payload"], "digest": contract["digest"]}))
            path.with_suffix(".feature").write_text(contract["payload"]["feature"])
            written = []
            app = SimpleNamespace(session=SimpleNamespace(key="k", settings=settings),
                                  store=SimpleNamespace(session=lambda key: {"contract": str(path)}), write=written.append)
            BrahmaApp.show_plan(app, plan)
        return "\n".join(e.text for e in written)

    def test_widening_settings_and_shell_checks_are_visible_before_approval(self):
        text = self.render(Settings(publish=True, ignored_allowlist=[".venv", "*e*"]), [["sh", "-c", "make"], ["pytest"]])
        self.assertIn("draft PR publication when ready", text)
        self.assertIn("*e*", text)
        self.assertIn("WARNING: approved checks run these shell or network programs", text)
        self.assertIn("sh", text.split("WARNING:")[1])

    def test_default_settings_say_so(self):
        text = self.render(Settings(), [["pytest"]])
        # Review round 8, finding 10: the defaults include a local auto-commit, so the screen must say so.
        self.assertIn("local commit on the brahma/ branch when ready", text)
        self.assertIn("no publication", text)
        self.assertNotIn("no automatic commit", text)
        self.assertNotIn("WARNING", text)

    def test_disabled_auto_commit_is_shown(self):
        self.assertIn("no automatic commit", self.render(Settings(auto_commit=False), [["pytest"]]))

    def test_the_budget_line_shows_the_locked_budget(self):
        self.assertIn("Budget: 10 active minutes/task; at most 2 repairs", self.render(Settings(task_timeout=600, repair_cycles=2), [["pytest"]]))


if __name__ == "__main__":
    unittest.main()
