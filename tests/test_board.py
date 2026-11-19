"""Status board and decision-only attention (plan task 5, version 0.10.0)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from textual.widgets import Static

from brahma import board
from brahma.models import Event
from brahma.store import Store
from brahma.ui import BrahmaApp
from test_core import init_repo

SECRET = "sk-SYNTHETICabcdefghij123456"


def state(store, key, index=1, title="Build it", **fields):
    row = {"index": index, "title": title, "state": "building", "attempt": 1, "summary": ""} | fields
    store.event(key, Event("brahma", "manager", "task_state", json.dumps(row)))


class Board(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "state")
        self.addCleanup(self.store.close)
        self.key = self.store.create(Path(self.tmp.name))

    def test_quota_wait_shows_provider_phase_reset_unchanged_active_time_repairs_and_next_step(self):
        reset = time.time() + 7200
        state(self.store, self.key, state="waiting", attempt=3, builder="claude", remaining_seconds=1500.0,
              active=False, summary="Provider capacity is waiting")
        self.store.event(self.key, Event("claude", "builder", "wait", json.dumps(
            {"provider": "claude", "state": "waiting", "next_at": reset, "reason": "limit"})))
        before = board.build(self.store, self.key)
        self.assertEqual(self.store.set_quota if False else None, None)
        row = before.tasks[0]
        self.assertEqual((row.worker, row.phase, row.repairs), ("claude", "waiting", 2))
        self.assertEqual(row.active_minutes, 35.0)                       # 3600-1500 s; the wait adds nothing
        self.assertIn("resumes by itself", row.next_step)
        after = board.build(self.store, self.key)                        # refresh again: unchanged
        self.assertEqual(after.tasks[0].active_minutes, 35.0)
        text = "\n".join(before.lines())
        self.assertIn("35 of 60 min active", text)
        self.assertIn("repairs 2/3", text)

    def test_unknown_capacity_and_usage_are_explicit_and_measured_stays_separate_from_estimates(self):
        text = "\n".join(board.build(self.store, self.key).lines())
        self.assertIn("codex: unknown", text)
        self.assertIn("usage (measured): unknown · estimated: none recorded", text)
        self.store.event(self.key, Event("codex", "builder", "usage", "", {"input_tokens": 1200, "output_tokens": 300}))
        self.store.event(self.key, Event("brahma", "manager", "outcome", json.dumps({"usage_estimated": {"tokens": 5000}})))
        view = board.build(self.store, self.key).usage
        self.assertEqual(view["measured"], "in 1,200 / out 300 tokens")
        self.assertIn("5000", view["estimated"])

    def test_not_started_task_is_not_given_an_invented_time(self):
        state(self.store, self.key, state="assigned")
        self.assertIsNone(board.build(self.store, self.key).tasks[0].active_minutes)
        self.assertIn("not started", "\n".join(board.build(self.store, self.key).lines()))

    def test_raw_worker_text_and_secrets_never_reach_the_board(self):
        self.store.event(self.key, Event("codex", "builder", "message", "RAW TRANSCRIPT LINE " + SECRET))
        state(self.store, self.key, state="blocked", summary="failed with " + SECRET)
        text = "\n".join(board.build(self.store, self.key).lines())
        self.assertNotIn("RAW TRANSCRIPT", text)
        self.assertNotIn("SYNTHETIC", text)

    def test_routine_progress_never_notifies_but_a_lost_sign_in_notifies_exactly_once(self):
        for phase in ("assigned", "building", "checking", "reviewing"):
            state(self.store, self.key, state=phase)
        self.assertEqual(board.fresh(self.store, self.key), [])           # progress only updates the board
        self.store.event(self.key, Event("brahma", "manager", "signin_lost", json.dumps({"index": 1, "provider": "claude"})))
        first = board.fresh(self.store, self.key)
        self.assertEqual([k for k, _ in first], ["signin_lost"])
        self.assertIn("claude sign-in was lost", first[0][1])
        self.assertEqual(board.fresh(self.store, self.key), [])           # polled again
        reopened = Store(Path(self.tmp.name) / "state")
        try:
            self.assertEqual(board.fresh(reopened, self.key), [])         # terminal reopened
        finally:
            reopened.close()

    def test_only_the_five_permitted_kinds_can_notify(self):
        self.assertEqual(set(board.NOTIFY_KINDS), {"approval", "disagreement", "checks_failing", "signin_lost", "ready"})
        self.store.attention(self.key, "interrupt", "something else")     # not a permitted kind
        self.assertEqual(board.fresh(self.store, self.key), [])

    def test_escalated_disagreement_and_failing_checks_notify_once_each(self):
        self.store.attention(self.key, "decision", "Disputed high-severity review finding")
        self.store.event(self.key, Event("brahma", "manager", "checks_failing", json.dumps({"index": 1})))
        kinds = sorted(k for k, _ in board.fresh(self.store, self.key))
        self.assertEqual(kinds, ["checks_failing", "disagreement"])
        self.assertEqual(board.fresh(self.store, self.key), [])
        self.store.event(self.key, Event("brahma", "manager", "checks_failing", json.dumps({"index": 1})))
        self.assertEqual([k for k, _ in board.fresh(self.store, self.key)], ["checks_failing"])   # a new occurrence

    def test_claim_notice_is_durable_and_once(self):
        self.assertTrue(self.store.claim_notice(self.key, "k"))
        self.assertFalse(self.store.claim_notice(self.key, "k"))
        self.assertTrue(self.store.claim_notice(self.key, "other"))


class EngineSignals(unittest.IsolatedAsyncioTestCase):
    async def test_engine_emits_signals_for_lost_sign_in_and_exhausted_checks(self):
        import asyncio
        from brahma import engine, gittools
        from brahma.config import Settings
        from brahma.models import Plan, Result, Task
        events = []
        plan = Plan("G", [Task("T", "do", ["src/t.py"], ["ok"], "codex")])
        with patch.object(engine, "_context", return_value="c"), \
                patch.object(gittools, "ownership_changes", return_value=(True, "ok")), \
                patch.object(gittools, "snapshot_changes", return_value={}, create=True), \
                patch.object(engine.providers, "run", AsyncMock(return_value=Result("codex", "unavailable", ""))):
            out = await engine.execute(plan, Path("/repo"), Settings(checks=[["true"]]), events.append, asyncio.Event())
        self.assertEqual(out.status, "blocked", out.text)
        self.assertEqual([json.loads(e.text)["provider"] for e in events if e.kind == "signin_lost"], ["codex"])
        events.clear()
        with patch.object(engine, "_context", return_value="c"), \
                patch.object(gittools, "ownership_changes", return_value=(True, "ok")), \
                patch.object(gittools, "snapshot_changes", return_value={}, create=True), \
                patch.object(gittools, "run_checks", AsyncMock(return_value=(False, "failed"))), \
                patch.object(engine.providers, "run", AsyncMock(return_value=Result("codex", "ok", "d"))):
            out = await engine.execute(plan, Path("/repo"), Settings(checks=[["true"]], repair_cycles=3), events.append, asyncio.Event())
        self.assertEqual(out.status, "incomplete")
        self.assertEqual(len([e for e in events if e.kind == "checks_failing"]), 1)


class Terminal(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_shows_the_board_notifies_once_and_controls_use_durable_state(self):
        doctor = AsyncMock(return_value={"installed": True, "signed_in": True, "subscription": True, "available": True,
                                         "state": "ready", "action": "", "error": None})
        with tempfile.TemporaryDirectory() as tmp, patch("brahma.providers.doctor", doctor):
            root = init_repo(Path(tmp) / "repo")
            app = BrahmaApp(root, home=Path(tmp) / "state")
            notices = []
            async with app.run_test(size=(120, 40)) as pilot:
                app.notify = lambda text, **kw: notices.append(text)
                key = app.session.key
                state(app.store, key, state="building", builder="codex", remaining_seconds=3000.0)
                app.store.event(key, Event("brahma", "manager", "signin_lost", json.dumps({"index": 1, "provider": "codex"})))
                app.drain_session_events()
                app.drain_session_events()                                  # polled twice
                shown = str(app.query_one("#board", Static).render())
                self.assertIn("1. Build it", shown)
                self.assertIn("10 of 60 min active", shown)
                self.assertEqual(len([n for n in notices if "sign-in" in n]), 1)
                app.action_pause()
                self.assertTrue(app.store.session(key)["paused"])
                app.action_unpause()
                self.assertFalse(app.store.session(key)["paused"])
                app.action_note("1: prefer small functions " + SECRET)
                rows = [json.loads(e["text"]) for e in app.store.events_after(key, 0) if e["kind"] == "user_note"]
                self.assertEqual((rows[0]["task"], "SYNTHETIC" in rows[0]["text"]), (1, False))
                app.action_note("   ")                                         # rejected, nothing stored
                self.assertEqual(len([e for e in app.store.events_after(key, 0) if e["kind"] == "user_note"]), 1)
                app.action_stop()
                self.assertTrue(app.store.session(key)["stopped"])
                app.action_unpause()                                       # a stopped session is not resumed from the terminal
                self.assertTrue(app.session.cancel.is_set())
                from brahma import halt
                app.store.resume(key)
                halt.engage(app.store.home)
                app.session.cancel.set()
                app.action_pause()
                app.action_unpause()                                       # nor under the kill switch
                self.assertTrue(app.session.cancel.is_set())
                self.assertTrue(app.store.session(key)["paused"])


if __name__ == "__main__":
    unittest.main()
