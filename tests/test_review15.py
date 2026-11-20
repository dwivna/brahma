"""Independent review round 15 (Codex): one regression per blocking finding."""
from __future__ import annotations

import asyncio
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import engine, gitsafe, gittools
from brahma.config import Settings
from brahma.models import Plan, Task
from brahma.session import Session
from brahma.store import Store
from tests.test_review10 import SECRET, make_repo
from tests.test_review12 import git

ROOT = Path(__file__).resolve().parents[1]


def follow_on(tmp: str, owned: list[str]):
    repo = make_repo(Path(tmp) / "r")
    (repo / "AGENTS.md").write_text("original rules\n")
    (repo / "CLAUDE.md").write_text("other rules\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "rules")
    store = Store(Path(tmp) / "state")
    session = Session(store, repo)
    worktree = Path(tmp) / "wt"
    git(repo, "worktree", "add", "-q", "-b", "brahma/" + session.key, str(worktree))
    store.set(session.key, worktree=str(worktree))
    session.set_plan(Plan("Next", [Task("T", "do", owned, ["ok"], "codex")], checks=[["true"]]))
    return store, session, worktree


class Finding1PublishFromTheEventLoop(unittest.IsolatedAsyncioTestCase):
    async def test_publication_reads_state_on_the_loop_thread_and_publishes_in_a_worker_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            store = Store(Path(tmp) / "state")            # the connection belongs to this (the loop's) thread
            self.addCleanup(store.close)
            session = Session(store, repo)
            session.plan = Plan("Goal", [Task("T", "do", ["src/**"], ["ok"])])
            store.set(session.key, worktree=str(repo))
            seen = []
            with patch.object(gittools, "publish", lambda *args: seen.append(args) or "https://example.invalid/pr/1"):
                url = await session.publish_ready_in_thread(
                    {"head": "h1", "uncommitted": {}, "ignored_allowlist": ["node_modules"]}, "body")
            self.assertEqual(url, "https://example.invalid/pr/1")
            self.assertEqual(seen, [(repo, "Goal", "body", "h1", {}, ["node_modules"])])
        self.assertIn("self.session.publish_ready_in_thread(", (ROOT / "src" / "brahma" / "ui.py").read_text())


class Finding2PlanOwnedInstructions(unittest.TestCase):
    def test_an_approved_plan_may_edit_the_instructions_it_owns_but_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, worktree = follow_on(tmp, ["AGENTS.md", "lib/**"])
            self.addCleanup(store.close)
            contract, _, _ = session.approved_contract()
            (worktree / "AGENTS.md").write_text("edited before approval\n")
            with self.assertRaisesRegex(ValueError, "Contract changed"):
                session.approved_contract()               # before approval every instruction must match
            (worktree / "AGENTS.md").write_text("original rules\n")
            store.set(session.key, approved_digest=contract["digest"])
            (worktree / "AGENTS.md").write_text("updated by the approved plan\n")
            session.approved_contract()                   # the plan's own authorised edit
            (worktree / "AGENTS.md").unlink()
            session.approved_contract()                   # ... or deletion
            (worktree / "CLAUDE.md").write_text("changed outside the plan\n")
            with self.assertRaisesRegex(ValueError, "Contract changed"):
                session.approved_contract()


class Finding3IgnoredFilesInOrdinaryCacheNamedDirectories(unittest.TestCase):
    def test_an_ignored_key_in_a_tracked_dist_directory_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "dist").mkdir()
            (repo / "dist" / "app.js").write_text("x\n")
            (repo / ".gitignore").write_text("dist/*.pem\n.venv/\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "dist is source")
            (repo / "dist" / "credentials.pem").write_text("key\n")
            (repo / ".venv").mkdir()
            (repo / ".venv" / "lib.py").write_text("cache\n")
            found = gittools.ignored_paths(repo)
            self.assertIn("dist/credentials.pem", found)
            self.assertNotIn(".venv/lib.py", found)             # a genuinely ignored cache stays exempt
            ok, reason = gittools.ownership_changes(repo, ["src/**"], baseline={})
            self.assertFalse(ok)
            self.assertIn("dist/credentials.pem", reason)


class Finding4UnverifiableFingerprints(unittest.TestCase):
    def test_an_unreadable_unowned_file_never_compares_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "notes.txt").write_text("before\n")
            (repo / "notes.txt").chmod(0)
            try:
                baseline = gittools.snapshot_changes(repo)
                self.assertEqual(baseline["notes.txt"], "unreadable")
                ok, _ = gittools.ownership_changes(repo, ["src/**"], baseline=baseline)
                self.assertFalse(ok)
                self.assertIn("notes.txt", gittools.violating_paths(repo, ["src/**"], baseline=baseline))
            finally:
                (repo / "notes.txt").chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_an_owned_fifo_is_never_accepted_and_never_binds_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            baseline = gittools.snapshot_changes(repo)
            os.mkfifo(repo / "src" / "pipe")
            self.assertTrue(gittools._fingerprint(repo / "src" / "pipe").startswith("special:"))
            ok, reason = gittools.ownership_changes(repo, ["src/**"], baseline=baseline)
            self.assertFalse(ok)
            self.assertIn("src/pipe", reason)
            settings = Settings()
            self.assertNotEqual(engine._tree(repo, settings), engine._tree(repo, settings))


class Finding5ProposalsWithQuestions(unittest.TestCase):
    def test_a_credential_in_a_plan_that_still_has_questions_is_never_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            session = Session(store, repo)
            plan = Plan("Goal", [Task("T", "do", ["src/**"], ["ok"])], questions=["Use token " + SECRET + "?"])
            with self.assertRaisesRegex(ValueError, "credential"):
                session.set_plan(plan)
            self.assertIsNone(store.session(session.key)["plan"])
            self.assertNotIn(SECRET, repr(store.events(session.key)))


class Finding6ReplacementObjects(unittest.TestCase):
    def test_a_replacement_for_the_starting_commit_cannot_hide_an_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            first = git(repo, "rev-parse", "HEAD")
            (repo / "src" / "keep.py").write_text("unowned edit\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "second")
            git(repo, "reset", "-q", "--soft", first)         # the edit is staged: index != starting HEAD
            fake = git(repo, "commit-tree", git(repo, "write-tree"), "-m", "fake")
            git(repo, "replace", first, fake)                 # HEAD now "looks like" the edited tree
            self.assertIn("src/keep.py", gittools.status_paths(repo))
            self.assertEqual(gitsafe.git_env({})["GIT_NO_REPLACE_OBJECTS"], "1")

    def test_grafts_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / ".git" / "info").mkdir(exist_ok=True)
            (repo / ".git" / "info" / "grafts").write_text(git(repo, "rev-parse", "HEAD") + "\n")
            self.assertIn("grafts", gitsafe.unsupported_repository(repo))


class Finding7NoSigningHelper(unittest.TestCase):
    def test_an_automatic_commit_never_runs_a_configured_signing_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            marker = Path(tmp) / "signer-ran"
            signer = Path(tmp) / "signer.sh"
            signer.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
            signer.chmod(0o755)
            git(repo, "config", "commit.gpgSign", "true")
            git(repo, "config", "gpg.program", str(signer))
            git(repo, "checkout", "-q", "-b", "brahma/s")
            (repo / "src" / "keep.py").write_text("x = 2\n")
            gittools.commit_changes(repo, "s")
            self.assertFalse(marker.exists())
            self.assertEqual(git(repo, "log", "-1", "--format=%s"), "Brahma: s")


if __name__ == "__main__":
    unittest.main()
