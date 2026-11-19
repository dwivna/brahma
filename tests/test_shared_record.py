"""Shared record, handoff packets and repository memory (Rounds 4 and 5; task 1 of the Rounds 4-7 plan)."""
from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from brahma import handoff, memory
from brahma.memory import Memory
from brahma.models import Event
from brahma.store import SCHEMA_VERSION, Store


def make_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.name", "t"), ("user.email", "t@e.invalid")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    (root / "a.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    return root


def good(**overrides) -> dict:
    data = {"version": 1, "session": "abc123", "task_index": 2, "phase": "review",
            "gherkin": ["Scenario: Export\n  Given a table\n  When I export\n  Then a CSV exists"],
            "owned_paths": ["src/export.py", "tests/test_export.py"], "checks": [["python", "-m", "unittest"]],
            "diff_ref": "a1b2c3d..e4f5a6b",
            "check_results": [{"argv": ["python", "-m", "unittest"], "status": "passed", "exit_code": 0}],
            "findings": [{"id": "F1", "severity": "high", "path": "src/export.py", "text": "Header row missing"}]}
    data.update(overrides)
    return data


class Migration(unittest.TestCase):
    def old_database(self, home: Path):
        home.mkdir()
        db = sqlite3.connect(home / "state.sqlite3")
        db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, repo TEXT, created TEXT, status TEXT, plan TEXT, worktree TEXT)")
        db.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, session TEXT, created TEXT, provider TEXT, role TEXT, kind TEXT, text TEXT, usage TEXT)")
        db.execute("INSERT INTO sessions VALUES ('old1','/repo','2026-11-01','blocked','{}',NULL)")
        db.execute("INSERT INTO events(session,created,provider,role,kind,text,usage) VALUES ('old1','t','codex','b','note','kept','{}')")
        db.commit()
        db.close()

    def test_existing_database_opens_and_keeps_its_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            self.old_database(home)
            store = Store(home)
            self.assertEqual(store.session("old1")["status"], "blocked")
            self.assertEqual(store.events("old1")[0]["text"], "kept")
            store.close()
            self.assertEqual(sqlite3.connect(home / "state.sqlite3").execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_task_state_and_attention_survive_reopening(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            self.old_database(home)
            store = Store(home)
            store.set_task_state("old1", 1, "check", "checking", attempt=2)
            store.set_task_state("old1", 1, "review", "reviewing", attempt=2)
            store.set_task_state("old1", 2, "build", "building")
            note = store.attention("old1", "decision", "Reviewer and author disagree")
            store.close()
            again = Store(home)
            states = again.task_states("old1")
            self.assertEqual([(s["task_index"], s["phase"], s["attempt"]) for s in states], [(1, "review", 2), (2, "build", 0)])
            self.assertEqual([a["id"] for a in again.open_attention("old1")], [note])
            again.resolve_attention(note)
            self.assertEqual(again.open_attention(), [])
            again.close()

    def test_migration_is_repeatable_and_unknown_newer_state_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "state"
            Store(home).close()
            Store(home).close()  # idempotent
            db = sqlite3.connect(home / "state.sqlite3")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            db.commit()
            db.close()
            with self.assertRaisesRegex(ValueError, "newer than this Brahma"):
                Store(home)

    def test_unknown_session_and_attention_kind_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            with self.assertRaises(ValueError):
                store.set_task_state("nope", 1, "build", "x")
            sid = store.create(Path(tmp))
            with self.assertRaises(ValueError):
                store.attention(sid, "gossip", "x")
            store.close()


class PacketSchema(unittest.TestCase):
    def test_a_review_packet_carries_exactly_the_bounded_evidence(self):
        packet = handoff.parse(good())
        data = packet.to_dict()
        self.assertEqual(set(data), {"version", "session", "task_index", "phase", "gherkin", "owned_paths", "checks",
                                     "diff_ref", "check_results", "findings"})
        self.assertEqual(data["diff_ref"], "a1b2c3d..e4f5a6b")
        self.assertEqual(data["findings"][0]["severity"], "high")
        text = json.dumps(data).lower()
        for banned in ("summary", "history", "transcript", "conversation"):
            self.assertNotIn(banned, text)

    def test_author_summary_history_and_transcripts_are_rejected(self):
        for field in ("summary", "author_summary", "history", "conversation", "transcript", "messages", "notes"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "not allowed"):
                handoff.parse(good(**{field: "I fixed everything, trust me"}))

    def test_unknown_and_invalid_fields_are_rejected(self):
        bad = (dict(extra="x"), dict(version=2), dict(phase="deploy"), dict(task_index=0), dict(task_index=True),
               dict(session=""), dict(gherkin=[]), dict(gherkin=[""]), dict(owned_paths=[]), dict(owned_paths=["/etc/passwd"]),
               dict(owned_paths=["../x"]), dict(diff_ref="my latest changes"), dict(diff_ref="HEAD; rm -rf /"),
               dict(diff_ref=None), dict(check_results=[{"argv": ["x"], "status": "great"}]),
               dict(check_results=[{"argv": ["x"], "status": "passed", "stdout": "log"}]),
               dict(check_results=[{"argv": ["x"], "status": "passed", "exit_code": "0"}]),
               dict(findings=[{"id": "1", "severity": "urgent", "text": "x"}]),
               dict(findings=[{"id": "1", "severity": "low", "text": "x", "author_note": "y"}]),
               dict(checks=[["ok"]] * 51), dict(gherkin=["x" * 2001]))
        for override in bad:
            with self.subTest(override=str(override)[:60]), self.assertRaises(ValueError):
                handoff.parse(good(**override))
        with self.assertRaises(ValueError):
            handoff.parse(["not", "an", "object"])

    def test_secrets_are_refused_everywhere_text_is_accepted(self):
        secrets = ("api_key = sk-abcdefghijklmnop12345", "token: ghp_abcdefghijklmnopqrstu", "AKIAABCDEFGHIJKLMNOP",
                   "-----BEGIN RSA PRIVATE KEY-----", "password=hunter2hunter2")
        for secret in secrets:
            for override in (dict(gherkin=[secret]), dict(findings=[{"id": "1", "severity": "low", "text": secret}]),
                             dict(checks=[["run", secret]])):
                with self.subTest(secret=secret[:20], field=list(override)[0]), self.assertRaisesRegex(ValueError, "secret"):
                    handoff.parse(good(**override))

    def test_a_build_packet_needs_no_diff_but_a_review_packet_does(self):
        handoff.parse(good(phase="build", diff_ref=None, check_results=[], findings=[]))
        with self.assertRaisesRegex(ValueError, "diff reference"):
            handoff.parse(good(phase="review", diff_ref=None))

    def test_oversized_packets_are_refused(self):
        big = [f"Scenario: s{n}\n  Given {'x' * 1900}" for n in range(40)]
        with self.assertRaisesRegex(ValueError, "too large"):
            handoff.parse(good(gherkin=big))


class PacketIsReadOnly(unittest.TestCase):
    def test_materialized_packet_is_outside_the_repo_read_only_and_separate_from_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, home = make_repo(Path(tmp) / "repo"), Path(tmp) / "state"
            store = Store(home)
            packet = handoff.parse(good())
            path = handoff.materialize(home, packet)
            self.assertFalse(path.is_relative_to(repo))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
            with self.assertRaises(PermissionError):
                open(path, "w").write("tampered")
            self.assertEqual(handoff.load(path), packet)
            self.assertNotEqual(path.name, "state.sqlite3")
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            store.close()

    def test_packet_objects_cannot_be_mutated_and_the_module_has_no_store_access(self):
        packet = handoff.parse(good())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            packet.phase = "build"
        with self.assertRaises(AttributeError):
            packet.owned_paths.append("src/other.py")  # a tuple
        namespace = vars(handoff)
        self.assertNotIn("Store", namespace)
        self.assertNotIn("sqlite3", namespace)
        self.assertNotIn("store", namespace)
        self.assertNotRegex(Path(handoff.__file__).read_text(), r"^\s*(from \.store|from \. import .*store|import sqlite3)", )

    def test_a_tampered_or_symlinked_packet_file_is_refused_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = handoff.materialize(home, handoff.parse(good()))
            os.chmod(path, 0o600)
            data = json.loads(path.read_text())
            data["summary"] = "all good"
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "not allowed"):
                handoff.load(path)
            link = home / "link.json"
            link.symlink_to(path)
            with self.assertRaisesRegex(ValueError, "symlink"):
                handoff.load(link)

    def test_rewriting_a_packet_replaces_the_read_only_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = handoff.materialize(Path(tmp), handoff.parse(good()))
            second = handoff.materialize(Path(tmp), handoff.parse(good(findings=[])))
            self.assertEqual(first, second)
            self.assertEqual(handoff.load(second).findings, ())


class RepositoryMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo_a, self.repo_b = make_repo(self.root / "a"), make_repo(self.root / "b")
        self.home = self.root / "state"
        self.store = Store(self.home)
        self.memory = Memory(self.store)

    def test_identity_is_stable_across_worktrees_and_distinct_between_repositories(self):
        worktree = self.root / "wt"
        subprocess.run(["git", "-C", str(self.repo_a), "worktree", "add", "-q", "-b", "brahma/x", str(worktree)], check=True)
        self.assertEqual(memory.repo_id(worktree), memory.repo_id(self.repo_a))
        self.assertNotEqual(memory.repo_id(self.repo_a), memory.repo_id(self.repo_b))
        with self.assertRaises(ValueError):
            memory.repo_id(self.root)

    def test_remember_recall_update_and_validation(self):
        a = memory.repo_id(self.repo_a)
        self.memory.remember(a, "convention", "tests", "unittest discover")
        self.memory.remember(a, "convention", "tests", "pytest -q")  # same key updates
        self.memory.remember(a, "failure", "flaky", "test_x fails when run twice")
        self.assertEqual([(m["kind"], m["key"], m["value"]) for m in self.memory.recall(a)],
                         [("convention", "tests", "pytest -q"), ("failure", "flaky", "test_x fails when run twice")])
        self.assertEqual(len(self.memory.recall(a, "failure")), 1)
        for args in (("gossip", "k", "v"), ("decision", "", "v"), ("decision", "k", " "), ("decision", "k", "x" * 2001)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.memory.remember(a, *args)
        with self.assertRaisesRegex(ValueError, "secrets"):
            self.memory.remember(a, "decision", "deploy", "api_key = sk-abcdefghijklmnop12345")

    def test_forgetting_one_repository_keeps_everything_else_across_restart(self):
        a, b = memory.repo_id(self.repo_a), memory.repo_id(self.repo_b)
        sid = self.store.create(self.repo_a)
        self.store.set_task_state(sid, 1, "check", "checking", 1)
        self.store.enqueue(sid, "gen", "d" * 64, 0.0, "due")
        self.store.event(sid, Event(provider="codex", role="b", kind="note", text="checkpoint"))
        for repo in (a, b):
            self.memory.remember(repo, "decision", "d", "use sqlite")
            for _ in range(6):
                self.memory.record_outcome(repo, provider="codex", task_type="feature", checks_passed=True, repairs=1,
                                           findings=0, ready=True, session=sid)
        self.assertTrue(self.memory.routing_history(a)["codex"]["feature"]["learned"])
        removed = self.memory.forget(a)
        self.assertEqual((removed["memory_removed"], removed["outcomes_removed"]), (1, 6))
        self.store.close()
        store = Store(self.home)  # restart
        again = Memory(store)
        self.assertEqual((again.recall(a), again.routing_history(a)), ([], {}))
        self.assertEqual(again.inspect(a)["outcomes"], [])
        self.assertEqual(len(again.recall(b)), 1)
        self.assertEqual(again.routing_history(b)["codex"]["feature"]["samples"], 6)
        self.assertEqual(store.session(sid)["repo"], str(self.repo_a))            # durable session record
        self.assertEqual(store.task_states(sid)[0]["state"], "checking")           # run checkpoint
        self.assertEqual(len(store.pending()), 1)                                  # job / approval queue
        self.assertEqual(store.events(sid)[0]["text"], "checkpoint")
        store.close()

    def test_routing_says_no_data_yet_until_enough_samples(self):
        a = memory.repo_id(self.repo_a)
        for n in range(memory.MIN_SAMPLES - 1):
            self.memory.record_outcome(a, provider="claude", task_type="bugfix", checks_passed=n % 2 == 0, repairs=n,
                                       findings=1, ready=n % 2 == 0)
        entry = self.memory.routing_history(a)["claude"]["bugfix"]
        self.assertFalse(entry["learned"])
        self.assertIn("no data yet", entry["note"])
        self.memory.record_outcome(a, provider="claude", task_type="bugfix", checks_passed=True, repairs=0, findings=0, ready=True)
        self.assertTrue(self.memory.routing_history(a, "bugfix")["claude"]["bugfix"]["learned"])
        self.assertEqual(self.memory.routing_history(a, "other"), {})

    def test_measured_usage_and_estimates_are_stored_in_separate_fields(self):
        a = memory.repo_id(self.repo_a)
        self.memory.record_outcome(a, provider="codex", task_type="feature", checks_passed=True, repairs=0, findings=0,
                                   ready=True, usage_measured={"tokens": 1200}, usage_estimated={"tokens": 900})
        self.memory.record_outcome(a, provider="codex", task_type="feature", checks_passed=True, repairs=0, findings=0, ready=True)
        first, second = self.memory.inspect(a)["outcomes"]
        self.assertEqual(json.loads(first["usage_measured"]), {"tokens": 1200})
        self.assertEqual(json.loads(first["usage_estimated"]), {"tokens": 900})
        self.assertIsNone(second["usage_measured"])  # an absent measurement is not filled from an estimate
        self.assertIsNone(second["usage_estimated"])

    def test_invalid_outcomes_are_rejected(self):
        a = memory.repo_id(self.repo_a)
        for kwargs in (dict(provider="gpt"), dict(task_type=" "), dict(repairs=-1), dict(findings=-1)):
            base = dict(provider="codex", task_type="t", checks_passed=True, repairs=0, findings=0, ready=True)
            base.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.memory.record_outcome(a, **base)


if __name__ == "__main__":
    unittest.main()
