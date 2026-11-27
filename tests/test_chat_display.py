"""1.1.0: the chat everywhere a person reads the conversation (CLI log, terminal UI) and local times (usage, board).

Deterministic via TZ and time.tzset(); no test launches a provider CLI (the provider guard replaces them on PATH and
`brahma.providers.doctor` is patched wherever the terminal UI would check sign-in)."""
from __future__ import annotations

import io
import json
import os
import re
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests import provider_guard  # noqa: F401

from brahma import board, cli
from brahma.models import Event, Result
from brahma.usage import format_usage
from tests.test_cli import repo_session, run_cli

RESET = datetime(2026, 11, 25, 16, 10, 58, tzinfo=timezone.utc)
SIX = ("completed", "evidence", "current_work", "next_work", "where_we_are", "needs_you")
STATUS_FIELDS = {"ok", "session", "status", "outcome", "exit_code", "approved", "contract_digest", "paused", "stopped",
                 "killed", "job", "repo", "worktree"}
LINE = re.compile(r"^(?:\w{3} \d{2} \w{3} \d{4} )?\d{2}:\d{2} IST  (You|Brahma|Codex|Claude)(?: → (Codex|Claude))?: \S")


def conversation() -> list[Event]:
    """A task assignment, worker progress, a review, usage counters and a work report, as `brahma run` would log them."""
    return [
        Event(kind="task_state", text=json.dumps({"index": 1, "title": "Chat renderer", "builder": "claude",
                                                  "state": "assigned", "complexity": "critical"})),
        Event("claude", "builder", "progress", "Reading the review docs"),
        Event("claude", "builder", "usage", usage={"input_tokens": 5, "output_tokens": 1}),
        Event("codex", "reviewer", "message", json.dumps({
            "verdict": "changes_requested", "summary": "One fix",
            "issues": [{"id": "F1", "severity": "high", "path": "src/x.py", "text": "wrong total"}]})),
        Event(kind="work_report", text=json.dumps({key: f"{key} value" for key in SIX})),
    ]


class LocalTime(unittest.TestCase):
    def setUp(self):
        original = os.environ.get("TZ")

        def restore():
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            time.tzset()
        self.addCleanup(restore)
        self.zone("Asia/Kolkata")

    @staticmethod
    def zone(name):
        os.environ["TZ"] = name
        time.tzset()


class TerminalStream(io.StringIO):
    def isatty(self):
        return True


class CliLog(LocalTime):
    def log(self, stream):
        clock = iter([datetime(2026, 11, 21, 9, 8 + n, tzinfo=timezone.utc) for n in range(20)])
        log = cli.ChatLog(stream, clock=lambda: next(clock))
        for event in conversation():
            log(event)
        return stream.getvalue()

    def test_every_line_has_local_time_speaker_and_readable_text_without_json(self):
        output = self.log(io.StringIO())
        lines = output.splitlines()
        self.assertEqual(lines[0], "Sat 21 Nov 2026 14:38 IST  Brahma → Claude: Task 1 “Chat renderer” was assigned")
        self.assertEqual(lines[1], "14:39 IST  Claude: Reading the review docs")
        self.assertIn("Codex → Claude: Review: changes requested with 1 finding: F1 (high) at src/x.py: wrong total",
                      lines[2])
        self.assertEqual([line.split(": ", 1)[1] for line in lines[3:]],
                         [f"{label}: {key} value" for key, label in cli.REPORT_FIELDS])     # six rows, in order
        for line in lines:
            self.assertRegex(line, LINE)
        for raw in ("{", '"verdict"', '"index"', "[claude/", "[brahma/", "input_tokens", "\x1b"):
            self.assertNotIn(raw, output)

    def test_colour_only_on_a_terminal_and_never_with_no_color(self):
        environment = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        with patch.dict(os.environ, environment, clear=True):
            self.assertIn("\x1b[", self.log(TerminalStream()))
            self.assertNotIn("\x1b", self.log(io.StringIO()))                # not a terminal
        for value in ("1", ""):
            with self.subTest(NO_COLOR=value), patch.dict(os.environ, NO_COLOR=value):
                self.assertNotIn("\x1b", self.log(TerminalStream()))

    def test_brahma_run_prints_chat_lines_to_stderr(self):
        events = conversation()

        class Approved:
            def __init__(self, store, repo, emit, key=None):
                self.emit, self.key = emit, key

            async def execute(self):
                for event in events:
                    self.emit(event)
                return Result("brahma", "ready", "Run ready")

        with tempfile.TemporaryDirectory() as tmp:
            store, session = repo_session(tmp)
            store.close()
            with patch.object(cli, "Session", Approved):
                code, out, err = run_cli("run", "--session", session.key, state=Path(tmp) / "state")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["status"], "ready")          # stdout keeps its result object
        lines = err.splitlines()
        self.assertEqual(len(lines), 3 + 6)
        for line in lines:
            self.assertRegex(line, LINE)
        self.assertIn("Claude: Reading the review docs", err)
        for raw in ("{", "\x1b", "[claude/builder]"):
            self.assertNotIn(raw, err)


class TerminalTimeline(LocalTime, unittest.IsolatedAsyncioTestCase):
    async def test_one_timeline_in_time_order_with_every_speaker_named(self):
        from textual.widgets import RichLog
        from brahma.store import Store
        from brahma.ui import BrahmaApp, SPEAKER_COLOURS
        self.assertEqual(set(SPEAKER_COLOURS), {"You", "Brahma", "Codex", "Claude"})
        rows = [
            ("2026-11-21T09:00:00+00:00", "brahma", "user", "user", "Add the chat view"),
            ("2026-11-21T09:01:00+00:00", "brahma", "manager", "task_state", json.dumps(
                {"index": 1, "title": "Chat renderer", "builder": "claude", "state": "assigned"})),
            ("2026-11-21T09:08:00+00:00", "claude", "builder", "progress", "Reading the review docs TOKEN=secret-value"),
            ("2026-11-21T09:20:00+00:00", "codex", "reviewer", "message", json.dumps({
                "verdict": "changes_requested",
                "issues": [{"id": "F1", "severity": "high", "path": "src/x.py", "text": "wrong total"}]})),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store, session = repo_session(tmp)
            with store.db:
                store.db.executemany("INSERT INTO events(session,created,provider,role,kind,text,usage) "
                                     "VALUES (?,?,?,?,?,?,'{}')", [(session.key, *row) for row in rows])
            store.close()
            ready = {"installed": True, "signed_in": True, "subscription": True, "available": True,
                     "state": "ready", "action": "", "error": None}
            with patch("brahma.providers.doctor", AsyncMock(return_value=ready)), \
                    patch("brahma.providers.run", side_effect=AssertionError("the timeline called a model")):
                app = BrahmaApp(home=Path(tmp) / "state", resume=session.key)
                try:
                    async with app.run_test(size=(160, 50)) as pilot:
                        await pilot.pause()
                        self.assertEqual(len(app.query(RichLog)), 1)
                        shown = "\n".join(strip.text for strip in app.query_one("#chat", RichLog).lines)
                finally:
                    app.store.close()
        flat = "".join(shown.split())
        expected = ["14:30 IST  You: Add the chat view",
                    "14:31 IST  Brahma → Claude: Task 1 “Chat renderer” was assigned",
                    "14:38 IST  Claude: Reading the review docs",
                    "14:50 IST  Codex → Claude: Review: changes requested with 1 finding: F1 (high) at src/x.py: wrong total",
                    "Brahma: Selected"]
        positions = [flat.index("".join(line.split())) for line in expected]
        self.assertEqual(positions, sorted(positions))                     # one timeline, in time order
        self.assertIn("── Sat 21 Nov 2026 ──", shown)
        self.assertNotIn("secret-value", shown)
        self.assertNotIn('"verdict"', shown)


class LocalResetTimes(LocalTime):
    def report(self):
        epoch = RESET.timestamp()
        return {"observed_at": epoch - 30, "cancelled": False, "providers": [{
            "provider": "codex", "readiness": {"installed": True, "signed_in": True, "subscription": True, "action": ""},
            "quota": {"provider": "codex", "source": "codex.app_server", "observed_at": epoch - 90, "blocked": None,
                      "reset_at": None, "error": None,
                      "windows": [{"name": "window_1", "duration_seconds": 18000, "used_percent": 20.0,
                                   "reset_at": epoch, "blocked": None}]},
            "origin": "cached", "refresh_error": None, "cancelled": False, "freshness": "stale", "age_seconds": 60}]}

    def test_usage_report_shows_local_reset_and_observation_times(self):
        output = format_usage(self.report())
        self.assertIn("reset Wed 25 Nov 2026 21:40 IST", output)
        self.assertIn("checked Wed 25 Nov 2026 21:40 IST", output)
        self.assertIn("(Wed 25 Nov 2026 21:39 IST)", output)
        self.assertNotIn("UTC", output)
        self.zone("America/Los_Angeles")
        self.assertIn("reset Wed 25 Nov 2026 08:10 PST", format_usage(self.report()))

    def test_board_shows_local_reset_times(self):
        self.assertEqual(board._when(RESET.timestamp()), "resets Wed 25 Nov 2026 21:40 IST")
        self.assertEqual(board._when(None), "reset unknown")
        self.assertEqual(board._when(True), "reset unknown")

        class Quotas:
            @staticmethod
            def quota(name):
                return {"provider": name}
        waiting = {"state": "waiting", "next_at": RESET.timestamp()}
        with patch("brahma.quota.eligibility", return_value=waiting):
            capacity = board.capacity(Quotas, [])
        self.assertEqual(capacity["codex"], "waiting, resets Wed 25 Nov 2026 21:40 IST")
        self.assertNotIn("UTC", "".join(board.Board("k", "waiting", False, False, capacity=capacity,
                                                    usage={"measured": "unknown", "estimated": "none"}).lines()))


class StatusReport(unittest.TestCase):
    def test_json_status_adds_only_the_latest_report_and_events_keep_their_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            store, session = repo_session(tmp)
            store.close()
            code, out, _ = run_cli("--json", "status", session.key, state=state)
            before = json.loads(out)
            self.assertEqual(set(before), STATUS_FIELDS | {"report"})
            self.assertIsNone(before["report"])
            from brahma.store import Store
            store = Store(state)
            older = {key: "older" for key in SIX}
            latest = {key: f"latest {key}" for key in SIX}
            for value in (older, latest):
                store.event(session.key, Event(kind="work_report", text=json.dumps(value)))
            store.close()
            code_after, out, _ = run_cli("--json", "status", session.key, state=state)
            after = json.loads(out)
            self.assertEqual(code_after, code)
            self.assertEqual(after["report"], latest)
            self.assertEqual({key: value for key, value in after.items() if key != "report"},
                             {key: value for key, value in before.items() if key != "report"})
            _, human, _ = run_cli("status", session.key, state=state)
            self.assertIn("Latest work report:", human)
            for _, label in cli.REPORT_FIELDS:
                self.assertIn(f"{label}: latest", human)
            _, lines, _ = run_cli("events", session.key, state=state)
            rows = [json.loads(line) for line in lines.splitlines()]
            self.assertEqual([row["kind"] for row in rows], ["work_report", "work_report"])
            for row in rows:
                self.assertEqual(set(row), {"seq", "created", "provider", "role", "kind", "text", "usage"})
                self.assertTrue(row["created"].endswith("+00:00"))       # stored times stay UTC
            self.assertEqual(json.loads(rows[-1]["text"]), latest)


if __name__ == "__main__":
    unittest.main()
