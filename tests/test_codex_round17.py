"""Independent review round 17 of v1.0.0 (stand-in for Codex, decision Round 26): one regression per MEDIUM finding."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from brahma import engine, gitsafe, gittools
from brahma.models import Result
from brahma.session import Session
from brahma.store import Store
from tests.test_review12 import git
from tests.test_review16 import publication_result, verified_branch
from tests.test_scheduler import approved_job


class Finding1PublicationUnderBrahmasGitRules(unittest.TestCase):
    def test_settings_that_redirect_a_push_or_run_a_command_are_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = verified_branch(tmp)
            self.assertEqual(gitsafe.publication_risks(repo), [])
            git(repo, "config", "core.hooksPath", ".husky")        # ordinary, and pinned off for every step
            self.assertEqual(gitsafe.publication_risks(repo), [])
            for key, value in (("remote.origin.pushurl", "https://example.invalid/other.git"),
                               ("url.https://example.invalid/.insteadOf", "https://github.com/"),
                               ("core.sshCommand", "sh -c 'touch planted'"),
                               ("credential.https://github.com.helper", "!sh -c 'touch planted'"),
                               ("include.path", "/tmp/planted.config"),
                               # Automated security review of e14778d: also a proxy command, protocols, a remote
                               # helper, signing programs, TLS and name resolution.
                               ("core.gitProxy", "sh -c 'touch planted'"),
                               ("protocol.ext.allow", "always"),
                               ("remote.origin.vcs", "planted"),
                               ("gpg.program", "sh -c 'touch planted'"),
                               ("push.gpgSign", "true"),
                               ("http.sslVerify", "false"),
                               ("http.https://github.com/.curloptResolve", "github.com:443:127.0.0.1"),
                               # Review of 1.0.1: headers sent to whatever host answers, and SSH overrides.
                               ("http.extraHeader", "Authorization: planted"),
                               ("http.https://github.com/.cookieFile", "/tmp/planted.cookies"),
                               ("http.sslCert", "/tmp/planted.cert"),
                               ("http.proxyAuthMethod", "basic"),
                               ("ssh.variant", "simple")):
                with self.subTest(key=key):
                    git(repo, "config", key, value)
                    section, name = key.rsplit(".", 1)          # Git lowercases the name, keeps the subsection
                    self.assertIn(f"{section}.{name.lower()}", gitsafe.publication_risks(repo))
                    git(repo, "config", "--unset", key)

    def test_a_planted_push_url_stops_publication_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            git(repo, "config", "remote.origin.pushurl", "https://example.invalid/other.git")
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step") as step:
                with self.assertRaisesRegex(ValueError, "remote.origin.pushurl"):
                    gittools.publish(repo, "s", "b", head, snapshot)
            step.assert_not_called()

    def test_origin_repointed_after_the_run_started_is_never_pushed_to(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            identity = gittools.git_identity(repo)
            self.assertTrue(identity["origin"])
            self.assertEqual(identity["origin"], gittools.push_target(repo))
            git(repo, "remote", "set-url", "origin", "https://example.invalid/elsewhere/repo.git")
            self.assertIn("the origin remote's URL changed", gittools.identity_problems(repo, identity))  # not ready
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step") as step:
                with self.assertRaisesRegex(ValueError, "origin remote's URL changed"):
                    gittools.publish(repo, "s", "b", head, snapshot, expected_origin=identity["origin"])
            step.assert_not_called()

    def test_changing_a_legacy_origin_changes_the_resolved_push_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = verified_branch(tmp)
            git(repo, "remote", "remove", "origin")
            origin = repo / ".git" / "remotes" / "origin"
            origin.parent.mkdir()
            self.assertEqual(gittools.push_target(repo), "")
            origin.write_text("URL: https://example.invalid/owner/repo.git\n")
            original = gittools.push_target(repo)
            origin.write_text("URL: https://example.invalid/elsewhere/repo.git\n")
            changed = gittools.push_target(repo)
            self.assertTrue(original and changed)
            self.assertNotEqual(original, changed)

    def test_a_resume_refuses_a_changed_origin_before_starting_a_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            with closing(store):
                git(session.repo, "remote", "add", "origin", "https://example.invalid/owner/repo.git")
                original = gittools.push_target(session.repo)

                async def incomplete(*args, **kwargs):
                    return Result("brahma", "incomplete", "checkpoint")

                with patch("brahma.engine.execute", incomplete):
                    self.assertEqual(asyncio.run(session.execute(scheduled=True)).status, "incomplete")
                self.assertTrue(original)
                git(session.repo, "remote", "set-url", "origin", "https://example.invalid/elsewhere/repo.git")
                resumed = Session(store, session.repo, key=session.key)
                with patch("brahma.engine.execute", side_effect=AssertionError("started work after origin changed")):
                    with self.assertRaisesRegex(ValueError, "push URL changed since this session first ran"):
                        asyncio.run(resumed.execute(scheduled=True))
                baselines = [json.loads(e["text"]) for e in store.events(session.key) if e["kind"] == "origin_baseline"]
                self.assertEqual(baselines, [{"sha256": original}])

    def test_a_legacy_checkpoint_can_finish_locally_without_acquiring_publication_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            with closing(store):
                worktree = gittools.create_worktree(session.repo, store.home, session.key)
                store.set(session.key, worktree=str(worktree), status="interrupted")
                git(session.repo, "remote", "add", "origin", "https://example.invalid/elsewhere/repo.git")
                with patch("brahma.engine.execute", ready_result):
                    self.assertEqual(asyncio.run(session.execute(scheduled=True)).status, "ready")
                receipt = session.ready_receipt()
                self.assertEqual(receipt["origin"], "")
                with self.assertRaisesRegex(ValueError, "records no origin push target from before work began"):
                    session._publish_arguments(receipt, "body")

    def test_legacy_or_originless_receipts_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            with closing(store):
                store.set(session.key, worktree=str(session.repo))
                receipt = {"head": "h", "uncommitted": {}, "ignored_allowlist": []}
                for candidate in (receipt, dict(receipt, origin="")):
                    with self.subTest(receipt=candidate):
                        with self.assertRaisesRegex(ValueError, "readiness receipt records no origin push target"):
                            session._publish_arguments(candidate, "body")

    def test_the_receipt_carries_the_origin_publication_checks(self):
        receipt = {"head": "h", "uncommitted": {}, "ignored_allowlist": [], "origin": "a" * 64,
                   "git_identity": {"git_dir": "/g/w", "common_dir": "/g", "branch": "brahma/k"}}
        session = Session.__new__(Session)
        session.plan, session.key, session.closing = type("P", (), {"summary": "s"})(), "k", __import__("threading").Event()
        session.store = type("S", (), {"home": Path("/nonexistent"), "session": lambda self, key: {"worktree": "/w"}})()
        arguments, _ = session._publish_arguments(receipt, "b", after=0)
        self.assertEqual(arguments[7], receipt["origin"])
        self.assertEqual(arguments[8], receipt["git_identity"])

    def test_pr_creation_runs_with_the_same_pins_as_the_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            calls = []

            def step(argv, directory, stopped, **kwargs):
                calls.append((argv, kwargs))
                if "push" in argv:
                    git(repo, "remote", "set-url", "origin", "https://example.invalid/elsewhere/repo.git")
                if "push" in argv or argv[1:2] == ["api"]:
                    self.assertNotEqual(directory, repo)
                    self.assertEqual(git(directory, "remote", "get-url", "--push", "origin"),
                                     "https://example.invalid/owner/repo.git")
                return publication_result(argv, head)

            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", side_effect=step):
                gittools.publish(repo, "s", "b", head, snapshot, expected_origin=gittools.push_target(repo))
            self.assertIn(".clone_url, .default_branch", calls[0][0])
            self.assertIn(".object.sha", calls[2][0])
            argv, kwargs = calls[3]
            self.assertEqual(argv[:3], ["/usr/bin/gh", "pr", "create"])
            self.assertEqual(argv[argv.index("--repo") + 1], "example.invalid/owner/repo")
            self.assertEqual(argv[argv.index("--head") + 1], "brahma/s")
            env = kwargs["env"]
            pins = {env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"] for n in range(int(env["GIT_CONFIG_COUNT"]))}
            for key, value in (("core.fsmonitor", "false"), ("core.hooksPath", "/dev/null"), ("core.pager", "cat")):
                self.assertEqual(pins[key], value)

    def test_a_remote_ref_mismatch_stops_draft_pr_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step",
                                 side_effect=lambda argv, *a, **k: publication_result(argv, "0" * 40)) as step:
                with self.assertRaisesRegex(ValueError, "could not verify the remote branch"):
                    gittools.publish(repo, "s", "b", head, snapshot)
            self.assertEqual(step.call_count, 3)

    def test_an_api_endpoint_for_a_different_repository_stops_before_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            response = subprocess.CompletedProcess([], 0, "https://example.invalid/different/repo.git\nmaster\n", "")
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", return_value=response) as step:
                with self.assertRaisesRegex(ValueError, "GitHub did not confirm the repository"):
                    gittools.publish(repo, "s", "b", head, snapshot)
            step.assert_called_once()

    def test_an_ssh_origin_is_left_for_manual_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            git(repo, "remote", "set-url", "origin", "git@github.com:owner/repo.git")
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step") as step:
                with self.assertRaisesRegex(ValueError, "requires one GitHub HTTPS origin"):
                    gittools.publish(repo, "s", "b", head, snapshot)
            step.assert_not_called()

    def test_the_remote_default_branch_is_never_pushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            response = subprocess.CompletedProcess([], 0, "https://example.invalid/owner/repo.git\nbrahma/s\n", "")
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", return_value=response) as step:
                with self.assertRaisesRegex(ValueError, "repository's default branch"):
                    gittools.publish(repo, "s", "b", head, snapshot)
            step.assert_called_once()

    def test_an_unverified_draft_is_reported_as_created_and_needing_inspection(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)

            def step(argv, *args, **kwargs):
                observed = "0" * 40 if argv[1:3] == ["pr", "view"] else head
                return publication_result(argv, observed)

            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", side_effect=step):
                with self.assertRaisesRegex(ValueError, "Draft PR created at .*; inspect it before use"):
                    gittools.publish(repo, "s", "b", head, snapshot)

    def test_a_successful_create_with_an_unusable_response_reports_an_uncertain_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)

            def step(argv, *args, **kwargs):
                return publication_result(argv, head, url="")

            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", side_effect=step) as run:
                with self.assertRaisesRegex(ValueError, "Draft PR may already exist for brahma/s"):
                    gittools.publish(repo, "s", "b", head, snapshot)
            self.assertEqual(run.call_count, 4)

    def test_an_interrupted_running_write_reports_that_remote_state_may_have_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "remote branch or draft PR may have changed"):
                gittools._run_publication_step(["sleep", "5"], Path(tmp), lambda: None, timeout=0, remote_write=True)

    def test_https_push_pins_override_the_owners_matching_url_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            target = "https://example.invalid/owner/repo.git"
            config = Path(tmp) / "user-config"
            git(repo, "config", "--file", str(config), f"http.{target}.sslVerify", "false")
            git(repo, "config", "--file", str(config), f"http.{target}.followRedirects", "true")

            def step(argv, *args, **kwargs):
                if "push" in argv:
                    prefix = argv[:argv.index("push")]
                    for key, expected in (("http.sslVerify", "true"), ("http.followRedirects", "false")):
                        output = subprocess.check_output([*prefix, "config", "--get-urlmatch", key, target],
                                                         text=True, env=kwargs["env"])
                        self.assertEqual(output.strip(), expected)
                return publication_result(argv, head)

            with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}), \
                    patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", side_effect=step):
                gittools.publish(repo, "s", "b", head, snapshot)

    def test_a_stop_after_setup_or_during_remote_verification_blocks_the_next_write(self):
        for after_setup in (True, False):
            with self.subTest(after_setup=after_setup), tempfile.TemporaryDirectory() as tmp:
                repo, head, snapshot = verified_branch(tmp)
                prepare, run_step = gittools._push_repository, gittools._run_publication_step
                stop = False
                launched = []

                def setup(*args):
                    nonlocal stop
                    prepare(*args)
                    stop = after_setup

                def step(argv, *args, **kwargs):
                    def launch(*_args, **_kwargs):
                        launched.append(argv)
                        result = publication_result(argv, head)

                        def communicate(*_args, **_kwargs):
                            nonlocal stop
                            if argv[-1] == ".object.sha":
                                stop = True
                            return result.stdout, result.stderr

                        return type("Process", (), {"communicate": staticmethod(communicate), "returncode": 0})()

                    with patch.object(subprocess, "Popen", side_effect=launch):
                        return run_step(argv, *args, **kwargs)

                with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                        patch.object(gittools, "_push_repository", side_effect=setup), \
                        patch.object(gittools, "_run_publication_step", side_effect=step):
                    with self.assertRaisesRegex(ValueError, "Publication stopped: Stopped by user"):
                        gittools.publish(repo, "s", "b", head, snapshot, stopped=lambda: "Stopped by user" if stop else None)
                self.assertEqual(len(launched), 0 if after_setup else 3)
                self.assertFalse(any(argv[1:3] == ["pr", "create"] for argv in launched))

    def test_another_brahma_branch_at_the_same_commit_cannot_use_the_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            identity = gittools.git_identity(repo)
            bound = {key: identity[key] for key in ("git_dir", "common_dir", "branch")}
            git(repo, "checkout", "-qb", "brahma/other")
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step") as step:
                with self.assertRaisesRegex(ValueError, "worktree no longer matches its readiness receipt"):
                    gittools.publish(repo, "s", "b", head, snapshot, expected_identity=bound)
            step.assert_not_called()

    def test_the_push_writes_only_the_receipt_branch_to_a_local_bare_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            remote = Path(tmp) / "remote.git"
            remote.mkdir()
            git(remote, "init", "--bare")
            git(repo, "remote", "set-url", "origin", str(remote))
            git(repo, "tag", "-a", "local-release", "-m", "Fixture tag")
            git(repo, "config", "push.followTags", "true")
            git(repo, "config", "push.recurseSubmodules", "on-demand")
            run_step = gittools._run_publication_step

            def step(argv, *args, **kwargs):
                if argv[0] == "/usr/bin/gh":
                    return publication_result(argv, head)
                return run_step(argv, *args, **kwargs)

            with patch.object(gittools, "_github_repository", return_value="example.invalid/owner/repo"), \
                    patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step", side_effect=step):
                self.assertEqual(gittools.publish(repo, "s", "b", head, snapshot),
                                 "https://example.invalid/owner/repo/pull/1")
            self.assertEqual(git(remote, "for-each-ref", "--format=%(objectname) %(refname)"), f"{head} refs/heads/brahma/s")

    def test_push_signing_and_command_remotes_are_pinned_off_for_every_git_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = verified_branch(tmp)
            git(repo, "config", "push.gpgSign", "true")
            git(repo, "config", "protocol.ext.allow", "always")
            got = [subprocess.run([*gitsafe.guarded(repo), "config", "--get", key], capture_output=True, text=True,
                                  env=gitsafe.git_env()).stdout.strip() for key in ("push.gpgSign", "protocol.ext.allow")]
            self.assertEqual(got, ["false", "never"])

    def test_an_unreadable_configuration_refuses_with_a_clear_reason(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            with self.assertRaisesRegex(ValueError, "Not published: the repository's Git configuration could not be read"):
                gitsafe.publication_risks(Path("/nonexistent"))

    def test_gh_style_git_calls_see_the_pins_over_the_repository_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = verified_branch(tmp)
            git(repo, "config", "core.fsmonitor", "sh -c 'touch planted'")
            git(repo, "config", "core.hooksPath", ".husky")
            seen = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.fsmonitor"], capture_output=True,
                                  text=True, env=gitsafe.pinned_env()).stdout.strip()
            hooks = subprocess.run(["git", "-C", str(repo), "config", "--get", "core.hooksPath"], capture_output=True,
                                   text=True, env=gitsafe.pinned_env()).stdout.strip()
            self.assertEqual((seen, hooks), ("false", "/dev/null"))


async def ready_result(plan, worktree, settings, *args, **kwargs):
    (worktree / "first.txt").write_text("verified change\n")
    result = Result("brahma", "ready", "done")
    result.tree = engine._tree(worktree, settings)
    return result


class Finding2ACommitWhoseReceiptFailsIsReported(unittest.IsolatedAsyncioTestCase):
    async def test_pausing_after_commit_tree_keeps_the_branch_at_the_verified_parent(self):
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as tmp:
                store, session, _ = approved_job(tmp)
                with closing(store):
                    base = git(session.repo, "rev-parse", "HEAD")
                    real_git = gittools.git

                    def pause_after_tree(worktree, *args):
                        result = real_git(worktree, *args)
                        if args[0] == "commit-tree":
                            with closing(Store(store.home)) as other:
                                other.pause(session.key)
                                if resume:
                                    other.resume(session.key)
                        return result

                    with patch("brahma.engine.execute", ready_result), \
                            patch.object(gittools, "git", side_effect=pause_after_tree):
                        result = await session.execute(scheduled=True)
                    saved = store.session(session.key)
                    self.assertEqual(result.status, "cancelled")
                    self.assertEqual(git(Path(saved["worktree"]), "rev-parse", "HEAD"), base)
                    self.assertEqual(store.job(session.key)["state"], "queued" if resume else "held")
                    self.assertNotIn("ready_receipt", [event["kind"] for event in store.events(session.key)])

    def test_finalization_cannot_overwrite_a_concurrently_advanced_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, _, _ = verified_branch(tmp)
            identity = gittools.git_identity(repo)
            (repo / "change.txt").write_text("verified change\n")
            real_run = subprocess.run
            concurrent = []

            def run(argv, *args, **kwargs):
                if "update-ref" in argv:
                    git(repo, "commit", "--allow-empty", "-m", "Concurrent update")
                    concurrent.append(git(repo, "rev-parse", "HEAD"))
                return real_run(argv, *args, **kwargs)

            with patch.object(subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(ValueError, "could not advance the verified session branch"):
                    gittools.commit_changes(repo, "Finalization", identity)
            self.assertEqual(concurrent, [git(repo, "rev-parse", "HEAD")])

    async def test_a_commit_that_lands_before_the_receipt_fails_is_reported_and_blocked(self):
        for pause in (False, True):
            with self.subTest(pause=pause), tempfile.TemporaryDirectory() as tmp:
                store, session, digest = approved_job(tmp)
                with closing(store):
                    base = git(session.repo, "rev-parse", "HEAD")
                    real_commit, real_status = gittools.commit_changes, gittools.status_paths
                    landed = []

                    def commit(worktree, summary, identity, **kwargs):
                        head = real_commit(worktree, summary, identity, **kwargs)
                        landed.append(head)
                        if pause:
                            with closing(Store(store.home)) as other:
                                other.pause(session.key)
                                other.hold(session.key, session.generation, digest, "Paused during finalization")
                        return head

                    def status(worktree):
                        if landed:
                            raise OSError("disk went away")
                        return real_status(worktree)

                    with patch("brahma.engine.execute", ready_result), \
                            patch.object(gittools, "commit_changes", side_effect=commit), \
                            patch.object(gittools, "status_paths", side_effect=status):
                        result = await session.execute(scheduled=True)
                    saved = store.session(session.key)
                    self.assertEqual(saved["status"], "blocked")
                    self.assertEqual(result.status, "blocked")
                    self.assertEqual(store.job(session.key)["state"], "blocked")
                    self.assertEqual(bool(saved["paused"]), pause)
                    self.assertFalse(store.resume(session.key))
                    self.assertEqual(landed, [git(Path(saved["worktree"]), "rev-parse", "HEAD")])
                    self.assertNotEqual(landed[0], base)
                    self.assertIn(f"committed as {landed[0]} on brahma/{session.key}", result.text)
                    self.assertIn("readiness receipt could not be made", result.text)
                    self.assertNotIn("ready_receipt", [e["kind"] for e in store.events(session.key)])

    async def test_auto_commit_failure_without_a_ref_change_does_not_report_a_landed_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            with closing(store):
                base = git(session.repo, "rev-parse", "HEAD")
                with patch("brahma.engine.execute", ready_result), \
                        patch.object(gittools, "commit_changes", side_effect=OSError("disk went away")) as commit:
                    with self.assertRaisesRegex(OSError, "disk went away"):
                        await session.execute(scheduled=True)
                commit.assert_called_once()
                saved = store.session(session.key)
                self.assertEqual(saved["status"], "interrupted")
                self.assertEqual(git(Path(saved["worktree"]), "rev-parse", "HEAD"), base)
                errors = [e["text"] for e in store.events(session.key) if e["kind"] == "error"]
                self.assertFalse(any("readiness receipt" in error for error in errors))

    async def test_a_ref_change_during_a_failed_commit_is_reported_as_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, session, _ = approved_job(tmp)
            with closing(store):
                def interrupted_commit(worktree, *args, **kwargs):
                    git(worktree, "commit", "--allow-empty", "-m", "Concurrent update")
                    raise OSError("our commit did not complete")

                with patch("brahma.engine.execute", ready_result), \
                        patch.object(gittools, "commit_changes", side_effect=interrupted_commit):
                    result = await session.execute(scheduled=True)
                worktree = Path(store.session(session.key)["worktree"])
                self.assertEqual(result.status, "blocked")
                self.assertIn("unverified commit " + git(worktree, "rev-parse", "HEAD"), result.text)
                self.assertNotIn("publish it by hand", result.text)


if __name__ == "__main__":
    unittest.main()
