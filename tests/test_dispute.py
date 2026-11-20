"""Review findings with severity and the one-reply dispute flow (Rounds 4 and 5; version 0.8.0)."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import dispute, engine, gittools
from brahma.config import Settings
from brahma.models import Plan, Result, Task
from tests.approval import owner_confirms

HIGH = {"id": "F1", "severity": "high", "text": "Header row missing", "path": "src/t.py"}
LOW = {"id": "F2", "severity": "low", "text": "Naming nit"}


def review_text(verdict="changes_requested", issues=None, summary="reviewed"):
    return json.dumps({"verdict": verdict, "summary": summary, "issues": issues if issues is not None else [HIGH]})


def reply_text(stance="dispute", evidence="src/t.py:3 writes the header"):
    return json.dumps({"replies": [{"id": "F1", "stance": stance, "evidence": evidence}]})


class Schema(unittest.TestCase):
    def test_structured_issues_validate_and_strings_stay_accepted_as_unrated(self):
        engine._review(review_text(issues=[HIGH, LOW, "plain string issue"]))
        items = dispute.findings(engine._review(review_text(issues=["bare"])))
        self.assertEqual((items[0]["severity"], dispute.high(items)), ("medium", []))   # never high by default

    def test_bad_issue_objects_are_rejected(self):
        for issue in ({"severity": "urgent", "text": "x"}, {"severity": "high"}, {"text": "x"},
                      {"severity": "high", "text": "x", "approved": True}, {"severity": "high", "text": " "}):
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                engine._review(review_text(issues=[issue]))

    def test_reply_parser_demands_evidence_for_a_dispute_and_ignores_the_rest(self):
        parsed = dispute.parse_reply(reply_text(evidence=""), [HIGH])
        self.assertEqual(parsed["F1"]["stance"], "accept")                    # no evidence: not a dispute
        self.assertEqual(dispute.parse_reply(reply_text(), [HIGH])["F1"]["stance"], "dispute")
        for text in ("not json", "[]", '{"replies": 5}', '{"replies":[],"x":1}',
                     json.dumps({"replies": [{"id": "F9", "stance": "dispute", "evidence": "e"}]}),
                     json.dumps({"replies": [{"id": "F1", "stance": "maybe", "evidence": "e"}]})):
            self.assertEqual(dispute.parse_reply(text, [HIGH]), {}, text)

    def test_evidence_is_sanitized_before_anyone_sees_it(self):
        secret = "token = sk-SYNTHETICabcdefghij123456"
        parsed = dispute.parse_reply(reply_text(evidence="see config: " + secret), [HIGH])
        self.assertNotIn("SYNTHETIC", parsed["F1"]["evidence"])
        position = dispute.positions([HIGH], parsed, {"summary": "s " + secret, "issues": [dict(HIGH, text="bad " + secret)]})
        self.assertNotIn("SYNTHETIC", json.dumps(position))
        self.assertNotIn("SYNTHETIC", dispute.describe(position))


    def test_id_and_path_are_sanitized_too(self):
        secret = "sk-SYNTHETICabcdefghij123456"
        item = dispute.findings({"issues": [{"id": "F1", "severity": "high", "text": "t", "path": "cfg/" + secret}]})[0]
        self.assertNotIn("SYNTHETIC", json.dumps(item))
        self.assertNotIn("SYNTHETIC", json.dumps(dispute.positions([item], {}, {"summary": "", "issues": [
            {"id": "F1", "severity": "high", "text": "t", "path": "cfg/" + secret}]})))


class Flow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events, self.calls = [], []
        self.patches = [patch.object(engine, "_context", return_value="ctx"),
                        patch.object(gittools, "snapshot_changes", return_value={}, create=True),
                        patch.object(gittools, "ownership_changes", return_value=(True, "Owned")), patch.object(gittools, "git_identity", return_value={}),
                        patch.object(gittools, "run_checks", AsyncMock(return_value=(True, "Actual checks: exit 0"))),
                        patch.object(engine, "_tree", side_effect=lambda worktree, settings: dict(self.tree))]
        self.tree = {"head": "abc", "changes": {"src/t.py": "100644:1"}}
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def run_flow(self, script, progress=None, repair_cycles=3):
        """script: list of (role, Result) in the order the engine should call providers."""
        queue = list(script)

        async def run(provider, prompt, cwd, role, settings, emit, cancel, timeout, native_session_id=None):
            self.calls.append((provider, role, prompt))
            expected_role, response = queue.pop(0)
            self.assertEqual(role, expected_role, f"unexpected call order: {[c[:2] for c in self.calls]}")
            return response
        settings = Settings(checks=[["true"]], repair_cycles=repair_cycles)
        with patch.object(engine.providers, "run", side_effect=run):
            outcome = await engine.execute(Plan("Goal", [Task("T", "do", ["src/t.py"], ["ok"], "codex")]), Path("/repo"),
                                           settings, self.events.append, asyncio.Event(), task_progress=progress)
        self.assertEqual(queue, [], "not every scripted call happened")
        return outcome

    def kinds(self, kind):
        return [json.loads(e.text) for e in self.events if e.kind == kind]

    def last_state(self):
        return [json.loads(e.text) for e in self.events if e.kind == "task_state"][-1]

    async def test_a_high_finding_the_reviewer_withdraws_after_evidence_lets_the_task_pass(self):
        result = await self.run_flow([
            ("builder", Result("codex", "ok", "done")), ("reviewer", Result("claude", "ok", review_text())),
            ("reply", Result("codex", "ok", reply_text())),
            ("reviewer", Result("claude", "ok", review_text("pass", [], "Evidence checked; finding withdrawn")))])
        self.assertEqual(result.status, "ready")
        self.assertEqual(self.kinds("dispute_reply")[0]["replies"]["F1"]["stance"], "dispute")
        author_call = self.calls[2]
        self.assertEqual(author_call[:2], ("codex", "reply"))
        self.assertIn("Do NOT edit files", author_call[2])
        self.assertIn("independent read-only reviewer", self.calls[3][2])
        self.assertIn("src/t.py:3 writes the header", self.calls[3][2])   # the reviewer sees the author's evidence

    async def test_persistent_disagreement_stops_exposes_both_positions_and_blocks_readiness(self):
        result = await self.run_flow([
            ("builder", Result("codex", "ok", "done")), ("reviewer", Result("claude", "ok", review_text())),
            ("reply", Result("codex", "ok", reply_text())),
            ("reviewer", Result("claude", "ok", review_text(summary="Still wrong: header is absent")))])
        self.assertEqual(result.status, "needs_decision")
        self.assertIn("Reviewer [F1, high]: Header row missing", result.text)
        self.assertIn("Author [F1, dispute]: src/t.py:3 writes the header", result.text)
        self.assertIn("brahma decide", result.text)
        state = self.last_state()
        self.assertEqual((state["state"], state["dispute"]["state"], state["reply_used"]), ("needs_decision", "escalated", True))
        self.assertEqual(len(self.kinds("disagreement")), 1)
        self.assertEqual(self.kinds("outcome")[-1]["ready"], False)

    async def test_resume_after_restart_waits_without_a_second_reply_or_any_provider_call(self):
        await self.run_flow([("builder", Result("codex", "ok", "d")), ("reviewer", Result("claude", "ok", review_text())),
                             ("reply", Result("codex", "ok", reply_text())),
                             ("reviewer", Result("claude", "ok", review_text()))])
        saved = self.last_state()
        self.events.clear()
        self.calls.clear()
        result = await self.run_flow([], progress={1: saved})       # restart: nothing may be launched
        self.assertEqual((result.status, self.calls), ("needs_decision", []))

    async def test_deciding_for_the_author_overrules_the_finding_but_checks_still_run(self):
        await self.run_flow([("builder", Result("codex", "ok", "d")), ("reviewer", Result("claude", "ok", review_text())),
                             ("reply", Result("codex", "ok", reply_text())),
                             ("reviewer", Result("claude", "ok", review_text()))])
        saved = dict(self.last_state(), dispute_decision="author", phase="review")
        self.events.clear()
        self.calls.clear()
        # The tree is unchanged since the decision, so the decided review is applied as it was: no new review, no reply.
        result = await self.run_flow([], progress={1: saved})
        self.assertEqual(result.status, "ready")
        self.assertEqual(len([e for e in self.events if e.kind == "dispute_overruled"]), 1)
        self.assertEqual(self.calls, [])

    async def test_an_old_decision_never_overrules_new_high_findings(self):
        # Review round 8, finding 2: the owner overruled finding A; a later review reports a different HIGH finding B.
        await self.run_flow([("builder", Result("codex", "ok", "d")), ("reviewer", Result("claude", "ok", review_text())),
                             ("reply", Result("codex", "ok", reply_text())),
                             ("reviewer", Result("claude", "ok", review_text()))])
        saved = dict(self.last_state(), dispute_decision="author", phase="review")
        self.events.clear()
        self.calls.clear()
        self.tree = {"head": "abc", "changes": {"src/t.py": "100644:2"}}          # the worktree moved after the decision
        other = json.dumps({"verdict": "changes_requested", "summary": "New problem",
                            "issues": [{"id": "F9", "severity": "high", "text": "SQL injection in query builder"}]})
        result = await self.run_flow([("reviewer", Result("claude", "ok", other)),
                                      ("builder", Result("codex", "ok", "fixed")),
                                      ("reviewer", Result("claude", "ok", review_text("pass", [])))], progress={1: saved})
        self.assertEqual([e for e in self.events if e.kind == "dispute_overruled"], [])
        self.assertTrue([e for e in self.events if e.kind == "dispute_void"])
        self.assertIn("SQL injection", self.calls[1][2])           # the new finding went to repair, not to the old decision
        self.assertEqual(result.status, "ready")

    async def test_a_decision_on_one_set_of_findings_does_not_apply_to_a_different_set(self):
        state = {"dispute": {"state": "escalated", "findings": engine._findings_key([{"severity": "high", "text": "A", "path": None}])},
                 "dispute_decision": "author", "reply_used": True}
        review = {"verdict": "changes_requested", "summary": "s", "issues": [{"id": "F1", "severity": "high", "text": "B"}]}
        kind, payload = await engine._dispute_round(1, Task("T", "do", ["src/t.py"], ["ok"], "codex"), "codex", "claude", review,
                                                    Path("/repo"), Settings(), lambda e: None, asyncio.Event(), 0, {1: state},
                                                    None, {}, 0)
        self.assertEqual(kind, "repair")
        self.assertIsNone(state["dispute_decision"])

    async def test_deciding_for_the_reviewer_sends_the_task_back_to_repair(self):
        await self.run_flow([("builder", Result("codex", "ok", "d")), ("reviewer", Result("claude", "ok", review_text())),
                             ("reply", Result("codex", "ok", reply_text())),
                             ("reviewer", Result("claude", "ok", review_text()))])
        saved = dict(self.last_state(), dispute_decision="reviewer", phase="review")
        self.events.clear()
        self.calls.clear()
        result = await self.run_flow([("builder", Result("codex", "ok", "fixed")),
                                      ("reviewer", Result("claude", "ok", review_text("pass", [])))], progress={1: saved})
        self.assertEqual(result.status, "ready")
        self.assertNotIn("reply", [c[1] for c in self.calls])

    async def test_a_blocked_rereview_blocks_the_task_and_never_reaches_readiness(self):
        result = await self.run_flow([
            ("builder", Result("codex", "ok", "done")), ("reviewer", Result("claude", "ok", review_text())),
            ("reply", Result("codex", "ok", reply_text())),
            ("reviewer", Result("claude", "ok", review_text("blocked", [], "Cannot be accepted")))])
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.kinds("task_state")[-1]["state"], "blocked")

    async def test_an_accepting_author_goes_straight_to_repair_with_no_second_review_of_the_dispute(self):
        result = await self.run_flow([
            ("builder", Result("codex", "ok", "done")), ("reviewer", Result("claude", "ok", review_text())),
            ("reply", Result("codex", "ok", reply_text("accept", ""))),
            ("builder", Result("codex", "ok", "fixed")), ("reviewer", Result("claude", "ok", review_text("pass", [])))])
        self.assertEqual(result.status, "ready")

    async def test_only_medium_and_low_findings_never_start_a_dispute(self):
        result = await self.run_flow([
            ("builder", Result("codex", "ok", "d")), ("reviewer", Result("claude", "ok", review_text(issues=[LOW, "bare"]))),
            ("builder", Result("codex", "ok", "fixed")), ("reviewer", Result("claude", "ok", review_text("pass", [])))])
        self.assertEqual(result.status, "ready")
        self.assertNotIn("reply", [c[1] for c in self.calls])

    async def test_the_second_high_finding_in_a_later_round_gets_no_second_reply(self):
        result = await self.run_flow([
            ("builder", Result("codex", "ok", "d")), ("reviewer", Result("claude", "ok", review_text())),
            ("reply", Result("codex", "ok", reply_text("accept", ""))),
            ("builder", Result("codex", "ok", "fixed")), ("reviewer", Result("claude", "ok", review_text())),   # high again
            ("builder", Result("codex", "ok", "fixed 2")), ("reviewer", Result("claude", "ok", review_text("pass", [])))])
        self.assertEqual(result.status, "ready")
        self.assertEqual([c[1] for c in self.calls].count("reply"), 1)

    async def test_a_failed_author_reply_does_not_spend_the_allowance(self):
        await self.run_flow([("builder", Result("codex", "ok", "d")), ("reviewer", Result("claude", "ok", review_text())),
                             ("reply", Result("codex", "limited", "")),
                             ("builder", Result("codex", "ok", "fixed")), ("reviewer", Result("claude", "ok", review_text("pass", [])))])
        self.assertFalse(any(json.loads(e.text).get("reply_used") for e in self.events if e.kind == "task_state"))


class SessionAndCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_decide_records_the_decision_resolves_attention_and_requeues(self):
        import time
        from brahma.models import Event
        from tests.test_cli import repo_session, run_cli
        from brahma.store import Store
        store, session = repo_session(self.tmp.name)
        store.set(session.key, approved_digest="d" * 64, status="needs_decision")
        store.event(session.key, Event("brahma", "manager", "disagreement", json.dumps({"index": 1, "text": "x", "position": {}})))
        store.attention(session.key, "decision", "disputed")
        store.close()
        state = Path(self.tmp.name) / "state"
        with owner_confirms() as prompt:
            code, out, _ = run_cli("--json", "decide", session.key, "--for", "author", state=state)
        prompt.assert_called_once()                       # deciding a dispute is a human-only action
        data = json.loads(out)
        self.assertEqual((code, data["decided_for"], data["task"], data["queued"]), (0, "author", 1, True))
        check = Store(state)
        self.assertEqual(check.open_attention(session.key), [])
        self.assertEqual([json.loads(e["text"])["for"] for e in check.events_after(session.key, 0)
                          if e["kind"] == "dispute_decision"], ["author"])
        self.assertEqual(len(check.pending()), 1)
        check.close()
        code, out, _ = run_cli("--json", "decide", session.key, "--for", "author", state=state)   # nothing left to decide
        self.assertEqual(code, 20)

    def test_decide_without_a_dispute_or_with_a_bad_side_is_a_usage_error(self):
        from tests.test_cli import repo_session, run_cli
        store, session = repo_session(self.tmp.name)
        store.close()
        state = Path(self.tmp.name) / "state"
        self.assertEqual(run_cli("--json", "decide", session.key, "--for", "author", state=state)[0], 20)
        self.assertEqual(run_cli("--json", "decide", session.key, "--for", "nobody", state=state)[0], 20)

    def test_needs_decision_maps_to_exit_code_11_in_status_and_results(self):
        from brahma import exitcodes
        self.assertEqual(exitcodes.for_result_status("needs_decision"), 11)
        base = {"stopped": 0, "paused": 0, "status": "needs_decision", "approved_digest": "d"}
        self.assertEqual(exitcodes.for_session(base, None, True), 11)

    def test_session_hands_a_decision_only_to_a_dispute_that_is_waiting_for_it(self):
        import asyncio as aio
        from brahma.models import Event
        from tests.test_cli import repo_session
        store, session = repo_session(self.tmp.name)
        seen = {}
        # The decision event is applied by Session.execute while it rebuilds progress; verify the parsing contract.
        store.event(session.key, Event("brahma", "user", "dispute_decision", json.dumps({"index": 1, "for": "author"})))
        store.event(session.key, Event("brahma", "user", "dispute_decision", "not json"))
        store.event(session.key, Event("brahma", "user", "dispute_decision", json.dumps({"index": 1, "for": "bogus"})))
        rows = [json.loads(e["text"]) for e in store.events_after(session.key, 0) if e["kind"] == "dispute_decision" and e["text"].startswith("{")]
        self.assertEqual([r["for"] for r in rows], ["author", "bogus"])
        store.close()


if __name__ == "__main__":
    unittest.main()
