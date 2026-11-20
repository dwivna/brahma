"""Independent review round 11 (Codex): one regression per blocking finding."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from brahma import board, engine, gittools, safety, toolchain
from brahma.config import Settings
from brahma.models import Event, Plan, Result, Task
from brahma.store import Store
from tests.test_review10 import SECRET, make_repo

PASS = json.dumps({"verdict": "pass", "summary": "ok", "issues": []})


def engine_patches(**extra):
    """The usual mocked engine environment; individual tests override what they examine."""
    patches = {"context": patch.object(engine, "_context", return_value="ctx"),
               "snapshot": patch.object(gittools, "snapshot_changes", return_value={}),
               "identity": patch.object(gittools, "git_identity", return_value={}),
               "owned": patch.object(gittools, "ownership_changes", return_value=(True, "ok")),
               "checks": patch.object(gittools, "run_checks", AsyncMock(return_value=(True, "ok"))),
               "tree": patch.object(engine, "_tree", return_value={"head": "h", "changes": {}})}
    patches.update(extra)
    return patches


class Finding2CredentialNamedFields(unittest.TestCase):
    def test_a_value_under_a_credential_name_is_hidden_at_any_depth_and_counters_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            row = {"index": 1, "dispute": {"review": {"issues": [{"evidence": {"password": "plain-words-here",
                                                                                "api_key": 12345678}}]}},
                   "task_usage": {"task_1_builder": {"input_tokens": 120, "output_tokens": 7}}}
            store.event(key, Event(kind="task_state", text=json.dumps(row)))
            stored = store.events(key)[-1]["text"]
            self.assertNotIn("plain-words-here", stored)
            self.assertNotIn("12345678", stored)
            self.assertEqual(json.loads(stored)["task_usage"]["task_1_builder"], {"input_tokens": 120, "output_tokens": 7})


class Finding3EveryPlanString(unittest.TestCase):
    def test_a_credential_in_the_feature_is_refused(self):
        from brahma import contracts
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            for plan in (Plan("Goal", [Task("T", "do", ["x.txt"], ["ok"])], feature="Feature: x\n  Scenario: y\n    Given token " + SECRET),):
                with self.assertRaisesRegex(ValueError, "credential-shaped"):
                    contracts.prepare_contract(plan, repo, [["true"]])


class Finding4ApprovedInstructionsPresent(unittest.TestCase):
    def test_a_missing_approved_instruction_file_refuses_and_a_bare_directory_brings_its_own(self):
        import hashlib
        from brahma.context import build_context
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            approved = {"AGENTS.md": hashlib.sha256(b"rules\n").hexdigest()}        # approved, absent from this checkout
            with self.assertRaisesRegex(ValueError, "missing here: AGENTS.md"):
                build_context(repo, ["x.txt"], 20000, approved=approved)
            self.assertIn("deleted within this approved plan",
                          build_context(repo, ["x.txt"], 20000, approved=approved, owned=("AGENTS.md",)))
            (repo / "docs").mkdir()
            (repo / "docs" / "AGENTS.md").write_text("docs rules\n")
            self.assertIn("docs rules", build_context(repo, ["docs"], 20000))


class Finding6ResumeChecksChangeFiles(unittest.IsolatedAsyncioTestCase):
    async def test_checks_that_change_reviewed_files_on_resume_accept_nothing(self):
        trees = iter([{"head": "h", "changes": {"a": "1"}}, {"head": "h", "changes": {"a": "2"}}])
        p = engine_patches(tree=patch.object(engine, "_tree", side_effect=lambda *a: next(trees)))
        with p["context"], p["snapshot"], p["identity"], p["owned"], p["checks"], p["tree"], \
                patch.object(engine.providers, "run", AsyncMock()) as run:
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), lambda e: None, asyncio.Event(), completed_indices={1})
        self.assertEqual(result.status, "incomplete")
        self.assertIn("changed files after they were reviewed (a)", result.text)
        run.assert_not_awaited()

    async def test_an_ownership_failure_on_resume_restores(self):
        p = engine_patches(owned=patch.object(gittools, "ownership_changes", return_value=(False, "outside.txt")))
        with p["context"], p["snapshot"], p["identity"], p["owned"], p["checks"], p["tree"], \
                patch.object(engine, "_restore_note", return_value=" Restored: outside.txt.") as restore:
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), lambda e: None, asyncio.Event(), completed_indices={1})
        self.assertEqual(result.status, "blocked")
        restore.assert_called_once()


class Finding14LongHistories(unittest.TestCase):
    def test_the_board_and_notes_read_the_newest_plan_beyond_any_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            store.event(key, Event(kind="task_state", text=json.dumps({"index": 1, "title": "old plan", "state": "ready"})))
            with store.db:
                store.db.executemany("INSERT INTO events(session,created,provider,role,kind,text,usage) VALUES (?,?,?,?,?,?,?)",
                                     [(key, "t", "brahma", "manager", "message", "x", "{}")] * 21000)
            store.event(key, Event(kind="plan_created", text="{}"))
            store.event(key, Event(kind="task_state", text=json.dumps({"index": 1, "title": "new plan", "state": "building"})))
            self.assertEqual([row.title for row in board.build(store, key).tasks], ["new plan"])


class Finding15EveryInvocationCounted(unittest.TestCase):
    def test_equal_usage_from_separate_invocations_is_never_merged(self):
        usage = engine._Invocations()
        for _ in range(3):
            usage["task_1_attempt_0_reviewer"] = {"input_tokens": 5}
        self.assertEqual(sum(v["input_tokens"] for v in usage.values()), 15)


class Finding17TerminalRedaction(unittest.TestCase):
    def test_text_written_to_the_terminal_ui_is_redacted(self):
        from brahma.ui import BrahmaApp
        shown = []
        log = SimpleNamespace(write=shown.append)
        app = SimpleNamespace(_pane_entries={"manager": []}, query_one=lambda *a: log, refresh_status=lambda: None)
        BrahmaApp.write(app, Event(kind="information", text="skill description with " + SECRET))
        self.assertNotIn(SECRET, shown[0].plain)
        self.assertIn(safety.REDACTED, shown[0].plain)


class Finding18VerifiedAfterEveryWorker(unittest.IsolatedAsyncioTestCase):
    async def test_an_edit_by_the_first_builder_is_caught_before_the_expansion_builder(self):
        calls = []

        async def run(provider, prompt, cwd, role, *args, **kwargs):
            calls.append(role)
            return Result(provider, "ok", "need more context")
        p = engine_patches(owned=patch.object(gittools, "ownership_changes", side_effect=[(False, "outside.txt")]))
        with p["context"], p["snapshot"], p["identity"], p["owned"], p["checks"], p["tree"], \
                patch("brahma.briefs.missing_context", return_value=["lib.py"]), \
                patch("brahma.briefs.expansion", return_value=("lib.py contents", ["lib.py"], [])), \
                patch.object(engine, "_restore_note", return_value=" Restored: outside.txt.") as restore, \
                patch.object(engine.providers, "run", side_effect=run):
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), lambda e: None, asyncio.Event())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(calls, ["builder"])            # the second builder never started
        restore.assert_called_once()

    async def test_an_edit_by_the_reviewer_is_caught_before_a_repair(self):
        calls = []

        async def run(provider, prompt, cwd, role, *args, **kwargs):
            calls.append(role)
            text = json.dumps({"verdict": "changes_requested", "summary": "fix", "issues": ["x"]}) if role == "reviewer" else "built"
            return Result(provider, "ok", text)
        p = engine_patches(owned=patch.object(gittools, "ownership_changes", side_effect=[(True, "ok"), (True, "ok"), (False, "outside.txt")]))
        with p["context"], p["snapshot"], p["identity"], p["owned"], p["checks"], p["tree"], \
                patch.object(engine, "_restore_note", return_value=""), patch.object(engine.providers, "run", side_effect=run):
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), lambda e: None, asyncio.Event())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(calls, ["builder", "reviewer"])  # no repair builder ran after the reviewer's edit


class Finding19SpecialStartupFiles(unittest.TestCase):
    def test_a_fifo_gradle_properties_is_refused_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkfifo(Path(tmp) / "gradle.properties")
            started = time.monotonic()
            self.assertTrue(toolchain._selects_own_jdk(Path(tmp), {"gradle.properties": "x"}))   # assume it might
            self.assertLess(time.monotonic() - started, 2)
            self.assertIsNone(toolchain._fingerprint(Path(tmp) / "gradle.properties"))

    def test_a_fifo_in_the_worktree_is_fingerprinted_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkfifo(Path(tmp) / "pipe")
            self.assertTrue(gittools._fingerprint(Path(tmp) / "pipe").startswith(("absent", "special")))


class Finding20ServiceStateDirectory(unittest.IsolatedAsyncioTestCase):
    async def test_the_service_and_its_git_guard_use_the_custom_state_directory(self):
        from brahma import scheduler
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "custom"
            self.assertEqual(scheduler.service_plist(home)["EnvironmentVariables"]["BRAHMA_STATE_DIR"], str(home))
            seen = {}

            async def inner(path, *, once):
                from brahma.config import state_home
                seen["home"] = state_home()
                return 0
            before = os.environ.get("BRAHMA_STATE_DIR")
            with patch.object(scheduler, "_serve", inner):
                await scheduler.serve(home, once=True)
            self.assertEqual(seen["home"], home)
            self.assertEqual(os.environ.get("BRAHMA_STATE_DIR"), before)       # restored afterwards


if __name__ == "__main__":
    unittest.main()
