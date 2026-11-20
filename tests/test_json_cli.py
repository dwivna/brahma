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
from tests.approval import at_terminal, owner_confirms


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
                with self.subTest(argv=argv), owner_confirms():
                    code, out, err = run_cli("--json", *argv, state=state)
                    data = json.loads(out)
                    self.assertEqual((code, data["ok"], data["exit_code"], err), (0, True, 0, ""))

    def test_models_usage_doctor_and_errors_honour_the_global_flag(self):
        # Review round 8, finding 12: `brahma --json models` printed text; doctor lacked ok/exit_code.
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as tmp, \
                patch("brahma.model_selection.read_models", new_callable=AsyncMock, return_value={"providers": []}), \
                patch("brahma.usage.read_usage", new_callable=AsyncMock, return_value={"providers": []}), \
                patch("brahma.providers.doctor", new_callable=AsyncMock, return_value={"provider": "x", "available": False}):
            for argv, code in ((("models",), 0), (("models", "--json"), 0), (("usage",), 0), (("doctor",), 30)):
                with self.subTest(argv=argv):
                    got, out, _ = run_cli("--json", *argv, state=Path(tmp) / "state")
                    data = json.loads(out)
                    self.assertEqual((got, data["exit_code"], data["ok"]), (code, code, code == 0))

    def test_unexpected_failures_are_still_one_json_object(self):
        from unittest.mock import AsyncMock, patch
        with tempfile.TemporaryDirectory() as tmp, \
                patch("brahma.model_selection.read_models", new_callable=AsyncMock, side_effect=RuntimeError("provider exploded")):
            code, out, _ = run_cli("--json", "models", state=Path(tmp) / "state")
        data = json.loads(out)
        self.assertEqual((code, data["ok"], data["exit_code"]), (13, False, 13))
        self.assertIn("provider exploded", data["error"])

    def test_needs_checks_has_its_own_exit_code(self):
        # Review round 8, finding 13.
        self.assertEqual(exitcodes.for_result_status("needs_checks"), exitcodes.NEEDS_CHECKS)

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


class OnlyAPersonApproves(unittest.TestCase):
    """Review round 8, finding 1: `run --approve` let any caller create an approval. Approval now needs the device owner."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from tests.test_cli import repo_session
        from brahma.models import Plan, Task
        self.store, self.session = repo_session(self.tmp.name)
        self.session.set_plan(Plan("Goal", [Task("T", "do", ["a.txt"], ["ok"])], checks=[["true"]]))
        self.store.close()
        self.state = Path(self.tmp.name) / "state"

    def digest(self):
        from brahma.contracts import read_contract
        from brahma.store import Store
        store = Store(self.state)
        try:
            return read_contract(Path(store.session(self.session.key)["contract"]))["digest"]
        finally:
            store.close()

    def approved(self):
        from brahma.store import Store
        store = Store(self.state)
        try:
            return store.session(self.session.key)["approved_digest"]
        finally:
            store.close()

    def test_approve_without_the_device_owner_records_nothing(self):
        with owner_confirms(False) as prompt, at_terminal(self.digest()[:8]):
            code, out, _ = run_cli("--json", "approve", self.session.key, state=self.state)
        prompt.assert_called_once()
        self.assertEqual((code, json.loads(out)["ok"]), (20, False))
        self.assertIn("Not confirmed by the device owner", json.loads(out)["error"])
        self.assertIsNone(self.approved())

    def test_approve_with_the_device_owner_records_the_digest(self):
        with owner_confirms(), at_terminal(self.digest()[:8]):
            code, out, err = run_cli("--json", "approve", self.session.key, state=self.state)
        data = json.loads(out)
        self.assertEqual((code, data["approved"]), (0, True))
        self.assertEqual(self.approved(), data["contract_digest"])
        self.assertIn("Owns: a.txt", err)                    # the person saw the contract before confirming
        self.assertIn('Checks: [["true"]]', err)

    def test_a_caller_without_a_terminal_never_raises_a_prompt(self):
        # Automated security review: an agent must not be able to pop a Touch ID prompt a person confirms unseen.
        with owner_confirms() as prompt:
            code, out, _ = run_cli("--json", "approve", self.session.key, state=self.state)
        prompt.assert_not_called()
        self.assertEqual(code, 20)
        self.assertIn("interactive terminal", json.loads(out)["error"])
        self.assertIsNone(self.approved())

    def test_a_wrong_digest_prefix_approves_nothing(self):
        with owner_confirms() as prompt, at_terminal("00000000"):
            code, _, _ = run_cli("--json", "approve", self.session.key, state=self.state)
        prompt.assert_not_called()
        self.assertEqual(code, 20)
        self.assertIsNone(self.approved())

    def test_running_never_approves(self):
        from unittest.mock import AsyncMock, patch
        with patch("brahma.engine.execute", AsyncMock(side_effect=AssertionError("ran without approval"))):
            code, out, _ = run_cli("--json", "run", "--session", self.session.key, state=self.state)
        self.assertEqual(code, 20)
        self.assertIn("has not been approved by a person", json.loads(out)["error"])
        self.assertIsNone(self.approved())

    def test_there_is_no_way_around_the_prompt(self):
        from brahma import presence
        import inspect
        source = inspect.getsource(presence)
        for bypass in ("os.environ", "getenv", "sys.argv", "Settings"):      # no variable, flag or setting skips the check
            self.assertNotIn(bypass, source.split('"""', 2)[2])

    def test_kill_release_and_decide_need_the_device_owner(self):
        from brahma import halt
        halt.engage(self.state)
        with owner_confirms(False):
            code, out, _ = run_cli("--json", "kill", "--release", state=self.state)
        self.assertEqual(code, 20)
        self.assertTrue(halt.engaged(self.state))


if __name__ == "__main__":
    unittest.main()
