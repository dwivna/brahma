"""Independent review round 16 (Codex): one regression per blocking finding."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import gittools, safety
from brahma.models import Event
from brahma.store import Store
from tests.test_review10 import SECRET, make_repo
from tests.test_review12 import git


def verified_branch(tmp: str):
    repo = make_repo(Path(tmp) / "r")
    git(repo, "checkout", "-q", "-b", "brahma/s")
    return repo, git(repo, "rev-parse", "HEAD"), gittools.snapshot_changes(repo)


class Finding1PushTheVerifiedCommit(unittest.TestCase):
    def test_the_push_sends_the_receipt_commit_not_the_branch_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            steps = []

            def step(argv, worktree, stopped, **kwargs):
                steps.append(argv)
                if "push" in argv:      # the branch advances while the push is starting
                    (repo / "src" / "later.py").write_text("x\n")
                    git(repo, "add", "-A")
                    git(repo, "commit", "-qm", "later")
                return type("R", (), {"returncode": 0, "stdout": "https://example.invalid/pr/1", "stderr": ""})()
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", step):
                gittools.publish(repo, "s", "b", head, snapshot)
            push = steps[0]
            self.assertEqual(push[-3:], ["push", "origin", f"{head}:refs/heads/brahma/s"])

    def test_the_guard_allows_an_exact_commit_only_to_a_brahma_branch(self):
        sha = "a" * 40
        self.assertIsNone(gittools.refuse(("push", "origin", f"{sha}:refs/heads/brahma/s")))
        for ref in (f"{sha}:refs/heads/main", f"{sha}:main", sha, f"+{sha}:refs/heads/brahma/s", "HEAD:brahma/s"):
            with self.subTest(ref=ref):
                self.assertIsNotNone(gittools.refuse(("push", "origin", ref)))


class Finding2StopReachesPublication(unittest.TestCase):
    def test_a_stop_before_publication_pushes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step") as step:
                with self.assertRaisesRegex(ValueError, "Stopped by user"):
                    gittools.publish(repo, "s", "b", head, snapshot, stopped=lambda: "Stopped by user")
            step.assert_not_called()

    def test_a_running_step_is_ended_when_the_session_is_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            stop_at = time.monotonic() + 0.5
            started = time.monotonic()
            with self.assertRaisesRegex(ValueError, "Publication stopped: Stopped by user"):
                gittools._run_publication_step(["sleep", "30"], Path(tmp),
                                               lambda: "Stopped by user" if time.monotonic() > stop_at else None)
            self.assertLess(time.monotonic() - started, 5)

    def test_session_controls_are_read_safely_from_another_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            store.stop(key)
            seen = []
            thread = threading.Thread(target=lambda: seen.append(Store.halt_reason(store.home, key)))
            thread.start()
            thread.join()
            self.assertEqual(seen, ["Stopped by user"])


class Finding3CredentialShapedPaths(unittest.TestCase):
    def test_a_credential_shaped_file_name_never_reaches_storage_or_the_live_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            event = Event(kind="task_state", text=json.dumps({"index": 1, "snapshot": {f"src/{SECRET}.txt": "absent",
                                                                                         "src/tokenizer": "absent"}}))
            store.event(key, event)
            self.assertNotIn(SECRET, event.text)                  # what the live callback receives
            self.assertNotIn(SECRET, repr(store.events(key)))
            self.assertIn("src/tokenizer", json.loads(event.text)["snapshot"])
        self.assertNotIn(SECRET, json.dumps(safety.redact_deep({"baseline": {SECRET: "absent"}})))


class Finding4SensitiveSpecialFilesInCaches(unittest.TestCase):
    def test_an_env_fifo_inside_an_ignored_cache_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / ".gitignore").write_text(".venv/\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "ignore venv")
            (repo / ".venv" / "lib").mkdir(parents=True)
            (repo / ".venv" / "lib" / "ordinary.py").write_text("x\n")
            os.mkfifo(repo / ".venv" / "lib" / ".env")
            self.assertIn(".venv/lib/.env", gittools.special_paths(repo))
            ok, reason = gittools.ownership_changes(repo, ["src/**"], baseline={})
            self.assertFalse(ok)
            self.assertIn(".venv/lib/.env", reason)


class Finding5OwnedUnreadableFiles(unittest.TestCase):
    def test_a_new_owned_file_brahma_cannot_read_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            path = repo / "src" / "new.txt"
            path.write_text("x\n")
            path.chmod(0)
            try:
                ok, reason = gittools.ownership_changes(repo, ["src/**"], baseline={})
                self.assertFalse(ok)
                self.assertIn("src/new.txt", reason)
                self.assertIn("src/new.txt", gittools.violating_paths(repo, ["src/**"], baseline={}))
            finally:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)


if __name__ == "__main__":
    unittest.main()
