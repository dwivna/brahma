# Handoff: Claude to Codex, 25 November 2026 (about 22:00 IST)

Abhinav: "ur limits are ending handoff the everywork to codex". Claude's usage is ending; Codex takes over everything
below. Read `AGENTS.md`, `docs/decisions.md` (Rounds 26 and earlier) and the CHANGELOG first. Approval stays human-only;
never approve, merge or tag without Abhinav's explicit word. Tests must never launch real provider CLIs.

Test command: `perl -e 'alarm 590; exec @ARGV' uv run python -m unittest discover -s tests`

## State

- `master` = tag `v1.0.0` = `6c4955d` (released).
- `feat/chat-view` (`540125b`, pushed): docs only. It holds decision Rounds 17-26 and the unapproved 1.1.0 contract
  `docs/plans/1.1.0-chat-view.plan.json`. It has to be rebased onto master after 1.0.1 merges.
- `fix/1.0.1` (pushed):
  - `e14778d` Release 1.0.1. Two MEDIUM findings of review round 17 of v1.0.0, which a Sonnet 5.5 agent ran as a
    stand-in for Codex (Round 26):
    - `gh pr create` now runs under `gitsafe.pinned_env()`, and `gitsafe.publication_risks()` refuses risky
      local or worktree Git config;
    - a commit whose receipt step fails is now reported, and the run is blocked, instead of the commit being
      stranded silently.
  - `3d42fce`: fixes from an automated security review. The origin is bound into `git_identity` and the receipt, and
    the risk list is wider.
  - **WIP commit after 3d42fce (tests failing, see below)**: fixes from the next automated security review and the
    Sonnet review of the patch:
    - `push.gpgSign=false` and `protocol.ext.allow=never` are pinned in `_GIT`;
    - `ssh.*`, `http.*extraheader` and `http.*curlopt*` are refused;
    - `publication_risks` gives a clear error if the config listing fails;
    - `gittools.push_target()` takes a sha256 of `git remote get-url --push --all origin`, resolved the way the
      push resolves it;
    - the origin is recorded once per session, as an `origin_baseline` event, at its first run, and is never
      re-recorded on a resume. A resume with a changed origin is refused;
    - `Session._publish_arguments` refuses a receipt with no origin. That includes 1.0.0 receipts and repos with
      no origin.

## Failing tests at the WIP commit (702 run: 2 failures, 4 errors). The causes are known; tests need updating, not the design.

1. `test_review14 ... test_a_clean_verified_result_is_published_with_its_receipt`,
   `test_review15 ... test_publication_reads_state_on_the_loop_thread_and_publishes_in_a_worker_thread`,
   `test_review17.SecondReviewPublication` (2 tests): their receipts or identities have no `"origin"`, so
   `_publish_arguments` now refuses. Add `"origin": "o"` (any non-empty value) to the receipt or identity mocks there.
2. `test_review9.SessionFlow.test_automatic_publication_pushes_the_verified_commit`: its real fixture repo has no origin
   remote, so the receipt's origin is "". Add `git remote add origin <url>` to that fixture.
3. `test_codex_round17 ... test_origin_repointed_after_the_run_started_is_never_pushed_to`: it still expects
   `identity["origin"]` to be the URL. It is now `gittools.push_target(repo)`, a sha256. Assert it is non-empty and
   equals `push_target(repo)`, and keep the rest of the test.
4. Add tests for:
   - a resume where `origin_baseline` differs from the current push target is refused;
   - a receipt with no origin is refused by `_publish_arguments`;
   - a legacy `.git/remotes/origin` file changes `push_target`.
5. Add the per-session origin, `ssh.*`, extra-header, curl-option and `_GIT` pin changes to `CHANGELOG.md` "1.0.1". Note
   that `origin_baseline` is a new event kind: the 1.1.0 chat renderer needs a rule for it, because task 1 tests
   that every kind has one.

## Then, in order

1. Run the full suite green, commit and push on `fix/1.0.1`.
2. Independent review of `git diff 6c4955d fix/1.0.1` by a fresh reviewer that did not write the code. A Sonnet review
   of `e14778d` found no HIGH; its MEDIUM about the incomplete risk list is fixed by the WIP. A review covering up to
   `3d42fce` was started, but its result was not collected before this handoff.
3. Report with the six-field table and ask Abhinav for the merge-and-tag decision for 1.0.1. After an explicit
   "merge":
   - fast-forward master and push;
   - tag `v1.0.1` only if told to;
   - `brahma service stop`, then `brahma service install`;
   - delete `fix/1.0.1` locally and on origin;
   - check out master;
   - `uv tool install --editable . --force`.
4. Rebase `feat/chat-view` onto master. Add a rule for `origin_baseline` to the 1.1.0 plan, task 1. Abhinav then
   approves it himself:
   `brahma run --repo /Users/dwivabh/Developer/development/brahma --approve docs/plans/1.1.0-chat-view.plan.json`.
5. Later, as decided in Round 26:
   - 1.1.1: a baseline gate (approved checks run on the untouched base; red means "blocked: baseline red", with no
     repair charged) and one retry for invalid reviewer JSON (a second failure is recorded as "reviewer output
     invalid", with no repair);
   - 1.2.0 same-provider fallback (Rounds 24-25);
   - 1.3.0 benchmark;
   - 1.4.0 the Claude builder runs approved checks;
   - 1.5.0 calibrated review;
   - then the security skills.
6. Also outstanding: Codex should answer `docs/plans/round-21-codex-questions.md` itself, if Abhinav wants its own view
   beside the stand-in's (Round 26).
