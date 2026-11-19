import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from textual.widgets import Button, Input, RichLog, Static

from brahma.models import Event, Plan, Task
from brahma.ui import BrahmaApp
from test_core import init_repo


class Terminal(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.doctor = AsyncMock(return_value={"installed": True, "signed_in": True,
                                             "subscription": True, "available": True,
                                             "state": "ready", "action": "", "error": None})
        self.auth_patch = patch("brahma.providers.doctor", self.doctor)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)

    @staticmethod
    def pane(app, name):
        return "\n".join(strip.text for strip in app.query_one("#" + name, RichLog).lines)

    async def test_usage_conversation_bypasses_planner_and_preserves_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            app = BrahmaApp(root, home=Path(tmp)/"state")
            try:
                with patch("brahma.ui.read_usage", new_callable=AsyncMock, return_value={"fixture": True}) as read, \
                        patch("brahma.ui.format_usage", return_value="Codex: 40% remaining. Claude: unknown."), \
                        patch("brahma.providers.run", side_effect=AssertionError("usage called a model")):
                    async with app.run_test(size=(120, 40)) as pilot:
                        await pilot.pause()
                        app.session.set_plan(Plan("Existing work", [Task("task", "change", ["first.txt"], ["correct"])]))
                        before = app.store.session(app.session.key)
                        generation = app.session.generation
                        for text in ("codex and claude both available usage limits",
                                     "yes i want current usage limits and remaining capacity for both claude and codex"):
                            app.query_one("#prompt", Input).value = text
                            await pilot.press("enter")
                            await pilot.pause()
                        self.assertEqual(read.await_count, 2)
                        self.assertEqual(app.store.session(app.session.key), before)
                        self.assertEqual(app.session.generation, generation)
                        self.assertEqual(app.store.pending(), [])
                        self.assertFalse(any(row["kind"] == "user" for row in app.store.events(app.session.key)))
                        pane = self.pane(app, "manager")
                        self.assertEqual(pane.count("Claude: unknown."), 2)
                        self.assertNotIn("empty plan", pane)
                        self.assertFalse(app.query_one("#approve", Button).disabled)
            finally:
                app.store.close()

    async def test_usage_inspection_does_not_replace_active_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BrahmaApp(home=Path(tmp)/"state")
            blocker = None
            try:
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    blocker = asyncio.create_task(asyncio.Event().wait())
                    app.job = blocker
                    with patch("brahma.ui.read_usage", new_callable=AsyncMock, return_value={}), \
                            patch("brahma.ui.format_usage", return_value="Quota report"):
                        app.query_one("#prompt", Input).value = "/usage"
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertIs(app.job, blocker)
                        self.assertFalse(blocker.done())
                        self.assertIn("Quota report", self.pane(app, "manager"))
            finally:
                if blocker:
                    blocker.cancel()
                    await asyncio.gather(blocker, return_exceptions=True)
                app.store.close()

    async def test_internal_protocol_hidden_but_readable_plan_and_usage_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BrahmaApp(home=Path(tmp)/"state", demo=True)
            try:
                async with app.run_test(size=(120, 40)) as pilot:
                    for event in (Event(kind="plan", text='{"summary":"RAW_JSON"}'),
                                  Event(kind="plan_created", text="INTERNAL_GENERATION"),
                                  Event("codex", "planner", text='{"summary":"RAW_JSON"}'),
                                  Event("codex", "planner", "native_session", "NATIVE_ID"),
                                  Event("codex", "planner", "usage", usage={"input_tokens": 40, "output_tokens": 2})):
                        app.write(event)
                    app.show_plan(Plan("Readable goal", [], ["Which file?"]))
                    await pilot.pause()
                    combined = self.pane(app, "manager") + self.pane(app, "codex")
                    for hidden in ("RAW_JSON", "INTERNAL_GENERATION", "NATIVE_ID"):
                        self.assertNotIn(hidden, combined)
                    self.assertEqual(combined.count("Readable goal"), 1)
                    self.assertIn("Which file?", combined)
                    self.assertEqual((app.inputs, app.outputs), (40, 2))
            finally:
                app.store.close()

    async def test_usage_demo_never_probes_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BrahmaApp(home=Path(tmp)/"state", demo=True)
            try:
                with patch("brahma.ui.read_usage", side_effect=AssertionError("demo queried account")), \
                        patch("brahma.model_selection.read_models", side_effect=AssertionError("demo queried models")):
                    async with app.run_test(size=(100, 30)) as pilot:
                        await app.handle("/usage codex")
                        await app.handle("/models claude")
                        await pilot.pause()
                        self.assertIn("quota inspection is skipped", self.pane(app, "manager"))
                        self.assertIn("model discovery is skipped", self.pane(app, "manager"))
            finally:
                app.store.close()

    async def test_models_inspection_keeps_existing_plan_and_uses_repository_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            (root / "brahma.toml").write_text('[brahma]\ncodex_model="gpt-6-sol"\n')
            app = BrahmaApp(root, home=Path(tmp)/"state")
            try:
                with patch("brahma.model_selection.read_models", new_callable=AsyncMock, return_value={}) as read, \
                        patch("brahma.model_selection.format_models", return_value="Native models checked"), \
                        patch("brahma.providers.run", side_effect=AssertionError("inspection generated work")):
                    async with app.run_test(size=(110, 36)) as pilot:
                        await pilot.pause()
                        app.session.set_plan(Plan("Existing plan", [Task("task", "change", ["first.txt"], ["correct"])]))
                        before = app.store.session(app.session.key)
                        await app.handle("/models codex")
                        await pilot.pause()
                        self.assertEqual(read.await_args.args[0].codex_model, "gpt-6-sol")
                        self.assertEqual(read.await_args.args[1], ("codex",))
                        self.assertEqual(app.store.session(app.session.key), before)
                        self.assertFalse(app.checking_auth)
                        self.assertIn("Native models checked", self.pane(app, "manager"))
            finally:
                app.store.close()

    async def test_three_panes_demo_approval_and_resize_without_provider_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            app = BrahmaApp(root, home=Path(tmp)/"state", demo=True)
            try:
                with patch("brahma.providers.run", side_effect=AssertionError("demo made a paid request")):
                    async with app.run_test(size=(120, 40)) as pilot:
                        self.assertEqual(len(app.query(RichLog)), 3)
                        prompt = app.query_one("#prompt", Input)
                        prompt.value = "Add a useful test"
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertFalse(app.query_one("#approve", Button).disabled)
                        await pilot.click("#approve")
                        await pilot.pause()
                        await pilot.resize_terminal(70, 32)
                        self.assertTrue(app.query_one("#workers").has_class("narrow"))
                        self.assertEqual((root/"first.txt").read_text(), "base\n")
                        self.doctor.assert_not_awaited()
            finally:
                app.store.close()

    async def test_new_session_and_control_text_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            app = BrahmaApp(root, home=Path(tmp)/"state")
            try:
                async with app.run_test(size=(110, 36)) as pilot:
                    old = app.session.key
                    app.session.emit(Event("codex", "builder", text="OLD_WORKER_MARKER"))
                    app.session.emit(Event("codex", "builder", "usage", "usage", {"input_tokens": 100, "output_tokens": 20}))
                    app.query_one("#prompt", Input).value = "/new"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertNotEqual(app.session.key, old)
                    self.assertEqual((app.inputs, app.outputs, app.have_usage), (0, 0, False))
                    self.assertNotIn("OLD_WORKER_MARKER", self.pane(app, "codex"))
                    app.select_repo(root, old)
                    self.assertEqual((app.inputs, app.outputs), (100, 20))
                    app.select_repo(root, old)
                    self.assertEqual((app.inputs, app.outputs), (100, 20))
                    app.action_fresh()
                    self.assertEqual((app.inputs, app.outputs, app.have_usage), (0, 0, False))
                    self.assertNotIn("OLD_WORKER_MARKER", self.pane(app, "codex"))
                    app.session.emit(Event(text="plain\x1b[31mred\x1b[0m"))
                    self.assertEqual(app.store.events(app.session.key)[-1]["text"], "plainred")
            finally:
                app.store.close()

    async def test_demo_ready_session_cannot_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            app = BrahmaApp(root, home=Path(tmp)/"state", demo=True)
            try:
                with patch("brahma.gittools.publish") as publish:
                    async with app.run_test(size=(80, 24)) as pilot:
                        app.store.set(app.session.key, status="ready", worktree=str(root))
                        await app.handle("/publish")
                        await pilot.pause()
                        publish.assert_not_called()
                        self.assertIn("Demo mode cannot publish", self.pane(app, "manager"))
                        status = app.query_one("#status", Static).render().plain
                        self.assertTrue(status.startswith("DEMO · ready"))
            finally:
                app.store.close()

    async def test_logs_reflow_without_horizontal_clipping_and_small_size_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BrahmaApp(home=Path(tmp)/"state", demo=True)
            try:
                async with app.run_test(size=(120, 40)) as pilot:
                    text = "A complete assignment must remain readable at every supported terminal width. " * 5
                    for name in ("codex", "claude", "brahma"):
                        app.write(Event(name, "reviewer", text=text))
                    await pilot.pause()
                    wide_count = len(app.query_one("#codex", RichLog).lines)
                    for width, height in ((80, 24), (120, 40), (90, 30)):
                        await pilot.resize_terminal(width, height)
                        await pilot.pause()
                        for log in app.query(RichLog):
                            self.assertLessEqual(log.virtual_size.width, log.scrollable_content_region.width)
                        flattened = "".join(self.pane(app, "codex").split())
                        self.assertIn("".join(text.split()), flattened)
                    await pilot.resize_terminal(80, 24)
                    await pilot.pause()
                    # The vertical worker pane is wider, so old text must actually reflow.
                    self.assertLess(len(app.query_one("#codex", RichLog).lines), wide_count)
                    for width, height in ((70, 20), (40, 12)):
                        await pilot.resize_terminal(width, height)
                        await pilot.pause()
                        self.assertTrue(app.query_one("#size-notice").display)
                        self.assertFalse(app.query_one("#manager").display)
                        await pilot.press("escape")
                    await pilot.resize_terminal(80, 24)
                    await pilot.pause()
                    self.assertFalse(app.query_one("#size-notice").display)
                    self.assertTrue(app.query_one("#manager").display)
                    self.assertIn("No active work to stop", self.pane(app, "manager"))
            finally:
                app.store.close()

    async def test_status_long_untrusted_name_remains_plain_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / ("[bold]" + "r"*120))
            app = BrahmaApp(root, home=Path(tmp)/"state", demo=True)
            try:
                async with app.run_test(size=(80, 24)) as pilot:
                    app.store.set(app.session.key, status="running")
                    app.write(Event(kind="usage", usage={"input_tokens": 1234, "output_tokens": 567}))
                    await pilot.pause()
                    status = app.query_one("#status", Static).render()
                    lines = status.plain.splitlines()
                    self.assertTrue(lines[0].startswith("DEMO · running"))
                    self.assertIn("in 1,234 / out 567", lines[0])
                    self.assertTrue(lines[1].startswith("API fallback OFF · 60m/task · 3 repairs"))
                    self.assertTrue(all(len(line) <= 78 for line in lines))
                    self.assertEqual(status.spans, [])
            finally:
                app.store.close()

    async def test_active_status_help_keep_job_and_rejected_request_then_stop_is_truthful(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            app = BrahmaApp(root, home=Path(tmp)/"state")
            blocker = None
            try:
                async with app.run_test(size=(100, 30)) as pilot:
                    app.action_stop()
                    self.assertIn("No active work to stop", self.pane(app, "manager"))
                    blocker = asyncio.create_task(asyncio.Event().wait())
                    app.job = blocker
                    prompt = app.query_one("#prompt", Input)
                    with patch.object(app.session, "ask", side_effect=AssertionError("inspection started a provider request")):
                        for command in ("/status", "/help"):
                            prompt.value = command
                            await pilot.press("enter")
                            await pilot.pause()
                            self.assertIs(app.job, blocker)
                            self.assertFalse(blocker.done())
                        prompt.value = "Another task that should remain editable"
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertEqual(prompt.value, "Another task that should remain editable")
                        await pilot.press("escape")
                        self.assertTrue(app.session.cancel.is_set())
                        self.assertIn("waiting for active work to end", self.pane(app, "manager"))
                        self.assertNotIn("are terminated", self.pane(app, "manager"))
                    blocker.cancel()
                    await asyncio.gather(blocker, return_exceptions=True)
                    app.job = None
            finally:
                if blocker:
                    blocker.cancel()
                    await asyncio.gather(blocker, return_exceptions=True)
                app.store.close()

    async def test_startup_checks_are_read_only_friendly_and_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BrahmaApp(home=Path(tmp)/"state")
            self.doctor.side_effect = [
                {"available": False, "error": "codex CLI is not installed",
                 "action": "Please install codex using https://developers.openai.com/codex/cli, then run /doctor again."},
                {"available": False, "error": "claude CLI is installed but not signed in",
                 "action": "Please run `claude auth login` in your terminal, then run /doctor again."},
                {"available": True}, {"available": True},
            ]
            try:
                with patch("brahma.providers.run", side_effect=AssertionError("startup called a model")):
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.pause()
                        self.assertEqual(self.doctor.await_count, 2)
                        self.assertIn("Please install codex", self.pane(app, "codex"))
                        self.assertIn("claude auth login", self.pane(app, "claude"))
                        app.query_one("#prompt", Input).value = "/doctor"
                        await pilot.press("enter")
                        await pilot.pause()
                        self.assertEqual(self.doctor.await_count, 4)
                        self.assertIn("Ready: installed and signed in", self.pane(app, "codex"))
            finally:
                app.store.close()

    async def test_quit_cancels_startup_auth_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = BrahmaApp(home=Path(tmp)/"state")
            started, cleaned_up = asyncio.Event(), asyncio.Event()

            async def waiting_doctor(provider):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned_up.set()

            self.doctor.side_effect = waiting_doctor
            try:
                async with app.run_test(size=(100, 30)) as pilot:
                    await asyncio.wait_for(started.wait(), 1)
                    await asyncio.wait_for(app.action_quit(), 2)
                    self.assertTrue(cleaned_up.is_set())
                    self.assertIsNone(app.job)
                    self.assertFalse(app.checking_auth)
            finally:
                app.store.close()
