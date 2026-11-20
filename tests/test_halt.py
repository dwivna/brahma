"""Kill switch, distinct pause/stop, and Git hard blocks (Rounds 6, 7, 11; version 0.3.0)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import gittools, halt, scheduler
from brahma.models import Plan, Result, Task
from brahma.session import Session
from brahma.store import Store

from tests.test_scheduler import approved_job, job


class KillFlag(unittest.TestCase):
    def test_engage_release_and_file_only_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            self.assertFalse(halt.engaged(home))
            halt.engage(home)
            self.assertTrue(halt.engaged(home))
            self.assertTrue(halt.release(home))
            self.assertFalse(halt.release(home))

    def test_kill_blocks_claims_and_lifting_it_restarts_nothing_until_resume(self):
        # Review round 8, finding 8: releasing the kill switch used to make queued work claimable by itself.
        from tests.approval import owner_confirms
        from tests.test_cli import run_cli
        with tempfile.TemporaryDirectory() as tmp:
            store, session, digest = approved_job(tmp)
            halt.engage(store.home)
            self.assertIsNone(store.claim(time.time()))
            self.assertEqual(job(store, session.key)["state"], "queued")
            self.assertEqual(store.session(session.key)["approved_digest"], digest)
            with self.assertRaisesRegex(ValueError, "Kill switch engaged"):
                store.resume(session.key)                      # resume waits for the switch to be lifted
            home = store.home
            store.close()
            with owner_confirms():
                code, out, _ = run_cli("--json", "kill", "--release", state=home)
            self.assertEqual((code, json.loads(out)["held_jobs"]), (0, 1))
            store = Store(home)
            self.assertEqual(job(store, session.key)["state"], "held")
            self.assertIsNone(store.claim(time.time() + 10))   # nothing restarts by itself
            self.assertTrue(store.resume(session.key))           # an explicit resume queues it again
            self.assertIsNotNone(store.claim(time.time() + 10))
            store.close()

    def test_publication_needs_a_committed_receipt_for_the_current_approval(self):
        from brahma.models import Event
        with tempfile.TemporaryDirectory() as tmp:
            store, session, digest = approved_job(tmp)
            store.set(session.key, status="ready")
            with self.assertRaisesRegex(ValueError, "No readiness receipt"):
                session.ready_receipt()
            session.emit(Event(kind="ready_receipt", text=json.dumps({"digest": "other", "head": "h", "committed": True})))
            with self.assertRaisesRegex(ValueError, "No readiness receipt"):
                session.ready_receipt()                           # a receipt for another approval does not count
            session.emit(Event(kind="ready_receipt", text=json.dumps({"digest": digest, "head": "h", "committed": False})))
            with self.assertRaisesRegex(ValueError, "not committed"):
                session.ready_receipt()
            session.emit(Event(kind="ready_receipt", text=json.dumps({"digest": digest, "head": "h", "committed": True})))
            self.assertEqual(session.ready_receipt()["head"], "h")
            store.pause(session.key)
            with self.assertRaisesRegex(ValueError, "paused"):
                session.ready_receipt()
            store.close()

    def test_a_paused_foreground_run_keeps_a_continuation_that_resume_queues(self):
        # Review round 8, finding 16: pausing a foreground run finished its job, so resume had nothing to continue.
        with tempfile.TemporaryDirectory() as tmp:
            store, session, digest = approved_job(tmp)
            store.finish_job(session.key)                       # a foreground run has no queued job

            async def engine_run(*args, **kwargs):
                store.pause(session.key)
                return Result("brahma", "cancelled", "paused")
            with patch("brahma.engine.execute", engine_run):
                result = asyncio.run(session.execute())
            self.assertEqual(result.status, "cancelled")
            self.assertEqual(job(store, session.key)["state"], "held")
            self.assertIsNone(store.claim(time.time() + 10))
            self.assertTrue(store.resume(session.key))
            claimed = store.claim(time.time() + 10)
            self.assertEqual((claimed["session"], claimed["digest"]), (session.key, digest))
            store.close()

    def test_kill_works_even_if_the_database_is_unusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            home.mkdir()
            (home / "state.sqlite3").write_text("not a database")
            halt.engage(home)
            self.assertTrue(halt.engaged(home))


class Execution(unittest.IsolatedAsyncioTestCase):
    async def test_execute_refuses_to_start_when_killed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            halt.engage(store.home)
            with self.assertRaisesRegex(ValueError, "Kill switch engaged"):
                await session.execute(scheduled=True)
            store.close()

    async def test_running_worker_is_cancelled_when_the_kill_switch_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            seen = {}

            async def fake_engine(*args, **kwargs):
                cancel = args[4]
                halt.engage(store.home)
                for _ in range(100):
                    if cancel.is_set():
                        seen["cancelled"] = True
                        break
                    await asyncio.sleep(.05)
                return Result("brahma", "cancelled", "stopped")
            with patch("brahma.engine.execute", fake_engine):
                result = await session.execute(scheduled=True)
            self.assertTrue(seen.get("cancelled"))
            self.assertEqual(result.status, "cancelled")
            self.assertIn("Kill switch engaged", result.text)
            store.close()

    async def test_an_external_kill_or_stop_cancels_an_active_planner(self):
        # Review round 8, finding 7: planning only checked the kill switch before it started.
        for action in ("kill", "stop"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                store, session, _ = approved_job(tmp)
                before = store.session(session.key)["plan"]
                seen = {}

                async def planner(request, repo, settings, emit, cancel, provider_gate=None):
                    other = Store(store.home)          # another process acting on the same state
                    halt.engage(other.home) if action == "kill" else other.stop(session.key)
                    other.close()
                    for _ in range(100):
                        if cancel.is_set():
                            seen["cancelled"] = True
                            raise RuntimeError("Planner cancelled")
                        await asyncio.sleep(.05)
                    return Plan("x", [Task("t", "do", ["a.py"], ["ok"])])
                with patch.object(Session, "discover_model_profiles", return_value=None), \
                        patch("brahma.engine.plan", planner):
                    with self.assertRaises((RuntimeError, ValueError)):
                        await session.ask("new goal")
                self.assertTrue(seen.get("cancelled"))
                self.assertEqual(store.session(session.key)["plan"], before)
                store.close()

    async def test_pause_mid_run_keeps_the_job_queued_and_stop_does_not(self):
        # A pause keeps the approved continuation held until `resume` (review round 8, findings 8 and 16).
        for hold, expected in (("pause", "held"), ("stop", "cancelled")):
            with self.subTest(hold=hold), tempfile.TemporaryDirectory() as tmp:
                store, session, _ = approved_job(tmp)

                async def execute(self_, *, scheduled=False):
                    getattr(store, hold)(session.key)
                    return Result("brahma", "cancelled", "held")
                with patch.object(Session, "execute", execute):
                    await scheduler.tick(store)
                self.assertEqual(job(store, session.key)["state"], expected)
                self.assertIsNone(store.claim(time.time() + 10))
                store.close()


class HaltBeforeGitWrites(unittest.IsolatedAsyncioTestCase):
    """A kill, stop or pause that arrives after the work is ready must stop the commit and the publication."""

    async def test_no_commit_or_publish_when_halted_after_the_engine_reports_ready(self):
        for hold in ("kill", "stop", "pause"):
            with self.subTest(hold=hold), tempfile.TemporaryDirectory() as tmp:
                store, session, _ = approved_job(tmp)
                session.settings.auto_commit = True
                session.settings.publish = True

                async def ready_then_halt(*args, **kwargs):
                    halt.engage(store.home) if hold == "kill" else getattr(store, hold)(session.key)
                    return Result("brahma", "ready", "done")
                with patch("brahma.engine.execute", ready_then_halt), \
                        patch.object(gittools, "commit_changes", side_effect=AssertionError("committed while halted")), \
                        patch.object(gittools, "publish", side_effect=AssertionError("published while halted")), \
                        patch.object(gittools, "ownership_changes", return_value=(True, "ok")), patch.object(gittools, "git_identity", side_effect=lambda wt: {"branch": "brahma/" + Path(wt).name, "git_dir": "/c/worktrees/x", "common_dir": "/c", "head": "h", "hidden": []}):
                    result = await session.execute(scheduled=True)
                self.assertEqual(result.status, "cancelled")
                store.close()

    async def test_stop_during_a_running_check_cancels_it_and_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)

            async def engine_running_a_check(*args, **kwargs):
                cancel = args[4]
                task = asyncio.create_task(gittools.run_checks(Path(tmp), [["sleep", "30"]], cancel, 60, lambda e: None))
                await asyncio.sleep(.3)
                store.stop(session.key)
                ok, text = await asyncio.wait_for(task, 10)
                return Result("brahma", "cancelled" if not ok else "ready", text)
            with patch("brahma.engine.execute", engine_running_a_check):
                result = await session.execute(scheduled=True)
            self.assertEqual(result.status, "cancelled")
            store.close()


class PauseResume(unittest.TestCase):
    def test_resume_does_not_undo_a_stop_and_pause_is_separate_from_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            store.pause(session.key)
            saved = store.session(session.key)
            self.assertEqual((saved["paused"], saved["stopped"]), (1, 0))
            store.resume(session.key)
            self.assertEqual(store.session(session.key)["paused"], 0)
            store.stop(session.key)
            store.resume(session.key)
            self.assertEqual(store.session(session.key)["stopped"], 1)
            self.assertIsNone(store.claim(time.time()))
            store.close()

    def test_unknown_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            for action in (store.pause, store.resume):
                with self.assertRaises(ValueError):
                    action("nope")
            store.close()


class GitHardBlocks(unittest.TestCase):
    def test_history_rewrites_merges_and_force_pushes_are_refused(self):
        blocked = (("merge", "x"), ("rebase", "master"), ("reset", "--hard", "HEAD~1"), ("cherry-pick", "abc"),
                   ("commit", "--amend", "-m", "x"), ("commit", "--amend"), ("update-ref", "-d", "refs/heads/x"),
                   ("filter-branch",), ("pull",), ("branch", "-D", "x"), ("branch", "--delete", "x"),
                   ("branch", "-m", "a", "b"), ("tag", "v1"), ("tag", "-d", "v1"),
                   ("push", "origin", "master"), ("push", "origin", "main"), ("push", "origin", "develop"),
                   ("push", "--force", "origin", "brahma/x"), ("push", "-f", "origin", "brahma/x"),
                   ("push", "--force-with-lease", "origin", "brahma/x"), ("push", "origin", "+brahma/x"),
                   ("push", "origin", ":brahma/x"), ("push", "--delete", "origin", "brahma/x"),
                   ("push", "origin", "brahma/x:master"), ("push", "origin", "HEAD:refs/heads/master"),
                   ("push", "origin", "HEAD"), ("push", "--mirror", "origin"), ("push", "--all", "origin"),
                   ("push", "origin"), ("push",), ("checkout", "-B", "x"))
        for args in blocked:
            with self.subTest(args=args):
                self.assertIsNotNone(gittools.refuse(args), args)

    def test_brahmas_own_operations_are_allowed(self):
        allowed = (("rev-parse", "HEAD"), ("status", "--porcelain"), ("add", "--", "a.py"), ("commit", "-m", "Brahma: x"),
                   ("worktree", "add", "-b", "brahma/s", "/p", "HEAD"), ("branch", "--show-current"),
                   ("push", "--set-upstream", "origin", "brahma/s"), ("push", "origin", "brahma/s:brahma/s"),
                   ("config", "user.name", "x"), ("log", "--oneline"), ("diff",), ("ls-files", "-o"))
        for args in allowed:
            with self.subTest(args=args):
                self.assertIsNone(gittools.refuse(args), args)

    def test_git_helper_raises_before_running_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            with patch("brahma.gittools.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "blocked"):
                    gittools.git(Path(tmp), "reset", "--hard")
                run.assert_not_called()

    def test_a_real_default_branch_is_untouched_by_a_blocked_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = Path(tmp) / "origin.git"
            work = Path(tmp) / "work"
            subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
            subprocess.run(["git", "init", "-q", str(work)], check=True)
            for key, value in (("user.name", "t"), ("user.email", "t@e.invalid")):
                subprocess.run(["git", "-C", str(work), "config", key, value], check=True)
            (work / "f").write_text("1")
            gittools.git(work, "add", "f")
            gittools.git(work, "commit", "-m", "one")
            subprocess.run(["git", "-C", str(work), "remote", "add", "origin", str(origin)], check=True)
            with self.assertRaises(ValueError):
                gittools.git(work, "push", "origin", "HEAD:master")
            heads = subprocess.run(["git", "-C", str(origin), "branch", "--list"], capture_output=True, text=True).stdout
            self.assertEqual(heads.strip(), "")


if __name__ == "__main__":
    unittest.main()
