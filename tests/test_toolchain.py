"""The approval binds the toolchain (found by live run 3: the service ran a different JDK than the terminal)."""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import contracts, toolchain
from brahma.models import Plan, Task


def tool(directory: Path, name: str, version: str, body: str = "") -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\necho '{version}'\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def repo_with_commit(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "x"], check=True)
    return repo


def plan() -> Plan:
    return Plan("g", [Task("t", "do it", ["a.txt"], ["ok"], "codex")])


class Probe(unittest.TestCase):
    def test_records_path_hash_version_and_tool_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            mvn = tool(bin_dir, "mvn", "Apache Maven 3.9.9\nJava version: 21, runtime: /jdk21")
            env = {"PATH": str(bin_dir), "JAVA_HOME": "/jdk21", "SECRET_TOKEN": "x"}
            result = toolchain.probe([["mvn", "clean", "install"], ["mvn", "test"]], Path(tmp) / "elsewhere", env)
        info = result["programs"]["mvn"]
        self.assertEqual(info["path"], str(mvn.resolve()))
        self.assertEqual(len(info["sha256"]), 64)
        self.assertIn("Java version: 21", info["version"])
        self.assertEqual(result["environment"], {"JAVA_HOME": "/jdk21"})      # only toolchain variables, never secrets
        self.assertEqual(list(result["programs"]), ["mvn"])                    # one entry per distinct program

    def test_a_missing_program_is_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = toolchain.probe([["no-such-tool-xyz"]], Path(tmp) / "r", {"PATH": tmp})
        self.assertEqual(result["programs"]["no-such-tool-xyz"], {"path": None, "sha256": None, "version": "not found"})

    def test_a_program_inside_the_repository_is_hashed_but_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            marker = repo / "ran"
            tool(repo, "mvnw", "wrapper", f"touch {marker}")
            result = toolchain.probe([["./mvnw", "test"]], repo, {"PATH": "/usr/bin:/bin"})
            self.assertFalse(marker.exists())
        self.assertIn("not executed", result["programs"]["./mvnw"]["version"])
        self.assertEqual(len(result["programs"]["./mvnw"]["sha256"]), 64)

    def test_differences_name_what_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tool(bin_dir, "mvn", "Java version: 21")
            other = Path(tmp) / "elsewhere"
            before = toolchain.probe([["mvn"]], other, {"PATH": str(bin_dir), "JAVA_HOME": "/jdk21"})
            tool(bin_dir, "mvn", "Java version: 26")
            after = toolchain.probe([["mvn"]], other, {"PATH": str(bin_dir)})
        moved = toolchain.differences(before, after)
        self.assertTrue(any("Java version: 21" in m and "Java version: 26" in m for m in moved))
        self.assertTrue(any(m.startswith("JAVA_HOME") for m in moved))
        self.assertEqual(toolchain.differences(before, before), [])


class Bound(unittest.TestCase):
    def test_the_toolchain_is_part_of_the_digest_and_a_changed_jdk_fails_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = repo_with_commit(Path(tmp))
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            tool(bin_dir, "mvn", "Java version: 21")
            checks = [["mvn", "install"]]
            with patch.dict(os.environ, {"PATH": str(bin_dir) + ":" + os.environ["PATH"], "JAVA_HOME": ""}):
                os.environ.pop("JAVA_HOME")
                first = contracts.prepare_contract(plan(), repo, checks)
                self.assertIn("toolchain", first["payload"])
                contracts.verify_contract(first, plan(), repo, checks)             # unchanged machine verifies
                tool(bin_dir, "mvn", "Java version: 26")                            # the JDK moved
                with self.assertRaises(ValueError) as caught:
                    contracts.verify_contract(first, plan(), repo, checks)
        self.assertIn("Toolchain changed", str(caught.exception))
        self.assertIn("Java version: 26", str(caught.exception))

    def test_a_contract_without_a_toolchain_is_not_honoured(self):
        self.assertGreaterEqual(contracts.CONTRACT_VERSION, 3)


if __name__ == "__main__":
    unittest.main()
