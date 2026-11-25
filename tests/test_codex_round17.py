"""Independent review round 17 of v1.0.0 (stand-in for Codex, decision Round 26): one regression per MEDIUM finding."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brahma import gitsafe, gittools
from tests.test_review12 import git
from tests.test_review16 import verified_branch
from tests import test_review17

OK = type("R", (), {"returncode": 0, "stdout": "https://example.invalid/pr/1", "stderr": ""})


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
                               ("http.https://github.com/.curloptResolve", "github.com:443:127.0.0.1")):
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
            git(repo, "remote", "add", "origin", "https://example.invalid/owner/repo.git")
            identity = gittools.git_identity(repo)
            self.assertEqual(identity["origin"], "https://example.invalid/owner/repo.git")
            git(repo, "remote", "set-url", "origin", "https://example.invalid/elsewhere/repo.git")
            self.assertIn("the origin remote's URL changed", gittools.identity_problems(repo, identity))  # not ready
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step") as step:
                with self.assertRaisesRegex(ValueError, "origin remote's URL changed"):
                    gittools.publish(repo, "s", "b", head, snapshot, expected_origin=identity["origin"])
            step.assert_not_called()

    def test_the_receipt_carries_the_origin_publication_checks(self):
        from brahma.session import Session
        receipt = {"head": "h", "uncommitted": {}, "ignored_allowlist": [], "origin": "https://example.invalid/r.git"}
        session = Session.__new__(Session)
        session.plan, session.key, session.closing = type("P", (), {"summary": "s"})(), "k", __import__("threading").Event()
        session.store = type("S", (), {"home": Path("/nonexistent"), "session": lambda self, key: {"worktree": "/w"}})()
        arguments, _ = session._publish_arguments(receipt, "b")
        self.assertEqual(arguments[7], "https://example.invalid/r.git")

    def test_pr_creation_runs_with_the_same_pins_as_the_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, head, snapshot = verified_branch(tmp)
            calls = []
            with patch("brahma.providers.find_executable", return_value="/usr/bin/gh"), \
                    patch.object(gittools, "_run_publication_step",
                                 side_effect=lambda argv, *a, **k: calls.append((argv, k)) or OK()):
                gittools.publish(repo, "s", "b", head, snapshot)
            argv, kwargs = calls[1]
            self.assertEqual(argv[:3], ["/usr/bin/gh", "pr", "create"])
            env = kwargs["env"]
            pins = {env[f"GIT_CONFIG_KEY_{n}"]: env[f"GIT_CONFIG_VALUE_{n}"] for n in range(int(env["GIT_CONFIG_COUNT"]))}
            for key, value in (("core.fsmonitor", "false"), ("core.hooksPath", "/dev/null"), ("core.pager", "cat")):
                self.assertEqual(pins[key], value)

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


class Finding2ACommitWhoseReceiptFailsIsReported(unittest.IsolatedAsyncioTestCase):
    _run = test_review17.ThirdReviewInterruptedAutomaticPublication._run

    async def test_a_commit_that_lands_before_the_receipt_fails_is_reported_and_blocked(self):
        published = []
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(gittools, "status_paths", side_effect=OSError("disk went away")):
                store, session, result, _ = await self._run(
                    tmp, None, status_patch=False, publish=lambda *a, **k: published.append(a) or "u")
            self.assertEqual(result.status, "blocked")
            self.assertEqual(store.session(session.key)["status"], "blocked")
            self.assertIn(store.job(session.key)["state"], ("done", "blocked"))     # closed, never re-queued
            kinds = [e["kind"] for e in store.events(session.key)]
            self.assertNotIn("ready_receipt", kinds)
            errors = [e["text"] for e in store.events(session.key) if e["kind"] == "error"]
            self.assertTrue(any("readiness receipt could not be made" in text and "brahma/" + session.key in text
                                and "disk went away" in text for text in errors), errors)
            self.assertEqual(published, [])

    async def test_a_failure_before_anything_was_committed_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(gittools, "status_paths", side_effect=OSError("disk went away")):
                store, session, result, _ = await self._run(tmp, None, status_patch=False, auto_commit=False)
            self.assertIsInstance(result, OSError)
            self.assertEqual(store.session(session.key)["status"], "interrupted")
            errors = [e["text"] for e in store.events(session.key) if e["kind"] == "error"]
            self.assertFalse(any("readiness receipt" in text for text in errors))


if __name__ == "__main__":
    unittest.main()
