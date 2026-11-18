from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma.config import Settings
from brahma import providers


class ParserTests(unittest.TestCase):
    def parser(self, provider="codex"):
        self.events = []
        return providers.StreamParser(provider, "builder", self.events.append)

    @staticmethod
    def send(parser, value):
        parser.consume(json.dumps(value).encode())

    def test_codex_messages_usage_session_and_terminal_stripping(self):
        parser = self.parser()
        self.send(parser, {"type": "thread.started", "thread_id": "thread-1"})
        self.send(parser, {"type": "item.completed", "item": {"type": "command_execution", "text": "do not display"}})
        self.send(parser, {"type": "item.completed", "item": {"type": "agent_message", "text": "\x1b[31mhello\x1b[0m\x1b]52;c;secret\x07"}})
        self.send(parser, {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4, "cost_usd": 8, "api_key": "secret"}})
        self.assertTrue(parser.completed)
        self.assertEqual(parser.text, "hello")
        self.assertEqual(parser.session_id, "thread-1")
        self.assertEqual(parser.usage, {"input_tokens": 12, "output_tokens": 4})
        self.assertEqual([e.text for e in self.events if e.kind == "message"], ["hello"])
        self.assertEqual([e.text for e in self.events if e.kind == "native_session"], ["thread-1"])

    def test_claude_final_not_duplicated_and_final_response_separate(self):
        parser = self.parser("claude")
        self.send(parser, {"type": "system", "session_id": "session-2"})
        for text in ("Inspecting files", '{"verdict":"pass"}'):
            self.send(parser, {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
        self.send(parser, {"type": "result", "subtype": "success", "is_error": False,
                           "result": '{"verdict":"pass"}', "usage": {"input_tokens": 2}})
        self.assertTrue(parser.completed)
        self.assertEqual(len([e for e in self.events if e.kind == "message"]), 2)
        self.assertEqual(parser.final_text, '{"verdict":"pass"}')
        self.assertEqual(parser.session_id, "session-2")

    def test_limit_and_non_success_results_fail(self):
        parser = self.parser("claude")
        self.send(parser, {"type": "result", "subtype": "error_during_execution", "is_error": True,
                           "result": "You've hit your limit"})
        self.assertTrue(parser.failed)
        self.assertTrue(parser.limited)

    def test_invalid_json_and_bounded_retention(self):
        parser = self.parser()
        with self.assertRaises(ValueError):
            parser.consume(b"not json")
        with patch.object(providers, "MAX_TEXT_CHARS", 10):
            parser.message("x" * 100)
            self.assertEqual(len(parser.text), 10)
            self.assertTrue(parser.truncated)
            self.assertEqual(self.events[0].text, "x" * 10)

    def test_environment_scrubs_auth_and_provider_overrides(self):
        with patch.dict(os.environ, {"PATH": "/bin", "HOME": "/fake/home", "OPENAI_API_KEY": "key",
                "ANTHROPIC_AUTH_TOKEN": "token", "ANTHROPIC_BASE_URL": "override",
                "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_OAUTH_TOKEN": "private",
                "CODEX_MODEL_PROVIDER": "paid", "SOME_API_KEY": "private"}, clear=True):
            env = providers.child_env()
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/fake/home", "NO_COLOR": "1", "TERM": "dumb"})

    def test_command_permissions(self):
        for role, sandbox in (("planner", "read-only"), ("reviewer", "read-only"), ("builder", "workspace-write")):
            cmd = providers._command("codex", "codex", Path("/repo"), role, Settings())
            self.assertEqual(cmd[cmd.index("--sandbox") + 1], sandbox)
            self.assertIn('approval_policy="never"', cmd)
            self.assertFalse(any("bypass" in arg for arg in cmd))
            cmd = providers._command("claude", "claude", Path("/repo"), role, Settings())
            tools = cmd[cmd.index("--tools") + 1]
            self.assertNotIn("Bash", tools)
            self.assertEqual("Edit" in tools, role == "builder")
            self.assertIn("--restricted", cmd)
            self.assertIn("--safe-mode", cmd)
            self.assertEqual(json.loads(cmd[cmd.index("--mcp-config") + 1]), {"mcpServers": {}})

    def test_native_resume_argv_preserves_permissions_and_uses_explicit_id(self):
        cmd = providers._command("codex", "codex", Path("/repo"), "reviewer", Settings(), "thread-1")
        self.assertEqual(cmd[-3:], ["resume", "thread-1", "-"])
        self.assertEqual(cmd[cmd.index("--sandbox")+1], "read-only")
        self.assertNotIn("--last", cmd)
        cmd = providers._command("claude", "claude", Path("/repo"), "builder", Settings(), "session-1")
        self.assertEqual(cmd[-2:], ["--resume", "session-1"])
        self.assertIn("--restricted", cmd)
        for invalid in ("--last", "../file", "session\nnext", "", "x" * 201):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                providers._command("codex", "codex", Path("/repo"), "builder", Settings(), invalid)

    def test_claude_quota_rejection_is_sticky_and_emitted_structured(self):
        parser = self.parser("claude")
        for status in ("rejected", "allowed"):
            self.send(parser, {"type": "rate_limit_event", "rate_limit_info": {"status": status, "resetsAt": 9000}})
        self.assertTrue(parser.limited)
        events = [json.loads(e.text) for e in self.events if e.kind == "quota"]
        self.assertEqual(len(events), 2)
        self.assertTrue(events[-1]["blocked"])

    def test_codex_quota_notification_is_emitted_without_inferred_tokens(self):
        parser = self.parser()
        self.send(parser, {"method": "account/rateLimits/updated", "params": {"rateLimits": {
            "primary": {"usedPercent": 100, "windowDurationMins": 300, "resetsAt": 9000}}}})
        self.assertTrue(parser.limited)
        value = json.loads(self.events[0].text)
        self.assertEqual(value["windows"][0]["used_percent"], 100)
        self.assertEqual(parser.usage, {})


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.events = []
        self.cancel = asyncio.Event()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def executable(self, body, provider="codex"):
        path = self.directory / provider
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    async def invoke(self, body, timeout=5, provider="codex"):
        executable = self.executable(body, provider)
        with patch.object(providers.shutil, "which", return_value=executable), patch.object(
                providers, "doctor", AsyncMock(return_value={"available": True})), patch.object(providers, "TERMINATE_GRACE", .03):
            return await providers.run(provider, "instructions", self.directory, "builder", Settings(),
                                       self.events.append, self.cancel, timeout)

    async def test_fake_cli_success_stdin_and_scrubbed_env(self):
        body = '''import os, sys, json
assert sys.stdin.read() == "instructions"
assert "OPENAI_API_KEY" not in os.environ
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":3}}))
'''
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}):
            result = await self.invoke(body)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.text, "done")
        self.assertEqual(result.usage["input_tokens"], 3)

    async def test_missing_completion_and_nonzero_exit_never_pass(self):
        result = await self.invoke('print(\'{"type":"item.completed","item":{"type":"agent_message","text":"all tests passed"}}\')')
        self.assertEqual(result.status, "error")
        result = await self.invoke('import sys\nprint(\'{"type":"turn.completed"}\')\nsys.exit(1)')
        self.assertEqual(result.status, "error")

    async def test_output_line_limit_stops_worker(self):
        with patch.object(providers, "MAX_LINE_BYTES", 100):
            result = await self.invoke('import time\nprint("x"*300, flush=True)\ntime.sleep(30)')
        self.assertEqual(result.status, "error")
        self.assertIn("line limit", result.text)

    async def test_output_retention_limit_is_error(self):
        with patch.object(providers, "MAX_TEXT_CHARS", 10):
            result = await self.invoke('import json\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"x"*100}}))\nprint(\'{"type":"turn.completed"}\')')
        self.assertEqual(result.status, "error")

    async def test_total_stream_limit_stops_worker(self):
        with patch.object(providers, "MAX_STREAM_BYTES", 100):
            result = await self.invoke('print(\'{"type":"progress"}\\n\'*100, flush=True)')
        self.assertEqual(result.status, "error")
        self.assertIn("byte limit", result.text)

    async def test_timeout_kills_process_group_descendant(self):
        marker = self.directory / "descendant.txt"
        code = f'''import os, signal, time
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(30)
else:
    open({str(marker)!r}, "w").write(str(child))
    def stop(*args):
        os.kill(child, signal.SIGKILL)
        os.waitpid(child, 0)
        raise SystemExit()
    signal.signal(signal.SIGTERM, stop)
    time.sleep(30)
'''
        result = await self.invoke(code, timeout=.3)
        self.assertEqual(result.status, "timeout")
        child = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    async def test_exited_leader_does_not_leave_stdout_holding_descendant(self):
        ready = self.directory / "child.ready"
        stopped = self.directory / "child.stopped"
        code = f'''import os, signal, time
child = os.fork()
if child == 0:
    def stop(*args):
        open({str(stopped)!r}, "w").write("terminated")
        raise SystemExit()
    signal.signal(signal.SIGTERM, stop)
    open({str(ready)!r}, "w").write(str(os.getpid()))
    time.sleep(30)
else:
    while not os.path.exists({str(ready)!r}):
        time.sleep(.005)
    raise SystemExit()
'''
        result = await self.invoke(code, timeout=.25)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(stopped.read_text(), "terminated")

    async def test_cancellation_stops_worker_promptly(self):
        async def stop():
            await asyncio.sleep(.15)
            self.cancel.set()
        task = asyncio.create_task(stop())
        result = await self.invoke("import time\ntime.sleep(30)")
        await task
        self.assertEqual(result.status, "cancelled")
        self.assertLess(result.seconds, 3)

    async def test_task_cancellation_also_reaps_process(self):
        marker = self.directory / "worker.pid"
        code = f'import os,time\nopen({str(marker)!r},"w").write(str(os.getpid()))\ntime.sleep(30)'
        running = asyncio.create_task(self.invoke(code))
        for _ in range(100):
            if marker.exists():
                break
            await asyncio.sleep(.01)
        self.assertTrue(marker.exists())
        pid = int(marker.read_text())
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_auth_only_accepts_known_subscription_and_hides_identity(self):
        fixtures = [
            ("codex", "Logged in using ChatGPT", True),
            ("codex", "Logged in using an API key", False),
            ("codex", "Logged in using another provider", False),
            ("claude", json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "private@example.org"}), True),
            ("claude", json.dumps({"loggedIn": True, "authMethod": "apiKey"}), False),
            ("claude", json.dumps({"loggedIn": True, "authMethod": "oauth"}), False),
            ("claude", json.dumps({"loggedIn": False, "authMethod": "none"}), False),
        ]
        for provider, output, available in fixtures:
            with self.subTest(provider=provider, output=output):
                exe = self.executable(f"print({output!r})", provider)
                with patch.object(providers.shutil, "which", return_value=exe), patch.object(providers, "TERMINATE_GRACE", .01):
                    result = await providers.doctor(provider)
                self.assertEqual(result["available"], available)
                self.assertNotIn("private@example", json.dumps(result))

    async def test_unavailable_auth_never_starts_worker(self):
        with patch.object(providers, "doctor", AsyncMock(return_value={"available": False, "error": "No subscription"})), patch.object(
                providers.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            result = await providers.run("codex", "prompt", self.directory, "builder", Settings(),
                                         self.events.append, self.cancel, 1)
        self.assertEqual(result.status, "unavailable")
        spawn.assert_not_called()

    async def test_missing_cli_requests_install_without_trying_auth_or_model(self):
        for name in ("claude", "codex"):
            with self.subTest(provider=name), patch.object(providers.shutil, "which", return_value=None), patch.object(
                    providers, "_auth_capture", AsyncMock()) as auth:
                result = await providers.doctor(name)
            self.assertEqual(result["state"], "missing")
            self.assertFalse(result["available"])
            self.assertIn("Please install", result["action"])
            auth.assert_not_called()

    async def test_signed_out_and_unknown_checks_request_distinct_remediation(self):
        cases = [("codex", 1, "Not logged in", "signed_out", "codex login"),
                 ("claude", 1, '{"loggedIn":false}', "signed_out", "claude auth login"),
                 ("codex", 0, "unknown newer status", "unverified", "codex login status"),
                 ("claude", 0, "not JSON", "unverified", "claude auth status")]
        for name, code, output, state, command in cases:
            with self.subTest(provider=name, state=state), patch.object(providers.shutil, "which", return_value=name), patch.object(
                    providers, "_auth_capture", AsyncMock(return_value=(code, output))):
                result = await providers.doctor(name)
            self.assertEqual(result["state"], state)
            self.assertIn(command, result["action"])
            self.assertFalse(result["available"])

    async def test_codex_auth_status_on_stderr_is_recognised(self):
        exe = self.executable('import sys\nprint("Logged in using ChatGPT", file=sys.stderr)')
        with patch.object(providers.shutil, "which", return_value=exe), patch.object(providers, "TERMINATE_GRACE", .01):
            result = await providers.doctor("codex")
        self.assertTrue(result["available"])

    async def test_cancel_during_auth_is_responsive(self):
        async def slow_auth(provider):
            await asyncio.sleep(30)
        async def stop():
            await asyncio.sleep(.05)
            self.cancel.set()
        task = asyncio.create_task(stop())
        with patch.object(providers, "doctor", side_effect=slow_auth), patch.object(
                providers.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            result = await providers.run("codex", "prompt", self.directory, "builder", Settings(),
                                         self.events.append, self.cancel, 5)
        await task
        self.assertEqual(result.status, "cancelled")
        self.assertLess(result.seconds, 1)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
