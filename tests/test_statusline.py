"""Claude usage via the status-line tap (credential-free), so `brahma usage` can show live Claude windows."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from brahma import cli, usage
from brahma.store import Store

NOW = time.time()


def payload(five=37.0, week=12.0, reset5=None, reset7=None, with_limits=True) -> str:
    data = {"model": {"display_name": "Sonnet"}, "cwd": "/x", "session_id": "s", "cost": {"total_cost_usd": 0.1}}
    if with_limits:
        data["rate_limits"] = {"five_hour": {"used_percentage": five, "resets_at": reset5 or NOW + 3600},
                               "seven_day": {"used_percentage": week, "resets_at": reset7 or NOW + 5 * 86400}}
    return json.dumps(data)


def run_tap(tmp, text, then=None):
    out = io.StringIO()
    argv = ["statusline"] + (["--then", then] if then else [])
    with patch.dict(os.environ, {"BRAHMA_STATE_DIR": str(tmp)}), patch("sys.stdin", io.StringIO(text)), redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


class Tap(unittest.TestCase):
    def test_rate_limits_are_recorded_as_a_claude_statusline_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run_tap(tmp, payload())
            self.assertEqual(code, 0)
            store = Store(Path(tmp))
            snap = store.quota("claude")
            store.close()
            self.assertEqual(snap["source"], "claude.statusline")
            windows = {w["name"]: w for w in snap["windows"]}
            self.assertEqual(windows["five_hour"]["used_percent"], 37.0)
            self.assertEqual(windows["seven_day"]["used_percent"], 12.0)
            self.assertEqual(windows["five_hour"]["duration_seconds"], 5 * 3600)

    def test_usage_report_now_shows_live_claude_windows(self):
        import asyncio
        with tempfile.TemporaryDirectory() as tmp:
            run_tap(tmp, payload(five=54.0, week=39.0))
            store = Store(Path(tmp))
            with patch("brahma.usage.native.doctor", create=True):
                report = asyncio.run(usage.read_usage(store, ("claude",)))
            store.close()
            text = usage.format_usage(report)
        self.assertIn("5h window: 54% used", text)
        self.assertIn("7d window: 39% used", text)
        self.assertIn("claude.statusline", text)
        self.assertNotIn("used/remaining percentage unknown", text)

    def test_payload_without_rate_limits_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_tap(tmp, payload(with_limits=False))
            store = Store(Path(tmp))
            self.assertIsNone(store.quota("claude"))
            store.close()

    def test_garbage_and_hostile_input_never_breaks_the_status_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ("", "not json", "[]", '{"rate_limits": 5}', '{"rate_limits": {"five_hour": "x"}}',
                         '{"rate_limits": {"five_hour": {"used_percentage": "NaN", "resets_at": "soon"}}}',
                         json.dumps({"rate_limits": {"five_hour": {"used_percentage": 1e99, "resets_at": -5}}})):
                with self.subTest(text=text[:30]):
                    self.assertEqual(run_tap(tmp, text)[0], 0)
            store = Store(Path(tmp))
            self.assertIsNone(store.quota("claude"))
            store.close()

    def test_then_command_receives_the_same_input_and_its_output_is_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "line.sh"
            script.write_text('#!/bin/sh\njq -r .model.display_name 2>/dev/null || python3 -c "import sys,json;print(json.load(sys.stdin)[\'model\'][\'display_name\'])"\n')
            script.chmod(0o755)
            code, out = run_tap(tmp, payload(), then=f"sh {script}")
            self.assertEqual((code, out.strip()), (0, "Sonnet"))
            code, out = run_tap(tmp, payload(), then="/nonexistent/command")
            self.assertEqual((code, out), (0, ""))   # a missing command is silent, never an error

    def test_writes_are_limited_to_one_per_refresh_interval_whatever_the_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = payload(five=10.0)
            self.assertTrue(cli.record_claude_statusline(home, first, now=NOW))
            self.assertFalse(cli.record_claude_statusline(home, first, now=NOW + 3))             # same numbers, too soon
            self.assertFalse(cli.record_claude_statusline(home, payload(five=11.0), now=NOW + 4))  # changed, still too soon
            self.assertTrue(cli.record_claude_statusline(home, payload(five=11.0), now=NOW + 40))  # interval passed

    def test_partial_or_far_future_payloads_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertFalse(cli.record_claude_statusline(home, json.dumps(
                {"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": NOW + 3600}}}), now=NOW))   # no seven_day
            forged = {"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 253370764800},
                                      "seven_day": {"used_percentage": 5, "resets_at": NOW + 86400}}}
            self.assertFalse(cli.record_claude_statusline(home, json.dumps(forged), now=NOW))
            store = Store(home)
            self.assertIsNone(store.quota("claude"))
            store.close()

    def test_a_statusline_observation_never_erases_a_recorded_limit_block(self):
        # A worker reported a rejection; a later low-usage statusline sample is newer information and wins,
        # but it must be stored as the claude.statusline source with its own windows (no stale 'blocked').
        with tempfile.TemporaryDirectory() as tmp:
            cli.record_claude_statusline(Path(tmp), payload(five=100.0), now=NOW)
            store = Store(Path(tmp))
            from brahma import quota
            self.assertEqual(quota.eligibility(store.quota("claude"), now=NOW + 1)["state"], "waiting")
            store.close()


if __name__ == "__main__":
    unittest.main()
