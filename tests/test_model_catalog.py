from __future__ import annotations

import asyncio
import json
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma.config import Settings
from brahma import model_catalog as catalog


class NativeCatalogs(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transcript = self.root / "requests.jsonl"
        self.executable = self.root / "fake-cli"

    def fake(self, behavior="normal"):
        script = """import json, sys, time
from pathlib import Path
record = Path(RECORD)
record.write_text(json.dumps({'argv':sys.argv[1:]})+'\\n')
for line in sys.stdin:
    value = json.loads(line)
    with record.open('a') as stream:
        stream.write(json.dumps(value)+'\\n')
    if BEHAVIOR == 'hang':
        time.sleep(20)
    if BEHAVIOR == 'oversize':
        sys.stdout.write('x'*4096)
        sys.stdout.flush()
        time.sleep(20)
    if BEHAVIOR == 'stderr':
        sys.stderr.write('x'*4096)
        sys.stderr.flush()
        time.sleep(.02)
    if BEHAVIOR == 'bad':
        print('not json', flush=True)
        continue
    if '--print' in sys.argv:
        result = {'models': [
            {'value':'opus','resolvedModel':'claude-opus-5-5', 'displayName':'Opus',
             'description':'Complex tasks', 'supportedEffortLevels':['low','high'],
             'secret':'do-not-retain'},
            {'value':'sonnet','resolvedModel':'claude-sonnet-5-5', 'displayName':'Sonnet'}],
            'account':{'email':'private@example.test','apiKey':'never-retain'}}
        response = {'type':'control_response','response':{'subtype':'success',
            'request_id':value['request_id'],'response':result}}
    else:
        method = value.get('method')
        if method == 'initialized':
            continue
        if method == 'initialize':
            result = {}
        else:
            first = not value['params'].get('cursor')
            result = {'data': [{'id':'gpt-6.2-sol' if first else 'gpt-6.2-astra',
                'model':'gpt-6.2-sol' if first else 'gpt-6.2-astra',
                'displayName':'A model\\x1b[31m', 'description':'Advertised model',
                'defaultReasoningEffort':'medium',
                'supportedReasoningEfforts':[{'reasoningEffort':'medium'}],
                'isDefault':first, 'account':'never-retain'}],
                'nextCursor':'page2' if first or BEHAVIOR == 'loop' else None}
        response = {'id':value['id'],'result':result}
    print(json.dumps(response), flush=True)
"""
        script = script.replace("RECORD", repr(str(self.transcript))).replace("BEHAVIOR", repr(behavior))
        self.executable.write_text(f"#!{sys.executable}\n" + script)
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)

    async def discover(self, provider="codex", **kwargs):
        with patch.object(catalog.providers, "doctor", AsyncMock(return_value={"available": True})), \
             patch.object(catalog.shutil, "which", return_value=str(self.executable)), \
             patch.object(catalog.providers, "run", AsyncMock(side_effect=AssertionError("No model requests"))):
            return await catalog.discover_models(provider, **kwargs)

    def requests(self):
        return [json.loads(line) for line in self.transcript.read_text().splitlines()]

    async def test_codex_paginates_without_a_thread_or_turn(self):
        self.fake()
        report = await self.discover()
        self.assertIsNone(report["error"])
        self.assertEqual([row["model"] for row in report["models"]], ["gpt-6.2-sol", "gpt-6.2-astra"])
        self.assertEqual(report["models"][0]["display_name"], "A model")
        self.assertNotIn("never-retain", json.dumps(report))
        requests = self.requests()
        self.assertEqual([row["method"] for row in requests[1:]], ["initialize", "initialized", "model/list", "model/list"])
        self.assertEqual(requests[-1]["params"]["cursor"], "page2")
        self.assertFalse(requests[-1]["params"]["includeHidden"])
        self.assertIn('forced_login_method="chatgpt"', requests[0]["argv"])

    async def test_claude_only_initializes_and_discards_account_data(self):
        self.fake()
        report = await self.discover("claude")
        self.assertIsNone(report["error"])
        self.assertEqual(report["models"][0]["id"], "claude-opus-5-5")
        self.assertEqual(report["models"][0]["model"], "opus")
        self.assertNotIn("private@example", json.dumps(report))
        self.assertNotIn("never-retain", json.dumps(report))
        self.assertNotIn("do-not-retain", json.dumps(report))
        requests = self.requests()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["type"], "control_request")
        self.assertEqual(requests[1]["request"]["subtype"], "initialize")
        argv = requests[0]["argv"]
        for flag in ("--restricted", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    async def test_missing_signin_does_not_spawn(self):
        with patch.object(catalog.providers, "doctor", AsyncMock(return_value={"available": False})), \
             patch.object(catalog.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            report = await catalog.discover_models("claude")
        self.assertIn("sign-in", report["error"])
        spawn.assert_not_awaited()

    async def test_malformed_readiness_is_unavailable_without_spawning(self):
        for readiness in (None, [], "ready", {"available": "true"}, {"available": 1}):
            with self.subTest(readiness=readiness), \
                 patch.object(catalog.providers, "doctor", AsyncMock(return_value=readiness)), \
                 patch.object(catalog.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
                report = await catalog.discover_models("claude")
                self.assertIn("sign-in", report["error"])
                self.assertEqual(report["models"], [])
                spawn.assert_not_awaited()

    async def test_deadline_and_cancel_include_auth(self):
        async def auth(provider):
            await asyncio.sleep(20)
        with patch.object(catalog.providers, "doctor", auth), \
             patch.object(catalog.asyncio, "create_subprocess_exec", AsyncMock()) as spawn:
            report = await catalog.discover_models("codex", timeout=.01)
            self.assertIn("timed out", report["error"])
            cancel = asyncio.Event()
            task = asyncio.create_task(catalog.discover_models("claude", cancel))
            await asyncio.sleep(.01)
            cancel.set()
            report = await task
            self.assertIn("cancelled", report["error"])
        spawn.assert_not_awaited()

    async def test_cancel_running_native_process(self):
        self.fake("hang")
        cancel = asyncio.Event()
        task = asyncio.create_task(self.discover(cancel=cancel))
        for _ in range(100):
            if self.transcript.exists():
                break
            await asyncio.sleep(.01)
        cancel.set()
        report = await asyncio.wait_for(task, 3)
        self.assertIn("cancelled", report["error"])

    async def test_external_task_cancellation_reaps_owned_process(self):
        self.fake("hang")
        stop = catalog.providers._stop
        with patch.object(catalog.providers, "_stop", AsyncMock(wraps=stop)) as cleanup:
            task = asyncio.create_task(self.discover())
            for _ in range(100):
                if self.transcript.exists():
                    break
                await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            cleanup.assert_awaited_once()
            self.assertIsNotNone(cleanup.await_args.args[0].returncode)

    async def test_output_protocol_and_pagination_are_bounded(self):
        for mode in ("oversize", "stderr", "bad", "loop"):
            with self.subTest(mode=mode):
                self.fake(mode)
                with patch.object(catalog, "MAX_LINE_BYTES", 2048), patch.object(catalog, "MAX_OUTPUT_BYTES", 2048):
                    report = await self.discover(timeout=2)
                self.assertTrue(report["error"])
                self.assertEqual(report["models"], [])

    async def test_pre_cancelled_and_invalid_timeout(self):
        cancel = asyncio.Event()
        cancel.set()
        with patch.object(catalog.providers, "doctor", AsyncMock()) as doctor:
            self.assertIn("cancelled", (await catalog.discover_models("codex", cancel))["error"])
            for timeout in (0, -1, float("nan"), True):
                self.assertIn("timed out", (await catalog.discover_models("codex", timeout=timeout))["error"])
            doctor.assert_not_awaited()


class ProfileSelection(unittest.TestCase):
    @staticmethod
    def observed(provider, names, **kwargs):
        return {"provider": provider, "source": "native.fixture", "observed_at": time.time(),
                "models": [{"model": name, "id": name} for name in names], "error": None, **kwargs}

    def test_configured_profiles_win_over_new_catalog_default(self):
        settings = Settings()
        result = catalog.select_profiles(settings, {
            "codex": self.observed("codex", ["gpt-6.2-sol", "gpt-6.2-astra", "gpt-999-astra"]),
            "claude": self.observed("claude", ["sonnet", "opus", "default", "claude-fable-5-2[1m]"])})
        self.assertEqual(result["selected"], result["configured"])
        self.assertTrue(all(row["verified"] for row in result["profiles"].values()))

    def test_missing_critical_never_downgrades_to_routine_model(self):
        result = catalog.select_profiles(Settings(), {
            "codex": self.observed("codex", ["gpt-6.2-sol"]),
            "claude": self.observed("claude", ["sonnet"])})
        self.assertIsNone(result["selected"]["codex_critical_model"])
        self.assertIsNone(result["selected"]["claude_critical_model"])
        self.assertFalse(result["profiles"]["claude_critical_model"]["available"])
        self.assertIn("critical work cannot", result["profiles"]["claude_critical_model"]["reason"])

    def test_missing_routine_may_use_configured_advertised_critical_model(self):
        result = catalog.select_profiles(Settings(), {
            "codex": self.observed("codex", ["gpt-6.2-astra"]),
            "claude": self.observed("claude", ["opus"])})
        self.assertEqual(result["selected"]["codex_model"], "gpt-6.2-astra")
        self.assertEqual(result["selected"]["claude_model"], "opus")
        self.assertIn("configured critical", result["profiles"]["claude_model"]["reason"])

    def test_catalog_failure_and_staleness_keep_explicit_unknown(self):
        settings = Settings()
        for values in ({}, {"codex": self.observed("codex", [], error="offline")},
                       {"codex": self.observed("codex", [], observed_at=time.time()-301)}):
            with self.subTest(values=values):
                result = catalog.select_profiles(settings, values)
                self.assertEqual(result["selected"], result["configured"])
                self.assertFalse(any(row["verified"] for row in result["profiles"].values()))
                self.assertTrue(all(row["available"] is None for row in result["profiles"].values()))

    def test_unsupported_and_credit_models_cannot_become_fallbacks(self):
        result = catalog.select_profiles(Settings(), {
            "codex": self.observed("codex", ["gpt-999-luna"]),
            "claude": self.observed("claude", ["default", "claude-fable-5-2[1m]"])})
        self.assertTrue(all(value is None for value in result["selected"].values()))
        self.assertTrue(all(row["available"] is False for row in result["profiles"].values()))

    def test_installed_codex_catalog_supersedes_unadvertised_configured_versions(self):
        observed = self.observed("codex", ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"])
        result = catalog.select_profiles(Settings(), {"codex": observed})
        self.assertEqual(result["selected"]["codex_model"], "gpt-6-sol")
        self.assertEqual(result["selected"]["codex_critical_model"], "gpt-6-astra")
        self.assertEqual(result["profiles"]["codex_critical_model"]["resolved_model"], "gpt-6-astra")
        self.assertIn("same task family", result["profiles"]["codex_model"]["reason"])

    def test_family_default_precedes_numeric_version_without_crossing_quality_family(self):
        observed = self.observed("codex", ["gpt-6-sol", "gpt-7-sol", "gpt-6-astra", "gpt-99-astra"])
        observed["models"][0]["is_default"] = True
        result = catalog.select_profiles(Settings(), {"codex": observed})
        self.assertEqual(result["selected"]["codex_model"], "gpt-6-sol")
        self.assertEqual(result["selected"]["codex_critical_model"], "gpt-99-astra")

    def test_claude_alias_is_resolved_to_exact_model_before_contract_lock(self):
        observed = self.observed("claude", [])
        observed["models"] = [{"model": "sonnet", "id": "claude-sonnet-5-5"},
                              {"model": "opus", "id": "claude-opus-5-5"}]
        result = catalog.select_profiles(Settings(), {"claude": observed})
        self.assertEqual(result["selected"]["claude_model"], "claude-sonnet-5-5")
        self.assertEqual(result["selected"]["claude_critical_model"], "claude-opus-5-5")
        self.assertEqual(result["profiles"]["claude_model"]["configured"], "sonnet")
        self.assertEqual(result["profiles"]["claude_model"]["resolved_model"], "claude-sonnet-5-5")

    def test_same_family_version_order_is_numeric_and_stable(self):
        names = ["gpt-5.9-sol", "gpt-5.10-sol", "gpt-5.6-astra"]
        for order in (names, list(reversed(names))):
            result = catalog.select_profiles(Settings(), {"codex": self.observed("codex", order)})
            self.assertEqual(result["selected"]["codex_model"], "gpt-5.10-sol")

    def test_alias_model_family_must_match_resolved_id_family(self):
        observed = self.observed("codex", [])
        observed["models"] = [{"model": "gpt-6.2-astra", "id": "gpt-6.2-sol"}]
        result = catalog.select_profiles(Settings(), {"codex": observed})
        self.assertIsNone(result["selected"]["codex_critical_model"])

    def test_catalog_failure_does_not_allow_unsafe_or_unfamiliar_configured_models(self):
        for model in ("default", "fable", "claude-fable-5-2[1m]", "future-special-model"):
            settings = Settings(claude_model=model, claude_critical_model=model)
            for values in ({}, {"claude": self.observed("claude", [], error="offline")}):
                with self.subTest(model=model, values=values):
                    result = catalog.select_profiles(settings, values)
                    for field in ("claude_model", "claude_critical_model"):
                        self.assertIsNone(result["selected"][field])
                        self.assertFalse(result["profiles"][field]["available"])
                        self.assertIn("unsupported", result["profiles"][field]["reason"])

    def test_full_resolved_id_matches_configured_model(self):
        settings = Settings(claude_model="claude-sonnet-5-5", claude_critical_model="claude-opus-5-5")
        observed = self.observed("claude", [])
        observed["models"] = [{"model": "sonnet", "id": "claude-sonnet-5-5"},
                              {"model": "opus", "id": "claude-opus-5-5"}]
        result = catalog.select_profiles(settings, {"claude": observed})
        self.assertEqual(result["selected"]["claude_model"], "claude-sonnet-5-5")
        self.assertTrue(result["profiles"]["claude_model"]["verified"])

    def test_safe_alias_cannot_resolve_to_a_credit_model(self):
        observed = self.observed("claude", [])
        observed["models"] = [{"model": "sonnet", "id": "claude-fable-5-2"},
                              {"model": "opus", "id": "claude-fable-5-2"}]
        result = catalog.select_profiles(Settings(), {"claude": observed})
        self.assertIsNone(result["selected"]["claude_model"])
        self.assertIsNone(result["selected"]["claude_critical_model"])


if __name__ == "__main__":
    unittest.main()
