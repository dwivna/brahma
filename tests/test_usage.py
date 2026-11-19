import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import quota
from brahma.store import Store
from brahma.usage import format_usage, read_usage, usage_request


def ready(provider):
    return {"provider": provider, "installed": True, "signed_in": True,
            "subscription": True, "available": True, "state": "ready", "action": ""}


def codex_observation(now, used=20, reset=None):
    return quota.normalize_codex({"rateLimits": {
        "primary": {"usedPercent": used, "windowDurationMins": 300,
                    "resetsAt": now + 3600 if reset is None else reset},
        "secondary": {"usedPercent": 35, "windowDurationMins": 10080,
                      "resetsAt": now + 86400}}}, observed_at=now)


class UsageIntentTests(unittest.TestCase):
    def test_screenshot_questions_and_provider_selection(self):
        examples = {
            "codex and claude both available usage limits": ("codex", "claude"),
            "yes i want current usage limits and remaining capacity for both claude and codex": ("codex", "claude"),
            "/usage": ("codex", "claude"),
            "/usage CODEX": ("codex",),
            "/usage claude": ("claude",),
            "How much quota is left for Claude?": ("claude",),
            "Show remaining capacity": ("codex", "claude"),
            "Could you give me my current quota?": ("codex", "claude"),
            "Show used and remaining quota for Codex and Claude": ("codex", "claude"),
            "What are Claude's current usage limits?": ("claude",),
            "Are codex and claude available?": ("codex", "claude"),
        }
        for question, providers in examples.items():
            with self.subTest(question=question):
                self.assertEqual(usage_request(question), providers)

    def test_feature_work_is_never_consumed_as_read_only(self):
        for request in ("implement quota routing", "add a usage command",
                        "fix the quota display", "show usage in a new dashboard",
                        "plan how to handle usage limits", "Show usage then fix the app",
                        "How can Brahma handle usage limits?", "show remaining quota in the header",
                        "create a quota report", "/usage other", "/usage codex claude",
                        "usage\nimplement routing", "continue", "yes"):
            with self.subTest(request=request):
                self.assertIsNone(usage_request(request))

    def test_mixed_actions_code_references_and_explanations_are_not_status(self):
        for request in (
            "Show remaining quota and update README.md",
            "Show remaining quota, then refactor engine.py",
            "How does Brahma determine available usage limits?",
            "How will remaining quota affect provider selection?",
            "Show current usage and update the documentation",
            "Show quota and run the checks",
            "Show quota and continue the work",
            "Show quota and inspect the repo",
            "Show usage for src/brahma/engine.py",
            "Show capacity from ~/Anvi",
            "Show quota in Makefile",
            "What usage is shown by the quota code?",
            "Show quota; resume my task",
        ):
            with self.subTest(request=request):
                self.assertIsNone(usage_request(request))


class ReadUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.addCleanup(self.store.close)
        self.now = time.time()
        self.auth = self.enterContext(patch("brahma.usage.native.doctor", new_callable=AsyncMock,
                                            side_effect=ready))
        self.probe = self.enterContext(patch("brahma.usage.quota.probe_codex", new_callable=AsyncMock,
                                             return_value=quota.unknown("codex", "codex.app_server", "unavailable")))
        self.worker = self.enterContext(patch("brahma.providers.run", new_callable=AsyncMock,
                                              side_effect=AssertionError("A usage query must not start a worker")))

    async def asyncTearDown(self):
        self.worker.assert_not_called()

    async def test_fresh_quota_is_measured_and_only_quota_state_changes(self):
        session = self.store.create(Path(self.temp.name))
        self.store.set(session, status="awaiting_approval", plan='{"tasks":[]}',
                       contract="locked-contract", approved_digest="approved-digest")
        self.store.enqueue(session, "generation", "approved-digest", self.now + 1800, "quota wait")
        before = (self.store.session(session), self.store.pending(), self.store.events(session))
        self.probe.return_value = codex_observation(self.now)
        report = await read_usage(self.store)
        self.assertEqual((self.store.session(session), self.store.pending(), self.store.events(session)), before)
        self.assertFalse((self.store.home / "worker.lock").exists())
        codex, claude = report["providers"]
        self.assertEqual(codex["origin"], "refreshed")
        self.assertEqual(codex["freshness"], "fresh")
        self.assertEqual(codex["quota"]["windows"][0]["used_percent"], 20)
        self.assertEqual(self.store.quota("codex"), codex["quota"])
        self.assertIsNone(claude["quota"])
        output = format_usage(report)
        self.assertIn("20% used, 80% remaining", output)
        self.assertIn("5h window", output)
        self.assertIn("7d window", output)
        self.assertIn("UTC", output)
        self.assertIn("codex.app_server", output)
        self.assertIn("0s ago", output)
        self.assertIn("Remaining capacity unknown", output)
        self.assertIn("Request token counters are not remaining subscription allowance", output)
        self.probe.assert_awaited_once()

    async def test_unavailable_refresh_preserves_cached_block_even_if_stale(self):
        cached = codex_observation(self.now - 800, used=100, reset=self.now + 1200)
        self.store.observe(cached)
        report = await read_usage(self.store, ("codex",))
        row = report["providers"][0]
        self.assertEqual(self.store.quota("codex"), cached)
        self.assertEqual(row["origin"], "cached")
        self.assertEqual(row["freshness"], "stale")
        self.assertEqual(row["eligibility"]["state"], "waiting")
        output = format_usage(report)
        self.assertIn("Last reported 5h window: 100% used, 0% remaining", output)
        self.assertIn("Current remaining capacity unknown (observation is stale)", output)
        self.assertIn("existing observations retained", output)

    async def test_worker_limit_observation_retains_known_rejection_without_inventing_quota(self):
        snapshot = quota.unknown("claude", "worker.limit")
        snapshot["blocked"] = True
        self.store.observe(snapshot)
        report = await read_usage(self.store, ("claude",))
        text = format_usage(report)
        self.assertTrue(report["providers"][0]["quota"]["blocked"])
        self.assertIn("provider limit reached", text)
        self.assertIn("percentage unknown", text)
        self.assertNotIn("0%", text)

    async def test_unknown_never_becomes_zero_or_free(self):
        report = await read_usage(self.store)
        output = format_usage(report)
        self.assertNotIn("0%", output)
        self.assertNotIn("free", output.lower())
        self.assertEqual([row["origin"] for row in report["providers"]], ["unavailable", "unavailable"])
        self.assertIsNone(self.store.quota("codex"))

    async def test_cache_without_refresh_is_explicitly_last_reported(self):
        self.store.observe(codex_observation(self.now))
        report = await read_usage(self.store, ("codex",), refresh=False)
        self.probe.assert_not_called()
        self.assertEqual(report["providers"][0]["freshness"], "fresh")
        output = format_usage(report)
        self.assertIn("Last reported 5h window", output)
        self.assertIn("Current remaining capacity unknown", output)

    async def test_past_reset_requires_new_observation_and_is_not_current(self):
        cached = codex_observation(self.now - 20, used=100, reset=self.now - 5)
        self.store.observe(cached)
        report = await read_usage(self.store, ("codex",), refresh=False)
        self.assertEqual(report["providers"][0]["freshness"], "reset_passed")
        self.assertIn("reported reset has passed", format_usage(report))
        self.assertIn("Last reported", format_usage(report))

    async def test_live_unknown_or_invalid_data_does_not_erase_known_data(self):
        cached = codex_observation(self.now, used=100)
        self.store.observe(cached)
        for fresh in (quota.normalize_codex({}, observed_at=self.now),
                      quota.normalize_codex({"rateLimits": {"primary": {}}}, observed_at=self.now),
                      codex_observation(self.now + 60),
                      {**quota.unknown("codex", "codex.app_server", observed_at=self.now), "blocked": False},
                      {**codex_observation(self.now), "provider": "claude"},
                      {**codex_observation(self.now), "source": "private@example.com"}):
            with self.subTest(fresh=fresh):
                self.probe.return_value = fresh
                report = await read_usage(self.store, ("codex",))
                self.assertEqual(report["providers"][0]["origin"], "cached")
                self.assertEqual(self.store.quota("codex"), cached)

    async def test_claude_uses_saved_observations_only_without_unit_guessing(self):
        self.store.observe(quota.normalize_claude({"status": "allowed", "utilization": 0.7,
                                                  "resetsAt": self.now + 600}, observed_at=self.now))
        report = await read_usage(self.store, ("claude",))
        self.probe.assert_not_called()
        output = format_usage(report)
        self.assertIn("Last reported", output)
        self.assertIn("duration unknown", output)
        self.assertIn("unspecified units", output)
        self.assertIn("Current remaining capacity unknown", output)
        self.assertNotIn("70%", output)
        self.assertNotIn("0.7%", output)
        self.assertNotIn("5h window", output)
        self.assertNotIn("7d window", output)

    async def test_claude_saved_percentages_keep_source_age_and_actual_windows(self):
        self.store.observe(quota.normalize_claude({
            "five_hour": {"used_percentage": 42, "resets_at": self.now + 100},
            "seven_day": {"used_percentage": 10, "resets_at": self.now + 86400}},
            observed_at=self.now - 60))
        report = await read_usage(self.store, ("claude",))
        output = format_usage(report)
        self.assertIn("claude.statusline", output)
        self.assertIn("1m ago", output)
        self.assertIn("Last reported 5h window: 42% used, 58% remaining", output)
        self.assertIn("Last reported 7d window: 10% used, 90% remaining", output)
        self.assertIn("no independent live quota poll", output)
        self.probe.assert_not_called()

    async def test_readiness_actions_do_not_expose_native_identity_or_error_output(self):
        self.auth.return_value = {"provider": "codex", "installed": True,
                                  "signed_in": False, "subscription": False, "state": "signed_out",
                                  "email": "secret@example.com", "action": "secret@example.com",
                                  "error": "access_token=secret", "raw": "private identity"}
        self.auth.side_effect = None
        report = await read_usage(self.store, ("codex",))
        value = json.dumps(report) + format_usage(report)
        for secret in ("secret@example.com", "access_token", "private identity"):
            self.assertNotIn(secret, value)
        self.assertIn("codex login", value)
        self.probe.assert_not_called()

    async def test_missing_cli_is_reported_without_quota_probe(self):
        self.auth.side_effect = None
        self.auth.return_value = {"installed": False, "signed_in": False,
                                  "subscription": False, "state": "missing"}
        report = await read_usage(self.store, ("codex",))
        self.assertIn("not installed", format_usage(report))
        self.assertIn("Install the codex CLI", format_usage(report))
        self.probe.assert_not_called()

    async def test_refresh_error_and_extra_snapshot_fields_do_not_leak(self):
        observation = codex_observation(self.now)
        observation.update(email="secret@example.com", raw="credential output")
        observation["windows"][0]["name"] = "private@example.com"
        self.probe.return_value = observation
        report = await read_usage(self.store, ("codex",))
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn("credential", json.dumps(report))
        self.assertNotIn("private@", json.dumps(report))
        self.probe.side_effect = RuntimeError("private@example.com: access_token=secret")
        failed = await read_usage(self.store, ("codex",))
        self.assertNotIn("private@", json.dumps(failed))
        self.assertNotIn("access_token", json.dumps(failed))
        self.assertEqual(failed["providers"][0]["origin"], "cached")

    async def test_cancel_auth_cleans_up_and_stops_following_checks(self):
        cancel, started, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def blocked_auth(provider):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.auth.side_effect = blocked_auth
        running = asyncio.create_task(read_usage(self.store, cancel=cancel))
        await asyncio.wait_for(started.wait(), 1)
        cancel.set()
        report = await asyncio.wait_for(running, 1)
        self.assertTrue(cleaned.is_set())
        self.assertTrue(report["cancelled"])
        self.assertTrue(all(row["cancelled"] for row in report["providers"]))
        self.assertEqual(self.auth.await_count, 1)
        self.probe.assert_not_called()

    async def test_cancel_probe_cleans_up_preserves_cached_quota_and_approval(self):
        cached = codex_observation(self.now, used=100)
        self.store.observe(cached)
        session = self.store.create(Path(self.temp.name))
        self.store.set(session, status="awaiting_approval", contract="locked", approved_digest="digest")
        before = self.store.session(session)
        cancel, started, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def blocked_probe(event):
            self.assertIs(event, cancel)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.probe.side_effect = blocked_probe
        running = asyncio.create_task(read_usage(self.store, cancel=cancel))
        await asyncio.wait_for(started.wait(), 1)
        cancel.set()
        report = await asyncio.wait_for(running, 1)
        self.assertTrue(cleaned.is_set())
        self.assertTrue(report["cancelled"])
        self.assertEqual(self.store.quota("codex"), cached)
        self.assertEqual(self.store.session(session), before)
        self.assertEqual(self.auth.await_count, 1)

    async def test_outer_task_cancellation_also_awaits_cleanup(self):
        started, cleaned = asyncio.Event(), asyncio.Event()
        async def blocked_auth(provider):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.auth.side_effect = blocked_auth
        running = asyncio.create_task(read_usage(self.store))
        await asyncio.wait_for(started.wait(), 1)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertTrue(cleaned.is_set())
        self.probe.assert_not_called()

    async def test_pre_cancelled_query_never_checks_native_tools(self):
        cancel = asyncio.Event()
        cancel.set()
        report = await read_usage(self.store, cancel=cancel)
        self.auth.assert_not_called()
        self.probe.assert_not_called()
        self.assertTrue(report["cancelled"])

    async def test_invalid_provider_selection_is_rejected_before_native_calls(self):
        for selected in ((), ("other",), ("codex", "codex")):
            with self.assertRaises(ValueError):
                await read_usage(self.store, selected)
        self.auth.assert_not_called()
        self.probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
