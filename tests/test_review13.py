"""Independent review round 13 (Codex): one regression per blocking finding."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import engine, gitsafe, gittools, safety
from brahma.config import Settings
from brahma.models import Event, Plan, Result, Task
from brahma.store import Store
from tests.test_review10 import make_repo
from tests.test_review11 import engine_patches
from tests.test_review12 import git


class Finding1RawContentAndModes(unittest.TestCase):
    def test_line_ending_and_executable_bit_changes_are_seen_whatever_the_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / ".gitattributes").write_text("* text=auto\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "attrs")
            git(repo, "config", "core.fileMode", "false")
            (repo / "src" / "keep.py").write_bytes(b"x = 1\r\n")          # same text once normalised; other bytes
            self.assertIn("src/keep.py", gittools.status_paths(repo))
            git(repo, "checkout", "--", "src/keep.py")
            (repo / "src" / "keep.py").chmod(0o755)
            self.assertIn("src/keep.py", gittools.status_paths(repo))


class Finding2NoFilterProgramEverRuns(unittest.TestCase):
    def test_status_add_and_hashing_never_run_a_configured_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            marker = Path(tmp) / "filter-ran"
            git(repo, "config", "filter.demo.clean", f"sh -c 'touch {marker}; cat'")
            (repo / ".gitattributes").write_text("*.bin filter=demo\n")    # as a worker could write after approval
            (repo / "a.bin").write_text("data\n")
            git(repo, "checkout", "-q", "-b", "brahma/s")
            gittools.status_paths(repo)
            gittools.content_changed_tracked(repo)
            gittools.commit_changes(repo, "s")
            self.assertFalse(marker.exists())
            self.assertEqual(git(repo, "show", "HEAD:a.bin"), "data")           # the checked bytes, unfiltered


class Finding3SpecialFilesDiscovered(unittest.TestCase):
    def test_a_new_fifo_or_socket_is_an_ownership_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            os.mkfifo(repo / "pipe")
            ok, reason = gittools.ownership_changes(repo, ["src/**"], baseline={})
            self.assertFalse(ok)
            self.assertIn("pipe", reason)
            restored, _ = gittools.restore_violations(repo, ["src/**"], {})
            self.assertIn("pipe", restored)
            self.assertFalse(os.path.lexists(repo / "pipe"))
            short = Path(tempfile.mkdtemp(dir="/tmp"))                     # socket paths must be short
            self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(short)]))
            repo2 = make_repo(short / "r")
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(repo2 / "s.sock"))
            self.addCleanup(server.close)
            self.assertIn("s.sock", gittools.changed_paths(repo2))


class Finding4FailedChecksVerified(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_check_that_edits_an_unowned_file_never_reaches_a_repair_builder(self):
        calls = []

        async def run(provider, prompt, cwd, role, *args, **kwargs):
            calls.append(role)
            return Result(provider, "ok", "built")
        p = engine_patches(owned=patch.object(gittools, "ownership_changes", side_effect=[(True, "ok"), (False, "outside.txt")]),
                           checks=patch.object(gittools, "run_checks", AsyncMock(return_value=(False, "failed"))))
        with p["context"], p["snapshot"], p["identity"], p["owned"], p["checks"], p["tree"], \
                patch.object(engine, "_restore_note", return_value=" Restored: outside.txt.") as restore, \
                patch.object(engine.providers, "run", side_effect=run):
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), lambda e: None, asyncio.Event())
        self.assertEqual(result.status, "blocked")
        self.assertEqual(calls, ["builder"])                 # no repair builder ran
        restore.assert_called_once()


class Finding5UsageIsCountersOnly(unittest.TestCase):
    def test_a_credential_named_number_never_reaches_stored_usage(self):
        from brahma import providers
        self.assertEqual(providers._usage({"input_tokens": 3, "access_token": 123456789, "num_turns": 2}),
                         {"input_tokens": 3, "num_turns": 2})
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            store.event(key, Event(kind="usage", usage={"output_tokens": 4, "access_token": 987654321,
                                                        "task_1_builder@r.1": {"input_tokens": 5, "api_key": 1}}))
            stored = store.events(key)[-1]["usage"]
            self.assertEqual(stored, {"output_tokens": 4, "task_1_builder@r.1": {"input_tokens": 5}})


class Finding6PublicationComparesIgnoredFiles(unittest.TestCase):
    def test_a_new_ignored_env_file_after_readiness_stops_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            git(repo, "checkout", "-q", "-b", "brahma/s")
            head = git(repo, "rev-parse", "HEAD")
            verified = gittools.snapshot_changes(repo)
            (repo / ".env").write_text("NEW=1\n")
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "git", wraps=gittools.git) as calls:
                with self.assertRaisesRegex(ValueError, "ignored or special files changed"):
                    gittools.publish(repo, "s", "b", head, verified)
            self.assertNotIn("push", [c.args[1] for c in calls.call_args_list])


class Finding7ResumeVerificationIsCharged(unittest.IsolatedAsyncioTestCase):
    async def run_resume(self, saved):
        events = []
        p = engine_patches()
        with p["context"], p["snapshot"], p["identity"], p["owned"], p["checks"] as checks, p["tree"]:
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), events.append, asyncio.Event(),
                                          completed_indices={1}, task_progress={1: saved})
        return result, checks, events

    async def test_an_exhausted_budget_gets_no_fresh_allowance(self):
        result, checks, _ = await self.run_resume({"state": "ready", "remaining_seconds": 0})
        self.assertEqual(result.status, "incomplete")
        checks.assert_not_awaited()
        result, checks, _ = await self.run_resume({"state": "ready", "remaining_seconds": 100, "verification_spent": 100})
        self.assertEqual(result.status, "incomplete")

    async def test_verification_runs_within_what_is_left_and_records_its_time(self):
        result, checks, events = await self.run_resume({"state": "ready", "remaining_seconds": 42})
        self.assertEqual(result.status, "ready")
        self.assertLessEqual(checks.await_args.args[3], 42)
        self.assertTrue([e for e in events if e.kind == "verification_spent"])


class Finding8FingerprintMapsKeepTheirPaths(unittest.TestCase):
    def test_an_extensionless_file_named_like_a_credential_keeps_its_fingerprint(self):
        row = {"baseline": {"tokenizer": "100644:aa", "secret": "100644:bb"}, "snapshot": {"password": "absent"},
               "token": "hide-me"}
        cleaned = safety.redact_deep(row)
        self.assertEqual(cleaned["baseline"], row["baseline"])
        self.assertEqual(cleaned["snapshot"], row["snapshot"])
        self.assertEqual(cleaned["token"], safety.REDACTED)


class Finding9FollowOnInstructions(unittest.TestCase):
    def test_instructions_are_fingerprinted_from_the_checkout_the_run_uses(self):
        import hashlib
        from brahma import contracts
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "AGENTS.md").write_text("original\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "rules")
            worktree = Path(tmp) / "wt"
            git(repo, "worktree", "add", "-q", "-b", "brahma/s", str(worktree))
            (worktree / "AGENTS.md").write_text("updated by an approved task\n")
            git(worktree, "commit", "-qam", "task")
            payload = contracts.prepare_contract(Plan("G", [Task("T", "do", ["x.txt"], ["ok"])]), repo, [["true"]],
                                                 base_revision=git(worktree, "rev-parse", "HEAD"),
                                                 instruction_root=worktree)["payload"]
            self.assertEqual(payload["instructions"]["AGENTS.md"],
                             hashlib.sha256(b"updated by an approved task\n").hexdigest())


class Finding10LinearHashDispatch(unittest.TestCase):
    def test_partitioning_many_names_is_linear(self):
        names = [f"f{n}.txt" for n in range(20000)]
        with patch.object(gittools.subprocess, "run") as run:
            run.return_value.stdout = ("0" * 40 + "\n").encode() * 2000
            started = time.monotonic()
            gittools._hash_paths(Path("/r"), names)
            self.assertLess(time.monotonic() - started, 1.0)


if __name__ == "__main__":
    unittest.main()
