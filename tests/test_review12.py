"""Independent review round 12 (Codex): one regression per blocking finding."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import engine, gitsafe, gittools, providers, safety
from brahma.config import Settings
from brahma.models import Event, Plan, Result, Task
from brahma.store import Store
from tests.test_review10 import make_repo
from tests.test_review11 import engine_patches


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def finishes(function, seconds: float = 5.0):
    """Run `function` in a daemon thread; return (finished, outcome). A regression that blocks cannot hang the suite."""
    box = {}

    def target():
        try:
            box["value"] = function()
        except BaseException as error:          # noqa: BLE001 - reported to the test
            box["error"] = error
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), box


class Finding1DecisionsBoundToTheirEscalation(unittest.TestCase):
    def test_only_a_decision_on_this_plans_newest_escalation_applies(self):
        from tests.test_cli import repo_session
        with tempfile.TemporaryDirectory() as tmp:
            store, session = repo_session(tmp)
            self.addCleanup(store.close)
            store.event(session.key, Event("brahma", "manager", "disagreement", json.dumps({"index": 1, "text": "x"})))
            escalation = store.latest_seq(session.key, "disagreement")

            def decide(**fields):
                store.event(session.key, Event("brahma", "user", "dispute_decision",
                                               json.dumps({"index": 1, "for": "author", "digest": "d", **fields})))
            decide(generation="another plan", escalation=escalation)       # an earlier plan's decision
            decide()                                                       # an unbound (old-format) decision
            self.assertEqual(session._decisions(), {})
            decide(generation=session.generation, escalation=escalation)
            self.assertEqual(session._decisions(), {1: ("author", "d")})
            store.event(session.key, Event("brahma", "manager", "disagreement", json.dumps({"index": 1, "text": "x"})))
            self.assertEqual(session._decisions(), {})                      # a new escalation needs a new decision


class Finding2And3CredentialFields(unittest.TestCase):
    def test_file_name_keys_keep_their_fingerprints(self):
        snapshot = {"snapshot": {"src/tokenizer.py": "100644:abc", "src/passwords.py": "100644:def"}}
        self.assertEqual(safety.redact_deep(snapshot), snapshot)

    def test_numbers_under_credential_names_are_hidden_and_usage_counters_are_kept(self):
        value = safety.redact_deep({"token": 123456, "auth": {"pin": 4321},
                                    "usage": {"input_tokens": 9, "cache_read_input_tokens": 3}})
        self.assertEqual(value["token"], safety.REDACTED)
        self.assertEqual(value["auth"], {"pin": safety.REDACTED})
        self.assertEqual(value["usage"], {"input_tokens": 9, "cache_read_input_tokens": 3})


class Finding4ResumeFailureVerifies(unittest.IsolatedAsyncioTestCase):
    async def test_a_failing_resume_check_still_verifies_and_restores(self):
        p = engine_patches(owned=patch.object(gittools, "ownership_changes", return_value=(False, "outside.txt")),
                           checks=patch.object(gittools, "run_checks", AsyncMock(return_value=(False, "failed"))))
        with p["context"], p["snapshot"], p["identity"], p["owned"], p["checks"], p["tree"], \
                patch.object(engine, "_restore_note", return_value=" Restored: outside.txt.") as restore:
            result = await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                          Settings(checks=[["true"]]), lambda e: None, asyncio.Event(), completed_indices={1})
        self.assertEqual(result.status, "blocked")
        restore.assert_called_once()

    async def test_an_exception_in_resume_checks_still_verifies(self):
        p = engine_patches(owned=patch.object(gittools, "ownership_changes", return_value=(False, "outside.txt")),
                           checks=patch.object(gittools, "run_checks", AsyncMock(side_effect=RuntimeError("check crashed"))))
        with p["context"], p["snapshot"], p["identity"], p["owned"], p["checks"], p["tree"], \
                patch.object(engine, "_restore_note", return_value="") as restore:
            await engine.execute(Plan("G", [Task("T", "do", ["a"], ["ok"], "codex")]), Path("/repo"),
                                 Settings(checks=[["true"]]), lambda e: None, asyncio.Event(), completed_indices={1})
        restore.assert_called_once()


class Finding5And12OverloadRetries(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, results, between=None):
        once = AsyncMock(side_effect=results)
        with patch.object(providers, "_run_once", once), patch.object(providers, "OVERLOAD_DELAYS", (0, 0)):
            result = await providers.run("codex", "p", Path("/w"), "builder", Settings(), lambda e: None,
                                         asyncio.Event(), 1000, between=between)
        return result, once

    async def test_a_retry_never_starts_before_the_first_invocation_is_verified(self):
        result, once = await self.run_with([Result("codex", "overloaded", usage={"input_tokens": 7})],
                                           between=lambda: "outside.txt")
        self.assertEqual(result.status, "blocked")
        self.assertEqual(once.await_count, 1)
        self.assertEqual(result.usage, {"input_tokens": 7})

    async def test_usage_of_every_invocation_is_kept(self):
        result, once = await self.run_with([Result("codex", "overloaded", usage={"input_tokens": 7}),
                                            Result("codex", "ok", usage={"input_tokens": 7})], between=lambda: None)
        self.assertEqual((result.status, once.await_count, result.usage["input_tokens"]), ("ok", 2, 14))


class Finding6NoTruncatedDisputes(unittest.IsolatedAsyncioTestCase):
    async def test_a_review_over_the_dispute_bound_is_repaired_whole(self):
        review = {"verdict": "changes_requested", "summary": "s",
                  "issues": [{"id": f"F{n}", "severity": "high", "text": "t"} for n in range(21)]}
        with patch.object(providers, "run", AsyncMock()) as run:
            kind, payload = await engine._dispute_round(1, Task("T", "do", ["a"], ["ok"]), "codex", "claude", review,
                                                        Path("/w"), Settings(), lambda e: None, asyncio.Event(), 1e12,
                                                        {1: {"reply_used": False}}, None, {}, 0)
        self.assertEqual((kind, payload), ("repair", review))
        run.assert_not_awaited()


class Finding7GitSeesEveryEdit(unittest.TestCase):
    def test_a_same_size_edit_with_mtime_put_back_is_seen_despite_lax_stat_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            git(repo, "config", "core.trustctime", "false")
            git(repo, "config", "core.checkStat", "minimal")
            path = repo / "src" / "keep.py"
            os.utime(path, (1577836800, 1577836800))             # an old mtime: the entry is never "racily clean"
            time.sleep(1.1)
            git(repo, "update-index", "--really-refresh")         # Git records the file's stat data now
            info = path.stat()
            path.write_text("x = 2\n")                            # same size, written in the same second
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            self.assertEqual(subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                                            text=True).stdout, "", "precondition: Git's stat cache misses the edit")
            self.assertIn("src/keep.py", gittools.status_paths(repo))


class Finding8FilterDrivers(unittest.TestCase):
    def test_a_repository_with_a_configured_filter_is_refused_and_converted_content_is_never_committed(self):
        from brahma import contracts
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / ".gitattributes").write_text("*.bin filter=demo\n")
            (repo / "a.bin").write_text("data\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "attrs")
            git(repo, "config", "filter.demo.clean", "cat")
            with self.assertRaisesRegex(ValueError, "filter driver"):
                contracts.prepare_contract(Plan("G", [Task("T", "do", ["x.txt"], ["ok"])]), repo, [["true"]])
            git(repo, "config", "--unset", "filter.demo.clean")
            git(repo, "checkout", "-q", "-b", "brahma/s")
            (repo / ".gitattributes").write_text("*.txt ident\n")
            (repo / "x.txt").write_text("$Id: not the blob $\n")      # `git add` would collapse this to $Id$
            with self.assertRaisesRegex(ValueError, "staged content differs"):
                gittools.commit_changes(repo, "s")
            self.assertEqual(git(repo, "diff", "--cached", "--name-only"), "")   # nothing left staged


class Finding9Submodules(unittest.TestCase):
    def test_submodules_are_refused_and_a_nested_repository_is_never_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / ".gitmodules").write_text('[submodule "s"]\n\tpath = s\n\turl = ./s\n')
            self.assertIn("submodules", gitsafe.unsupported_repository(repo))
            (repo / ".gitmodules").unlink()
            (repo / "sub").mkdir()
            git(repo / "sub", "init", "-q")
            (repo / "sub" / "f").write_text("x")
            ok, reason = gittools.ownership_changes(repo, ["sub/**", "sub"])
            self.assertFalse(ok)
            self.assertIn("sub", reason)


class Finding10RollbackIdentity(unittest.TestCase):
    def test_rollback_refuses_when_the_git_directory_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "outside.txt").write_text("new\n")
            identity = dict(gittools.git_identity(repo), git_dir="/somewhere/else/.git")
            restored, kept = gittools.restore_violations(repo, ["src/**"], {}, identity=identity)
            self.assertEqual(restored, [])
            self.assertIn("nothing was restored", kept[0])
            self.assertTrue((repo / "outside.txt").exists())


class Finding11AbsoluteProviderPaths(unittest.TestCase):
    def test_a_relative_path_entry_never_supplies_a_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / ".venv" / "bin"
            local.mkdir(parents=True)
            (local / "codex").write_text("#!/bin/sh\n")
            (local / "codex").chmod(0o755)
            old = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.dict(os.environ, {"PATH": ".venv/bin"}):
                    self.assertIsNone(providers.find_executable("codex"))
                with patch.dict(os.environ, {"PATH": str(local)}):
                    self.assertTrue(os.path.isabs(providers.find_executable("codex")))
            finally:
                os.chdir(old)


class Finding13EventsExport(unittest.TestCase):
    def test_every_stored_event_is_printed_without_follow(self):
        from brahma.cli import stream_events
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "state")
            self.addCleanup(store.close)
            key = store.create(Path(tmp))
            for n in range(501):
                store.event(key, Event(kind="message", text=str(n)))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                stream_events(store, key, 0, False)
            self.assertEqual(len(out.getvalue().splitlines()), 501)


class Finding14NoBlockingReads(unittest.TestCase):
    def test_a_fifo_instruction_file_or_configuration_is_refused_without_blocking(self):
        from brahma import contracts
        from brahma.config import Settings as Config
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            os.mkfifo(repo / "AGENTS.md")
            done, box = finishes(lambda: contracts.prepare_contract(Plan("G", [Task("T", "do", ["x.txt"], ["ok"])]),
                                                                    repo, [["true"]]))
            self.assertTrue(done, "contract preparation blocked on a FIFO")
            self.assertIsInstance(box.get("error"), ValueError)
            os.unlink(repo / "AGENTS.md")
            os.mkfifo(repo / "brahma.toml")
            done, box = finishes(lambda: Config.load(repo))
            self.assertTrue(done, "configuration loading blocked on a FIFO")
            self.assertIsInstance(box.get("error"), ValueError)


class Finding15SpecialFilesRollback(unittest.TestCase):
    def test_a_violating_fifo_is_removed_before_it_is_reported_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            with patch.object(gittools, "violating_paths", return_value=["pipe"]):
                os.mkfifo(repo / "pipe")
                restored, kept = gittools.restore_violations(repo, ["src/**"], {})
            self.assertEqual((restored, kept), (["pipe"], []))
            self.assertFalse(os.path.lexists(repo / "pipe"))


if __name__ == "__main__":
    unittest.main()


class AutomatedSecurityReviewOfE2d7d15(unittest.TestCase):
    def test_dotted_credential_names_are_hidden_and_only_numeric_token_counters_kept(self):
        value = safety.redact_deep({"db.password": "plain", "max_tokens": "zz-not-a-number", "input_tokens": 4,
                                    "passwords.py": "100644:abc"})
        self.assertEqual(value, {"db.password": safety.REDACTED, "max_tokens": safety.REDACTED, "input_tokens": 4,
                                 "passwords.py": "100644:abc"})

    def test_paths_that_line_based_hashing_would_misread_are_hashed_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            for name in ("odd\nname.txt", '"quoted.txt', "cr\r.txt"):
                (repo / name).write_text("one\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "odd names")
            self.assertEqual(gittools.content_changed_tracked(repo), [])
            (repo / "odd\nname.txt").write_text("two\n")
            self.assertEqual(gittools.content_changed_tracked(repo), ["odd\nname.txt"])

    def test_the_filter_check_reads_the_revision_being_checked_out_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            git(repo, "config", "filter.demo.clean", "cat")
            (repo / ".gitattributes").write_text("*.bin filter=demo\n")
            (repo / "a.bin").write_text("data\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-qm", "filtered")
            revision = git(repo, "rev-parse", "HEAD")
            (repo / ".gitattributes").unlink()                   # the working tree no longer shows the attribute
            self.assertIn("filter driver", gitsafe.unsupported_repository(repo, revision))
            git(repo, "rm", "-q", "--cached", ".gitattributes")
            (repo / ".gitmodules").write_text("x")
            git(repo, "add", ".gitmodules")
            git(repo, "commit", "-qm", "modules")
            (repo / ".gitmodules").unlink()                      # present in the revision only
            self.assertIn("submodules", gitsafe.unsupported_repository(repo, git(repo, "rev-parse", "HEAD")))
