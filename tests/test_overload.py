"""Round 13: a transient provider "overloaded" error is retried, never treated as quota."""
from __future__ import annotations

import asyncio
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import engine, gittools, providers
from brahma.config import Settings
from brahma.models import Plan, Result, Task


def failure(text):
    return json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": text})


class Classification(unittest.TestCase):
    def parse(self, text):
        events = []
        parser = providers.StreamParser("claude", "builder", events.append)
        parser.consume(failure(text).encode())
        return parser, events

    def test_overloaded_is_transient_not_a_limit(self):
        parser, events = self.parse("API Error: 529 Overloaded, please retry")
        self.assertTrue(parser.failed)
        self.assertTrue(parser.overloaded)
        self.assertFalse(parser.limited)
        self.assertEqual([e for e in events if e.kind == "quota"], [])

    def test_real_limits_are_still_limits(self):
        for text in ("You've hit your limit", "usage limit reached", "rate limit exceeded", "quota exceeded"):
            with self.subTest(text=text):
                parser, _ = self.parse(text)
                self.assertTrue(parser.limited)
                self.assertFalse(parser.overloaded)

    def test_limit_wins_when_both_phrases_appear(self):
        parser, _ = self.parse("overloaded: you have hit your limit")
        self.assertTrue(parser.limited)
        self.assertFalse(parser.overloaded)


class FalsePositives(unittest.TestCase):
    def test_model_prose_mentioning_overloaded_is_not_a_provider_error(self):
        for text in ("I refactored the overloaded request handler but ran out of turns",
                     "the function is overloaded for two types", "Tests pass; operator overloaded correctly"):
            with self.subTest(text=text):
                parser, _ = Classification().parse(text)
                self.assertFalse(parser.overloaded)
                self.assertFalse(parser.limited)

    def test_real_overload_shapes_are_recognised(self):
        for text in ('API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}', "529 Overloaded",
                     "Overloaded", "API Error: Overloaded - try again", "error: overloaded_error"):
            with self.subTest(text=text):
                parser, _ = Classification().parse(text)
                self.assertTrue(parser.overloaded, text)


class Retry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.counter = self.directory / "count"
        self.events = []
        self.cancel = asyncio.Event()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def cli(self, fail_times, message="API Error: 529 overloaded"):
        body = f'''import json, sys
path = {str(self.counter)!r}
try:
    count = int(open(path).read())
except OSError:
    count = 0
open(path, "w").write(str(count + 1))
sys.stdin.read()
if count < {fail_times}:
    print(json.dumps({{"type":"result","subtype":"error_during_execution","is_error":True,"result":{message!r}}}))
    sys.exit(1)
print(json.dumps({{"type":"assistant","message":{{"content":[{{"type":"text","text":"done"}}]}}}}))
print(json.dumps({{"type":"result","subtype":"success","is_error":False,"result":"done","session_id":"s1"}}))
'''
        path = self.directory / "claude"
        path.write_text(f"#!{sys.executable}\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def runs(self) -> int:
        return int(self.counter.read_text())

    async def invoke(self, fail_times, timeout=20, delays=(0.01, 0.01, 0.01), message="API Error: 529 overloaded",
                     min_budget=0.0):
        with patch.object(providers.shutil, "which", return_value=self.cli(fail_times, message)), patch.object(
                providers, "doctor", AsyncMock(return_value={"available": True})), patch.object(
                providers, "OVERLOAD_DELAYS", delays), patch.object(providers, "OVERLOAD_MIN_BUDGET", min_budget), patch.object(
                providers, "TERMINATE_GRACE", .03):
            return await providers.run("claude", "go", self.directory, "builder", Settings(),
                                       self.events.append, self.cancel, timeout)

    async def test_recovers_after_transient_overload_without_quota_events(self):
        result = await self.invoke(2)
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.runs(), 3)
        retries = [e for e in self.events if e.kind == "progress" and "overloaded" in e.text]
        self.assertEqual(len(retries), 2)
        self.assertEqual([e for e in self.events if e.kind == "quota"], [])

    async def test_persistent_overload_stops_after_three_retries_as_overloaded(self):
        result = await self.invoke(99)
        self.assertEqual(result.status, "overloaded")
        self.assertEqual(self.runs(), 4)  # first run + three retries
        self.assertEqual([e for e in self.events if e.kind == "quota"], [])

    async def test_a_real_limit_is_never_retried(self):
        result = await self.invoke(99, message="You've hit your limit")
        self.assertEqual(result.status, "limited")
        self.assertEqual(self.runs(), 1)

    async def test_retry_waits_stay_inside_the_time_budget(self):
        result = await self.invoke(99, timeout=5, delays=(60.0,))
        self.assertEqual(result.status, "overloaded")
        self.assertEqual(self.runs(), 1)  # no room to wait 60s inside a 5s budget

    async def test_default_minimum_budget_prevents_a_useless_last_retry(self):
        # 5s budget, 1s delay: only 4s would remain for the retry, far below the default 60s minimum.
        result = await self.invoke(99, timeout=5, delays=(1.0,), min_budget=providers.OVERLOAD_MIN_BUDGET)
        self.assertEqual(result.status, "overloaded")  # not masked as a "timeout" by a starved retry
        self.assertEqual(self.runs(), 1)

    async def test_cancel_during_backoff_returns_promptly(self):
        task = asyncio.create_task(self.invoke(99, timeout=100, delays=(30.0,)))
        for _ in range(100):
            await asyncio.sleep(0.1)
            if self.counter.exists() and any("retrying" in e.text for e in self.events):
                break
        self.cancel.set()
        result = await asyncio.wait_for(task, 5)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self.runs(), 1)


class EngineMapping(unittest.IsolatedAsyncioTestCase):
    async def test_overloaded_builder_blocks_with_reason_and_is_not_a_quota_wait(self):
        events = []
        gate = AsyncMock(return_value={"state": "available", "next_at": None, "reason": ""})
        run = AsyncMock(return_value=Result("codex", "overloaded", "Provider overloaded"))
        task = Task("Implement", "do it", ["Implement.py"], ["works"], "codex", "routine")
        with patch.object(engine, "_context", return_value="ctx"), patch.object(
                gittools, "snapshot_changes", return_value={}), patch.object(
                gittools, "ownership_changes", return_value=(True, "ok")), patch.object(
                gittools, "run_checks", AsyncMock(return_value=(True, "ok"))), patch.object(
                engine.providers, "run", run):
            result = await engine.execute(Plan("Goal", [task]), Path("/repo"), Settings(checks=[["true"]]),
                                          events.append, asyncio.Event(), provider_gate=gate)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(run.await_count, 1)          # no engine-level retry loop either
        gate.assert_awaited_once()                    # only the pre-launch check; no post-failure limit wait
        self.assertEqual([e for e in events if e.kind in ("wait", "quota")], [])
        states = [json.loads(e.text)["state"] for e in events if e.kind == "task_state"]
        self.assertEqual(states[-1], "blocked")


if __name__ == "__main__":
    unittest.main()
