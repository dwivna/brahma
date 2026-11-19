"""The approval binds the toolchain without executing it (live run 3: the service ran a different JDK than the terminal;
independent review: planning must not run unapproved programs, and the bound file must be the one that runs)."""
from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import contracts, gittools, toolchain
from brahma.models import Plan, Task


def script(directory: Path, name: str, body: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def jdk(root: Path, version: str, marker: str = "") -> Path:
    home = root / f"jdk{version}"
    script(home / "bin", "java", f"echo {marker}")
    (home / "release").write_text(f'JAVA_VERSION="{version}"\nIMPLEMENTOR="x"\n')
    return home


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
    def test_records_path_hash_jdk_and_only_the_variables_that_steer_the_program(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mvn = script(root / "bin", "mvn")
            home = jdk(root, "21.0.8")
            env = {"PATH": str(root / "bin"), "JAVA_HOME": str(home), "VIRTUAL_ENV": "/v", "SECRET_TOKEN": "x"}
            result = toolchain.probe([["mvn", "clean"], ["mvn", "test"]], root / "elsewhere", env)
        info = result["programs"]["mvn"]
        self.assertEqual(info["path"], str(mvn.resolve()))
        self.assertEqual(len(info["sha256"]), 64)
        self.assertEqual(info["runtime"]["release"], "21.0.8")
        self.assertEqual(len(info["runtime"]["sha256"]), 64)
        self.assertEqual(info["variables"], {"JAVA_HOME": str(home)})        # no VIRTUAL_ENV, never secrets
        self.assertEqual(list(result["programs"]), ["mvn"])

    def test_probing_executes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "ran"
            script(root / "bin", "mvn", f"touch {marker}")
            home = jdk(root, "21", f"touch {marker}")
            script(root / "repo", "mvnw", f"touch {marker}")
            toolchain.probe([["mvn"], ["./mvnw"]], root / "repo", {"PATH": str(root / "bin"), "JAVA_HOME": str(home)})
            self.assertFalse(marker.exists())

    def test_a_hostile_release_file_never_reaches_the_contract_as_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script(root / "bin", "mvn")
            home = jdk(root, "21")
            (home / "release").write_text('JAVA_VERSION="21 token=sk-SYNTHETIC-0123456789abcdef"\n')
            result = toolchain.probe([["mvn"]], root / "r", {"PATH": str(root / "bin"), "JAVA_HOME": str(home)})
        self.assertEqual(result["programs"]["mvn"]["runtime"]["release"], "unrecognised")
        self.assertNotIn("SYNTHETIC", repr(result))

    def test_a_missing_program_is_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = toolchain.probe([["no-such-tool-xyz"]], Path(tmp) / "r", {"PATH": tmp})
        self.assertIsNone(result["programs"]["no-such-tool-xyz"]["path"])
        self.assertTrue(any("NOT FOUND" in line for line in toolchain.describe(result)))

    def test_a_device_file_cannot_hang_the_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "mvnw").symlink_to("/dev/zero")
            done = []
            worker = threading.Thread(target=lambda: done.append(toolchain.probe([["./mvnw"]], repo, {"PATH": "/usr/bin"})))
            worker.start()
            worker.join(10)
            self.assertFalse(worker.is_alive(), "fingerprinting /dev/zero must end")
        self.assertIsNone(done[0]["programs"]["./mvnw"]["sha256"])
        self.assertTrue(any("not bound" in line for line in toolchain.describe(done[0])))

    def test_a_relative_path_entry_is_never_searched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = script(root / "sys", "mvn")
            script(root / "repo" / ".venv" / "bin", "mvn")                   # a worker-created shim
            env = gittools.check_env()
            env["PATH"] = ".venv/bin::" + str(root / "sys")
            self.assertEqual(toolchain.trusted_path(env), str(root / "sys"))
            result = toolchain.probe([["mvn"]], root / "repo", env)
        self.assertEqual(result["programs"]["mvn"]["path"], str(real.resolve()))

    def test_launchers_are_flagged_as_not_binding_their_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            script(Path(tmp) / "bin", "uv")
            result = toolchain.probe([["uv", "run", "pytest"]], Path(tmp) / "r", {"PATH": str(Path(tmp) / "bin")})
        self.assertTrue(any(line.startswith("WARNING: uv launches") for line in toolchain.describe(result)))

    def test_differences_and_describe_show_the_hash_and_never_program_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script(root / "bin", "mvn")
            old, new = jdk(root, "21"), jdk(root, "26")
            before = toolchain.probe([["mvn"]], root / "r", {"PATH": str(root / "bin"), "JAVA_HOME": str(old)})
            after = toolchain.probe([["mvn"]], root / "r", {"PATH": str(root / "bin"), "JAVA_HOME": str(new)})
        text = " ".join(toolchain.differences(before, after))
        self.assertIn("JDK 21", text)
        self.assertIn("JDK 26", text)
        self.assertEqual(toolchain.differences(before, before), [])
        self.assertIn("sha256:" + before["programs"]["mvn"]["sha256"][:12], toolchain.describe(before)[0])


class Bound(unittest.TestCase):
    def test_the_toolchain_is_part_of_the_digest_and_a_changed_jdk_fails_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = repo_with_commit(root)
            script(root / "bin", "mvn")
            old, new = jdk(root, "21"), jdk(root, "26")
            checks = [["mvn", "install"]]
            with patch.dict(os.environ, {"PATH": str(root / "bin") + ":" + os.environ["PATH"], "JAVA_HOME": str(old)}):
                first = contracts.prepare_contract(plan(), repo, checks)
                self.assertIn("toolchain", first["payload"])
                contracts.verify_contract(first, plan(), repo, checks)
                os.environ["JAVA_HOME"] = str(new)                           # the JDK moved
                with self.assertRaises(ValueError) as caught:
                    contracts.verify_contract(first, plan(), repo, checks)
        self.assertIn("Toolchain changed", str(caught.exception))
        self.assertIn("JDK 26", str(caught.exception))

    def test_a_contract_without_a_toolchain_is_not_honoured(self):
        self.assertGreaterEqual(contracts.CONTRACT_VERSION, 3)


class BeforeEveryCheck(unittest.TestCase):
    def bound(self, root: Path, program: str, directory: Path):
        env = gittools.check_env()
        return env, toolchain.probe([[program]], directory, env)

    def test_the_verified_absolute_path_is_what_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = script(root / "bin", "mytool")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["mytool"]], root / "wt", env)
            argv, problem = toolchain.verify_before_run(["mytool", "-x"], root / "wt", bound, env)
        self.assertIsNone(problem)
        self.assertEqual(argv, [str(tool.resolve()), "-x"])

    def test_a_binary_swapped_after_approval_is_refused_before_it_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "ran"
            tool = script(root / "bin", "mytool")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["mytool"]], root / "wt", env)
            tool.write_text(f"#!/bin/sh\ntouch {marker}\n")
            argv, problem = toolchain.verify_before_run(["mytool"], root / "wt", bound, env)
        self.assertIn("Toolchain changed", problem)
        self.assertFalse(marker.exists())

    def test_a_program_in_the_worktree_that_a_worker_edited_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, worktree = Path(tmp) / "repo", Path(tmp) / "wt"
            script(repo, "mvnw", "echo clean")
            script(worktree, "mvnw", "echo clean")
            env = {"PATH": "/usr/bin"}
            bound = toolchain.probe([["./mvnw"]], repo, env)
            self.assertIsNone(toolchain.verify_before_run(["./mvnw"], worktree, bound, env)[1])
            script(worktree, "mvnw", "echo evil")
            self.assertIn("Toolchain changed", toolchain.verify_before_run(["./mvnw"], worktree, bound, env)[1])

    def test_a_program_outside_the_approval_and_the_unbound_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": "/usr/bin:/bin"}
            bound = toolchain.probe([["true"]], Path(tmp), env)
            self.assertIn("not part of the approved toolchain", toolchain.verify_before_run(["git", "status"], Path(tmp), bound, env)[1])
            self.assertEqual(toolchain.verify_before_run(["git", "status"], Path(tmp), None, env), (["git", "status"], None))

    def test_run_checks_refuses_a_swapped_binary_and_runs_a_verified_one(self):
        async def go(root: Path):
            events, results = [], []
            stop = asyncio.Event()
            worktree = root / "wt"
            worktree.mkdir()
            subprocess.run(["git", "-C", str(worktree), "init", "-q"], check=True)
            tool = script(root / "bin", "mytool", "echo hello")
            with patch.dict(os.environ, {"PATH": str(root / "bin") + ":" + os.environ["PATH"]}):
                bound = toolchain.probe([["mytool"]], worktree, gittools.check_env())
                ok, text = await gittools.run_checks(worktree, [["mytool"]], stop, 20, events.append, results, bound)
                self.assertTrue(ok, text)
                self.assertIn("hello", text)
                tool.write_text("#!/bin/sh\necho evil\n")
                ok, text = await gittools.run_checks(worktree, [["mytool"]], stop, 20, events.append, results, bound)
                self.assertFalse(ok)
                self.assertIn("Toolchain changed", text)
                self.assertNotIn("evil", text)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(go(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
