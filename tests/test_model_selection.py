"""Offline native-catalog -> locked-contract -> worker-dispatch behavior."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from brahma import engine, model_catalog, model_selection
from brahma.config import Settings
from brahma.discovery import Catalog
from brahma.models import Plan, Result, Task
from brahma.session import Session
from brahma.store import Store
from test_cli import run_cli
from test_core import init_repo


def catalogs(codex=("gpt-6.2-sol", "gpt-6.2-astra"), claude=("sonnet", "opus")):
    return {provider: {"provider": provider, "source": provider + ".fixture",
            "observed_at": time.time(), "models": [{"model": name,
                "id": "claude-"+name+"-5-5" if provider == "claude" and name in ("sonnet", "opus", "haiku") else name}
                for name in names],
            "error": None} for provider, names in (("codex", codex), ("claude", claude))}


def proposal(complexity="routine", provider="auto"):
    return Plan("Change an owned file", [Task("Change file", "Update first.txt", ["first.txt"],
                ["The file contains implemented"], provider=provider, complexity=complexity,
                scenarios=["Scenario: Change the owned file\n  Given a base file\n  When the task runs\n  Then its contents are implemented"])],
                checks=[[sys.executable, "-c", "pass"]])


async def available(provider):
    return {"state": "available", "next_at": None, "reason": "fixture available"}


class SelectedModels(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = init_repo(Path(temporary.name) / "repo")
        self.store = Store(Path(temporary.name) / "state")
        self.addCleanup(self.store.close)
        self.session = Session(self.store, self.repo)
        definitions = patch("brahma.discovery.discover", return_value=Catalog())
        definitions.start()
        self.addCleanup(definitions.stop)
        auth = patch("brahma.providers.doctor", AsyncMock(side_effect=AssertionError("No native auth in these tests")))
        auth.start()
        self.addCleanup(auth.stop)

    async def test_catalog_discovery_applies_task_profiles_without_model_generation(self):
        observed = catalogs(codex=("gpt-6.2-astra",), claude=("opus",))
        async def native(provider, cancel):
            self.assertIs(cancel, self.session.cancel)
            return observed[provider]
        with patch("brahma.model_catalog.discover_models", side_effect=native) as discover, \
             patch("brahma.providers.run", AsyncMock(side_effect=AssertionError("Discovery generated a model request"))):
            selection = await self.session.discover_model_profiles()
        settings = self.session.selected_settings(Settings.load(self.repo), selection)
        self.assertEqual(discover.await_count, 2)
        self.assertEqual(settings.codex_model, "gpt-6.2-astra")
        self.assertEqual(settings.claude_model, "claude-opus-5-5")
        self.assertIs(settings._model_selection, selection)
        self.assertIsNone(self.store.session(self.session.key)["plan"])

    async def test_ask_locks_discovered_selection_before_approval(self):
        observed = catalogs()
        proposed = proposal()
        async def native(provider, cancel):
            return observed[provider]
        with patch("brahma.model_catalog.discover_models", side_effect=native), \
             patch("brahma.scheduler.Capacity", return_value=available), \
             patch("brahma.providers.run", AsyncMock(return_value=Result("codex", "ok", json.dumps(proposed.to_dict())))) as run:
            result = await self.session.ask("Update the owned file")
        self.assertEqual(result.to_dict(), proposed.to_dict())
        self.assertEqual(run.await_count, 1)
        self.assertEqual(run.await_args.args[3], "planner")
        planner_settings = run.await_args.args[4]
        self.assertEqual(planner_settings.codex_model, "gpt-6.2-sol")
        saved = self.store.session(self.session.key)
        self.assertEqual(saved["status"], "awaiting_approval")
        self.assertIsNone(saved["approved_digest"])
        contract, settings, _ = self.session.approved_contract()
        self.assertEqual(contract["payload"]["models"]["selection"], planner_settings._model_selection)
        self.assertEqual(settings._model_selection["selected"]["claude_critical_model"], "claude-opus-5-5")

    async def test_reopen_preserves_selected_models_without_rediscovery(self):
        selection = model_catalog.select_profiles(Settings(), catalogs(codex=("gpt-6.2-astra",), claude=("opus",)))
        self.session.set_plan(proposal(), model_selection=selection)
        contract, _, _ = self.session.approved_contract()
        self.store.set(self.session.key, approved_digest=contract["digest"])
        reopened = Session(self.store, self.repo, key=self.session.key)
        with patch("brahma.model_catalog.discover_models", AsyncMock(side_effect=AssertionError("Approved choices must remain locked"))), \
             patch("brahma.providers.run", AsyncMock(side_effect=AssertionError("Validation generated a model request"))):
            restored_contract, settings, _ = reopened.approved_contract()
        self.assertEqual(restored_contract["digest"], contract["digest"])
        self.assertEqual(settings.codex_model, "gpt-6.2-astra")
        self.assertEqual(settings.claude_model, "claude-opus-5-5")
        self.assertEqual(settings._model_selection, selection)

    async def test_configured_profile_drift_rejects_approval_before_worker(self):
        selection = model_catalog.select_profiles(Settings(), catalogs())
        self.session.set_plan(proposal(), model_selection=selection)
        (self.repo / "brahma.toml").write_text('[brahma]\ncodex_model="gpt-6.1-sol"\n')
        with patch("brahma.providers.run", AsyncMock()) as run, \
             patch("brahma.gittools.create_worktree") as worktree:
            with self.assertRaisesRegex(ValueError, "Model settings changed"):
                await self.session.execute()
        run.assert_not_awaited()
        worktree.assert_not_called()
        self.assertEqual(self.store.session(self.session.key)["status"], "awaiting_approval")

    async def execute_model_case(self, plan, selection, gate=None):
        settings = self.session.selected_settings(Settings(checks=plan.checks), selection)
        settings._shared_contract = {"digest": "fixture-lock", "payload": {"feature": "Locked feature"}}
        settings._definition_catalog = []
        calls, events = [], []
        async def worker(provider, prompt, cwd, role, selected_settings, emit, cancel, timeout, **kwargs):
            calls.append((provider, role, selected_settings, prompt))
            if role == "builder":
                (cwd / "first.txt").write_text("implemented\n")
                return Result(provider, "ok", "Edited owned file")
            return Result(provider, "ok", json.dumps({"verdict": "pass", "summary": "Inspected locked diff", "issues": []}))
        with patch("brahma.providers.run", side_effect=worker), \
             patch("brahma.gittools.run_checks", AsyncMock(return_value=(True, "fixture checks: exit 0"))):
            result = await engine.execute(plan, self.repo, settings, events.append, asyncio.Event(), provider_gate=gate)
        return result, calls, events

    async def test_routine_and_critical_models_apply_to_builder_and_independent_reviewer(self):
        selection = model_catalog.select_profiles(Settings(), catalogs())
        for complexity, expected in (("routine", ("gpt-6.2-sol", "claude-sonnet-5-5")),
                                     ("critical", ("gpt-6.2-astra", "claude-opus-5-5"))):
            with self.subTest(complexity=complexity):
                result, calls, _ = await self.execute_model_case(proposal(complexity, "codex"), selection)
                self.assertEqual(result.status, "ready", result.text)
                self.assertEqual([(provider, role) for provider, role, *_ in calls],
                                 [("codex", "builder"), ("claude", "reviewer")])
                self.assertEqual([settings.model_for(provider) for provider, _, settings, _ in calls], list(expected))
                self.assertTrue(all("Shared locked feature fixture-lock" in prompt for *_, prompt in calls))

    async def test_unavailable_critical_builder_or_reviewer_blocks_all_generation(self):
        for observed in (catalogs(codex=("gpt-6.2-sol",)), catalogs(claude=("sonnet",))):
            with self.subTest(observed=observed):
                selection = model_catalog.select_profiles(Settings(), observed)
                result, calls, events = await self.execute_model_case(proposal("critical", "codex"), selection)
                self.assertEqual(result.status, "blocked")
                self.assertEqual(calls, [])
                self.assertIn("critical model", result.text)
                self.assertTrue(any(event.kind == "task_state" and '"state": "blocked"' in event.text for event in events))

    async def test_quota_wait_overrides_preferred_provider_without_downgrading_model(self):
        selection = model_catalog.select_profiles(Settings(), catalogs())
        async def gate(provider):
            if provider == "codex":
                return {"state": "waiting", "next_at": time.time()+3600, "reason": "fixture quota exhausted"}
            return await available(provider)
        result, calls, events = await self.execute_model_case(proposal("critical"), selection, gate)
        self.assertEqual(result.status, "waiting")
        self.assertEqual([(provider, role) for provider, role, *_ in calls], [("claude", "builder")])
        self.assertEqual(calls[0][2].claude_model, "claude-opus-5-5")
        self.assertIn("fixture quota exhausted", result.text)
        self.assertTrue(any(event.kind == "wait" for event in events))

    async def test_settings_for_preserves_locked_context_and_selection(self):
        selection = model_catalog.select_profiles(Settings(), catalogs())
        settings = self.session.selected_settings(Settings(), selection)
        settings._shared_contract = {"digest": "contract", "payload": {"feature": "exact approved scenario"}}
        settings._definition_catalog = []
        derived = engine._settings_for(settings, "codex", "critical")
        self.assertEqual(derived.codex_model, "gpt-6.2-astra")
        self.assertEqual(settings.codex_model, "gpt-6.2-sol")
        for attr in ("_model_selection", "_shared_contract", "_definition_catalog"):
            self.assertIs(getattr(derived, attr), getattr(settings, attr))
        changed = copy.deepcopy(selection)
        changed["configured"]["codex_model"] = "gpt-0-sol"
        with self.assertRaisesRegex(ValueError, "Model settings changed"):
            self.session.selected_settings(Settings(), changed)

    async def test_readable_model_inspection_exposes_unknowns_without_session_mutation(self):
        observed = catalogs(claude=("sonnet",))
        observed["codex"]["error"] = "Catalog temporarily unavailable"
        async def native(provider, cancel):
            return observed[provider]
        before = self.store.session(self.session.key)
        with patch("brahma.model_selection.discover_models", side_effect=native) as discovery, \
             patch("brahma.providers.run", AsyncMock(side_effect=AssertionError("Inspection requested generation"))):
            report = await model_selection.read_models(Settings())
        text = model_selection.format_models(report)
        self.assertEqual(discovery.await_count, 2)
        for phrase in ("without a model request", "unverified", "no permitted model", "no performance data yet", "remaining quota"):
            self.assertIn(phrase, text)
        self.assertEqual(before, self.store.session(self.session.key))
        self.assertEqual(self.store.events(self.session.key), [])


class ModelsCommand(unittest.TestCase):
    def test_plain_models_command_uses_current_repository_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = init_repo(Path(temporary) / "repo")
            (repo / "brahma.toml").write_text('[brahma]\ncodex_model="gpt-6.1-sol"\nclaude_model="haiku"\n')
            report = {"catalogs": [], "selection": {}}
            with patch("brahma.cli.Path.cwd", return_value=repo), \
                 patch("brahma.model_selection.read_models", AsyncMock(return_value=report)) as read, \
                 patch("brahma.model_selection.format_models", return_value="fixture catalog"):
                code, text, error = run_cli("models", state=Path(temporary)/"state")
            self.assertEqual((code, error), (0, ""))
            self.assertIn("fixture catalog", text)
            self.assertEqual(read.await_args.args[0].codex_model, "gpt-6.1-sol")
            self.assertEqual(read.await_args.args[0].claude_model, "haiku")

    def test_models_cli_human_and_json_select_provider_without_workers_or_sessions(self):
        observed = catalogs()
        report = {"catalogs": list(observed.values()), "selection": model_catalog.select_profiles(Settings(), observed)}
        for argv, names in ((["models"], ("codex", "claude")), (["models", "claude", "--json"], ("claude",))):
            with self.subTest(argv=argv), tempfile.TemporaryDirectory() as temporary, \
                 patch("brahma.cli.repository", return_value=Path(temporary)), \
                 patch("brahma.model_selection.read_models", AsyncMock(return_value=report)) as read, \
                 patch("brahma.providers.run", AsyncMock(side_effect=AssertionError("Model command generated work"))), \
                 patch("brahma.session.Session.ask", AsyncMock(side_effect=AssertionError("Inspection created a plan"))):
                state = Path(temporary) / "state"
                code, text, error = run_cli(*argv, state=state)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(read.await_args.args[1], names)
                if "--json" in argv:
                    self.assertEqual(json.loads(text), report)
                else:
                    self.assertIn("Task model profiles", text)
                    self.assertIn("gpt-6.2-sol", text)
                self.assertFalse(state.exists(), "Inspection must not create session state")


if __name__ == "__main__":
    unittest.main()
