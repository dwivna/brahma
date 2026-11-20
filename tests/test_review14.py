"""Independent review round 14 (Codex): one regression per blocking finding."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import gitsafe, gittools, scheduler, session as session_mod, toolchain
from brahma.models import Event, Plan, Task
from brahma.session import Session
from brahma.store import Store
from tests.test_review10 import make_repo
from tests.test_review12 import git
from tests.test_toolchain import jdk, script

ROOT = Path(__file__).resolve().parents[1]


class Finding1RepositoryLocalAttributes(unittest.TestCase):
    def test_info_attributes_stop_every_git_call_before_a_filter_can_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            marker = Path(tmp) / "filter-ran"
            git(repo, "config", "filter.demo.clean", f"sh -c 'touch {marker}; cat'")
            (repo / "src" / "keep.py").write_text("changed\n")
            info = Path(git(repo, "rev-parse", "--absolute-git-dir")) / "info"
            info.mkdir(exist_ok=True)
            (info / "attributes").write_text("* filter=demo\n")
            with self.assertRaisesRegex(ValueError, "info/attributes"):
                gittools.status_paths(repo)
            with self.assertRaisesRegex(ValueError, "info/attributes"):
                gittools.content_changed_tracked(repo)
            self.assertIn("info/attributes", gitsafe.unsupported_repository(repo))
            self.assertFalse(marker.exists())
            (info / "attributes").write_text("")                 # empty: nothing applies
            self.assertIn("src/keep.py", gittools.status_paths(repo))
            self.assertFalse(marker.exists())

    def test_a_linked_worktree_checks_the_shared_git_directory_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            git(repo, "worktree", "add", "-q", "-b", "brahma/s", str(Path(tmp) / "wt"))
            (repo / ".git" / "info").mkdir(exist_ok=True)
            (repo / ".git" / "info" / "attributes").write_text("*.py text eol=crlf\n")
            with self.assertRaisesRegex(ValueError, "info/attributes"):
                gittools.status_paths(Path(tmp) / "wt")

    def test_a_repointed_git_directory_is_checked_not_a_remembered_one(self):
        # Automated security review of 0dd720d: the Git-directory lookup was cached per path.
        with tempfile.TemporaryDirectory() as tmp:
            repo, other = make_repo(Path(tmp) / "r"), make_repo(Path(tmp) / "o")
            git(repo, "worktree", "add", "-q", "-b", "brahma/s", str(Path(tmp) / "wt"))
            wt = Path(tmp) / "wt"
            self.assertIsNone(gitsafe.attribute_override(wt))
            (other / ".git" / "info").mkdir(exist_ok=True)
            (other / ".git" / "info" / "attributes").write_text("* filter=demo\n")
            (wt / ".git").write_text(f"gitdir: {other / '.git'}\n")
            self.assertIn("info/attributes", gitsafe.attribute_override(wt))

    def test_system_attributes_are_off(self):
        self.assertEqual(gitsafe.git_env({})["GIT_ATTR_NOSYSTEM"], "1")


class Finding2FollowOnInstructions(unittest.TestCase):
    def test_a_follow_on_plan_after_an_instruction_edit_is_approvable_and_verifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "AGENTS.md").write_text("original rules\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "rules")
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            session = Session(store, repo)
            worktree = Path(tmp) / "wt"
            git(repo, "worktree", "add", "-q", "-b", "brahma/" + session.key, str(worktree))
            (worktree / "AGENTS.md").write_text("rules updated by an approved task\n")
            git(worktree, "commit", "-qam", "task 1")
            store.set(session.key, worktree=str(worktree))
            session.set_plan(Plan("Next", [Task("T", "do", ["src/**"], ["ok"], "codex")], checks=[["true"]]))
            contract, _, _ = session.approved_contract()        # verifies against the same checkout
            self.assertEqual(contract["payload"]["instruction_basis"], "worktree")
            self.assertEqual(contract["payload"]["instructions"]["AGENTS.md"],
                             hashlib.sha256(b"rules updated by an approved task\n").hexdigest())
            (worktree / "AGENTS.md").write_text("changed after approval\n")
            with self.assertRaisesRegex(ValueError, "Contract changed"):
                session.approved_contract()


class Finding3PublishFromTheInterface(unittest.TestCase):
    def test_a_clean_verified_result_is_published_with_its_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            session = Session(store, repo)
            session.plan = Plan("Goal", [Task("T", "do", ["src/**"], ["ok"])])
            git(repo, "checkout", "-q", "-b", "brahma/" + session.key)
            store.set(session.key, worktree=str(repo))
            receipt = {"head": git(repo, "rev-parse", "HEAD"), "uncommitted": gittools.snapshot_changes(repo),
                       "ignored_allowlist": list(gittools.DEFAULT_IGNORED_ALLOWLIST)}
            pushed = []
            real, real_run = gittools.git, subprocess.run
            done = type("R", (), {"returncode": 0, "stdout": "https://example.invalid/pr/1"})()
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "git", side_effect=lambda r, *a: pushed.append(a) if a[0] == "push" else real(r, *a)), \
                    patch.object(gittools.subprocess, "run",
                                 side_effect=lambda argv, *a, **k: done if argv[0] == "/usr/bin/gh" else real_run(argv, *a, **k)):
                self.assertEqual(session.publish_ready(receipt, "body"), "https://example.invalid/pr/1")
            self.assertEqual(len(pushed), 1)
        # /publish uses the same receipt-bound path (from the event loop since round 15).
        self.assertTrue("self.session.publish_ready_in_thread(" in (ROOT / "src" / "brahma" / "ui.py").read_text())


class Finding4OneTimeLedger(unittest.TestCase):
    def test_each_verification_is_charged_once_whatever_happens_next(self):
        gen = "g1"
        state = lambda **row: {"kind": "task_state", "text": json.dumps({"generation": gen, "index": 1, **row})}
        spent = lambda s: {"kind": "verification_spent", "text": json.dumps({"generation": gen, "index": 1, "seconds": s})}
        events = [state(deadline_epoch=1, remaining_seconds=100, state="ready"), spent(90)]
        self.assertEqual(session_mod.verification_charges(events, gen), {1: 90})
        # The task is worked on again: its new checkpoint starts from the charged budget, so nothing is charged twice.
        events += [state(deadline_epoch=2, remaining_seconds=10, state="building"), spent(3)]
        self.assertEqual(session_mod.verification_charges(events, gen), {1: 3})
        self.assertEqual(session_mod.verification_charges(events, "other"), {})


class Finding5OrdinaryDistIsWalked(unittest.TestCase):
    def test_a_fifo_in_a_tracked_dist_directory_is_found_and_an_ignored_cache_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "dist").mkdir()
            (repo / "dist" / "app.js").write_text("x\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "dist is source here")
            os.mkfifo(repo / "dist" / "pipe")
            self.assertIn("dist/pipe", gittools.special_paths(repo))
            ok, reason = gittools.ownership_changes(repo, ["src/**"], baseline={})
            self.assertFalse(ok)
            os.unlink(repo / "dist" / "pipe")
            (repo / "node_modules").mkdir()
            os.mkfifo(repo / "node_modules" / "pipe")
            (repo / ".gitignore").write_text("node_modules/\n")
            self.assertNotIn("node_modules/pipe", gittools.special_paths(repo))


class Finding6TrackedSymlinks(unittest.TestCase):
    def test_a_symlink_replaced_by_a_file_holding_its_target_text_is_a_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            os.symlink("src/keep.py", repo / "link")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "link")
            git(repo, "config", "core.symlinks", "false")
            os.unlink(repo / "link")
            (repo / "link").write_text("src/keep.py")
            self.assertIn("link", gittools.status_paths(repo))
            os.unlink(repo / "link")
            os.symlink("elsewhere", repo / "link")
            self.assertIn("link", gittools.content_changed_tracked(repo))


class Finding7GradleStartupVariables(unittest.TestCase):
    def test_gradle_opts_that_select_a_daemon_jdk_mark_the_jdk_unbound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            script(root / "bin", "gradle")
            home = jdk(root, "21")
            for value in ("-Dorg.gradle.java.home=/opt/other", "-Dorg.\"gradle\".java.home=/opt/other",
                          "-Dgradle.user.home=/tmp/h", "-Duser.home=/tmp/h"):    # automated review of 0dd720d
                with self.subTest(value=value):
                    env = {"PATH": str(root / "bin"), "JAVA_HOME": str(home), "GRADLE_OPTS": value}
                    info = toolchain.probe([["gradle", "build"]], repo, env)["programs"]["gradle"]
                    self.assertTrue(info["runtime_unbound"])
                    self.assertEqual(info["unbound_reason"], "gradle-environment")
            env = {"PATH": str(root / "bin"), "JAVA_HOME": str(home), "GRADLE_OPTS": "-Xmx2g"}
            self.assertFalse(toolchain.probe([["gradle", "build"]], repo, env)["programs"]["gradle"]["runtime_unbound"])


class Finding8CacheEviction(unittest.TestCase):
    def test_a_full_cache_never_makes_unchanged_files_look_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            self.assertEqual(gittools.content_changed_tracked(repo), [])      # caches the real hashes
            saved = dict(gittools._BLOB_CACHE)
            self.addCleanup(lambda: (gittools._BLOB_CACHE.clear(), gittools._BLOB_CACHE.update(saved)))
            gittools._BLOB_CACHE.update({("filler", n): "0" for n in range(200001)})
            self.assertEqual(gittools.content_changed_tracked(repo), [])


class Finding9VersioningPolicy(unittest.TestCase):
    def test_the_changelog_states_major_for_breaking_changes(self):
        intro = (ROOT / "CHANGELOG.md").read_text().split("## ", 1)[0]
        self.assertIn("MAJOR for any breaking change", intro)
        self.assertNotIn("MINOR for new capabilities or any change", intro)


class WorkerRestartsAfterAnUpgrade(unittest.IsolatedAsyncioTestCase):
    """Found in real use: an approved run resumed in a worker started before an upgrade failed with
    `ImportError: cannot import name 'git_identity' from 'brahma.gittools'` (old module in memory, new engine on disk)."""

    def test_the_code_fingerprint_changes_when_a_module_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.py").write_text("x = 1\n")
            before = scheduler.code_fingerprint(Path(tmp))
            (Path(tmp) / "a.py").write_text("x = 22\n")
            self.assertNotEqual(scheduler.code_fingerprint(Path(tmp)), before)

    def test_every_module_is_loaded_at_start(self):
        scheduler.load_all_modules()
        for name in ("engine", "gittools", "session", "contracts", "toolchain", "ui"):
            self.assertIn("brahma." + name, sys.modules)

    async def test_an_upgraded_worker_exits_before_taking_a_job(self):
        ticks = []

        async def tick(store, stop=None):
            ticks.append(1)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(scheduler, "code_fingerprint", side_effect=["old", "new"]), \
                patch.object(scheduler, "tick", tick), patch("brahma.procs.reap"):
            self.assertEqual(await scheduler._serve(Path(tmp), once=False), 0)
        self.assertEqual(ticks, [])


if __name__ == "__main__":
    unittest.main()
