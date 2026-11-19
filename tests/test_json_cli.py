"""JSON command-line interface and exit codes (Rounds 9 and 11, version 0.4.0)."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from brahma import exitcodes
from brahma.models import Event

from tests.test_cli import repo_session, run_cli


def stored(tmp, **fields):
    store, session = repo_session(tmp)
    if fields:
        store.set(session.key, **fields)
    return store, session


class ExitCodeTable(unittest.TestCase):
    def test_table_is_the_documented_one(self):
        self.assertEqual({"READY": 0, "WAITING": 10, "NEEDS_APPROVAL": 11, "NEEDS_CHECKS": 12, "BLOCKED": 13,
                          "CANCELLED": 14, "USAGE": 20, "SIGN_IN": 30},
                         {name: getattr(exitcodes, name) for name in
                          ("READY", "WAITING", "NEEDS_APPROVAL", "NEEDS_CHECKS", "BLOCKED", "CANCELLED", "USAGE", "SIGN_IN")})
        self.assertEqual(len(set(exitcodes.TABLE)), len(exitcodes.TABLE))

    def test_result_status_mapping(self):
        for status, code in (("ready", 0), ("waiting", 10), ("cancelled", 14), ("interrupted", 14), ("blocked", 13),
                             ("incomplete", 13), ("failed", 13), ("overloaded", 13)):
            self.assertEqual(exitcodes.for_result_status(status), code, status)

    def test_readme_documents_every_code(self):
        text = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        for code in exitcodes.TABLE:
            self.assertRegex(text, rf"\|\s*`{code}`\s*\|", f"exit code {code} missing from README table")


class Status(unittest.TestCase):
    def status(self, tmp, key):
        code, out, _ = run_cli("--json", "status", key, state=Path(tmp) / "state")
        return code, json.loads(out)

    def test_awaiting_approval_is_11_and_never_reports_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session = stored(tmp, status="awaiting_approval")
            store.close()
            code, data = self.status(tmp, session.key)
        self.assertEqual((code, data["exit_code"], data["ok"], data["approved"]), (11, 11, False, False))
        self.assertEqual(data["outcome"], "needs a human decision")

    def test_ready_paused_stopped_and_waiting_map_to_their_codes(self):
        cases = (({"status": "ready"}, 0), ({"status": "waiting", "approved_digest": "d"}, 10),
                 ({"status": "blocked", "approved_digest": "d"}, 13), ({"paused": 1}, 14), ({"stopped": 1}, 14))
        for fields, expected in cases:
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                store, session = stored(tmp, **fields)
                store.close()
                code, data = self.status(tmp, session.key)
                self.assertEqual(code, expected)
                self.assertEqual(data["exit_code"], expected)

    def test_status_reflects_the_kill_switch_and_queued_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session = stored(tmp, status="interrupted", approved_digest="d")
            store.enqueue(session.key, session.generation, "d" * 64, time.time(), "due")
            store.close()
            run_cli("kill", state=Path(tmp) / "state")
            code, data = self.status(tmp, session.key)
        self.assertTrue(data["killed"])
        self.assertEqual(data["job"]["state"], "queued")
        self.assertEqual(code, 10)

    def test_unknown_session_is_a_json_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = stored(tmp)
            store.close()
            code, out, err = run_cli("--json", "status", "missing", state=Path(tmp) / "state")
        self.assertEqual(code, 20)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out), {"ok": False, "error": "Unknown session", "exit_code": 20})

    def test_status_never_creates_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            code, out, _ = run_cli("--json", "status", "x", state=state)
            self.assertEqual(code, 20)
            self.assertFalse(state.exists())


class Events(unittest.TestCase):
    def test_events_stream_as_json_lines_with_sequence_numbers_and_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session = stored(tmp)
            for n in range(3):
                store.event(session.key, Event(provider="claude", role="builder", kind="note", text=f"e{n}"))
            store.close()
            code, out, _ = run_cli("events", session.key, state=Path(tmp) / "state")
            rows = [json.loads(line) for line in out.splitlines()]
            self.assertEqual((code, [r["text"] for r in rows]), (0, ["e0", "e1", "e2"]))
            seqs = [r["seq"] for r in rows]
            self.assertEqual(seqs, sorted(seqs))
            _, out, _ = run_cli("events", session.key, "--after", str(seqs[0]), state=Path(tmp) / "state")
            self.assertEqual([json.loads(l)["text"] for l in out.splitlines()], ["e1", "e2"])

    def test_follow_ends_when_the_session_has_settled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session = stored(tmp, status="ready")
            store.event(session.key, Event(provider="brahma", role="x", kind="note", text="done"))
            store.close()
            started = time.monotonic()
            code, out, _ = run_cli("events", session.key, "--follow", state=Path(tmp) / "state")
        self.assertEqual(code, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertLess(time.monotonic() - started, 5)

    def test_unknown_session_errors_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = stored(tmp)
            store.close()
            code, _, err = run_cli("events", "nope", state=Path(tmp) / "state")
        self.assertEqual(code, 20)
        self.assertIn("Unknown session", err)


class JsonOnEveryCommand(unittest.TestCase):
    def test_control_commands_emit_one_json_object_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session = stored(tmp)
            store.close()
            state = Path(tmp) / "state"
            for argv in (("pause", session.key), ("resume", session.key), ("stop", session.key), ("kill",),
                         ("kill", "--release"), ("sessions",)):
                with self.subTest(argv=argv):
                    code, out, err = run_cli("--json", *argv, state=state)
                    data = json.loads(out)
                    self.assertEqual((code, data["ok"], data["exit_code"], err), (0, True, 0, ""))

    def test_usage_errors_are_json_with_code_20(self):
        for argv in (["--json", "pause"], ["--json", "nonsense"], ["--json", "service"]):
            with self.subTest(argv=argv):
                with tempfile.TemporaryDirectory() as tmp:
                    code, out, _ = run_cli(*argv, state=tmp)
                data = json.loads(out)
                self.assertEqual((code, data["ok"], data["exit_code"]), (20, False, 20))


class MemoryCommand(unittest.TestCase):
    def test_show_then_forget_requires_confirmation_and_only_touches_memory(self):
        from brahma.memory import Memory, repo_id
        from brahma.store import Store
        with tempfile.TemporaryDirectory() as tmp:
            store, session = stored(tmp)
            root = Path(session.repo)
            identity = repo_id(root)
            Memory(store).remember(identity, "convention", "tests", "unittest")
            Memory(store).record_outcome(identity, provider="codex", task_type="t", checks_passed=True, repairs=0,
                                         findings=0, ready=True)
            store.close()
            state = Path(tmp) / "state"
            code, out, _ = run_cli("--json", "memory", "show", "--repo", str(root), state=state)
            self.assertEqual((code, len(json.loads(out)["memory"]), len(json.loads(out)["outcomes"])), (0, 1, 1))
            code, out, _ = run_cli("--json", "memory", "forget", "--repo", str(root), state=state)
            self.assertEqual((code, json.loads(out)["ok"]), (20, False))           # refused without --yes
            self.assertEqual(len(Memory(Store(state)).recall(identity)), 1)
            code, out, _ = run_cli("--json", "memory", "forget", "--repo", str(root), "--yes", state=state)
            self.assertEqual(json.loads(out)["memory_removed"], 1)
            check = Store(state)
            self.assertEqual(Memory(check).recall(identity), [])
            self.assertEqual(check.session(session.key)["repo"], str(root))            # sessions remain
            check.close()


class ApprovalIsNotExposed(unittest.TestCase):
    def test_there_is_no_command_for_an_ai_caller_to_approve_with(self):
        from brahma import cli
        names = set(cli.parser()._subparsers._group_actions[0].choices)
        self.assertNotIn("approve", names)  # Round 9 item 3 / Round 11 item 2: only a human-signed policy may approve


if __name__ == "__main__":
    unittest.main()
