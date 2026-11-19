"""Versioning (Rounds 10-13): one version everywhere, and unknown contract formats are refused."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import brahma
from brahma import contracts, model_catalog, quota
from brahma.models import Plan, Task

ROOT = Path(__file__).resolve().parents[1]


def tiny_repo(directory: str) -> Path:
    root = Path(directory)
    for args in (("init", "-q"), ("commit", "-q", "--allow-empty", "-m", "x")):
        subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                       check=True, capture_output=True)
    return root


class Version(unittest.TestCase):
    def test_single_version_everywhere(self):
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        self.assertEqual(pyproject, brahma.__version__)
        self.assertRegex(brahma.__version__, r"^0\.\d+\.\d+$")  # SemVer 0.x until 1.0.0 (Round 10)

    def test_no_stale_hardcoded_version_in_sources(self):
        for module in (quota, model_catalog):
            source = Path(module.__file__).read_text()
            self.assertNotIn('"version": "0.', source)

    def test_changelog_records_the_current_version(self):
        text = (ROOT / "CHANGELOG.md").read_text()
        self.assertIn(f"## {brahma.__version__}", text)


class ContractFormat(unittest.TestCase):
    def make(self):
        plan = Plan("Goal", [Task("One", "do", ["src/**"], ["ok"])], checks=[["true"]])
        with tempfile.TemporaryDirectory() as directory:
            return contracts.prepare_contract(plan, tiny_repo(directory), [["true"]])

    def test_new_contracts_carry_the_current_format(self):
        contract = self.make()
        self.assertEqual(contract["version"], contracts.CONTRACT_VERSION)
        self.assertEqual(contracts.CONTRACT_VERSION, 2)
        contracts._validate(contract)

    def test_old_format_is_refused_not_reinterpreted(self):
        contract = self.make()
        old = {"version": 1, "payload": contract["payload"], "digest": contract["digest"]}
        with self.assertRaisesRegex(ValueError, r"Contract format 1 is not supported.*approve a new plan"):
            contracts._validate(old)

    def test_unknown_future_and_malformed_formats_are_refused(self):
        contract = self.make()
        for version in (0, 3, 99):
            with self.subTest(version=version), self.assertRaises(ValueError):
                contracts._validate({**contract, "version": version})
        for bad in ("2", 2.0, None, True):
            with self.subTest(version=bad), self.assertRaises(ValueError):
                contracts._validate({**contract, "version": bad})

    def test_changed_allowlist_invalidates_the_approval(self):
        from brahma.config import Settings
        from dataclasses import asdict
        plan = Plan("Goal", [Task("One", "do", ["src/**"], ["ok"])], checks=[["true"]])
        with tempfile.TemporaryDirectory() as directory:
            repo = tiny_repo(directory)
            first = contracts.prepare_contract(plan, repo, [["true"]], model_profiles=asdict(Settings()))
            widened = Settings(ignored_allowlist=[".venv", "secrets_cache"])
            with self.assertRaisesRegex(ValueError, "Contract changed"):
                contracts.verify_contract(first, plan, repo, [["true"]], model_profiles=asdict(widened))


if __name__ == "__main__":
    unittest.main()
