"""Independent review round 10 (Codex): one regression per finding."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from brahma import board, engine, gittools, halt, quota, safety
from brahma.config import Settings
from brahma.models import Event, Plan, Result, Task
from brahma.store import Store
from tests.approval import approve
from tests.test_hardening import make_repo as _make_repo
from tests.test_scheduler import approved_job, job

SECRET = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    return _make_repo(root)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class Finding1GitEnvironment(unittest.TestCase):
    def test_ownership_queries_ignore_a_redirected_repository_or_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, other = make_repo(Path(tmp) / "r"), make_repo(Path(tmp) / "other")
            (repo / "x.txt").write_text("changed\n")
            with patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                                         "GIT_INDEX_FILE": str(Path(tmp) / "nowhere.index")}):
                self.assertIn("x.txt", gittools.status_paths(repo))
                self.assertEqual(gittools.hidden_index_entries(repo), [])


class Finding2NestedSecrets(unittest.TestCase):
    def test_a_review_nested_in_a_checkpoint_is_redacted_before_storage_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            row = {"index": 1, "state": "needs_decision", "dispute": {"review": {"summary": "token " + SECRET,
                                                                                 "issues": [{"text": SECRET}]}}}
            store.event(key, Event(kind="task_state", text=json.dumps(row)))
            stored = [e["text"] for e in store.events(key)]
            self.assertNotIn(SECRET, json.dumps(stored))
            self.assertEqual(json.loads(stored[-1])["dispute"]["review"]["issues"][0]["text"], safety.redact(SECRET))


class Finding3CoordinatorPrompts(unittest.TestCase):
    def test_repository_instructions_are_redacted_and_a_plan_with_a_credential_is_refused(self):
        from brahma import contracts
        from brahma.context import build_context
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "AGENTS.md").write_text("Deploy with " + SECRET + "\n")
            self.assertNotIn(SECRET, build_context(repo, ["x.txt"], 20000))
            plan = Plan("Goal", [Task("T", "use token " + SECRET, ["x.txt"], ["ok"])])
            with self.assertRaisesRegex(ValueError, "credential-shaped"):
                contracts.prepare_contract(plan, repo, [["true"]])


class Finding4InstructionBinding(unittest.TestCase):
    def test_instructions_that_differ_from_the_approved_ones_are_never_sent(self):
        import hashlib
        from brahma.context import build_context
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "AGENTS.md").write_text("approved rules\n")
            approved = {"AGENTS.md": hashlib.sha256(b"approved rules\n").hexdigest()}
            self.assertIn("approved rules", build_context(repo, ["x.txt"], 20000, approved=approved))
            (repo / "AGENTS.md").write_text("rules changed on the branch\n")
            with self.assertRaisesRegex(ValueError, "changed since approval"):
                build_context(repo, ["x.txt"], 20000, approved=approved)
            # An instruction file the approved plan itself owns may change within that plan, and says so.
            text = build_context(repo, ["x.txt"], 20000, approved=approved, owned=("AGENTS.md",))
            self.assertIn("edited within this approved plan", text)
            # Ownership is decided by Brahma's ownership matcher, where `*` never crosses a directory: `docs/*` does not own
            # docs/sub/AGENTS.md, so a changed nested instruction file is refused rather than sent as plan-owned.
            nested = repo / "docs" / "sub"
            nested.mkdir(parents=True)
            (nested / "AGENTS.md").write_text("nested rules\n")
            approved["docs/sub/AGENTS.md"] = hashlib.sha256(b"approved nested\n").hexdigest()
            (repo / "AGENTS.md").write_text("approved rules\n")
            with self.assertRaisesRegex(ValueError, "changed since approval: docs/sub/AGENTS.md"):
                build_context(repo, ["docs/sub/page.md"], 20000, approved=approved, owned=("docs/*",))


class Finding5TerminalApproval(unittest.TestCase):
    def test_the_terminal_ui_approves_only_the_contract_it_displayed(self):
        from brahma.ui import BrahmaApp
        with tempfile.TemporaryDirectory() as tmp:
            from tests.test_cli import repo_session
            store, session = repo_session(tmp)
            self.addCleanup(store.close)
            session.set_plan(Plan("Goal", [Task("T", "do the thing", ["a.txt"], ["ok"])], checks=[["true"]]))
            written = []
            app = SimpleNamespace(session=session, store=store, write=written.append)
            BrahmaApp.show_plan(app, session.plan)
            text = "\n".join(e.text for e in written)
            self.assertEqual(app.displayed_digest, session.approval_summary()[0])
            for expected in ("Base revision:", "Instructions to the worker:", "do the thing", "Model: claude critical",
                             "Repository instructions:"):
                self.assertIn(expected, text)


class Finding6ReadinessBoundToCheckedFiles(unittest.IsolatedAsyncioTestCase):
    async def test_files_changed_during_review_are_checked_and_reviewed_again(self):
        trees = iter([{"head": "h", "changes": {"a": "1"}}, {"head": "h", "changes": {"a": "2"}},   # changed during review
                      {"head": "h", "changes": {"a": "2"}}, {"head": "h", "changes": {"a": "2"}}])
        calls = []

        async def run(provider, prompt, cwd, role, *args, **kwargs):
            calls.append(role)
            return Result(provider, "ok", json.dumps({"verdict": "pass", "summary": "ok", "issues": []}) if role == "reviewer" else "built")
        checks = AsyncMock(return_value=(True, "ok"))
        with patch.object(engine, "_context", return_value="ctx"), patch.object(gittools, "snapshot_changes", return_value={}), \
                patch.object(gittools, "ownership_changes", return_value=(True, "ok")), \
                patch.object(gittools, "git_identity", return_value={}), patch.object(gittools, "run_checks", checks), \
                patch.object(engine, "_tree", side_effect=lambda *a: next(trees)), \
                patch.object(engine.providers, "run", side_effect=run):
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), lambda e: None, asyncio.Event())
        self.assertEqual(result.status, "ready")
        self.assertEqual(checks.await_count, 2)                    # the changed contents were checked again
        self.assertEqual(calls, ["builder", "reviewer", "reviewer"])  # and reviewed again
        self.assertEqual(result.tree, {"head": "h", "changes": {"a": "2"}})


class Finding7TerminalKill(unittest.TestCase):
    def test_the_terminal_kill_stops_orphaned_worker_groups_like_the_cli(self):
        from brahma.ui import BrahmaApp
        with tempfile.TemporaryDirectory() as tmp:
            written = []
            app = SimpleNamespace(usage_cancel=asyncio.Event(), session=None, demo=False,
                                  store=SimpleNamespace(home=Path(tmp)), write=written.append)
            with patch("brahma.halt.kill_everything", return_value=([], [4242])) as kill:
                BrahmaApp.action_kill(app)
            kill.assert_called_once_with(Path(tmp))
            self.assertIn("could not be stopped", written[-1].text)


class Finding8RepeatedStopDuringPlanning(unittest.IsolatedAsyncioTestCase):
    async def test_a_new_stop_is_seen_even_when_an_old_stop_flag_is_still_set(self):
        from brahma.session import Session
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            store.stop(session.key)                         # left over from an earlier plan: planning is still allowed
            seen = {}

            async def planner(request, repo, settings, emit, cancel, provider_gate=None):
                other = Store(store.home)
                other.stop(session.key)                    # a new stop from another process while planning
                other.close()
                for _ in range(100):
                    if cancel.is_set():
                        seen["cancelled"] = True
                        raise RuntimeError("Planner cancelled")
                    await asyncio.sleep(.05)
                return Plan("x", [Task("t", "do", ["a.py"], ["ok"])])
            with patch.object(Session, "discover_model_profiles", return_value=None), patch("brahma.engine.plan", planner):
                with self.assertRaises((RuntimeError, ValueError)):
                    await session.ask("new goal")
            self.assertTrue(seen.get("cancelled"))
            store.close()


class Finding9CommittedViolationsAreRestored(unittest.TestCase):
    def test_an_unowned_edit_the_worker_also_committed_is_put_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "outside.txt").write_text("original\n")
            git(repo, "add", "-A")
            git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
            git(repo, "checkout", "-q", "-b", "brahma/abc")
            identity = gittools.git_identity(repo)
            baseline = gittools.snapshot_changes(repo)
            (repo / "outside.txt").write_text("worker edit\n")
            (repo / "planted.txt").write_text("new file\n")
            git(repo, "add", "-A")
            git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "worker commit")
            ok, _ = gittools.ownership_changes(repo, ["src/**"], baseline=baseline, identity=identity)
            self.assertFalse(ok)
            restored, kept = gittools.restore_violations(repo, ["src/**"], baseline, identity=identity)
            self.assertEqual(sorted(restored), ["outside.txt", "planted.txt"])
            self.assertEqual((repo / "outside.txt").read_text(), "original\n")
            self.assertFalse((repo / "planted.txt").exists())


class Finding10CaseVariants(unittest.TestCase):
    def test_upper_case_secret_and_git_names_are_sensitive(self):
        for name in ("src/.ENV", "src/.Env.local", ".GIT/config", "Credentials.json", "keys/ID.PEM"):
            self.assertTrue(gittools._sensitive(name), name)


class Finding11JsonCommands(unittest.TestCase):
    def test_worker_and_statusline_refuse_json_with_a_json_error(self):
        from tests.test_cli import run_cli
        with tempfile.TemporaryDirectory() as tmp:
            for argv in (("--json", "worker", "--home", tmp, "--once"), ("--json", "statusline")):
                code, out, _ = run_cli(*argv, state=Path(tmp))
                data = json.loads(out)
                self.assertEqual((code, data["ok"], data["exit_code"]), (20, False, 20))


class Finding12ScheduledPauseResume(unittest.IsolatedAsyncioTestCase):
    async def test_a_scheduled_run_paused_and_resumed_keeps_its_continuation(self):
        from brahma import scheduler
        with tempfile.TemporaryDirectory() as tmp:
            store, session, digest = approved_job(tmp)

            async def engine_run(*args, **kwargs):
                store.pause(session.key)
                await asyncio.sleep(.5)
                store.resume(session.key)
                return Result("brahma", "cancelled", "cancelled")
            with patch("brahma.engine.execute", engine_run):
                self.assertTrue(await scheduler.tick(store))
            self.assertEqual(job(store, session.key)["state"], "queued")
            store.close()


class Finding13ClaudeLimitWithoutReset(unittest.IsolatedAsyncioTestCase):
    async def test_a_claude_limit_without_a_reset_waits_for_confirmation(self):
        from brahma.scheduler import Capacity
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            store.observe({"provider": "claude", "source": "claude.worker", "observed_at": time.time(),
                           "windows": [], "blocked": True})
            with patch("brahma.providers.doctor", AsyncMock(return_value={"available": True})):
                decision = await Capacity(store, asyncio.Event())("claude")
            self.assertEqual(decision["state"], "waiting")
            store.observe(quota.unknown("claude", "user.confirmed"))       # the person confirmed capacity
            with patch("brahma.providers.doctor", AsyncMock(return_value={"available": True})):
                self.assertEqual((await Capacity(store, asyncio.Event())("claude"))["state"], "unknown")


class Finding14LiveBoard(unittest.TestCase):
    def test_active_time_advances_between_checkpoints_and_old_plans_are_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            store.event(key, Event(kind="task_state", text=json.dumps({"index": 2, "title": "old plan", "state": "ready"})))
            store.event(key, Event(kind="plan_created", text=json.dumps({"tasks": 1})))
            now = time.time()
            store.event(key, Event(kind="task_state", text=json.dumps({
                "index": 1, "title": "T", "state": "building", "active": True, "budget_seconds": 3600,
                "remaining_seconds": 3000, "deadline_epoch": now + 3000})))
            first = board.build(store, key, now=now).tasks
            later = board.build(store, key, now=now + 40).tasks
            self.assertEqual([row.index for row in first], [1])
            self.assertAlmostEqual(later[0].active_minutes - first[0].active_minutes, 40 / 60, places=2)


class Finding15MeasuredAcrossContinuations(unittest.IsolatedAsyncioTestCase):
    async def test_the_outcome_counts_time_and_usage_from_earlier_continuations(self):
        events = []

        async def run(provider, prompt, cwd, role, *args, **kwargs):
            return Result(provider, "ok", json.dumps({"verdict": "pass", "summary": "ok", "issues": []}),
                          usage={"input_tokens": 5})
        saved = {"state": "reviewing", "phase": "review", "attempt": 0, "next_attempt": 1, "builder": "codex",
                 "remaining_seconds": 3000, "deadline_epoch": time.time() + 3000,
                 "task_usage": {"task_1_attempt_0_builder": {"input_tokens": 100}}}
        with patch.object(engine, "_context", return_value="ctx"), patch.object(gittools, "snapshot_changes", return_value={}), \
                patch.object(gittools, "ownership_changes", return_value=(True, "ok")), \
                patch.object(gittools, "git_identity", return_value={}), \
                patch.object(gittools, "run_checks", AsyncMock(return_value=(True, "ok"))), \
                patch.object(engine, "_tree", return_value={"head": "h", "changes": {}}), \
                patch.object(engine.providers, "run", side_effect=run):
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), events.append, asyncio.Event(), task_progress={1: saved})
        self.assertEqual(result.status, "ready")
        outcome = [json.loads(e.text) for e in events if e.kind == "outcome"][-1]
        self.assertGreaterEqual(outcome["seconds"], 600)                      # 3600 budget - 3000 remaining before resume
        self.assertEqual(outcome["usage_measured"]["task_1_attempt_0_builder"], {"input_tokens": 100})
        self.assertEqual(outcome["usage_measured"]["task_1_attempt_0_reviewer"], {"input_tokens": 5})


if __name__ == "__main__":
    unittest.main()
