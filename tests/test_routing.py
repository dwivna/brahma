"""Quota-aware routing, outcome recording and read-only proposals (Round 4; version 0.7.0)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import engine, gittools, memory as memory_mod, routing
from brahma.config import Settings
from brahma.memory import Memory, MIN_SAMPLES
from brahma.models import Event, Plan, Result, Task
from brahma.session import Session
from brahma.store import Store

AVAILABLE = {"state": "available", "next_at": None, "reason": ""}
UNKNOWN = {"state": "unknown", "next_at": None, "reason": ""}
EXHAUSTED = {"state": "waiting", "next_at": time.time() + 3600, "reason": "limit"}


def history(codex=None, claude=None, kind="code"):
    def stats(ready, n=MIN_SAMPLES + 3, repairs=0.5, findings=0.2):
        return {kind: {"samples": n, "ready_rate": ready, "checks_passed_rate": 1.0, "avg_repairs": repairs,
                       "avg_findings": findings, "learned": n >= MIN_SAMPLES, "note": ""}}
    result = {}
    if codex is not None:
        result["codex"] = stats(codex) if isinstance(codex, float) else {kind: codex}
    if claude is not None:
        result["claude"] = stats(claude) if isinstance(claude, float) else {kind: claude}
    return result


class TaskType(unittest.TestCase):
    def test_types_come_from_owned_paths(self):
        self.assertEqual(routing.task_type(["tests/test_a.py", "tests/test_b.py"]), "tests")
        self.assertEqual(routing.task_type(["docs/a.md", "README.md"]), "docs")
        self.assertEqual(routing.task_type(["src/a.py", "tests/test_a.py"]), "code")
        self.assertEqual(routing.task_type([]), "code")


class Choose(unittest.TestCase):
    def pick(self, codex, claude, hist, pinned=None):
        return routing.choose({"codex": codex, "claude": claude}, hist, "code", pinned)

    def test_learned_ranking_applies_when_both_have_enough_data_and_capacity(self):
        decision = self.pick(AVAILABLE, AVAILABLE, history(codex=0.5, claude=0.9))
        self.assertEqual((decision.provider, decision.mode), ("claude", "learned"))
        self.assertIn("measured", decision.reason)

    def test_quota_overrides_a_better_learned_record(self):
        decision = self.pick(AVAILABLE, EXHAUSTED, history(codex=0.2, claude=0.99))
        self.assertEqual(decision.provider, "codex")
        self.assertNotEqual(decision.mode, "learned")
        self.assertIn("only codex has capacity", decision.reason)

    def test_no_eligible_provider_records_a_durable_wait_on_the_earliest_reset(self):
        soon = {"state": "waiting", "next_at": time.time() + 60, "reason": "x"}
        decision = self.pick(EXHAUSTED, soon, history(codex=0.9, claude=0.1))
        self.assertEqual((decision.provider, decision.mode), ("claude", "wait"))
        self.assertEqual(self.pick({"state": "blocked"}, {"state": "unavailable"}, {}).mode, "none")

    def test_sparse_data_says_no_data_yet_and_uses_the_documented_default(self):
        sparse = {"samples": MIN_SAMPLES - 1, "ready_rate": 1.0, "checks_passed_rate": 1.0, "avg_repairs": 0,
                  "avg_findings": 0, "learned": False, "note": ""}
        for hist in ({}, history(codex=sparse, claude=0.1), history(codex=0.1, claude=sparse)):
            decision = self.pick(AVAILABLE, AVAILABLE, hist)
            self.assertEqual((decision.provider, decision.mode), ("codex", "default"))
            self.assertIn("no data yet", decision.reason)
            self.assertIn("not a learned preference", decision.reason)
        self.assertIn("no data yet", routing.choose({"codex": AVAILABLE, "claude": AVAILABLE}, {}, "code").table["claude"]["history"])
        self.assertIn(f"4 of {MIN_SAMPLES}", self.pick(AVAILABLE, AVAILABLE, history(codex=sparse)).table["codex"]["history"])

    def test_unknown_quota_is_kept_as_unknown_and_ranks_below_available(self):
        decision = self.pick(UNKNOWN, AVAILABLE, {})
        self.assertEqual(decision.provider, "claude")
        self.assertEqual(decision.table["codex"]["quota"], "unknown")
        self.assertEqual(self.pick(UNKNOWN, UNKNOWN, {}).provider, "codex")

    def test_ties_in_learned_scores_break_on_repairs_then_findings(self):
        a = {"samples": 9, "ready_rate": 0.8, "checks_passed_rate": 1, "avg_repairs": 2.0, "avg_findings": 0.1, "learned": True, "note": ""}
        b = dict(a, avg_repairs=0.5)
        self.assertEqual(self.pick(AVAILABLE, AVAILABLE, history(codex=a, claude=b)).provider, "claude")

    def test_pinned_provider_is_honoured_even_against_the_record(self):
        decision = self.pick(AVAILABLE, AVAILABLE, history(codex=0.1, claude=0.9), pinned="codex")
        self.assertEqual((decision.provider, decision.mode), ("codex", "pinned"))

    def test_history_for_another_task_type_is_not_consulted(self):
        decision = routing.choose({"codex": AVAILABLE, "claude": AVAILABLE}, history(codex=0.1, claude=0.9, kind="docs"), "code")
        self.assertEqual(decision.mode, "default")


class Proposals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "x")):
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                           check=True, capture_output=True)
        self.store = Store(Path(self.tmp.name) / "state")
        self.memory = Memory(self.store)
        self.repo_id = memory_mod.repo_id(self.repo)

    def test_a_failed_check_yields_a_draft_with_gherkin_and_starts_nothing(self):
        self.memory.remember(self.repo_id, "failure", "check:python -m unittest", "python -m unittest => exit 1\nFAIL: test_x")
        with patch("brahma.providers.run", side_effect=AssertionError("a worker must not start")):
            drafts = routing.draft_proposals(self.repo, self.memory, self.repo_id)
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        self.assertEqual((draft["source"], draft["status"], draft["approved"]), ("check_failure", "draft", False))
        self.assertIn("Scenario:", draft["gherkin"])
        self.assertIn("exit 1", draft["evidence"])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)

    def test_the_module_cannot_launch_workers_or_approve(self):
        import ast
        tree = ast.parse(Path(routing.__file__).read_text())
        imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual(imported - {"__future__", "json", "subprocess", "time", "dataclasses", "pathlib", "memory", "gitsafe"}, set())   # gitsafe: read-only Git queries
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(calls & {"enqueue", "execute", "set", "stop"})   # nothing that queues, runs or approves

    def test_stale_brahma_branches_and_supplied_feedback_become_drafts(self):
        subprocess.run(["git", "-C", str(self.repo), "branch", "brahma/old"], check=True)
        later = time.time() + 40 * 86400
        stale = routing.stale_branches(self.repo, now=later)
        self.assertEqual([p.title for p in stale], ["Decide what to do with stale branch brahma/old"])
        self.assertEqual(routing.stale_branches(self.repo, now=time.time()), [])
        drafts = routing.draft_proposals(self.repo, self.memory, self.repo_id, feedback="Reviewer: rename helper  and add docs")
        self.assertEqual([d["source"] for d in drafts], ["feedback"])
        self.assertEqual(routing.from_feedback("  "), [])
        # the branch is still there: proposing never deletes or merges
        self.assertIn("brahma/old", subprocess.run(["git", "-C", str(self.repo), "branch"], text=True, capture_output=True).stdout)

    def test_no_signals_means_no_proposals(self):
        self.assertEqual(routing.draft_proposals(self.repo, self.memory, self.repo_id), [])


class EngineRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events, self.calls = [], []
        self.patches = [patch.object(engine, "_context", return_value="ctx"),
                        patch.object(gittools, "snapshot_changes", return_value={}, create=True),
                        patch.object(gittools, "ownership_changes", return_value=(True, "Owned")), patch.object(gittools, "git_identity", return_value={}),
                        patch.object(gittools, "run_checks", AsyncMock(return_value=(True, "Actual checks: exit 0"))),
                        patch.object(engine, "_tree", return_value={"head": "h", "changes": {}})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def run_engine(self, gate, hist, tasks=None, settings=None):
        settings = settings or Settings(checks=[["true"]])
        settings._routing_history = hist
        review = json.dumps({"verdict": "pass", "summary": "ok", "issues": []})

        async def run(provider, prompt, cwd, role, selected, emit, cancel, timeout, native_session_id=None, **_extra):
            self.calls.append((provider, role))
            return Result(provider, "ok", review if role == "reviewer" else "done")
        with patch.object(engine.providers, "run", side_effect=run):
            return await engine.execute(Plan("Goal", tasks or [Task("T", "do", ["src/t.py"], ["ok"])]), Path("/repo"),
                                        settings, self.events.append, asyncio.Event(), provider_gate=gate)

    def kinds(self, kind):
        return [json.loads(e.text) for e in self.events if e.kind == kind]

    async def test_learned_record_chooses_the_builder_and_the_decision_is_recorded(self):
        gate = AsyncMock(return_value=AVAILABLE)
        result = await self.run_engine(gate, history(codex=0.3, claude=0.9))
        self.assertEqual(result.status, "ready")
        self.assertEqual(self.calls, [("claude", "builder"), ("codex", "reviewer")])  # reviewer is the non-author
        decision = self.kinds("routing")[0]
        self.assertEqual((decision["provider"], decision["mode"], decision["task_type"]), ("claude", "learned", "code"))
        self.assertEqual(set(decision["table"]), {"codex", "claude"})

    async def test_exhausted_quota_beats_the_learned_ranking_inside_the_engine(self):
        async def gate(name):
            return EXHAUSTED if name == "claude" else AVAILABLE
        await self.run_engine(gate, history(codex=0.1, claude=0.99))
        self.assertEqual(self.calls[0], ("codex", "builder"))
        self.assertEqual(self.kinds("routing")[0]["mode"], "default")

    async def test_pinned_tasks_skip_routing_and_sparse_history_says_so(self):
        await self.run_engine(AsyncMock(return_value=AVAILABLE), {}, [Task("T", "do", ["src/t.py"], ["ok"], "claude")])
        self.assertEqual(self.calls[0], ("claude", "builder"))
        self.assertEqual(self.kinds("routing"), [])
        self.calls.clear()
        await self.run_engine(AsyncMock(return_value=AVAILABLE), {})
        self.assertIn("no data yet", self.kinds("routing")[0]["reason"])

    async def test_a_measured_outcome_is_emitted_for_a_ready_task(self):
        await self.run_engine(AsyncMock(return_value=AVAILABLE), {})
        outcome = self.kinds("outcome")[-1]
        self.assertEqual((outcome["provider"], outcome["task_type"], outcome["ready"], outcome["checks_passed"]),
                         ("codex", "code", True, True))
        self.assertEqual(outcome["repairs"], 0)
        self.assertIsNone(outcome["usage_measured"])  # nothing was reported, so nothing is invented


class SessionRecording(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "x")):
            subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                           check=True, capture_output=True)
        self.store = Store(Path(self.tmp.name) / "state")
        self.session = Session(self.store, self.repo)
        self.memory = Memory(self.store)
        self.repo_id = memory_mod.repo_id(self.repo)

    def test_outcome_events_become_routing_history_and_survive_a_restart(self):
        for n in range(MIN_SAMPLES):
            self.session.emit(Event("brahma", "manager", "outcome", json.dumps(
                {"index": 1, "provider": "claude", "task_type": "code", "ready": n != 0, "checks_passed": True,
                 "repairs": n % 2, "findings": 0, "seconds": 5.0, "usage_measured": {"task_1_attempt_0_builder": {"tokens": 10}}})))
        self.store.close()
        again = Memory(Store(Path(self.tmp.name) / "state"))
        stats = again.routing_history(self.repo_id)["claude"]["code"]
        self.assertEqual((stats["samples"], stats["learned"]), (MIN_SAMPLES, True))
        self.assertAlmostEqual(stats["ready_rate"], (MIN_SAMPLES - 1) / MIN_SAMPLES)
        row = again.inspect(self.repo_id)["outcomes"][0]
        self.assertIsNotNone(row["usage_measured"])
        self.assertIsNone(row["usage_estimated"])

    def test_a_failing_check_is_remembered_and_a_passing_one_is_not(self):
        self.session.emit(Event(kind="check", text="python -m unittest => exit 1\nFAIL: test_x"))
        self.session.emit(Event(kind="check", text="git diff --check => exit 0\n"))
        failures = routing.recorded_check_failures(self.memory, self.repo_id)
        self.assertEqual(len(failures), 1)
        self.assertIn("exit 1", failures[0])
        self.assertEqual(len(routing.draft_proposals(self.repo, self.memory, self.repo_id)), 1)

    def test_a_bad_outcome_event_never_breaks_the_run(self):
        self.session.emit(Event("brahma", "manager", "outcome", json.dumps({"index": 1, "provider": "nobody"})))
        self.assertEqual(self.memory.inspect(self.repo_id)["outcomes"], [])


if __name__ == "__main__":
    unittest.main()
