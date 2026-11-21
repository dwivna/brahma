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

    async def test_a_cancel_that_arrives_during_publication_stops_it_but_a_stale_one_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self._session(tmp)
            arguments = session._publish_arguments({"head": "h", "uncommitted": {}, "ignored_allowlist": []}, "b")
            self.assertIsNone(arguments[6]())
            session.cancel.set()
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


class Finding10TestsNeverUseTheRealStateDirectory(unittest.TestCase):
    def test_the_suite_runs_with_a_private_state_directory(self):
        guard = Path(os.environ["BRAHMA_TEST_PROVIDER_GUARD"]).parent
        self.assertEqual(Path(os.environ["BRAHMA_STATE_DIR"]), guard / "state")
        self.assertFalse((ROOT / "brahma-publish.lock").exists())


if __name__ == "__main__":
    unittest.main()
