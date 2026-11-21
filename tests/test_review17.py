"""Review of 0901567..263e2b9 (substitute for Codex round 17, decision Round 16): one regression per fixed finding."""
from __future__ import annotations

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import gittools
from brahma.models import Plan, Task
from brahma.session import Session
from brahma.store import Store
from tests.test_review10 import make_repo
from tests.test_review12 import git
from tests.test_review16 import verified_branch

ROOT = Path(__file__).resolve().parents[1]


class Finding1SlowStepWithInput(unittest.TestCase):
    def test_a_step_that_reads_stdin_and_takes_longer_than_one_poll_still_receives_its_input(self):
        # `gh pr create --body-file -` reads the body from stdin; a retry of communicate() with input raised ValueError.
        with tempfile.TemporaryDirectory() as tmp:
            result = gittools._run_publication_step(["sh", "-c", "sleep 1; cat"], Path(tmp), lambda: None,
                                                    input_text="the PR body")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "the PR body")


class Finding2RedactedBaselineNeverDeletes(unittest.TestCase):
    def test_a_redacted_baseline_keeps_what_it_may_name_and_restores_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            mine = repo / "notes" / "ghp_0123456789abcdefghijklmnopqrstuvwxyz.txt"   # the user's own file at launch
            mine.parent.mkdir()
            mine.write_text("mine\n")
            (repo / "stray.txt").write_text("x\n")                  # a worker's file outside ownership
            restored, kept = gittools.restore_violations(repo, ["src/**"], {"notes/[REDACTED].txt": "absent"})
            self.assertTrue(mine.exists())
            self.assertIn("notes/ghp_0123456789abcdefghijklmnopqrstuvwxyz.txt", kept)
            # Second review: one redacted name must not switch off restoration of everything else.
            self.assertEqual(restored, ["stray.txt"])
            self.assertFalse((repo / "stray.txt").exists())


class Finding5OwnedUnverifiableOutputIsKept(unittest.TestCase):
    def test_an_owned_new_file_that_cannot_be_verified_blocks_readiness_but_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            path = repo / "src" / "out.log"
            path.write_text("x\n")
            path.chmod(0)
            try:
                self.assertIn("src/out.log", gittools.violating_paths(repo, ["src/**"], baseline={}))
                restored, kept = gittools.restore_violations(repo, ["src/**"], {})
                self.assertNotIn("src/out.log", restored)
                self.assertIn("src/out.log", kept)
                self.assertTrue(os.path.lexists(path))
            finally:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_an_owned_escaping_symlink_or_special_file_is_still_removed(self):
        # Automated security review of ac43d7e: only an owned *regular* file is kept.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            os.symlink(Path(tmp), repo / "src" / "escape")
            os.mkfifo(repo / "src" / "pipe")
            restored, _ = gittools.restore_violations(repo, ["src/**"], {})
            self.assertEqual(sorted(restored), ["src/escape", "src/pipe"])
            self.assertFalse(os.path.lexists(repo / "src" / "escape"))
            self.assertFalse(os.path.lexists(repo / "src" / "pipe"))

    def test_an_unowned_new_file_is_still_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "stray.txt").write_text("x\n")
            restored, _ = gittools.restore_violations(repo, ["src/**"], {})
            self.assertEqual(restored, ["stray.txt"])
            self.assertFalse((repo / "stray.txt").exists())


class Finding3And4PublicationControls(unittest.IsolatedAsyncioTestCase):
    async def test_a_stale_in_memory_cancel_does_not_block_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            session = Session(store, repo)
            session.plan = Plan("Goal", [Task("T", "do", ["src/**"], ["ok"])])
            store.set(session.key, worktree=str(repo))
            session.cancel.set()          # left by an earlier kill that was lifted, or a resumed pause
            seen = []
            with patch.object(gittools, "publish", lambda *args: seen.append(args) or "u"):
                await session.publish_ready_in_thread({"head": "h", "uncommitted": {}, "ignored_allowlist": []}, "b")
            self.assertIsNone(seen[0][6]())
            store.pause(session.key)
            self.assertEqual(seen[0][6](), "Paused by user")      # a pause is named as a pause, read durably

    def test_automatic_publication_runs_off_the_event_loop(self):
        source = (ROOT / "src" / "brahma" / "session.py").read_text()
        self.assertIn("await self.publish_ready_in_thread(receipt, result.text)", source)


class Finding6StateErrorsRefuseInsteadOfCrashing(unittest.TestCase):
    def test_halt_reason_reads_a_state_directory_with_uri_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "st?a#te %41")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            self.assertIsNone(Store.halt_reason(store.home, key))
            store.stop(key)
            self.assertEqual(Store.halt_reason(store.home, key), "Stopped by user")

    def test_an_unusable_lock_is_a_refusal_the_caller_handles(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            state = Path(tmp) / "state"
            (state / "publish-locks").mkdir(parents=True)
            with patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(state)}):
                os.mkfifo(gittools.publish_lock_path(repo))           # O_WRONLY|O_NONBLOCK on a FIFO: ENXIO
            with patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(state)}), \
                    patch.object(gittools, "_run_publication_step") as step:
                with self.assertRaisesRegex(ValueError, "Not published"):
                    gittools.publish(repo, "s", "b", head, snapshot)
            step.assert_not_called()

    def test_a_held_lock_waits_but_a_stop_ends_the_wait(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            state = Path(tmp) / "state"
            (state / "publish-locks").mkdir(parents=True)
            with patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(state)}):
                holder = os.open(gittools.publish_lock_path(repo), os.O_WRONLY | os.O_CREAT, 0o600)
            self.addCleanup(os.close, holder)
            fcntl.flock(holder, fcntl.LOCK_EX)                  # another publication of this worktree is running
            stop_at = time.monotonic() + 0.5
            started = time.monotonic()
            with patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(state)}), \
                    patch.object(gittools, "_run_publication_step") as step:
                with self.assertRaisesRegex(ValueError, "Stopped by user"):
                    gittools.publish(repo, "s", "b", head, snapshot,
                                     stopped=lambda: "Stopped by user" if time.monotonic() > stop_at else None)
            self.assertLess(time.monotonic() - started, 5)
            step.assert_not_called()


class SecondReviewPublication(unittest.IsolatedAsyncioTestCase):
    def _session(self, tmp):
        repo = make_repo(Path(tmp) / "r")
        store = Store(Path(tmp) / "state")
        self.addCleanup(store.close)
        session = Session(store, repo)
        session.plan = Plan("Goal", [Task("T", "do", ["src/**"], ["ok"])])
        store.set(session.key, worktree=str(repo))
        return session

    async def test_quitting_while_publishing_stops_the_publication_thread(self):
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(tmp)
            seen = []

            def publishing(*args):          # a long push: polls its stop check like _run_publication_step
                for _ in range(200):
                    reason = args[6]()
                    if reason:
                        seen.append(reason)
                        return "stopped"
                    time.sleep(0.02)
                seen.append("ran to the end")
                return "u"
            with patch.object(gittools, "publish", publishing):
                job = asyncio.ensure_future(session.publish_ready_in_thread(
                    {"head": "h", "uncommitted": {}, "ignored_allowlist": []}, "b"))
                await asyncio.sleep(0.1)
                job.cancel()                # the terminal UI quit, or the service stopped: nothing durable written
                with self.assertRaises(asyncio.CancelledError):
                    await job
                for _ in range(100):
                    if seen:
                        break
                    await asyncio.sleep(0.02)
            self.assertEqual(len(seen), 1)
            self.assertIn("Cancelled", seen[0])

    async def test_only_durable_controls_or_an_abandoned_await_stop_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(tmp)
            session.cancel.set()           # the in-memory event can be stale (lifted kill switch, resumed pause)
            arguments, abandoned = session._publish_arguments({"head": "h", "uncommitted": {}, "ignored_allowlist": []}, "b")
            self.assertIsNone(arguments[6]())
            self.assertTrue(session.cancel.is_set())            # not cleared: a quit during approval is not discarded
            session.store.stop(session.key)                     # a durable Stop is named as a stop
            self.assertEqual(arguments[6](), "Stopped by user")
            abandoned.set()                                     # the awaiting task was cancelled
            self.assertIn("Cancelled", arguments[6]())

    def test_a_stuck_publication_holding_the_lock_ends_the_wait_at_a_deadline(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            with patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(Path(tmp) / "state")}):
                lock = gittools.publish_lock_path(repo)
                lock.parent.mkdir(parents=True)
                holder = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
                self.addCleanup(os.close, holder)
                fcntl.flock(holder, fcntl.LOCK_EX)
                with self.assertRaisesRegex(ValueError, "still running"):
                    gittools.publish(repo, "s", "b", head, snapshot, lock_wait=0.5)

    def test_an_error_after_the_push_says_the_commit_was_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)

            def step(argv, worktree, stopped, **kwargs):
                if "push" in argv:
                    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                raise FileNotFoundError("gh")
            with patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(Path(tmp) / "state")}), \
                    patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", step):
                with self.assertRaisesRegex(ValueError, "pushed to brahma/s"):
                    gittools.publish(repo, "s", "b", head, snapshot)

    def test_a_stopped_or_failed_pr_step_also_says_the_commit_was_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            for failure in (ValueError("Publication stopped: Stopped by user"), UnicodeEncodeError("utf-8", "", 0, 1, "x")):
                def step(argv, worktree, stopped, **kwargs):
                    if "push" in argv:
                        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
                    raise failure
                with self.subTest(failure=type(failure).__name__), \
                        patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(Path(tmp) / "state")}), \
                        patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                        patch.object(gittools, "_run_publication_step", step):
                    with self.assertRaisesRegex(ValueError, "pushed to brahma/s"):
                        gittools.publish(repo, "s", "b", head, snapshot)

    def test_an_owned_file_missing_for_a_moment_is_kept_not_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            tracked = next(p for p in (repo / "src").iterdir() if p.is_file())
            name = tracked.relative_to(repo).as_posix()
            with patch.object(gittools, "violating_paths", return_value=[name]), \
                    patch.object(gittools.os, "lstat", side_effect=FileNotFoundError):
                restored, kept = gittools.restore_violations(repo, ["src/**"], {})
            self.assertEqual(restored, [])
            self.assertIn(name, kept)

    def test_a_stored_placeholder_is_reported_but_never_acted_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            stored = {"notes/[REDACTED].txt": "33188:" + "0" * 64}     # a file that existed, stored redacted
            restored, kept = gittools.restore_violations(repo, ["src/**"], stored)
            self.assertNotIn("notes/[REDACTED].txt", restored)
            self.assertTrue(any("stored redacted" in entry for entry in kept))   # a possible deletion stays reported


class Finding10TestsNeverUseTheRealStateDirectory(unittest.TestCase):
    def test_the_suite_runs_with_a_private_state_directory(self):
        guard = Path(os.environ["BRAHMA_TEST_PROVIDER_GUARD"]).parent
        self.assertEqual(Path(os.environ["BRAHMA_STATE_DIR"]), guard / "state")
        self.assertFalse((ROOT / "brahma-publish.lock").exists())



class ThirdReviewInterruptedAutomaticPublication(unittest.IsolatedAsyncioTestCase):
    async def test_a_quit_during_automatic_publication_stops_it_and_keeps_the_run_ready(self):
        import asyncio
        import threading
        from brahma.models import Result
        from tests.test_halt import approved_job
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            self.addCleanup(store.close)
            original = Session.approved_contract

            def approved(self_, *args, **kwargs):      # the approved settings enable commit and publication
                contract, settings, catalog = original(self_, *args, **kwargs)
                settings.auto_commit = settings.publish = True
                return contract, settings, catalog

            async def ready(*args, **kwargs):
                result = Result("brahma", "ready", "done")
                result.tree = "verified-tree"
                return result
            started, seen = threading.Event(), []

            def publishing(*args):                     # a long push that polls its stop check
                started.set()
                for _ in range(500):
                    reason = args[6]()
                    if reason:
                        seen.append(reason)
                        raise ValueError("Publication stopped: " + reason)
                    time.sleep(0.02)
                return "https://example.invalid/pr/1"
            identity = lambda wt: {"branch": "brahma/" + Path(wt).name, "git_dir": "/c/worktrees/x",  # noqa: E731
                                   "common_dir": "/c", "head": "h", "hidden": []}
            with patch("brahma.engine.execute", ready), patch("brahma.engine._tree", return_value="verified-tree"), \
                    patch.object(Session, "approved_contract", approved), \
                    patch.object(gittools, "commit_changes", return_value="c"), \
                    patch.object(gittools, "status_paths", return_value=[]), \
                    patch.object(gittools, "snapshot_changes", return_value={}), \
                    patch.object(gittools, "publish", publishing), \
                    patch.object(gittools, "ownership_changes", return_value=(True, "ok")), \
                    patch.object(gittools, "git_identity", side_effect=identity):
                work = asyncio.ensure_future(session.execute(scheduled=True))
                await asyncio.to_thread(started.wait, 10)
                work.cancel()                          # the terminal UI quits / the service stops mid-push
                result = await work
                for _ in range(100):
                    if seen:
                        break
                    await asyncio.sleep(0.02)
            self.assertEqual(result.status, "ready", result.text)     # verified and committed: /publish still works
            self.assertIn("Brahma closed during publication", result.text)
            self.assertEqual(store.session(session.key)["status"], "ready")
            self.assertEqual(seen, ["Cancelled (Brahma is closing)"])  # the push itself was stopped


if __name__ == "__main__":
    unittest.main()
