"""Fixes from the independent review of 0.3.0-0.10.0 (version 0.11.0)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import board, briefs, dispute, engine, gittools, safety
from brahma.config import Settings
from brahma.models import Event, Plan, Result, Task
from brahma.store import Store
from tests.approval import owner_confirms

SECRET = "sk-SYNTHETICabcdefghij123456"
HIGH = {"id": "F1", "severity": "high", "text": "Header missing"}
LOW = {"id": "F2", "severity": "low", "text": "Naming nit"}


def review(verdict="changes_requested", issues=None, summary="s"):
    return json.dumps({"verdict": verdict, "summary": summary, "issues": [HIGH] if issues is None else issues})


def reply():
    return json.dumps({"replies": [{"id": "F1", "stance": "dispute", "evidence": "t.py:1"}]})


class Redaction(unittest.TestCase):
    def test_task_state_summary_and_feedback_are_redacted_but_stay_parseable(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            key = store.create(Path(d))
            store.event(key, Event("brahma", "manager", "task_state", json.dumps(
                {"index": 1, "title": "T", "state": "blocked", "attempt": 1, "summary": "failed: " + SECRET,
                 "feedback": "Actual checks failed:\ntoken = " + SECRET})))
            row = json.loads(store.events(key)[0]["text"])
            store.close()
            self.assertEqual((row["index"], row["state"]), (1, "blocked"))
            self.assertNotIn("SYNTHETIC", json.dumps(row))

    def test_more_credential_shapes_are_redacted(self):
        for value in ("sk_live_" + "a1B2c3D4e5F6g7H8i9", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop",
                      "AIza" + "x" * 35, "glpat-" + "a" * 24, "npm_" + "b" * 36, "hf_" + "c" * 34):
            with self.subTest(value=value[:8]):
                self.assertNotIn(value, safety.redact("found " + value + " here"))

    def test_publish_text_is_redacted_before_it_is_sent(self):
        from tests.test_review16 import publication_result, verified_branch
        sent = []
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)

            def step(argv, worktree, stopped, **kwargs):
                sent.append((argv, kwargs.get("input_text", "")))
                return publication_result(argv, head)

            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", side_effect=step):
                gittools.publish(repo, "Fix " + SECRET, "body " + SECRET, head, snapshot)
        self.assertEqual(len(sent), 5)          # repository lookup, push, remote ref verification, create/view draft
        self.assertNotIn("SYNTHETIC", json.dumps(sent))


class GitSafety(unittest.TestCase):
    def test_refuse_is_not_fooled_by_global_option_values(self):
        for args in (("-C", "/x", "merge", "main"), ("-c", "a=b", "reset", "--hard"), ("-c", "x=y", "push", "--force", "origin", "brahma/a:brahma/a")):
            with self.subTest(args=args):
                self.assertIsNotNone(gittools.refuse(args))

    def test_kill_switch_refuses_coordinator_git_writes_but_not_reads(self):
        from brahma import halt
        with tempfile.TemporaryDirectory() as d:
            halt.engage(Path(d))
            with patch("brahma.config.state_home", return_value=Path(d)), patch("subprocess.run") as run:
                for args in (("commit", "-m", "x"), ("add", "-A"), ("push", "origin", "brahma/a:brahma/a"), ("worktree", "add", "x")):
                    with self.subTest(args=args), self.assertRaisesRegex(ValueError, "Kill switch"):
                        gittools.git(Path("/w"), *args)
                run.return_value.returncode, run.return_value.stdout = 0, "ok"
                self.assertEqual(gittools.git(Path("/w"), "status", "--short"), "ok")
                self.assertEqual(gittools.git(Path("/w"), "branch", "--show-current"), "ok")


class Flow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events, self.calls = [], []
        for p in (patch.object(engine, "_context", return_value="ctx"),
                  patch.object(gittools, "snapshot_changes", return_value={}, create=True),
                  patch.object(gittools, "ownership_changes", return_value=(True, "ok")), patch.object(gittools, "git_identity", return_value={}),
                  patch.object(gittools, "run_checks", AsyncMock(return_value=(True, "ok"))), patch.object(engine, "_tree", return_value={"head": "h", "changes": {}})):
            p.start()
            self.addCleanup(p.stop)

    async def run_flow(self, script, progress=None, settings=None):
        queue = list(script)

        async def run(provider, prompt, cwd, role, s, emit, cancel, timeout, native_session_id=None, **_extra):
            self.calls.append((role, prompt))
            return queue.pop(0)[1]
        with patch.object(engine.providers, "run", side_effect=run):
            out = await engine.execute(Plan("G", [Task("T", "do", ["src/t.py"], ["ok"], "codex")]), Path("/repo"),
                                       settings or Settings(checks=[["true"]]), self.events.append, asyncio.Event(),
                                       task_progress=progress)
        self.assertEqual(queue, [])
        return out

    def states(self):
        return [json.loads(e.text) for e in self.events if e.kind == "task_state"]

    async def test_check_output_secret_never_reaches_feedback_or_stored_summary(self):
        async def run(provider, prompt, *a, **k):
            self.calls.append(("b", prompt))
            return Result("codex", "ok", "d")
        with patch.object(gittools, "run_checks", AsyncMock(return_value=(False, "AssertionError token=" + SECRET))), \
                patch.object(engine.providers, "run", side_effect=run):
            await engine.execute(Plan("G", [Task("T", "do", ["src/t.py"], ["ok"], "codex")]), Path("/repo"),
                                 Settings(checks=[["true"]]), self.events.append, asyncio.Event())
        self.assertNotIn("SYNTHETIC", json.dumps([s.get("feedback", "") + s.get("summary", "") for s in self.states()]))
        self.assertNotIn("SYNTHETIC", " ".join(p for _, p in self.calls))   # nor into the next builder prompt

    async def test_blocked_rereview_with_a_high_issue_blocks_and_is_never_escalated(self):
        result = await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review())),
                                      ("reply", Result("codex", "ok", reply())),
                                      ("r", Result("claude", "ok", review("blocked", [HIGH], "cannot accept")))])
        self.assertEqual(result.status, "blocked")
        self.assertEqual([e for e in self.events if e.kind == "disagreement"], [])

    async def test_withdrawn_high_finding_still_repairs_the_medium_and_low_ones(self):
        result = await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review(issues=[HIGH, LOW]))),
                                      ("reply", Result("codex", "ok", reply())),
                                      ("r", Result("claude", "ok", review("pass", []))),
                                      ("b", Result("codex", "ok", "fixed the nit")), ("r", Result("claude", "ok", review("pass", [])))])
        self.assertEqual(result.status, "ready")
        self.assertIn("Naming nit", self.calls[4][1])                   # the repair prompt carries the low finding

    async def test_reply_is_persisted_the_moment_it_is_spent(self):
        await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review())),
                             ("reply", Result("codex", "ok", reply())), ("r", Result("claude", "ok", review()))])
        spent = [i for i, s in enumerate(self.states()) if s.get("reply_used")]
        self.assertTrue(spent)
        self.assertEqual(self.states()[spent[0]]["state"], "reviewing")  # a checkpoint right after the reply, before the re-review

    async def test_a_decision_for_a_different_dispute_is_not_applied(self):
        await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review())),
                             ("reply", Result("codex", "ok", reply())), ("r", Result("claude", "ok", review()))])
        saved = self.states()[-1]
        self.assertTrue(saved["dispute"]["digest"])
        self.assertEqual(saved["dispute"]["digest"], hashlib.sha256(saved["dispute"]["text"].encode()).hexdigest()[:32])

    async def test_a_decision_is_cleared_when_the_fresh_review_has_no_high_finding(self):
        await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review())),
                             ("reply", Result("codex", "ok", reply())), ("r", Result("claude", "ok", review()))])
        last = self.states()[-1]
        # The worktree moved after the decision, so the decided review is not reused: a fresh review runs.
        saved = dict(last, dispute_decision="author", phase="review", dispute=dict(last["dispute"], tree={"head": "moved"}))
        self.events.clear()
        await self.run_flow([("r", Result("claude", "ok", review("pass", [])))], progress={1: saved})
        self.assertIsNone(self.states()[-1]["dispute_decision"])

    async def test_note_is_counted_applied_only_after_a_builder_really_received_it(self):
        settings = Settings(checks=[["true"]])
        settings._notes = [briefs.Note(7, None, "keep it small")]
        await self.run_flow([("b", Result("codex", "limited", ""))], settings=settings)
        self.assertEqual(self.states()[-1]["notes_seen"], 0)             # builder never ran: the note is not spent
        self.events.clear()
        await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review("pass", [])))], settings=settings)
        self.assertEqual(self.states()[-1]["notes_seen"], 7)

    async def test_notes_can_be_a_loader_called_at_each_attempt(self):
        calls = []
        settings = Settings(checks=[["true"]])
        settings._notes = lambda: calls.append(1) or [briefs.Note(1, None, "late note")]
        await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review("pass", [])))], settings=settings)
        self.assertTrue(calls)
        self.assertIn("late note", self.calls[0][1])


class PassWithNotes(Flow):
    """Found by live run 5: a pass carrying only low-severity notes was rejected as invalid and burned every repair."""

    async def test_a_pass_with_low_severity_notes_is_ready_and_the_notes_are_recorded_sanitized(self):
        notes = [{"id": "F1", "severity": "low", "text": "minor nit " + SECRET, "path": "src/t.py"},
                 {"id": "F2", "severity": "medium", "text": "consider a follow-up"}]
        result = await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review("pass", notes)))])
        self.assertEqual(result.status, "ready")
        recorded = [json.loads(e.text) for e in self.events if e.kind == "review_notes"]
        self.assertEqual(len(recorded), 1)
        self.assertEqual([n["severity"] for n in recorded[0]["notes"]], ["low", "medium"])
        self.assertNotIn("SYNTHETIC", json.dumps(recorded))

    async def test_a_pass_with_a_high_or_unrated_issue_is_still_invalid_and_repaired(self):
        for issues in ([HIGH], ["bare string"], [{"severity": "low", "text": "x"}, HIGH]):
            with self.subTest(issues=issues):
                self.events.clear()
                result = await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", review("pass", issues))),
                                              ("b", Result("codex", "ok", "fixed")), ("r", Result("claude", "ok", review("pass", [])))])
                self.assertEqual(result.status, "ready")
                self.assertEqual([e for e in self.events if e.kind == "review_notes"], [])


class ToolchainBlock(Flow):
    """A toolchain change after approval is an environment problem, not a test failure: block at once, spend no repair."""

    async def test_a_toolchain_change_blocks_immediately_and_spends_no_repair(self):
        from brahma import toolchain
        problem = "Toolchain changed since approval, so the check was not run: mvn: approved [a], now [b]"
        with patch.object(gittools, "run_checks", AsyncMock(side_effect=toolchain.ToolchainChanged(problem))):
            result = await self.run_flow([("b", Result("codex", "ok", "d"))])
        self.assertEqual(result.status, "blocked")
        self.assertIn("Toolchain changed", result.text)
        self.assertEqual(len(self.calls), 1)                                  # one builder run, no repair attempts
        last = self.states()[-1]
        self.assertEqual((last["state"], last["attempt"]), ("waiting", 0))   # inactive and resumable at the same attempt: nothing is spent


class Session(unittest.TestCase):
    def test_decision_is_bound_to_the_exact_dispute_and_stale_attention_is_resolved_on_a_new_plan(self):
        from tests.test_cli import repo_session, run_cli
        with tempfile.TemporaryDirectory() as d:
            store, session = repo_session(d)
            text = "Persistent disagreement ... positions A"
            store.set(session.key, approved_digest="d" * 64, status="needs_decision")
            store.event(session.key, Event("brahma", "manager", "disagreement", json.dumps({"index": 1, "text": text, "position": {}})))
            store.attention(session.key, "decision", "disputed")
            store.close()
            state = Path(d) / "state"
            with owner_confirms():
                self.assertEqual(run_cli("--json", "decide", session.key, "--for", "author", state=state)[0], 0)
            check = Store(state)
            decided = [json.loads(e["text"]) for e in check.events_after(session.key, 0) if e["kind"] == "dispute_decision"][0]
            self.assertEqual(decided["digest"], hashlib.sha256(text.encode()).hexdigest()[:32])
            check.attention(session.key, "decision", "old")
            from brahma.session import Session as S
            s = S(check, Path(check.session(session.key)["repo"]), key=session.key)
            s.set_plan(Plan("New", [Task("T", "i", ["src/a.py"], ["ok"])], checks=[["true"]]))
            self.assertEqual(check.open_attention(session.key), [])
            check.close()

    def test_notes_from_before_the_current_plan_do_not_apply(self):
        from tests.test_cli import repo_session
        with tempfile.TemporaryDirectory() as d:
            store, session = repo_session(d)
            store.event(session.key, Event("brahma", "user", "user_note", json.dumps({"task": None, "text": "old"})))
            store.event(session.key, Event(kind="plan_created", text="g2"))
            store.event(session.key, Event("brahma", "user", "user_note", json.dumps({"task": None, "text": "new"})))
            self.assertEqual([n.text for n in session._notes()], ["new"])
            store.close()


class BoardStale(unittest.TestCase):
    def test_old_generation_signals_do_not_notify_and_a_decision_attention_alone_hides_approval(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d))
            key = store.create(Path(d))
            store.event(key, Event("brahma", "manager", "signin_lost", json.dumps({"provider": "codex"})))
            store.event(key, Event(kind="plan_created", text="g2"))
            self.assertEqual(board.fresh(store, key), [])
            store.close()


if __name__ == "__main__":
    unittest.main()
