"""Offline scheduler tests: temp Git/SQLite fixtures, Session.execute and launchctl mocked."""
import asyncio
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from brahma import scheduler
from brahma.contracts import read_contract
from brahma.gittools import git
from brahma.models import Plan, Result, Task
from brahma.session import Session
from brahma.store import Store


def init_repo(root):
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git(root, "config", "user.name", "Brahma test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "first.txt").write_text("base\n")
    git(root, "add", "first.txt")
    git(root, "commit", "-m", "fixture")
    return root


def approved_job(tmp, due=0.0):
    """Fixture: an approved plan with a queued job. Setting approved_digest here stands in for
    the user's explicit approval, which production scheduler code never creates."""
    root = init_repo(Path(tmp) / "repo")
    store = Store(Path(tmp) / "state")
    session = Session(store, root)
    session.set_plan(Plan("goal", [Task("task", "change", ["first.txt"], ["correct"])]))
    digest = read_contract(Path(store.session(session.key)["contract"]))["digest"]
    store.set(session.key, approved_digest=digest)
    store.enqueue(session.key, session.generation, digest, due, "due")
    return store, session, digest


def job(store, key):
    row = store.db.execute("SELECT state,next_at,reason FROM jobs WHERE session=?", (key,)).fetchone()
    return dict(zip(("state", "next_at", "reason"), row))


class Tick(unittest.IsolatedAsyncioTestCase):
    async def test_runs_due_approved_job_scheduled_and_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            calls = []
            async def execute(self, *, scheduled=False):
                calls.append(scheduled)
                return Result("brahma", "ready", "ok")
            with patch.object(Session, "execute", execute):
                self.assertTrue(await scheduler.tick(store))
                self.assertFalse(await scheduler.tick(store))
            self.assertEqual(calls, [True])
            self.assertEqual(job(store, session.key)["state"], "done")
            store.close()

    async def test_future_job_does_not_run_and_unknown_reset_never_spins(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, digest = approved_job(tmp, due=time.time() + 3600)
            async def execute(self, *, scheduled=False):
                raise AssertionError("must not run")
            with patch.object(Session, "execute", execute):
                self.assertFalse(await scheduler.tick(store))
                store.enqueue(session.key, session.generation, digest, None, "reset unknown")
                self.assertFalse(await scheduler.tick(store, now=time.time() + 10**7))
            self.assertEqual(job(store, session.key)["state"], "queued")
            store.close()

    async def test_mismatched_digest_or_generation_is_cancelled_not_run(self):
        for field in ("digest", "generation"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                store, session, digest = approved_job(tmp)
                if field == "digest":
                    store.enqueue(session.key, session.generation, "0" * 64, 0.0, "stale")
                else:
                    store.enqueue(session.key, "old-generation", digest, 0.0, "stale")
                async def execute(self, *, scheduled=False):
                    raise AssertionError("must not run")
                with patch.object(Session, "execute", execute):
                    self.assertTrue(await scheduler.tick(store))
                self.assertEqual(job(store, session.key)["state"], "cancelled")
                store.close()

    async def test_unapproved_session_never_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            store.set(session.key, approved_digest=None)
            async def execute(self, *, scheduled=False):
                raise AssertionError("must not run")
            with patch.object(Session, "execute", execute):
                self.assertTrue(await scheduler.tick(store))
            self.assertEqual(job(store, session.key)["state"], "cancelled")
            self.assertIsNone(store.session(session.key)["approved_digest"])
            store.close()

    async def test_paused_session_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            store.stop(session.key)
            async def execute(self, *, scheduled=False):
                raise AssertionError("must not run")
            with patch.object(Session, "execute", execute):
                self.assertFalse(await scheduler.tick(store))
            self.assertEqual(job(store, session.key)["state"], "cancelled")
            store.close()

    async def test_completed_session_job_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            store.set(session.key, status="ready")
            async def execute(self, *, scheduled=False):
                raise AssertionError("must not run")
            with patch.object(Session, "execute", execute):
                self.assertTrue(await scheduler.tick(store))
            self.assertEqual(job(store, session.key)["state"], "done")
            store.close()

    async def test_active_worker_requeues_with_delay_and_no_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            other = Session(store, session.repo, key=session.key)
            async def execute(self, *, scheduled=False):
                with self.lock():  # same global lock as the real execute
                    return Result("brahma", "ready", "must not get here")
            with other.lock(), patch.object(Session, "execute", execute):  # a foreground worker is active
                before = time.time()
                self.assertTrue(await scheduler.tick(store))
            state = job(store, session.key)
            self.assertEqual(state["state"], "queued")
            self.assertGreaterEqual(state["next_at"], before + scheduler.RETRY_SECONDS - 1)
            self.assertFalse(await scheduler.tick(store))  # not retried immediately
            store.close()

    async def test_blocked_and_unexpected_errors_do_not_retry(self):
        for error in (ValueError("Scheduled approval is no longer valid"), RuntimeError("boom")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as tmp:
                store, session, _ = approved_job(tmp)
                async def execute(self, *, scheduled=False):
                    raise error
                with patch.object(Session, "execute", execute):
                    self.assertTrue(await scheduler.tick(store))
                    self.assertFalse(await scheduler.tick(store))
                self.assertEqual(job(store, session.key)["state"], "blocked")
                self.assertTrue(any(e["kind"] == "error" for e in store.events(session.key)))
                store.close()

    async def test_waiting_result_keeps_persisted_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, digest = approved_job(tmp)
            later = time.time() + 7200
            async def execute(self, *, scheduled=False):
                self.store.enqueue(self.key, self.generation, digest, later, "weekly reset")
                return Result("brahma", "waiting", "wait")
            with patch.object(Session, "execute", execute):
                self.assertTrue(await scheduler.tick(store))
                self.assertFalse(await scheduler.tick(store))
                self.assertEqual(job(store, session.key), {"state": "queued", "next_at": later, "reason": "weekly reset"})
                # Still patched: this tick used to run the real Session.execute and launch real provider CLIs.
                self.assertTrue(await scheduler.tick(store, now=later + 1) is True)
            store.close()

    async def test_service_stop_cancels_child_work_and_requeues_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            started, stop, seen = asyncio.Event(), asyncio.Event(), []
            async def execute(self, *, scheduled=False):
                started.set()
                await self.cancel.wait()
                seen.append("child cancelled")
                self.store.finish_job(self.key)  # real execute marks a cancelled run done
                return Result("brahma", "cancelled", "stopped")
            with patch.object(Session, "execute", execute):
                task = asyncio.create_task(scheduler.tick(store, stop=stop, grace=2))
                await started.wait()
                stop.set()
                await asyncio.wait_for(task, 5)
            self.assertEqual(seen, ["child cancelled"])
            state = job(store, session.key)
            self.assertEqual(state["state"], "queued")
            self.assertLessEqual(state["next_at"], time.time())
            store.close()

    async def test_service_stop_hard_cancels_unresponsive_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            started, stop = asyncio.Event(), asyncio.Event()
            async def execute(self, *, scheduled=False):
                started.set()
                await asyncio.sleep(60)
            with patch.object(Session, "execute", execute):
                task = asyncio.create_task(scheduler.tick(store, stop=stop, grace=.05))
                await started.wait()
                stop.set()
                await asyncio.wait_for(task, 5)
            self.assertEqual(job(store, session.key)["state"], "queued")
            store.close()

    async def test_user_pause_during_service_stop_is_not_undone(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            started, stop = asyncio.Event(), asyncio.Event()
            async def execute(self, *, scheduled=False):
                started.set()
                await self.cancel.wait()
                return Result("brahma", "cancelled", "stopped")
            with patch.object(Session, "execute", execute):
                task = asyncio.create_task(scheduler.tick(store, stop=stop, grace=2))
                await started.wait()
                store.stop(session.key)
                stop.set()
                await asyncio.wait_for(task, 5)
            self.assertEqual(job(store, session.key)["state"], "cancelled")
            self.assertFalse(await scheduler.tick(store))
            store.close()

    async def test_outer_cancellation_cancels_work_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            started, seen = asyncio.Event(), []
            async def execute(self, *, scheduled=False):
                started.set()
                await self.cancel.wait()
                seen.append("cancel event")
                return Result("brahma", "cancelled", "stopped")
            with patch.object(Session, "execute", execute):
                task = asyncio.create_task(scheduler.tick(store, grace=2))
                await started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(seen, ["cancel event"])
            self.assertEqual(job(store, session.key)["state"], "queued")
            store.close()


class Serve(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_abandoned_running_job_once_and_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            self.assertIsNotNone(store.claim(time.time()))  # daemon died while running
            self.assertEqual(job(store, session.key)["state"], "running")
            store.close()
            calls = []
            async def execute(self, *, scheduled=False):
                calls.append(scheduled)
                return Result("brahma", "ready", "ok")
            with patch.object(Session, "execute", execute):
                self.assertEqual(await scheduler.serve(Path(tmp) / "state", once=True), 0)
            self.assertEqual(calls, [True])
            check = Store(Path(tmp) / "state")
            self.assertEqual(job(check, session.key)["state"], "done")
            mode = stat.S_IMODE((Path(tmp) / "state" / "scheduler.lock").stat().st_mode)
            self.assertEqual(mode, 0o600)
            check.close()

    async def test_recovered_job_does_not_overlap_existing_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            store.claim(time.time())
            other = Session(store, session.repo, key=session.key)
            async def execute(self, *, scheduled=False):
                with self.lock():
                    return Result("brahma", "ready", "must not get here")
            with other.lock(), patch.object(Session, "execute", execute):
                self.assertEqual(await scheduler.serve(Path(tmp) / "state", once=True), 0)
            self.assertEqual(job(store, session.key)["state"], "queued")
            store.close()

    async def test_second_scheduler_exits_without_running_work(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            store.close()
            async def execute(self, *, scheduled=False):
                raise AssertionError("must not run")
            with (Path(tmp) / "state" / "scheduler.lock").open("a") as held, patch.object(Session, "execute", execute):
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch("sys.stderr"):
                    self.assertEqual(await scheduler.serve(Path(tmp) / "state", once=True), 0)

    async def test_tick_failure_does_not_kill_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            async def broken(store, **kwargs):
                raise RuntimeError("database busy")
            with patch.object(scheduler, "tick", broken), patch("sys.stderr"):
                self.assertEqual(await scheduler.serve(Path(tmp) / "state", once=True), 0)


class ServiceRegistration(unittest.TestCase):
    def test_plist_uses_absolute_paths_and_no_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "OPENAI_API_KEY": "sk-secret", "ANTHROPIC_API_KEY": "sk-secret2", "GH_TOKEN": "gh-secret-xyz"}):
            data = scheduler.service_plist(Path(tmp) / "state")
            self.assertTrue(all(Path(part).is_absolute() for i, part in enumerate(data["ProgramArguments"])
                                if i in (0, 5)))
            self.assertEqual(data["ProgramArguments"][1:5], ["-m", "brahma", "worker", "--home"])
            text = plistlib.dumps(data).decode()
            for secret in ("sk-secret", "gh-secret-xyz", "API_KEY", "GH_TOKEN"):
                self.assertNotIn(secret, text)
            self.assertEqual(set(data["EnvironmentVariables"]) - {"JAVA_HOME"}, {"PATH", "PYTHONPATH"})
            self.assertTrue(Path(data["EnvironmentVariables"]["PYTHONPATH"]).is_absolute())

    def test_plist_carries_java_home_only_when_it_is_a_real_absolute_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            with patch.dict(os.environ, {"JAVA_HOME": tmp}):
                self.assertEqual(scheduler.service_plist(home)["EnvironmentVariables"]["JAVA_HOME"], str(tmp))
            for bad in ("relative/jdk", str(Path(tmp) / "missing"), ""):
                with self.subTest(java_home=bad), patch.dict(os.environ, {"JAVA_HOME": bad}):
                    self.assertNotIn("JAVA_HOME", scheduler.service_plist(home)["EnvironmentVariables"])

    def test_install_status_stop_use_private_file_and_launchctl_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(scheduler.sys, "platform", "darwin"):
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, "", "")
            agents, home = Path(tmp) / "agents", Path(tmp) / "state"
            message = scheduler.service("install", home, agents=agents, run=run)
            self.assertIn("approved queued contracts", message)
            plist = agents / (scheduler.LABEL + ".plist")
            self.assertEqual(stat.S_IMODE(plist.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
            self.assertEqual(plistlib.loads(plist.read_bytes())["Label"], scheduler.LABEL)
            self.assertEqual([c[0] for c in commands], ["launchctl", "launchctl"])
            self.assertEqual(commands[-1][1], "bootstrap")
            self.assertIn("registered", scheduler.service("status", home, agents=agents, run=run))
            self.assertIn("stopped", scheduler.service("stop", home, agents=agents, run=run))
            self.assertFalse(list(agents.glob("*.tmp")))

    def test_status_and_stop_report_missing_service(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(scheduler.sys, "platform", "darwin"):
            run = lambda command, **kwargs: subprocess.CompletedProcess(command, 113, "", "")
            self.assertIn("not registered", scheduler.service("status", Path(tmp), agents=Path(tmp), run=run))
            self.assertIn("not running", scheduler.service("stop", Path(tmp), agents=Path(tmp), run=run))

    def test_registration_failure_and_unsupported_platform_are_clear_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(scheduler.sys, "platform", "linux"), self.assertRaises(ValueError):
                scheduler.service("install", Path(tmp), agents=Path(tmp))
            def run(command, **kwargs):
                if command[1] == "bootstrap":
                    raise subprocess.CalledProcessError(5, command, stderr=b"denied")
                return subprocess.CompletedProcess(command, 0, "", "")
            with patch.object(scheduler.sys, "platform", "darwin"), self.assertRaisesRegex(ValueError, "denied"):
                scheduler.service("install", Path(tmp) / "s", agents=Path(tmp) / "a", run=run)
            with patch.object(scheduler.sys, "platform", "darwin"), self.assertRaises(ValueError):
                scheduler.service("restart", Path(tmp), agents=Path(tmp))


if __name__ == "__main__":
    unittest.main()
