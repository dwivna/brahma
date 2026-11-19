"""Packet-driven briefs, one bounded context expansion, scoped notes (0.9.0)."""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import briefs, engine, gittools
from brahma.config import Settings
from brahma.models import Plan, Result, Task

TASK = Task("T", "do", ["src/t.py"], ["ok"], "codex", scenarios=["Given a file When run Then ok"])


class Pure(unittest.TestCase):
    def test_brief_carries_the_validated_packet(self):
        packet = briefs.make_packet(TASK, 1, "review", "a" * 12, [["true"]], diff_ref="abc1234",
                                    check_results=[(["true"], "passed", 0)])
        data = json.loads(briefs.brief(TASK, packet))
        self.assertEqual(data["handoff_packet"]["phase"], "review")
        self.assertEqual(data["handoff_packet"]["diff_ref"], "abc1234")
        self.assertNotIn("handoff_packet", json.loads(briefs.brief(TASK, None)))

    def test_missing_context_line_is_parsed_and_capped(self):
        self.assertEqual(briefs.missing_context("done\nMISSING_CONTEXT: a.py, `b/c.py`\n"), ["a.py", "b/c.py"])
        self.assertEqual(briefs.missing_context("nothing asked"), [])
        self.assertLessEqual(len(briefs.missing_context("MISSING_CONTEXT: " + ",".join(f"f{i}" for i in range(50)))), 10)

    def test_expansion_reads_only_safe_regular_files_inside_the_worktree(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            root = Path(d)
            (root / "ok.py").write_text("print(1)\n")
            (root / ".env").write_text("KEY=sk-SYNTHETICabcdefghij123456")
            (root / "link.py").symlink_to(Path(outside) / "x")
            (Path(outside) / "x").write_text("outside")
            text, accepted, rejected = briefs.expansion(root, ["ok.py", ".env", "link.py", "../x", "/etc/passwd", "nope.py"], 10000)
            self.assertEqual(accepted, ["ok.py"])
            self.assertEqual(sorted(rejected), sorted([".env", "link.py", "../x", "/etc/passwd", "nope.py"]))
            self.assertNotIn("SYNTHETIC", text)
            self.assertNotIn("outside", text)

    def test_expansion_rejects_case_variants_hardlinks_and_sanitizes_reported_names(self):
        import os
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outside:
            root = Path(d)
            (root / ".ENV").write_text("A=1")
            (root / "Custody").mkdir()
            (root / "Custody" / "x.txt").write_text("secret")
            (Path(outside) / "t.txt").write_text("outside-secret")
            try:
                os.link(Path(outside) / "t.txt", root / "hard.txt")
            except OSError:
                self.skipTest("hardlinks across directories unavailable")
            odd = "sk-SYNTHETICabcdefghij123456.py"
            text, accepted, rejected = briefs.expansion(root, [".ENV", "Custody/x.txt", "hard.txt", odd], 10000)
            self.assertEqual((text, accepted), ("", []))
            self.assertNotIn("SYNTHETIC", json.dumps(rejected))

    def test_expansion_is_bounded_in_count_and_size(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for i in range(8):
                (root / f"f{i}.txt").write_text("x" * 20000)
            text, accepted, rejected = briefs.expansion(root, [f"f{i}.txt" for i in range(8)], 100000)
            self.assertLessEqual(len(accepted), briefs.MAX_EXPANSION_FILES)
            self.assertLessEqual(len(text), 5 * (briefs.MAX_EXPANSION_FILE_CHARS + 40))
            small, acc, rej = briefs.expansion(root, ["f0.txt"], 100)
            self.assertEqual((acc, rej), ([], ["f0.txt"]))

    def test_notes_are_scoped_sanitized_and_applied_once(self):
        notes = [briefs.Note(1, None, "all tasks"), briefs.Note(2, 2, "only task two"), briefs.Note(3, 1, "only task one")]
        text, seen = briefs.notes_for(notes, 1, 0)
        self.assertIn("all tasks", text)
        self.assertIn("only task one", text)
        self.assertNotIn("only task two", text)
        self.assertIn("cannot change scope", text)
        self.assertEqual(seen, 3)
        self.assertEqual(briefs.notes_for(notes, 1, seen), ("", seen))
        with self.assertRaises(ValueError):
            briefs.clean_note("   ")
        self.assertNotIn("SYNTHETIC", briefs.clean_note("use sk-SYNTHETICabcdefghij123456"))
        self.assertLessEqual(len(briefs.clean_note("x" * 5000)), briefs.MAX_NOTE_CHARS)


class Engine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "x")):
            subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True, capture_output=True)
        (self.root / "helper.py").write_text("HELPER = 1\n")
        self.prompts, self.events = [], []
        for p in (patch.object(engine, "_context", return_value="ctx"),
                  patch.object(gittools, "ownership_changes", return_value=(True, "Owned")),
                  patch.object(gittools, "run_checks", AsyncMock(return_value=(True, "ok")))):
            p.start()
            self.addCleanup(p.stop)

    async def run_flow(self, script, settings=None, progress=None):
        queue = list(script)

        async def run(provider, prompt, cwd, role, s, emit, cancel, timeout, native_session_id=None):
            self.prompts.append((role, prompt))
            item = queue.pop(0)
            return item[1]
        settings = settings or Settings(checks=[["true"]])
        with patch.object(engine.providers, "run", side_effect=run):
            out = await engine.execute(Plan("G", [TASK]), self.root, settings, self.events.append, asyncio.Event(), task_progress=progress)
        self.assertEqual(queue, [])
        return out

    PASS = json.dumps({"verdict": "pass", "summary": "ok", "issues": []})

    async def test_builder_and_reviewer_briefs_contain_phase_packets(self):
        settings = Settings(checks=[["true"]])
        settings._packet_home = self.root / "state"
        settings._session_key = "b" * 12
        result = await self.run_flow([("b", Result("codex", "ok", "done")), ("r", Result("claude", "ok", self.PASS))], settings)
        self.assertEqual(result.status, "ready")
        builder, reviewer = self.prompts[0][1], self.prompts[1][1]
        self.assertIn('"phase": "build"', builder)
        self.assertIn('"phase": "review"', reviewer)
        self.assertRegex(reviewer, r'"diff_ref": "[0-9a-f]{40}"')
        self.assertTrue(any((self.root / "state").rglob("*.json")))      # materialized read-only outside the repo

    async def test_one_context_expansion_then_never_again(self):
        built = Result("codex", "ok", "need more\nMISSING_CONTEXT: helper.py, .env")
        result = await self.run_flow([("b", built), ("b2", Result("codex", "ok", "done\nMISSING_CONTEXT: helper.py")),
                                      ("r", Result("claude", "ok", self.PASS))])
        self.assertEqual(result.status, "ready")
        self.assertIn("HELPER = 1", self.prompts[1][1])                  # widened prompt carries the file
        self.assertEqual(len(self.prompts), 3)                           # second MISSING_CONTEXT triggers nothing
        info = [json.loads(e.text) for e in self.events if e.kind == "context_expansion"]
        self.assertEqual(len(info), 1)
        self.assertEqual((info[0]["accepted"], info[0]["rejected"]), (["helper.py"], [".env"]))

    async def test_expansion_survives_a_restart_without_repeating(self):
        await self.run_flow([("b", Result("codex", "ok", "MISSING_CONTEXT: helper.py")), ("b2", Result("codex", "ok", "d")),
                             ("r", Result("claude", "ok", self.PASS))])
        saved = [json.loads(e.text) for e in self.events if e.kind == "task_state"][-1]
        self.assertEqual(saved["expansion"], ["helper.py"])
        self.prompts.clear()
        await self.run_flow([("b", Result("codex", "ok", "MISSING_CONTEXT: helper.py")),
                             ("r", Result("claude", "ok", self.PASS))], progress={1: dict(saved, phase="build", state="building")})
        self.assertEqual(len(self.prompts), 2)                           # no second widening

    async def test_notes_reach_the_next_checkpoint_once_and_are_recorded(self):
        settings = Settings(checks=[["true"]])
        settings._notes = [briefs.Note(4, None, "prefer small functions")]
        await self.run_flow([("b", Result("codex", "ok", "d")), ("r", Result("claude", "ok", self.PASS))], settings)
        self.assertIn("prefer small functions", self.prompts[0][1])
        self.assertNotIn("prefer small functions", self.prompts[1][1])   # reviewer is not instructed by builder notes
        self.assertEqual([e.text for e in self.events if e.kind == "note_applied"], ["Applied notes up to #4 at this checkpoint"])
        self.assertEqual([json.loads(e.text) for e in self.events if e.kind == "task_state"][-1]["notes_seen"], 4)


class Cli(unittest.TestCase):
    def test_note_command_saves_a_sanitized_note_and_session_reads_it_back(self):
        from tests.test_cli import repo_session, run_cli
        from brahma.store import Store
        with tempfile.TemporaryDirectory() as d:
            store, session = repo_session(d)
            store.close()
            state = Path(d) / "state"
            code, out, _ = run_cli("--json", "note", session.key, "use sk-SYNTHETICabcdefghij123456 please", "--task", "1", state=state)
            self.assertEqual(code, 0)
            check = Store(state)
            notes = [json.loads(e["text"]) for e in check.events_after(session.key, 0) if e["kind"] == "user_note"]
            check.close()
            self.assertEqual(notes[0]["task"], 1)
            self.assertNotIn("SYNTHETIC", notes[0]["text"])
            self.assertEqual(run_cli("--json", "note", session.key, "  ", state=state)[0], 20)
            self.assertEqual(run_cli("--json", "note", session.key, "x", "--task", "0", state=state)[0], 20)
            self.assertEqual(run_cli("--json", "note", "nosuchsession", "x", state=state)[0], 20)


if __name__ == "__main__":
    unittest.main()
