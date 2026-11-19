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
        self.assertEqual(info["path"], str(mvn))
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
        self.assertEqual(result["programs"]["mvn"]["path"], str(real))

    def test_java_project_files_are_bound_so_a_jvm_config_edit_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree = root / "repo", root / "wt"
            for base in (repo, worktree):
                (base / ".mvn" / "wrapper").mkdir(parents=True)
                (base / ".mvn" / "jvm.config").write_text("-Xmx512m\n")
            script(root / "bin", "mvn")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["mvn"]], repo, env)
            self.assertIn(".mvn/jvm.config", bound["programs"]["mvn"]["project_files"])
            self.assertIsNone(toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])
            (worktree / ".mvn" / "jvm.config").write_text("-javaagent:/tmp/evil.jar\n")
            problem = toolchain.verify_before_run(["mvn"], worktree, bound, env)[1]
            self.assertIn("Toolchain changed", problem)
            self.assertIn(".mvn/jvm.config", problem)

    def test_a_program_shipped_in_the_repository_is_a_launcher_with_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            script(repo, "mvnw")
            result = toolchain.probe([["./mvnw", "test"]], repo, {"PATH": "/usr/bin"})
        self.assertTrue(result["programs"]["./mvnw"]["launcher"])
        self.assertTrue(any("launches other programs" in line for line in toolchain.describe(result)))

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
        self.assertEqual(argv, [str(tool), "-x"])

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
                bound = toolchain.probe([["git"], ["mytool"]], worktree, gittools.check_env())
                ok, text = await gittools.run_checks(worktree, [["mytool"]], stop, 20, events.append, results, bound)
                self.assertTrue(ok, text)
                self.assertIn("hello", text)
                tool.write_text("#!/bin/sh\necho evil\n")
                with self.assertRaises(toolchain.ToolchainChanged) as caught:
                    await gittools.run_checks(worktree, [["mytool"]], stop, 20, events.append, results, bound)
                self.assertIn("Toolchain changed", str(caught.exception))
                self.assertNotIn("evil", str(caught.exception))
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(go(Path(tmp)))


class ReviewRegressions(unittest.TestCase):
    """Codex's re-review of the first toolchain fix."""

    def test_a_symlinked_program_keeps_its_invocation_path_and_hashes_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = script(root / "real", "python3")
            link_dir = root / "venv" / "bin"
            link_dir.mkdir(parents=True)
            (link_dir / "python").symlink_to(real)
            env = {"PATH": str(link_dir)}
            bound = toolchain.probe([["python"]], root / "wt", env)
            argv, problem = toolchain.verify_before_run(["python", "-V"], root / "wt", bound, env)
        self.assertIsNone(problem)
        self.assertEqual(argv[0], str(link_dir / "python"))                  # the venv path, not the resolved target
        self.assertEqual(bound["programs"]["python"]["target"], str(real.resolve()))

    def test_a_java_build_tool_without_java_home_reports_an_unbound_jdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script(root / "bin", "mvn")
            script(root / "bin", "java")
            result = toolchain.probe([["mvn"]], root / "r", {"PATH": str(root / "bin")})
        self.assertTrue(result["programs"]["mvn"]["runtime_unbound"])
        self.assertIsNone(result["programs"]["mvn"]["runtime"])
        self.assertTrue(any("chooses its own JDK" in line for line in toolchain.describe(result)))

    def test_the_java_program_itself_derives_its_runtime_from_its_own_file_not_java_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mine, other = jdk(root, "17"), jdk(root, "26")
            env = {"PATH": str(mine / "bin"), "JAVA_HOME": str(other)}
            result = toolchain.probe([["java"]], root / "r", env)
        self.assertEqual(result["programs"]["java"]["runtime"]["release"], "17")

    def test_an_absolute_path_into_the_source_checkout_does_not_falsely_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, worktree = Path(tmp) / "repo", Path(tmp) / "wt"
            tool = script(repo, "mvnw")
            worktree.mkdir()
            env = {"PATH": "/usr/bin"}
            bound = toolchain.probe([[str(tool)]], repo, env)
            self.assertIsNone(toolchain.verify_before_run([str(tool)], worktree, bound, env)[1])

    def test_a_missing_program_is_refused_never_run_as_the_text_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script(root / "bin", "None")                                     # a program literally named None
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["ghost"]], root / "wt", env)
            argv, problem = toolchain.verify_before_run(["ghost"], root / "wt", bound, env)
        self.assertIn("was not found", problem)

    def test_a_token_shaped_release_value_is_never_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script(root / "bin", "mvn")
            home = jdk(root, "21")
            (home / "release").write_text('JAVA_VERSION="ghp_0123456789abcdefghij0123456789abcdef"\n')
            result = toolchain.probe([["mvn"]], root / "r", {"PATH": str(root / "bin"), "JAVA_HOME": str(home)})
        self.assertEqual(result["programs"]["mvn"]["runtime"]["release"], "unrecognised")
        self.assertNotIn("ghp_", repr(result))


class ThirdReview(unittest.TestCase):
    def test_a_startup_file_in_a_subproject_is_bound_for_mvn_dash_f(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree = root / "repo", root / "wt"
            for base in (repo, worktree):
                (base / "service" / ".mvn").mkdir(parents=True)
                (base / "service" / ".mvn" / "jvm.config").write_text("-Xmx1g\n")
            script(root / "bin", "mvn")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["mvn", "-f", "service/pom.xml", "test"]], repo, env)
            self.assertIn("service/.mvn/jvm.config", bound["programs"]["mvn"]["project_files"])
            self.assertIsNone(toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])
            (worktree / "service" / ".mvn" / "jvm.config").write_text("-javaagent:/tmp/x.jar\n")
            self.assertIn("service/.mvn/jvm.config", toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])

    def test_a_symlinked_startup_file_is_bound_through_its_target_and_retargeting_is_a_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.config"
            outside.write_text("-Xmx1g\n")
            repo, worktree = root / "repo", root / "wt"
            for base in (repo, worktree):
                (base / ".mvn").mkdir(parents=True)
                (base / ".mvn" / "jvm.config").symlink_to(outside)
            script(root / "bin", "mvn")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["mvn"]], repo, env)
            self.assertIn("@", bound["programs"]["mvn"]["project_files"][".mvn/jvm.config"])
            self.assertIsNone(toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])
            outside.write_text("-javaagent:/tmp/x.jar\n")
            self.assertIn("Toolchain changed", toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])

    def test_a_new_startup_file_created_after_approval_is_a_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree = root / "repo", root / "wt"
            repo.mkdir(); worktree.mkdir()
            script(root / "bin", "mvn")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["mvn"]], repo, env)
            (worktree / ".mvn").mkdir()
            (worktree / ".mvn" / "jvm.config").write_text("-javaagent:/tmp/x.jar\n")
            self.assertIn("Toolchain changed", toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])

    def test_plain_java_is_not_refused_because_a_maven_config_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree = root / "repo", root / "wt"
            for base in (repo, worktree):
                (base / ".mvn").mkdir(parents=True)
                (base / ".mvn" / "jvm.config").write_text("a\n")
            home = jdk(root, "21")
            env = {"PATH": str(home / "bin")}
            bound = toolchain.probe([["java", "-version"]], repo, env)
            (worktree / ".mvn" / "jvm.config").write_text("b\n")
            self.assertIsNone(toolchain.verify_before_run(["java", "-version"], worktree, bound, env)[1])

    def test_a_java_selector_without_an_adjacent_release_file_is_not_treated_as_a_jdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script(root / "usrbin", "java", "exec \"$JAVA_HOME/bin/java\" \"$@\"")     # the macOS-style stub: no release file nearby
            home = jdk(root, "21")
            unbound = toolchain.probe([["java"]], root / "r", {"PATH": str(root / "usrbin")})
            bound = toolchain.probe([["java"]], root / "r", {"PATH": str(root / "usrbin"), "JAVA_HOME": str(home)})
        self.assertTrue(unbound["programs"]["java"]["runtime_unbound"])
        self.assertTrue(bound["programs"]["java"]["runtime_unbound"])               # a shim may select by .java-version: never bound by JAVA_HOME

    def test_dot_dot_components_are_not_collapsed_into_another_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            script(repo / ".venv" / "bin", "python")
            result = toolchain.probe([["./missing/../.venv/bin/python"]], repo, {"PATH": "/usr/bin"})
        self.assertIsNone(result["programs"]["./missing/../.venv/bin/python"]["path"])     # the path as written does not exist


class FourthReview(unittest.TestCase):
    def test_a_symlinked_mvn_directory_still_binds_its_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree = root / "repo", root / "wt"
            for base in (repo, worktree):
                (base / "config" / "maven").mkdir(parents=True)
                (base / "config" / "maven" / "jvm.config").write_text("-Xmx1g\n")
                (base / ".mvn").symlink_to(base / "config" / "maven")
            script(root / "bin", "mvn")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["mvn"]], repo, env)
            files = bound["programs"]["mvn"]["project_files"]
            self.assertIn(".mvn/jvm.config", files)
            self.assertTrue(files[".mvn"].startswith("<symlinked-dir>@"))
            self.assertIsNone(toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])    # identical checkouts compare equal
            (worktree / "config" / "maven" / "jvm.config").write_text("-javaagent:/tmp/x.jar\n")
            self.assertIn("Toolchain changed", toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])

    def test_an_internal_file_symlink_does_not_falsely_mismatch_across_checkouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, worktree = root / "repo", root / "wt"
            for base in (repo, worktree):
                (base / ".mvn").mkdir(parents=True)
                (base / "config").mkdir()
                (base / "config" / "jvm.config").write_text("-Xmx1g\n")
                (base / ".mvn" / "jvm.config").symlink_to("../config/jvm.config")
            script(root / "bin", "mvn")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["mvn"]], repo, env)
            self.assertIsNone(toolchain.verify_before_run(["mvn"], worktree, bound, env)[1])

    def test_a_nested_gradle_properties_is_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "app").mkdir(parents=True)
            (repo / "app" / "gradle.properties").write_text("org.gradle.jvmargs=-Xmx1g\n")
            script(Path(tmp) / "bin", "gradle")
            bound = toolchain.probe([["gradle", "build"]], repo, {"PATH": str(Path(tmp) / "bin")})
        self.assertIn("app/gradle.properties", bound["programs"]["gradle"]["project_files"])


class FifthReview(unittest.TestCase):
    def test_the_implicit_git_check_is_bound_and_a_replacement_git_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git = script(root / "bin", "git", "echo real")
            env = {"PATH": str(root / "bin")}
            bound = toolchain.probe([["git"]], root / "wt", env)
            command = ["git", "-c", "core.hooksPath=/dev/null", "diff", "--check"]
            self.assertIsNone(toolchain.verify_before_run(command, root / "wt", bound, env)[1])
            git.write_text("#!/bin/sh\necho evil\n")
            self.assertIn("Toolchain changed", toolchain.verify_before_run(command, root / "wt", bound, env)[1])

    def test_every_contract_binds_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = repo_with_commit(Path(tmp))
            contract = contracts.prepare_contract(plan(), repo, [])
        self.assertIn("git", contract["payload"]["toolchain"]["programs"])

    def test_jvm_startup_variables_are_bound_by_value_and_a_javaagent_is_a_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = jdk(root, "21")
            env = {"PATH": str(home / "bin")}
            bound = toolchain.probe([["java"]], root / "wt", env)
            self.assertIsNone(toolchain.verify_before_run(["java"], root / "wt", bound, env)[1])
            env["JAVA_TOOL_OPTIONS"] = "-javaagent:/external/unapproved.jar"
            problem = toolchain.verify_before_run(["java"], root / "wt", bound, env)[1]
        self.assertIn("Toolchain changed", problem)
        self.assertIn("JAVA_TOOL_OPTIONS", problem)

    def test_a_secret_shaped_startup_variable_is_stored_as_a_digest_not_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script(root / "bin", "mvn")
            env = {"PATH": str(root / "bin"), "MAVEN_OPTS": "-Dpassword=hunter2hunter2 -Xmx1g"}
            result = toolchain.probe([["mvn"]], root / "r", env)
        value = result["programs"]["mvn"]["variables"]["MAVEN_OPTS"]
        self.assertTrue(value.startswith("sha256:"))
        self.assertNotIn("hunter2", repr(result))

    def test_gradle_that_selects_its_own_jdk_is_reported_unbound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "gradle.properties").write_text("org.gradle.java.home=/opt/other-jdk\n")
            script(root / "bin", "gradle")
            home = jdk(root, "21")
            result = toolchain.probe([["gradle", "build"]], repo, {"PATH": str(root / "bin"), "JAVA_HOME": str(home)})
        self.assertTrue(result["programs"]["gradle"]["runtime_unbound"])
        self.assertTrue(any("gradle.properties (org.gradle.java.home)" in line for line in toolchain.describe(result)))

    def test_gradle_properties_under_a_symlinked_directory_is_bound_and_cycles_terminate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = root / "external"
            external.mkdir()
            (external / "gradle.properties").write_text("org.gradle.jvmargs=-Xmx1g\n")
            repo = root / "repo"
            repo.mkdir()
            (repo / "gradle").symlink_to(external)
            (repo / "loop").symlink_to(repo)                                # a cycle
            script(root / "bin", "gradle")
            done = []
            worker = threading.Thread(target=lambda: done.append(toolchain.probe([["gradle", "-p", "gradle", "test"]], repo, {"PATH": str(root / "bin")})))
            worker.start()
            worker.join(15)
            self.assertFalse(worker.is_alive(), "a symlink cycle must not hang the walk")
        files = done[0]["programs"]["gradle"]["project_files"]
        self.assertIn("gradle/gradle.properties", files)
        self.assertTrue(files["gradle"].startswith("<symlinked-dir>@"))


if __name__ == "__main__":
    unittest.main()
