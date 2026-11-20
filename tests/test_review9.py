"""Independent review round 9 (Codex): regressions for every reproduced finding."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import engine, gittools, providers, safety
from brahma.config import Settings
from brahma.models import Event, Plan, Result, Task
from tests.approval import approve, owner_confirms
from tests.test_hardening import make_repo as _make_repo


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return _make_repo(root)


class GitCannotRunAHelper(unittest.TestCase):
    """Git config from the environment or the repository must never make Brahma's Git calls run a program."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(Path(self.tmp.name) / "r")
        self.marker = Path(self.tmp.name) / "helper-ran"
        (self.repo / "x.txt").write_text("changed\n")

    def helper_env(self):
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
                "GIT_CONFIG_VALUE_0": f"touch {self.marker}; false"}

    def test_environment_config_does_not_reach_brahma_git_or_the_implicit_diff_check(self):
        with patch.dict(os.environ, self.helper_env()):
            gittools.git(self.repo, "status", "--porcelain")
            from brahma.gitsafe import run_git
            run_git(self.repo, "diff", "--stat")
            ok, _ = asyncio.run(gittools.run_checks(self.repo, [], asyncio.Event(), 30, lambda event: None))
        self.assertTrue(ok)
        self.assertFalse(self.marker.exists())

    def test_repository_config_does_not_run_during_the_implicit_diff_check_or_context(self):
        subprocess.run(["git", "-C", str(self.repo), "config", "core.fsmonitor", f"touch {self.marker}; false"], check=True)
        asyncio.run(gittools.run_checks(self.repo, [], asyncio.Event(), 30, lambda event: None))
        from brahma.context import build_context
        build_context(self.repo, ["x.txt"], 20000)
        self.assertFalse(self.marker.exists())


class ChecksLeaveNothingRunning(unittest.TestCase):
    def test_a_background_process_started_by_a_passing_check_is_ended(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            events = []
            check = ["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo child=$!"]
            ok, _ = asyncio.run(gittools.run_checks(repo, [check], asyncio.Event(), 30, events.append))
            self.assertTrue(ok)
            text = "".join(e.text for e in events if e.kind == "check")
            child = int(text.rsplit("child=", 1)[1].split()[0])      # the check's output, not the echoed command
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)


class Banners(unittest.TestCase):
    def test_no_banner_claims_writes_are_confined(self):
        for provider in ("codex", "claude"):
            for role in ("builder", "reviewer", "planner"):
                self.assertNotIn("confined to the worktree", safety.enforcement(provider, role).describe())


class MalformedProviderEvents(unittest.TestCase):
    def test_a_malformed_event_is_a_worker_error_not_a_crash(self):
        parser = providers.StreamParser("claude", "builder", lambda event: None)
        with self.assertRaisesRegex(ValueError, "malformed event"):
            parser.consume(b'{"type":"result","is_error":true,"errors":1}')


class FailingWorkersAreStillVerified(unittest.TestCase):
    def test_an_exception_from_a_worker_restores_an_unowned_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "brahma/abc"], check=True)
            (repo / "src").mkdir(exist_ok=True)
            (repo / "outside.txt").write_text("original\n")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)

            async def worker(*args, **kwargs):
                (repo / "outside.txt").write_text("edited by the worker\n")
                raise RuntimeError("worker cleanup failed")
            settings = Settings(checks=[["true"]])
            with patch.object(providers, "run", side_effect=worker), patch.object(engine, "_context", return_value="ctx"):
                result = asyncio.run(engine.execute(Plan("G", [Task("T", "do", ["src/**"], ["ok"], provider="codex")]),
                                                    repo, settings, lambda event: None, asyncio.Event()))
            self.assertEqual(result.status, "blocked")
            self.assertIn("ownership", result.text.lower())
            self.assertEqual((repo / "outside.txt").read_text(), "original\n")


class SessionFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from tests.test_cli import repo_session
        self.store, self.session = repo_session(self.tmp.name)
        self.addCleanup(self.store.close)
        (self.session.repo / "brahma.toml").write_text('[brahma]\nchecks=[["true"]]\n')
        self.session.set_plan(Plan("Goal", [Task("T", "do", ["a.txt"], ["ok"])], checks=[["true"]]))
        self.digest = approve(self.session)

    def job(self):
        return self.store.job(self.session.key)

    def test_pause_then_resume_before_the_run_winds_down_keeps_the_work_queued(self):
        store, session = self.store, self.session

        async def engine_run(*args, **kwargs):
            store.pause(session.key)
            await asyncio.sleep(.5)                          # the monitor sees the pause
            store.resume(session.key)                        # resumed while the worker is still winding down
            return Result("brahma", "cancelled", "cancelled")
        with patch("brahma.engine.execute", engine_run):
            result = asyncio.run(session.execute())
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.job()["state"], "queued")

    def test_a_kill_switch_released_before_the_run_winds_down_does_not_requeue_work(self):
        # Automated security review of 21b93d3: lifting the kill switch must never restart work by itself.
        from brahma import halt
        store, session = self.store, self.session

        async def engine_run(*args, **kwargs):
            halt.engage(store.home)
            await asyncio.sleep(.5)                          # the monitor sees the kill switch
            store.hold_all("Kill switch released")
            halt.release(store.home)                         # released while the worker is still winding down
            return Result("brahma", "cancelled", "cancelled")
        with patch("brahma.engine.execute", engine_run):
            asyncio.run(session.execute())
        self.assertEqual(self.job()["state"], "held")
        self.assertIsNone(store.claim(time.time() + 10))

    def test_held_work_does_not_run_from_run_session_until_resume_and_a_new_approval_supersedes_it(self):
        self.store.hold(self.session.key, self.session.generation, self.digest, "kill switch")
        with patch("brahma.engine.execute", AsyncMock(side_effect=AssertionError("ran held work"))):
            with self.assertRaisesRegex(ValueError, "held"):
                asyncio.run(self.session.execute())
        approve(self.session)                                 # a person approves again, deliberately
        self.assertEqual(self.job()["state"], "superseded")

    def test_automatic_publication_pushes_the_verified_commit(self):
        # Round 9: auto-publication asked for a receipt before the session was marked ready, so it never published.
        (self.session.repo / "brahma.toml").write_text('[brahma]\npublish=true\nchecks=[["true"]]\n')
        self.session.set_plan(Plan("Goal", [Task("T", "do", ["a.txt"], ["ok"])], checks=[["true"]]))
        approve(self.session)
        published = []

        async def engine_run(*args, **kwargs):
            return Result("brahma", "ready", "done")

        def publish(worktree, summary, body, expected_head):
            published.append(expected_head)
            return "https://example.invalid/pr/1"
        with patch("brahma.engine.execute", engine_run), patch("brahma.gittools.publish", publish):
            result = asyncio.run(self.session.execute())
        self.assertEqual(result.status, "ready", result.text)
        receipt = self.session.ready_receipt()
        self.assertEqual(published, [receipt["head"]])
        self.assertIn("Draft PR", result.text)

    def test_the_approval_display_shows_what_the_digest_covers(self):
        _, lines = self.session.approval_summary()
        text = "\n".join(lines)
        for expected in ("Base revision: ", "Gherkin feature:", "Repository instructions:", "Owns: a.txt", "Run settings:"):
            self.assertIn(expected, text)


class CliOutputIsRedacted(unittest.TestCase):
    def test_secret_shaped_text_in_a_result_never_reaches_json(self):
        import io
        from contextlib import redirect_stdout
        from brahma import cli
        secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        out = io.StringIO()
        with redirect_stdout(out):
            cli.emit({"status": "blocked", "text": "reviewer saw " + secret, "rows": [{"t": secret}]}, None, 13, True)
            cli.fail("failed near " + secret, 13, True)
        self.assertNotIn(secret, out.getvalue())


if __name__ == "__main__":
    unittest.main()
