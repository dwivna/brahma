"""Offline CLI tests: no provider, model or launchctl calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from brahma import cli, quota
from brahma.models import Plan, Task
from brahma.session import Session
from brahma.store import Store


def run_cli(*argv, state):
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(state)}), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def repo_session(tmp):
    root = Path(tmp) / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.name", "Brahma test"), ("user.email", "test@example.invalid")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    (root / "a.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    store = Store(Path(tmp) / "state")
    return store, Session(store, root)


def provider_rows(codex=True, claude=True):
    def row(name, ok):
        return {"provider": name, "installed": ok, "signed_in": ok, "signedin": ok, "subscription": ok,
                "available": ok, "error": None if ok else f"{name} CLI is not installed",
                "state": "ready" if ok else "missing", "action": ""}
    return {"codex": row("codex", codex), "claude": row("claude", claude)}


class Parsing(unittest.TestCase):
    def test_new_commands_and_existing_options_parse(self):
        p = cli.parser()
        args = p.parse_args(["worker", "--home", "/tmp/h", "--once"])
        self.assertEqual((args.command, args.home, args.once), ("worker", Path("/tmp/h"), True))
        self.assertFalse(p.parse_args(["worker", "--home", "/tmp/h"]).once)
        for action in ("install", "status", "stop"):
            self.assertEqual(p.parse_args(["service", action]).action, action)
        self.assertEqual(p.parse_args(["pause", "abc"]).session, "abc")
        self.assertTrue(p.parse_args(["--demo"]).demo)
        self.assertEqual(p.parse_args(["--resume", "abc"]).resume, "abc")
        self.assertTrue(p.parse_args(["run", "plan.json", "--approve"]).approve)

    def test_invalid_arguments_exit_with_usage_error(self):
        for argv in (["worker"], ["service"], ["service", "restart"], ["pause"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                cli.parser().parse_args(argv)
            self.assertEqual(cm.exception.code, 2)


class Commands(unittest.TestCase):
    def test_usage_is_readable_or_json_without_plans_or_model_calls(self):
        report = {"observed_at": 1000, "providers": [], "cancelled": False}
        for argv in (("usage",), ("usage", "codex", "--json")):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as tmp, \
                    patch("brahma.usage.read_usage", new_callable=AsyncMock, return_value=report) as read, \
                    patch("brahma.usage.format_usage", return_value="Quota unknown"), \
                    patch("brahma.providers.run", side_effect=AssertionError("usage called a model")):
                code, out, err = run_cli(*argv, state=Path(tmp)/"state")
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(read.await_args.args[1], ("codex",) if "codex" in argv else ("codex", "claude"))
                self.assertEqual(json.loads(out), report) if "--json" in argv else self.assertIn("Quota unknown", out)
                store = Store(Path(tmp)/"state")
                try:
                    self.assertEqual(store.recent(), [])
                    self.assertEqual(store.pending(), [])
                finally:
                    store.close()

    def test_worker_passes_resolved_home_and_once(self):
        seen = []
        async def serve(home, *, once=False):
            seen.append((home, once))
            return 0
        with tempfile.TemporaryDirectory() as tmp, patch("brahma.scheduler.serve", serve):
            code, _, _ = run_cli("worker", "--home", tmp, "--once", state=Path(tmp) / "other")
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(Path(tmp).resolve(), True)])

    def test_worker_reports_errors_without_traceback(self):
        async def serve(home, *, once=False):
            raise OSError("disk unavailable")
        with tempfile.TemporaryDirectory() as tmp, patch("brahma.scheduler.serve", serve):
            code, _, err = run_cli("worker", "--home", tmp, state=tmp)
        self.assertEqual(code, 2)
        self.assertIn("disk unavailable", err)

    def test_service_actions_delegate_and_errors_are_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            def service(action, home, **kwargs):
                calls.append((action, home))
                return f"{action} ok"
            with patch("brahma.scheduler.service", service):
                for action in ("install", "status", "stop"):
                    code, out, _ = run_cli("service", action, state=tmp)
                    self.assertEqual(code, 0)
                    self.assertIn(f"{action} ok", out)
            self.assertEqual(calls, [(a, Path(tmp).resolve()) for a in ("install", "status", "stop")])
            def unsupported(action, home, **kwargs):
                raise ValueError("Automatic service installation currently supports macOS")
            with patch("brahma.scheduler.service", unsupported):
                code, _, err = run_cli("service", "install", state=tmp)
            self.assertEqual(code, 2)
            self.assertIn("supports macOS", err)
            def failing(action, home, **kwargs):
                raise subprocess.CalledProcessError(1, ["launchctl"])
            with patch("brahma.scheduler.service", failing):
                self.assertEqual(run_cli("service", "stop", state=tmp)[0], 2)

    def test_service_status_reports_queue_without_claiming_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session = repo_session(tmp)
            store.enqueue(session.key, session.generation, "d" * 64, time.time(), "due")
            store.close()
            with patch("brahma.scheduler.service", lambda action, home, **kw: "registered"):
                code, out, _ = run_cli("service", "status", state=Path(tmp) / "state")
            self.assertEqual(code, 0)
            self.assertIn("Queued approved jobs: 1", out)
            self.assertIn("unknown quota permits one bounded approved attempt", out)

    def test_pause_is_durable_and_blocks_automatic_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session = repo_session(tmp)
            store.enqueue(session.key, session.generation, "d" * 64, 0.0, "due")
            store.close()
            code, out, _ = run_cli("pause", session.key, state=Path(tmp) / "state")
            self.assertEqual(code, 0)
            self.assertIn("no automatic resume", out)
            check = Store(Path(tmp) / "state")
            self.assertEqual(check.session(session.key)["stopped"], 1)
            self.assertIsNone(check.claim(time.time()))
            self.assertEqual(check.pending(), [])
            check.enqueue(session.key, session.generation, "d" * 64, 0.0, "provider reset")
            self.assertEqual(check.pending(), [])  # a reset cannot revive a paused session
            check.close()

    def test_pause_unknown_session_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_cli("pause", "missing", state=tmp)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("Unknown session", err)

    def test_sessions_still_lists_saved_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session = repo_session(tmp)
            store.close()
            code, out, _ = run_cli("sessions", state=Path(tmp) / "state")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["id"], session.key)


class Doctor(unittest.TestCase):
    def doctor(self, state, rows):
        async def fake(name):
            return dict(rows[name])
        with patch("brahma.providers.doctor", fake):
            return run_cli("doctor", state=state)

    def test_ready_providers_report_quota_unknown_and_create_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            code, out, _ = self.doctor(state, provider_rows())
            data = json.loads(out)
            self.assertEqual(code, 0)
            for row in data["providers"]:
                self.assertEqual(row["readiness"], "ready")
                self.assertEqual(row["quota"]["state"], "unknown")
            self.assertFalse(state.exists())
            self.assertFalse(data["api_fallback"])
            self.assertTrue(data["usage_remaining"].startswith("unknown"))

    def test_missing_cli_is_flagged_and_exit_is_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = self.doctor(Path(tmp) / "state", provider_rows(codex=False))
        rows = {r["provider"]: r for r in json.loads(out)["providers"]}
        self.assertEqual(code, 1)
        self.assertEqual(rows["codex"]["readiness"], "missing")
        self.assertEqual(rows["claude"]["readiness"], "ready")

    def test_observed_quota_wait_is_reported_separately_from_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            store = Store(state)
            reset = time.time() + 3600
            store.observe(quota.normalize_codex({"rateLimits": {"primary": {
                "usedPercent": 100, "windowDurationMins": 300, "resetsAt": reset}}}))
            store.close()
            code, out, _ = self.doctor(state, provider_rows())
        rows = {r["provider"]: r for r in json.loads(out)["providers"]}
        self.assertEqual(code, 0)  # installed and signed in
        self.assertEqual(rows["codex"]["readiness"], "ready")
        self.assertEqual(rows["codex"]["quota"]["state"], "waiting")
        self.assertEqual(rows["claude"]["quota"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
