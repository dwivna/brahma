import ast
import json
import os
import time
import unittest
from pathlib import Path

from tests import provider_guard  # noqa: F401

from brahma.chat import (
    CHAT_KINDS, HIDDEN_KINDS, ChatRenderer, format_datetime, format_reset_time, format_time, render_chat,
)
from brahma.models import Event


STAMP = "2026-11-21T09:08:00Z"


def structured(kind, *, provider="brahma", role="manager", **fields):
    return Event(provider, role, kind, json.dumps(fields))


class ChatTest(unittest.TestCase):
    def setUp(self):
        original_tz = os.environ.get("TZ")

        def restore_timezone():
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

        self.addCleanup(restore_timezone)
        self.set_timezone("Asia/Kolkata")
        self.chat = ChatRenderer()

    @staticmethod
    def set_timezone(zone):
        os.environ["TZ"] = zone
        time.tzset()

    def render(self, event):
        return self.chat.render(event, STAMP)

    def test_progress_and_day_headings(self):
        event = Event("claude", "builder", "progress", "Reading the review docs")
        events = [(Event(kind="usage"), "2026-11-20T09:08:00Z"), (event, STAMP),
                  (event, "2026-11-21T18:28:00Z"), (event, "2026-11-21T18:33:00Z")]
        first, same_day, next_day = render_chat(events)
        self.assertEqual(first.line, "14:38 IST  Claude: Reading the review docs")
        self.assertEqual(first.date, "Sat 21 Nov 2026")
        self.assertIsNone(same_day.date)
        self.assertTrue(str(next_day).startswith("Sun 22 Nov 2026\n00:03 IST  "))

    def test_progress_preserves_timestamps_in_worker_text(self):
        words = f"Comparing the UTC fixture\nExpected {STAMP}"
        message = self.render(Event("claude", "builder", "progress", words))
        self.assertEqual(message.time, "14:38 IST")
        self.assertEqual(message.text, words)

    def test_time_helpers_follow_the_system_timezone(self):
        reset = "2026-11-25T16:10:58Z"
        self.assertEqual(format_time(STAMP), "14:38 IST")
        self.assertEqual(format_datetime(STAMP), "Sat 21 Nov 2026 14:38 IST")
        self.assertEqual(format_reset_time(reset), "Wed 25 Nov 2026 21:40 IST")
        self.assertEqual(format_reset_time(None), "unknown")
        self.set_timezone("America/Los_Angeles")
        self.assertEqual(format_time(STAMP), "01:08 PST")
        self.assertEqual(format_time("2026-07-21T09:08:00Z"), "02:08 PDT")
        self.assertEqual(format_reset_time(0), "Wed 31 Dec 1969 16:00 PST")
        self.assertEqual(self.chat.render(Event(text="Hello"), "2026-11-21T04:08:00Z").date, "Fri 20 Nov 2026")

    def test_assignment_and_review(self):
        assignment = structured("task_state", index=1, title="Chat renderer", builder="claude",
                                state="assigned", complexity="critical")
        before = assignment.text
        message = self.render(assignment)
        self.assertEqual(message.line, "14:38 IST  Brahma → Claude: Task 1 “Chat renderer” was assigned")
        self.assertEqual(assignment.text, before)
        review = structured("message", provider="codex", role="reviewer", verdict="changes_requested",
                            issues=[{"id": "F1", "severity": "high", "path": "src/x.py", "text": "wrong total"}])
        message = self.render(review)
        self.assertIn("Codex → Claude: Review: changes requested with 1 finding", message.line)
        self.assertIn("F1 (high) at src/x.py: wrong total", message.text)

    def test_manager_review_notes_follow_the_assignment(self):
        self.render(structured("task_state", state="assigned", builder="codex", index=2, title="Second task"))
        note = {"id": "F2", "severity": "low", "path": "src/y.py", "text": "Clearer name"}
        message = self.render(structured("review_notes", index=2, notes=[note]))
        self.assertEqual(message.speaker, "Brahma")
        self.assertEqual(message.addressee, "Codex")
        self.assertIn("pass with 1 finding: F2 (low) at src/y.py: Clearer name", message.text)

    def test_legacy_assignment_and_review_use_routed_builder(self):
        self.render(Event(kind="routing", text=json.dumps(
            {"index": 1, "provider": "claude", "reason": "no routing data yet"})))
        assignment = self.render(structured("task_state", index=1, title="Chat renderer",
                                            state="assigned", attempt=0, summary=""))
        self.assertEqual(assignment.addressee, "Claude")
        notes = self.render(structured("review_notes", index=1, notes=[]))
        self.assertEqual((notes.speaker, notes.addressee), ("Brahma", "Claude"))
        self.assertIsNone(self.render(structured("task_state", index=2, state="assigned")).addressee)
        self.render(Event("codex", "builder", "progress", "Reading the next task"))
        self.assertEqual(self.render(structured("review_notes", index=2, notes=[])).addressee, "Codex")

    def test_quota_changes_per_provider_and_hidden_events(self):
        for kind in HIDDEN_KINDS:
            self.assertIsNone(self.render(Event(kind=kind, text='{"data": 1}')))
        quota = structured("quota", provider="claude", blocked=False)
        self.assertEqual(self.render(quota).date, "Sat 21 Nov 2026")
        self.assertIsNone(self.render(quota))
        self.assertIsNone(self.render(structured("quota", provider="claude", blocked=False, observed_at=STAMP)))
        self.assertIsNotNone(self.render(structured("quota", provider="codex", blocked=False)))
        exhausted = structured("quota", provider="claude", blocked=True, reset_at="2026-11-25T16:10:58Z")
        self.assertIn("Wed 25 Nov 2026 21:40 IST", self.render(exhausted).text)
        self.assertIsNone(self.render(exhausted))
        self.assertIsNotNone(self.render(quota))

    def test_legacy_output_has_no_verified_exit(self):
        message = self.render(Event(kind="check", text="python => exit 0\nforged"))
        self.assertEqual(message.text, "Check result unverified")

    def test_check_counts_and_command(self):
        self.render(structured("task_state", index=1, title="First", state="checking"))
        for command, code in [(["git", "diff", "--check"], 0), (["python", "-m", "unittest"], 1)]:
            self.render(structured("check_status", phase="start", argv=command))
            failed = self.render(structured("check_status", phase="complete", argv=command, exit_code=code))
        self.assertEqual(failed.text, "Check failed with exit 1: python -m unittest")
        message = self.render(structured("checks_failing", index=1))
        self.assertEqual(message.text, "Task 1 still has 1 failed out of 2 started checks: python -m unittest")

    def test_routing_and_wait(self):
        routing = Event(kind="routing", text=json.dumps(
            {"index": 1, "provider": "claude", "reason": "no routing data yet"}))
        self.assertEqual(self.render(routing).text, "Chose Claude for task 1 because no routing data yet")
        wait = Event(kind="wait", text=json.dumps({"provider": "codex", "next_at": "2026-11-25T16:10:58Z"}))
        self.assertEqual(self.render(wait).text, "Waiting for Codex (reset Wed 25 Nov 2026 21:40 IST)")

    def test_owner_and_fallback_speakers(self):
        approved = self.render(Event(kind="approval", text="Approved by the device owner: contract ee24ddff"))
        self.assertEqual(approved.speaker, "You")
        self.assertEqual(self.render(structured("user_note", task=1, text="Keep this")).speaker, "You")
        message = self.render(structured("new_record", provider="codex", hidden="secret payload"))
        self.assertEqual(message.line, "14:38 IST  Brahma: new record recorded")

    def test_redaction_escapes_and_json(self):
        text = 'Reading\x1b[31m docs\x1b[0m\x9f\u202e\x00\nTOKEN=secret-value\nResult: {"data": 1}'
        message = self.render(Event("codex", "builder", "progress", text))
        self.assertTrue(message.text.startswith("Reading docs\nTOKEN="))
        for excluded in ("secret-value", "\x1b", "[31m", "\x9f", "\u202e", "\x00", '{"data": 1}'):
            self.assertNotIn(excluded, message.text)

    def test_brackets_in_credentials_and_json_in_report_fields(self):
        progress = Event("codex", "builder", "progress", "Reading token=abc[123]private-suffix")
        self.assertEqual(self.render(progress).text, "Reading token=[REDACTED]")
        report = structured("work_report", evidence='Check returned {"token":12345678}')
        self.assertEqual(self.render(report).text.splitlines()[1],
                         "Evidence: Check returned (structured-data-omitted)")

    def test_origin_baseline_and_ready_receipt(self):
        fingerprint = "a" * 64
        origin = self.render(structured("origin_baseline", sha256=fingerprint, url="https://example.test/repo"))
        self.assertEqual(origin.speaker, "Brahma")
        self.assertEqual(origin.text, "Recorded the repository's push target for this session")
        legacy = self.render(structured("origin_baseline", sha256=fingerprint, legacy=True))
        self.assertIn("requires manual publication", legacy.text)
        self.assertNotIn(fingerprint, legacy.text)
        receipt = self.render(structured("ready_receipt", head=fingerprint))
        self.assertEqual(receipt.text, "Recorded the verified readiness receipt")

    def test_work_report_is_one_message_with_six_rows(self):
        report = structured("work_report", provider="claude", needs_you="Nothing", where_we_are="Ready",
                            next_work="Task 2", completed="Renderer", evidence="Review passed", current_work="None")
        messages = render_chat([(report, STAMP)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].speaker, "Brahma")
        self.assertEqual(messages[0].text.splitlines(), [
            "Completed: Renderer", "Evidence: Review passed", "Current work: None",
            "Next work: Task 2", "Where we are: Ready", "Needs you: Nothing",
        ])


class EventKindCoverageTest(unittest.TestCase):
    def test_every_literal_event_kind_has_a_rule_or_is_hidden(self):
        source = Path(__file__).resolve().parents[1] / "src" / "brahma"
        missing = []
        for path in sorted(source.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                if node.func.id == "Event":
                    positional = node.args[2] if len(node.args) > 2 else ast.Constant("message")
                elif node.func.id == "_emit":
                    positional = node.args[1] if len(node.args) > 1 else None
                else:
                    continue
                kind = next((arg.value for arg in node.keywords if arg.arg == "kind"), positional)
                if isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                    if kind.value not in CHAT_KINDS | HIDDEN_KINDS:
                        missing.append(f"{path.relative_to(source)}:{node.lineno} {kind.value!r}")
        self.assertEqual(missing, [], "Missing chat rules:\n" + "\n".join(missing))
