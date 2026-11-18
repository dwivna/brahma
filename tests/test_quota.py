from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import providers, quota


def codex_raw(primary=25, secondary=50, first_reset=2000, weekly_reset=9000):
    return {"rateLimits": {"primary": {"usedPercent": primary, "windowDurationMins": 300, "resetsAt": first_reset},
                           "secondary": {"usedPercent": secondary, "windowDurationMins": 10080, "resetsAt": weekly_reset}}}


class NormalizationTests(unittest.TestCase):
    def test_codex_windows_and_real_zero_are_preserved(self):
        result = quota.normalize_codex(codex_raw(primary=0), observed_at=1000)
        self.assertEqual(result["provider"], "codex")
        self.assertEqual(result["windows"][0]["used_percent"], 0)
        self.assertEqual(result["windows"][1]["duration_seconds"], 604800)
        self.assertEqual(quota.eligibility(result, 1001)["state"], "available")
        self.assertNotIn("savings", json.dumps(result))

    def test_unknown_values_are_not_zero_or_available(self):
        result = quota.normalize_codex({}, observed_at=1000)
        self.assertIsNone(result["blocked"])
        self.assertEqual(result["windows"], [])
        self.assertEqual(quota.eligibility(result, 1001)["state"], "unknown")

    def test_exhausted_weekly_window_dominates_five_hour_reset(self):
        result = quota.normalize_codex(codex_raw(100, 100), observed_at=1000)
        self.assertEqual(result["reset_at"], 9000)
        eligible = quota.eligibility(result, 1001)
        self.assertEqual(eligible["state"], "waiting")
        self.assertEqual(eligible["next_at"], 9000)
        self.assertEqual(quota.eligibility(result, 3000)["next_at"], 9000)
        self.assertEqual(quota.eligibility(result, 9000)["state"], "unknown")

    def test_staleness_and_reset_require_recheck_not_assumed_capacity(self):
        result = quota.normalize_codex(codex_raw(), observed_at=1000)
        self.assertEqual(quota.eligibility(result, 1400)["state"], "unknown")
        self.assertEqual(quota.eligibility(result, 2000)["state"], "unknown")
        self.assertEqual(quota.eligibility(result, 2000)["next_at"], 2000)
        self.assertEqual(quota.eligibility(result, 900)["state"], "unknown")

    def test_multibucket_limits_are_all_considered(self):
        raw = {"rateLimitsByLimitId": {"codex": codex_raw()["rateLimits"],
                "codex_other": codex_raw(100, 100, 10000, 20000)["rateLimits"]}}
        result = quota.normalize_codex(raw, 1000)
        self.assertEqual(len(result["windows"]), 4)
        self.assertEqual(quota.eligibility(result, 1001)["next_at"], 20000)

    def test_server_reached_state_overrides_rounded_percentage(self):
        raw = codex_raw(primary=99)
        raw["rateLimits"]["rateLimitReachedType"] = "primary"
        result = quota.normalize_codex(raw, 1000)
        self.assertEqual(quota.eligibility(result, 1001)["next_at"], 2000)

    def test_strict_numeric_bounds_reject_bool_nan_infinity_and_strings(self):
        for value in (True, "80", -1, 101, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quota.normalize_codex(codex_raw(primary=value), 1000)
        for value in (False, -1, math.nan, "tomorrow"):
            with self.subTest(reset=value), self.assertRaises(ValueError):
                quota.normalize_codex(codex_raw(first_reset=value), 1000)
        with self.assertRaises(ValueError):
            quota.normalize_codex({"rateLimits": {"primary": {"windowDurationMins": 0}}}, 1000)

    def test_claude_rejected_preserves_reset_without_guessing_utilization_units(self):
        result = quota.normalize_claude({"status": "rejected", "resetsAt": 2000, "utilization": .9}, 1000)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["utilization"], .9)
        self.assertIsNone(result["windows"][0]["used_percent"])
        self.assertEqual(quota.eligibility(result, 1001)["next_at"], 2000)
        self.assertEqual(quota.eligibility(result, 2001)["state"], "unknown")

    def test_claude_statusline_explicit_five_hour_and_weekly_percentages(self):
        result = quota.normalize_claude({"five_hour": {"used_percentage": 10, "resets_at": 2000},
            "seven_day": {"used_percentage": 100, "resets_at": 9000}}, 1000)
        self.assertEqual(result["source"], "claude.statusline")
        self.assertEqual(quota.eligibility(result, 1001)["next_at"], 9000)

    def test_rejection_cannot_be_cleared_by_allowed_event(self):
        rejected = quota.normalize_claude({"status": "rejected", "resetsAt": 9000}, 1000)
        allowed = quota.normalize_claude({"status": "allowed", "resetsAt": 2000}, 1001)
        merged = quota.sticky(rejected, allowed)
        self.assertTrue(merged["blocked"])
        self.assertEqual(merged["reset_at"], 9000)
        rejected2 = quota.normalize_claude({"status": "rejected", "resetsAt": 2000}, 1002)
        self.assertEqual(quota.sticky(rejected, rejected2)["reset_at"], 9000)

    def test_no_reset_and_credit_error_remain_unknown(self):
        result = quota.normalize_claude({"status": "rejected", "errorCode": "credits_required", "overageEnabled": False}, 1000)
        self.assertEqual(result["error"], "credits_required")
        self.assertEqual(quota.eligibility(result, 1001)["state"], "unknown")

    def test_claude_invalid_fields_fail_closed(self):
        for info in ({"status": "maybe"}, {"utilization": math.nan}, {"overageEnabled": 1},
                     {"status": "rejected", "resetsAt": "tomorrow"}):
            with self.subTest(info=info), self.assertRaises(ValueError):
                quota.normalize_claude(info, 1000)

    def test_malformed_saved_snapshot_is_unknown(self):
        for value in (None, {}, {"observed_at": 1000, "windows": "x"},
                      {"observed_at": math.nan, "windows": [], "blocked": False},
                      {"observed_at": 1000, "windows": [], "blocked": "false"}):
            with self.subTest(value=value):
                self.assertEqual(quota.eligibility(value, 1001)["state"], "unknown")


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.cancel = asyncio.Event()
        self.patch_auth = patch.object(providers, "doctor", AsyncMock(return_value={"available": True}))
        self.auth = self.patch_auth.start()
        self.patch_grace = patch.object(providers, "TERMINATE_GRACE", .02)
        self.patch_grace.start()
        self.addCleanup(self.patch_auth.stop)
        self.addCleanup(self.patch_grace.stop)
        self.addCleanup(self.tmp.cleanup)

    def executable(self, code):
        path = self.directory / "codex"
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    async def probe(self, code, timeout=2):
        exe = self.executable(code)
        with patch.object(quota.shutil, "which", return_value=exe):
            return await quota.probe_codex(self.cancel, timeout)

    async def test_rpc_only_initializes_and_reads_quota_without_model_request(self):
        log = self.directory / "rpc.json"
        response = codex_raw(25, 100, time.time()+100, time.time()+9000)
        code = f'''import json, os, sys
assert sys.argv[1:4] == ["app-server", "--listen", "stdio://"]
assert 'forced_login_method="chatgpt"' in sys.argv
assert "OPENAI_API_KEY" not in os.environ
seen=[]
for line in sys.stdin:
    value=json.loads(line)
    seen.append(value["method"])
    if value["method"] == "initialize":
        print(json.dumps({{"id":1,"result":{{"userAgent":"private@example.org"}}}}),flush=True)
    elif value["method"] == "account/rateLimits/read":
        open({str(log)!r}, "w").write(json.dumps(seen))
        print(json.dumps({{"id":2,"result":{response!r}}}),flush=True)
    elif value["method"] != "initialized":
        raise SystemExit(4)
'''
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}):
            result = await self.probe(code)
        self.assertEqual(json.loads(log.read_text()), ["initialize", "initialized", "account/rateLimits/read"])
        self.assertTrue(result["blocked"])
        self.assertNotIn("private@example", json.dumps(result))
        self.assertEqual(quota.eligibility(result)["state"], "waiting")

    async def test_timeout_and_cancel_leave_unknown_not_zero_capacity(self):
        result = await self.probe("import time\ntime.sleep(30)", timeout=.1)
        self.assertIsNone(result["blocked"])
        self.assertIn("timed out", result["error"])
        self.cancel.set()
        result = await self.probe("raise SystemExit(8)")
        self.assertIn("cancelled", result["error"])

    async def test_cancel_during_native_probe(self):
        async def stop():
            await asyncio.sleep(.05)
            self.cancel.set()
        stopped = asyncio.create_task(stop())
        result = await self.probe("import time\ntime.sleep(30)")
        await stopped
        self.assertIn("cancelled", result["error"])

    async def test_invalid_or_overlong_rpc_is_unknown(self):
        for code in ('print("not json",flush=True)', 'print("x"*1000,flush=True)'):
            with self.subTest(code=code), patch.object(quota, "MAX_RPC_LINE", 100):
                result = await self.probe(code)
            self.assertIsNone(result["blocked"])
            self.assertIn("unavailable", result["error"])

    async def test_unavailable_subscription_never_starts_rpc(self):
        self.auth.return_value = {"available": False}
        with patch.object(quota.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            result = await quota.probe_codex(self.cancel)
        self.assertIn("sign-in", result["error"])
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
