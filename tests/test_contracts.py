import copy
import json
from pathlib import Path
import tempfile
import unittest

from brahma.contracts import (prepare_contract, read_contract, render_feature,
                              validate_ownership, verify_contract, write_contract)
from brahma.discovery import discover
from brahma.models import Plan, Task
from test_core import init_repo


class Contracts(unittest.TestCase):
    def plan(self):
        return Plan("Useful behavior", [Task("Implement", "Make the behavior", ["src/**"],
                                            ["The requested behavior works"], "codex"),
                                        Task("Tests", "Verify the behavior", ["tests/**"],
                                             ["Regression checks pass"], "claude")],
                    checks=[["python", "-m", "unittest"]])

    def test_plan_roundtrip_and_acceptance_feature_is_honestly_labelled(self):
        plan = self.plan()
        plan.tasks[0].allowed_skills = ["codex/skill/example"]
        clone = Plan.parse(json.dumps(plan.to_dict()))
        self.assertEqual(clone, plan)
        feature = render_feature(plan)
        self.assertIn("not an executable BDD test suite", feature)
        self.assertIn("@task_2", feature)
        self.assertIn("Then The requested behavior works", feature)

    def test_immutable_artifacts_reject_feature_or_payload_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            contract = prepare_contract(self.plan(), repo)
            path = write_contract(Path(tmp) / "contracts", contract)
            self.assertEqual(read_contract(path), contract)
            write_contract(path.parent, contract)  # Idempotent, never overwrites.
            path.with_suffix(".feature").write_text("Feature: changed\n")
            with self.assertRaises(ValueError):
                read_contract(path)
            with self.assertRaises(ValueError):
                write_contract(path.parent, contract)
            forged = copy.deepcopy(contract)
            forged["payload"]["checks"] = [["echo", "skip checks"]]
            with self.assertRaises(ValueError):
                verify_contract(forged, self.plan(), repo)

    def test_checks_budget_models_and_git_base_are_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            plan = self.plan()
            contract = prepare_contract(plan, repo)
            verify_contract(contract, plan, repo)
            for kwargs in ({"task_timeout": 30}, {"repair_cycles": 2},
                           {"model_profiles": {"codex": "other"}}, {"base_revision": "another-base"}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    verify_contract(contract, plan, repo, **kwargs)
            with self.assertRaises(ValueError):
                verify_contract(contract, plan, repo, checks=[["echo", "different"]])
            plan.tasks[0].files = ["other/**"]
            with self.assertRaises(ValueError):
                verify_contract(contract, plan, repo)

    def test_changed_governing_instructions_and_selected_definitions_invalidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            home = Path(tmp) / "user"
            (repo / "AGENTS.md").write_text("Keep the boundary\n")
            skill = repo / ".agents/skills/example/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: example\ndescription: Example\n---\nDo the work\n")
            catalog = discover(repo, home)
            plan = self.plan()
            plan.tasks[0].allowed_skills = ["codex/skill/example"]
            contract = prepare_contract(plan, repo, definitions=catalog.entries)
            verify_contract(contract, plan, repo, definitions=catalog.entries)
            (repo / "AGENTS.md").write_text("Changed boundary\n")
            with self.assertRaises(ValueError):
                verify_contract(contract, plan, repo, definitions=catalog.entries)
            (repo / "AGENTS.md").write_text("Keep the boundary\n")
            skill.write_text("Changed skill\n")
            with self.assertRaises(ValueError):
                verify_contract(contract, plan, repo, definitions=catalog.entries)
            with self.assertRaises(ValueError):
                verify_contract(contract, plan, repo, definitions=discover(repo, home).entries)

    def test_ownership_overlaps_fail_closed_and_disjoint_trees_work(self):
        plan = self.plan()
        validate_ownership(plan)
        for pair in (("src/**", "src/code.py"), ("src/*.py", "src/test*.py"),
                     ("**", "tests/test.py"), ("src/a.py", "src/a.py")):
            plan.tasks[0].files, plan.tasks[1].files = [pair[0]], [pair[1]]
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                validate_ownership(plan)
        plan.tasks[0].files, plan.tasks[1].files = ["src/a.py"], ["src/b.py"]
        validate_ownership(plan)

    def test_unknown_selection_questions_shell_checks_and_symlinks_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = init_repo(Path(tmp) / "repo")
            plan = self.plan()
            plan.tasks[0].allowed_agents = ["codex/agent/missing"]
            with self.assertRaises(ValueError):
                prepare_contract(plan, repo)
            plan.tasks[0].allowed_agents = []
            plan.questions = ["Unanswered"]
            with self.assertRaises(ValueError):
                prepare_contract(plan, repo)
            plan.questions = []
            with self.assertRaises(ValueError):
                prepare_contract(plan, repo, checks=["echo dangerous"])
            outside = Path(tmp) / "outside"
            outside.mkdir()
            link = Path(tmp) / "link"
            link.symlink_to(outside)
            contract = prepare_contract(plan, repo)
            with self.assertRaises(ValueError):
                write_contract(link, contract)
            (repo / "AGENTS.md").symlink_to(outside / "instructions")
            with self.assertRaises(ValueError):
                prepare_contract(plan, repo)

    def test_explicit_feature_preserved_and_malformed_scenarios_refused(self):
        plan = self.plan()
        plan.feature = "Feature: behavior\n  Scenario: works\n    Given a repository\n    When checked\n    Then it passes\n"
        self.assertEqual(render_feature(plan), plan.feature)
        plan.feature = "not gherkin"
        with self.assertRaises(ValueError):
            render_feature(plan)
        plan.feature = ""
        plan.tasks[0].scenarios = ["silently erase the boundary"]
        with self.assertRaises(ValueError):
            render_feature(plan)


if __name__ == "__main__":
    unittest.main()
