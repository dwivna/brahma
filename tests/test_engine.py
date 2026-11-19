from __future__ import annotations

import asyncio
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from brahma import engine, gittools
from brahma.config import Settings
from brahma.models import Plan, Result, Task


def task(title="Implement", provider="auto", complexity="routine"):
    return Task(title, "Implement the behavior", [title + ".py"], ["Behavior works"], provider, complexity)


def review(verdict="pass", issues=None):
    return Result("claude", "ok", json.dumps({"verdict": verdict, "summary": "Inspected actual changes",
                                              "issues": issues or []}))


class ReviewTests(unittest.TestCase):
    def test_review_requires_exact_structured_verdict(self):
        for value in ("PASS", "Tests PASS and no issues", '{"verdict":"pass"}',
                      '{"verdict":"pass","summary":"ok","issues":["bug"]}',
                      '{"verdict":"pass","summary":"ok","issues":[],"other":1}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                engine._review(value)
        self.assertEqual(engine._review(review().text)["verdict"], "pass")


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []
        self.cancel = asyncio.Event()
        self.calls = []
        self.settings = Settings(checks=[["python", "-m", "unittest"]], repair_cycles=3)
        self.patches = [
            patch.object(engine, "_context", return_value="AGENTS: preserve accepted scope"),
            patch.object(gittools, "snapshot_changes", return_value={}, create=True),
            patch.object(gittools, "ownership_changes", return_value=(True, "Owned")),
            patch.object(gittools, "run_checks", AsyncMock(return_value=(True, "Actual checks: exit 0"))),
        ]
        self.mocks = [p.start() for p in self.patches]
        for p in self.patches:
            self.addCleanup(p.stop)

    async def execute(self, responses, tasks=None, settings=None, completed=None, progress=None):
        responses = iter(responses)
        active = 0
        async def run(provider, prompt, cwd, role, selected_settings, emit, cancel, timeout):
            nonlocal active
            active += 1
            self.assertEqual(active, 1, "Workers must never overlap")
            self.calls.append((provider, role, prompt, selected_settings, timeout))
            await asyncio.sleep(0)
            active -= 1
            return next(responses)
        with patch.object(engine.providers, "run", side_effect=run):
            return await engine.execute(Plan("Goal", tasks or [task()]), Path("/repo"),
                        settings or self.settings, self.events.append, self.cancel,
                        completed_indices=completed, task_progress=progress)

    def states(self):
        return [json.loads(e.text) for e in self.events if e.kind == "task_state"]

    async def test_success_is_sequential_independent_and_uses_actual_checks(self):
        result = await self.execute([Result("codex", "ok", "all done"), review()])
        self.assertEqual(result.status, "ready")
        self.assertEqual([(p, r) for p, r, *_ in self.calls], [("codex", "builder"), ("claude", "reviewer")])
        self.assertIn("Actual checks: exit 0", self.calls[1][2])
        self.assertIn("Behavior works", self.calls[1][2])
        self.assertEqual(self.states()[-1]["state"], "ready")
        self.assertEqual(self.states()[-1]["index"], 1)

    async def test_empty_checks_never_claim_ready(self):
        result = await self.execute([Result("codex", "ok", "tests passed"), review()], settings=Settings(checks=[]))
        self.assertEqual(result.status, "needs_checks")
        self.assertNotIn("ready", [s["state"] for s in self.states()])
        self.mocks[3].assert_awaited_once()

    async def test_check_failure_uses_exactly_three_repair_cycles(self):
        self.mocks[3].return_value = (False, "Actual failure")
        result = await self.execute([Result("codex", "ok", "all tests pass")] * 4)
        self.assertEqual(result.status, "incomplete")
        self.assertEqual(len(self.calls), 4)
        self.assertTrue(all(role == "builder" for _, role, *_ in self.calls))
        self.assertIn("Actual failure", self.calls[1][2])
        self.assertEqual(max(s["attempt"] for s in self.states()), 3)

    async def test_review_feedback_gets_bounded_repair(self):
        result = await self.execute([Result("codex", "ok"), review("changes_requested", ["bug in validation"]),
                                     Result("codex", "ok"), review()])
        self.assertEqual(result.status, "ready")
        self.assertEqual(len(self.calls), 4)
        self.assertIn("bug in validation", self.calls[2][2])

    async def test_plaintext_pass_is_not_accepted(self):
        result = await self.execute([Result("codex", "ok"), Result("claude", "ok", "PASS")],
                                    settings=Settings(checks=[["true"]], repair_cycles=0))
        self.assertEqual(result.status, "incomplete")
        self.assertNotIn("ready", [s["state"] for s in self.states()])

    async def test_second_vendor_limits_block_and_keep_draft(self):
        result = await self.execute([Result("codex", "ok"), Result("claude", "limited", "limit")])
        self.assertEqual(result.status, "blocked")
        self.assertIn("draft retained", result.text)
        self.assertEqual(len(self.calls), 2)

    async def test_builder_unavailable_never_falls_back(self):
        result = await self.execute([Result("codex", "unavailable", "subscription required")])
        self.assertEqual(result.status, "blocked")
        self.assertEqual(len(self.calls), 1)

    async def test_ownership_failure_blocks_before_checks_or_review(self):
        self.mocks[2].return_value = (False, "outside approved files")
        result = await self.execute([Result("codex", "ok")])
        self.assertEqual(result.status, "blocked")
        self.mocks[3].assert_not_awaited()

    async def test_questions_never_execute(self):
        with patch.object(engine.providers, "run", AsyncMock()) as run:
            result = await engine.execute(Plan("Goal", [task()], ["Need a decision"]), Path("/repo"),
                                          self.settings, self.events.append, self.cancel)
        self.assertEqual(result.status, "blocked")
        run.assert_not_awaited()

    async def test_cancel_before_worker(self):
        self.cancel.set()
        result = await self.execute([])
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(self.calls)

    async def test_critical_models_and_explicit_claude_builder(self):
        result = await self.execute([Result("claude", "ok"), Result("codex", "ok", review().text)],
                                    tasks=[task(provider="claude", complexity="critical")])
        self.assertEqual(result.status, "ready")
        self.assertEqual(self.calls[0][3].claude_model, "opus")
        self.assertEqual(self.calls[1][3].codex_model, "gpt-6.2-astra")

    async def test_task_baseline_once_across_repairs_and_new_each_task(self):
        self.mocks[1].side_effect = [{"old.py": "one"}, {"Implement.py": "two"}]
        result = await self.execute([Result("codex", "ok"), review(), Result("codex", "ok"), review()],
                                    tasks=[task(), task("Next")])
        self.assertEqual(result.status, "ready")
        baselines = [call.kwargs["baseline"] for call in self.mocks[2].call_args_list]
        self.assertEqual(baselines, [{"old.py": "one"}] * 2 + [{"Implement.py": "two"}] * 2)

    async def test_resume_skips_only_recorded_ready_task_indices(self):
        result = await self.execute([Result("codex", "ok"), review()], tasks=[task(), task("Next")], completed={1})
        self.assertEqual(result.status, "ready")
        self.assertEqual(self.states()[0]["index"], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.mocks[3].await_count, 2)

    async def test_all_ready_resume_rechecks_without_model_calls(self):
        result = await self.execute([], completed={1})
        self.assertEqual(result.status, "ready")
        self.assertFalse(self.calls)
        self.mocks[3].assert_awaited_once()
        self.assertEqual(self.states(), [])

    async def test_all_ready_resume_check_failure_never_claims_ready(self):
        self.mocks[3].return_value = (False, "Actual check failed")
        result = await self.execute([], completed={1})
        self.assertEqual(result.status, "incomplete")
        self.assertIn("Actual check failed", result.text)

    async def test_missing_check_executable_is_checkpointed(self):
        self.mocks[3].side_effect = FileNotFoundError("missing test command")
        result = await self.execute([Result("codex", "ok")])
        self.assertEqual(result.status, "blocked")
        self.assertIn("worktree retained", result.text)
        self.assertTrue(any(e.kind == "checkpoint" for e in self.events))

    async def test_persisted_attempt_budget_is_not_reset_on_resume(self):
        result = await self.execute([], progress={1: {"deadline_epoch": time.time()+100, "next_attempt": 4}})
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(self.calls)
        self.assertEqual(self.states()[-1]["next_attempt"], 4)

    async def test_persisted_deadline_is_not_reset_on_resume(self):
        result = await self.execute([], progress={1: {"deadline_epoch": time.time()-1, "next_attempt": 1}})
        self.assertEqual(result.status, "incomplete")
        self.assertFalse(self.calls)

    async def test_attempt_is_charged_before_worker_limit(self):
        result = await self.execute([Result("codex", "limited")],
                     progress={1: {"deadline_epoch": time.time()+100, "next_attempt": 2}})
        self.assertEqual(result.status, "blocked")
        self.assertEqual(self.states()[-1]["next_attempt"], 3)
        self.assertEqual(self.states()[-1]["attempt"], 2)

    async def test_deadline_is_shared_across_build_checks_and_review(self):
        async def check(*args):
            await asyncio.sleep(.06)
            return True, "exit 0"
        self.mocks[3].side_effect = check
        result = await self.execute([Result("codex", "ok"), review()],
                                    settings=Settings(task_timeout=.04, checks=[["true"]]))
        self.assertNotEqual(result.status, "ready")
        self.assertLessEqual(self.calls[-1][-1], 0)

    async def test_planner_strict_json_and_questions(self):
        proposal = Plan("Need decision", [], ["Which target?"])
        with patch.object(engine.providers, "run", AsyncMock(return_value=Result("codex", "ok", json.dumps(proposal.to_dict())))):
            result = await engine.plan("Build it", Path("/repo"), self.settings, self.events.append, self.cancel)
        self.assertEqual(result.questions, ["Which target?"])
        with patch.object(engine.providers, "run", AsyncMock(return_value=Result("codex", "ok", "Here is a plan"))):
            with self.assertRaises(ValueError):
                await engine.plan("Build it", Path("/repo"), self.settings, self.events.append, self.cancel)

    async def test_planner_prompt_sentences_are_not_spliced(self):
        proposal = Plan("Need decision", [], ["Which target?"])
        run = AsyncMock(return_value=Result("codex", "ok", json.dumps(proposal.to_dict())))
        with patch.object(engine.providers, "run", run):
            await engine.plan("Build it", Path("/repo"), self.settings, self.events.append, self.cancel)
        prompt = run.call_args.args[1]
        # The complexity guidance must not interrupt the instruction to ask questions.
        self.assertIn("Ask questions when scope or a user decision cannot be established", prompt)
        self.assertIn("Use critical for architecture, security", prompt)
        self.assertLess(prompt.index("Use critical for"), prompt.index("Ask questions when"))


if __name__ == "__main__":
    unittest.main()
