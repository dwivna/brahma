import asyncio
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from brahma.config import Settings
from brahma.context import build_context
from brahma.gittools import (changed_paths, create_worktree, git, ownership_changes,
                             run_checks, snapshot_changes)
from brahma.models import Event, Plan, Result, Task
from brahma.session import Session
from brahma.store import Store


def init_repo(root):
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    git(root, "config", "user.name", "Brahma test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "first.txt").write_text("base\n")
    git(root, "add", "first.txt")
    git(root, "commit", "-m", "fixture")
    return root


class Core(unittest.TestCase):
    def test_invalid_plan_paths_and_questions_fail_closed(self):
        for name in ("../secret", "/tmp/secret", "~/.ssh", ".git/config"):
            value = dict(summary="x", tasks=[dict(title="task", instructions="x", files=[name], acceptance=["x"])])
            with self.subTest(name=name), self.assertRaises(ValueError):
                Plan.parse(json.dumps(value))
        with self.assertRaises(ValueError):
            Plan.parse('{"summary":"empty","tasks":[]}')

    def test_unknown_config_and_shell_checks_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for config in ('[brahma]\nchecks=["echo unsafe"]', '[brahma]\ntask_timeout=3601',
                           '[brahma]\nrepair_cycles=4', '[brahma]\npublish="yes"', '[brahma]\nunknown=1'):
                (root / "brahma.toml").write_text(config)
                with self.subTest(config=config), self.assertRaises(ValueError):
                    Settings.load(root)

    def test_store_roundtrip_and_plan_kept_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / "state")
            key = store.create(root)
            store.event(key, Event(text="private task", usage={"input_tokens": 3}))
            store.close()
            store = Store(root / "state")
            self.assertEqual(store.events(key)[0]["usage"], {"input_tokens": 3})
            self.assertEqual(store.recent()[0]["id"], key)
            store.close()

    def test_task_baseline_detects_old_file_mutation_and_restoration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            (root / "first.txt").write_text("task one\n")
            before = snapshot_changes(root)
            (root / "second.txt").write_text("task two\n")
            self.assertTrue(ownership_changes(root, ["second.txt"], before)[0])
            (root / "first.txt").write_text("base\n")
            self.assertFalse(ownership_changes(root, ["second.txt"], before)[0])

    def test_sensitive_paths_and_escaping_symlinks_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            (root / ".env.local").write_text("not-a-real-secret")
            self.assertFalse(ownership_changes(root, ["**"])[0])
            (root / ".env.local").unlink()
            (root / "link.txt").symlink_to(Path(tmp) / "outside.txt")
            self.assertFalse(ownership_changes(root, ["**"])[0])

    def test_dirty_source_refuses_and_isolated_branch_preserves_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            home = Path(tmp) / "state"
            wt = create_worktree(root, home, "one")
            (wt / "first.txt").write_text("isolated\n")
            self.assertEqual((root / "first.txt").read_text(), "base\n")
            self.assertEqual(git(wt, "branch", "--show-current"), "brahma/one")
            (root / "first.txt").write_text("user edit\n")
            with self.assertRaises(ValueError):
                create_worktree(root, home, "two")

    def test_required_instructions_never_silently_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            (root / "AGENTS.md").write_text("mandatory " * 500)
            with self.assertRaises(ValueError):
                build_context(root, ["src/**"], 2000)


class AsyncCore(unittest.IsolatedAsyncioTestCase):
    async def test_checks_actual_failure_missing_binary_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            events, stop = [], asyncio.Event()
            ok, report = await run_checks(root, [[sys.executable, "-c", "raise SystemExit(7)"]], stop, 5, events.append)
            self.assertFalse(ok)
            self.assertIn("exit 7", report)
            ok, _ = await run_checks(root, [["brahma-test-nonexistent-executable"]], stop, 5, events.append)
            self.assertFalse(ok)
            stop.set()
            ok, _ = await run_checks(root, [], stop, 5, events.append)
            self.assertFalse(ok)

    async def test_exited_check_leader_descendant_does_not_hold_pipe_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            script = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])"
            ok, _ = await asyncio.wait_for(run_checks(root, [[sys.executable, "-c", script]],
                                                      asyncio.Event(), .15, lambda e: None), 4)
            self.assertFalse(ok)

    async def test_session_generation_and_stale_ready_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            store = Store(Path(tmp) / "state")
            session = Session(store, root)
            (root / "brahma.toml").write_text("[brahma]\nauto_commit=false\nchecks=" + json.dumps([[sys.executable, "-c", "pass"]]))
            session.set_plan(Plan("goal", [Task("task", "change", ["first.txt"], ["correct"])]))
            worktree = create_worktree(root, store.home, session.key)
            store.set(session.key, worktree=str(worktree))
            (worktree / "first.txt").write_text("reviewed\n")
            session.emit(Event(kind="task_state", text=json.dumps(dict(index=1,title="task",state="ready"))))
            (worktree / "first.txt").write_text("different after review\n")
            seen = []
            async def execute(plan, wt, settings, emit, cancel, **kwargs):
                seen.append(kwargs)
                return Result("brahma", "incomplete", "fixture")
            with patch("brahma.engine.execute", execute):
                await session.execute()
            self.assertEqual(seen[0]["completed_indices"], set())
            session.set_plan(Plan("new goal", [Task("task", "change", ["first.txt"], ["correct"])]))
            with patch("brahma.engine.execute", execute):
                await session.execute()
            self.assertEqual(seen[-1]["completed_indices"], set())
            store.close()

    async def test_global_lock_blocks_second_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            store = Store(Path(tmp) / "state")
            one, two = Session(store, root), Session(store, root)
            with one.lock():
                with self.assertRaises(ValueError), two.lock():
                    pass
            store.close()

    async def test_newer_incomplete_state_invalidates_previous_ready_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = init_repo(Path(tmp) / "repo")
            store = Store(Path(tmp) / "state")
            session = Session(store, root)
            (root / "brahma.toml").write_text("[brahma]\nauto_commit=false\n")
            session.set_plan(Plan("goal", [Task("task", "change", ["first.txt"], ["correct"])]))
            worktree = create_worktree(root, store.home, session.key)
            store.set(session.key, worktree=str(worktree))
            session.emit(Event(kind="task_state", text=json.dumps(dict(index=1, title="task", state="ready"))))
            session.emit(Event(kind="task_state", text=json.dumps(dict(index=1, title="task", state="incomplete"))))
            seen = []
            async def execute(plan, wt, settings, emit, cancel, **kwargs):
                seen.append(kwargs["completed_indices"])
                return Result("brahma", "incomplete", "fixture")
            with patch("brahma.engine.execute", execute):
                await session.execute()
            self.assertEqual(seen, [set()])
            store.close()
