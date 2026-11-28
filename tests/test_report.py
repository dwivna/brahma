import json
import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from brahma.models import Event
from brahma.report import build_report, latest_report
from brahma.store import Store
from tests.test_codex_round17 import ready_result
from tests.test_scheduler import approved_job


class WorkReports(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name) / "state")
        self.addCleanup(self.store.close)
        self.key = self.store.create(Path(tmp.name))
        self.store.set(self.key, plan=json.dumps({"tasks": [{"title": "First"}, {"title": "Second"}]}))
        self.record("plan_created", "generation")

    def record(self, kind, value, provider="brahma", role="manager"):
        text = value if isinstance(value, str) else json.dumps(value)
        self.store.event(self.key, Event(provider, role, kind, text))

    def state(self, state, summary="", index=1):
        self.record("task_state", {"index": index, "title": ["First", "Second"][index - 1],
                                   "state": state, "summary": summary})

    def report(self):
        return build_report(self.store, self.key)

    def test_order_scope_and_evidence(self):
        self.assertIsNone(latest_report(self.store, self.key))
        self.record("check", "Running approved check: stale")
        self.record("plan_created", "replacement")
        self.state("checking")
        self.record("message", "all tests pass", "claude", "builder")
        self.assertIn("No checks were run", self.report()["evidence"])
        self.record("check_status", {"argv": ["check", "first"], "phase": "start"})
        self.record("check_status", {"argv": ["check", "first"], "phase": "complete", "exit_code": 0})
        self.state("reviewing")
        self.record("message", {"verdict": "pass", "summary": "all tests pass",
                                "issues": [{"severity": "low", "text": "A note"}]}, "codex", "reviewer")
        self.record("review_result", {"index": 1, "reviewer": "codex", "verdict": "pass",
                                      "issues": [{"severity": "low", "text": "A note"}]})
        self.state("ready")
        report = self.report()
        self.assertEqual(list(report), ["completed", "evidence", "current_work", "next_work", "where_we_are", "needs_you"])
        self.assertTrue(all(isinstance(value, str) for value in report.values()))
        self.assertIn("Task 2 Second", report["next_work"])
        self.assertIn("check first => exit 0", report["evidence"])
        self.assertIn("Codex pass (1 low", report["evidence"])
        for excluded in ("stale", "all tests pass", "stdout"):
            self.assertNotIn(excluded, report["evidence"])
        self.assertEqual(report["needs_you"], "None")
        self.state("ready", index=2)
        self.record("summary", "ready: done")
        final = self.report()
        self.assertIn("Task 1 First", final["completed"])
        self.assertIn("Task 2 Second", final["completed"])
        self.assertIn("check first => exit 0", final["evidence"])
        self.record("work_report", final)
        self.assertEqual(latest_report(self.store, self.key), final)

    def test_owner_override_records_the_independent_rejection(self):
        self.state("reviewing")
        self.record("message", {"verdict": "pass", "summary": "ok", "issues": []}, "codex", "reviewer")
        self.record("review_result", {"index": 1, "reviewer": "codex", "verdict": "changes_requested",
                                      "issues": [{"severity": "high", "text": "Fix needed"}]})
        self.record("dispute_overruled", "Abhinav sided with the author.")
        self.state("ready")
        evidence = self.report()["evidence"]
        self.assertIn("Owner decision: Abhinav sided with the author", evidence)
        self.assertIn("Codex changes_requested", evidence)
        self.assertNotIn("Codex pass", evidence)

    def test_severity_counts_include_findings_after_the_dispute_limit(self):
        self.state("reviewing")
        self.record("review_result", {"index": 1, "reviewer": "codex", "verdict": "changes_requested",
                                      "issues": ["legacy"] * 20 + [{"severity": "high", "text": "late"}]})
        self.assertIn("20 medium, 1 high", self.report()["evidence"])

    def test_invalidated_ready_evidence_requires_check_and_review(self):
        self.state("ready")
        self.state("ready", index=2)
        self.record("evidence_invalidated", {"indices": [1], "paths": ["a"]})
        self.record("summary", "incomplete: Reviewed files changed")
        report = self.report()
        self.assertNotIn("Task 1", report["completed"])
        self.assertIn("Task 2 Second", report["completed"])
        self.assertEqual(report["next_work"], "Task 1 First")
        self.assertIn("Checks outstanding", report["evidence"])
        self.assertIn("Independent review outstanding for Task 1 First", report["where_we_are"])

    def test_owner_actions(self):
        for state, reason, action in [
            ("needs_decision", "Disputed finding", "brahma decide " + self.key),
            ("incomplete", "Attempt budget exhausted", "new plan"),
            ("blocked", "Kill switch engaged", "resume " + self.key),
            ("cancelled", "Paused by user", "resume " + self.key),
            ("blocked", "Sign in to Codex", "Sign in to Codex"),
            ("waiting", "Toolchain changed", "Toolchain changed"),
        ]:
            with self.subTest(state=state, reason=reason):
                self.state(state, reason)
                self.assertIn(action, self.report()["needs_you"])
        self.record("wait", {"provider": "codex", "state": "waiting", "next_at": "2026-11-25T16:10:58Z"})
        self.state("waiting")
        self.addCleanup(time.tzset)
        with patch.dict(os.environ, TZ="Asia/Kolkata"):
            time.tzset()
            action = self.report()["needs_you"]
            self.assertIn("Codex", action)
            self.assertIn("Wed 25 Nov 2026 21:40 IST", action)

    def test_run_blocked_before_assignment(self):
        self.record("summary", "blocked: Kill switch engaged")
        report = self.report()
        self.assertIn("No checks were run", report["evidence"])
        self.assertIn("Independent review unverified", report["evidence"])
        self.assertIn("resume " + self.key, report["needs_you"])

    def test_budget_exhaustion_leaves_no_task_underway(self):
        self.state("building")
        self.assertIn("Task 1 First", self.report()["current_work"])
        self.state("incomplete", "Attempt budget exhausted")
        self.assertEqual(self.report()["current_work"], "None")
        self.record("summary", "incomplete: Attempt budget exhausted")
        report = self.report()
        self.assertEqual(report["current_work"], "None")
        self.assertIn("Task 1 First", report["next_work"])
        self.assertIn("new plan", report["needs_you"])

    def test_repair_requires_another_review(self):
        self.state("reviewing")
        self.record("message", {"verdict": "pass", "summary": "Reviewed", "issues": []},
                    "codex", "reviewer")
        self.state("changes_requested", "Files changed after checks")
        self.state("incomplete")
        report = self.report()
        self.assertNotIn("Codex pass", report["evidence"])
        self.assertIn("Independent review unverified", report["evidence"])
        self.assertIn("Independent review outstanding for Task 1 First", report["where_we_are"])

    def test_blocked_capacity_names_provider_and_unknown_reset(self):
        self.state("assigned")
        self.record("wait", {"provider": "claude", "state": "blocked", "next_at": None})
        self.state("blocked")
        self.assertIn("Claude capacity. Reset unknown", self.report()["needs_you"])

    def test_implicit_check_distinct_from_missing_project_checks(self):
        self.state("checking")
        self.record("check", "Running approved check: git diff --check")
        self.record("check", "git diff --check => exit 0")
        self.state("needs_checks")
        self.record("summary", "needs_checks: configure project checks")
        report = self.report()
        self.assertNotIn("=> exit 0", report["evidence"])
        self.assertIn("Legacy check result unverified", report["evidence"])
        self.assertIn("No project checks configured", report["evidence"])
        self.assertIn("Configure checks", report["needs_you"])

    def test_skills_are_identical(self):
        root = Path(__file__).resolve().parents[1]
        skill = Path("skills/work-report/SKILL.md")
        self.assertEqual((root / ".agents" / skill).read_bytes(), (root / ".claude" / skill).read_bytes())


class ReportIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_stranded_commit_gets_a_blocked_report_when_display_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            with closing(store):
                def display(event):
                    if event.kind == "error":
                        raise RuntimeError("display failed")

                session.callback = display
                head = "a" * 40
                steps = [((True, "ok"), True), (None, None, (OSError("untrusted detail"), head, True))]
                with patch("brahma.engine.execute", ready_result), \
                        patch("brahma.session._finish_in_thread", side_effect=steps), \
                        self.assertRaisesRegex(RuntimeError, "display failed"):
                    await session.execute(scheduled=True)
                self.assertEqual(store.session(session.key)["status"], "blocked")
                final = latest_report(store, session.key)
                self.assertIn("Run blocked", final["where_we_are"])
                self.assertIn(head, final["completed"])
                self.assertNotIn("untrusted detail", str(final))

    async def test_interruption_replaces_an_earlier_report_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            with closing(store):
                async def interrupt(plan, worktree, settings, emit, *args, **kwargs):
                    emit(Event(kind="task_state", text=json.dumps({"index": 1, "title": "task", "state": "waiting"})))
                    raise RuntimeError("untrusted detail")

                with patch("brahma.engine.execute", interrupt), self.assertRaisesRegex(RuntimeError, "untrusted detail"):
                    await session.execute(scheduled=True)
                self.assertEqual(store.session(session.key)["status"], "interrupted")
                reports = [json.loads(e["text"]) for e in store.events(session.key) if e["kind"] == "work_report"]
                self.assertEqual(len(reports), 2)
                self.assertIn("Run interrupted", reports[-1]["where_we_are"])
                self.assertNotIn("untrusted detail", str(reports[-1]))

    async def test_report_failures_preserve_ready_run(self):
        for failure in ("none", "build", "store"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                store, session, _ = approved_job(tmp)
                with closing(store):
                    async def ready(plan, worktree, settings, emit, *args, **kwargs):
                        settings.publish = True
                        result = await ready_result(plan, worktree, settings)
                        emit(Event(kind="task_state", text=json.dumps({
                            "index": 1, "title": "task", "state": "ready"})))
                        return result

                    def build(*args):
                        if failure == "build":
                            raise RuntimeError("report unavailable")
                        return build_report(*args)

                    save = store.event

                    def record(key, event):
                        if failure == "store" and event.kind == "work_report":
                            raise RuntimeError("report write failed")
                        save(key, event)

                    with patch("brahma.engine.execute", ready), patch("brahma.report.build_report", build), \
                            patch.object(store, "event", record), \
                            patch.object(session, "publish_ready_in_thread", return_value="fixture-pr"):
                        result = await session.execute(scheduled=True)
                    self.assertEqual(result.status, "ready")
                    self.assertEqual(store.session(session.key)["status"], "ready")
                    self.assertEqual(store.job(session.key)["state"], "done")
                    receipt = json.loads(store.latest_event(session.key, "ready_receipt")["text"])
                    self.assertTrue(receipt["committed"])
                    self.assertIn(receipt["head"], result.text)
                    self.assertIn("Draft PR: fixture-pr", result.text)
                    kinds = [event["kind"] for event in store.events(session.key)]
                    if failure == "none":
                        self.assertEqual(kinds[kinds.index("task_state") + 1], "work_report")
                        self.assertEqual(kinds[-2:], ["summary", "work_report"])
                    else:
                        self.assertNotIn("work_report", kinds)
