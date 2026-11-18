# Active build coordination — 18 November 2026

The shared repository is `/Users/dwivabh/Developer/development/brahma`.
Anvi is outside this development task. Decisions are in `docs/decisions.md`.
Round 3 answers are all A; read the actual register before implementation.

## Current ownership

- **Codex coordinator:** `engine.py`, `session.py`, `store.py`, `ui.py`,
  `config.py`, their existing tests, integration, documentation and final review.
- **Claude worker:** `scheduler.py`, `cli.py`, new `tests/test_scheduler.py`
  and `tests/test_cli.py` only. Implement against the store/session APIs below.
- Provider, quota, contract, discovery and model modules have initial focused
  tests. The coordinator owns further changes to them until reassigned.

One worker runs at a time. Do not revert other work. Request an interface change
in your final report rather than editing another owner's files. No account
changes, paid API setup, external messages, Git push, release or merge.

## Claude's bounded assignment

Complete and test the local scheduling/CLI interfaces already scaffolded.

1. Preserve existing CLI behavior and add `worker --home PATH [--once]`,
   `service install|status|stop`, and a durable `pause SESSION` command. A paused
   session cannot resume automatically on a provider reset. CLI output must
   distinguish installation/authentication readiness from unknown quota.
2. Finish `scheduler.tick`, `serve` and macOS service registration. Only existing
   approved jobs with matching session generation and contract digest may run.
   `Session.execute(scheduled=True)` verifies approval and handles active-budget
   persistence. Never create an approval on the user's behalf.
3. Persist waits and run due jobs sequentially. Recover abandoned running jobs
   after daemon restart without overlapping an existing worker. Cancellation
   must stop active child work, preserve checkpoints and avoid retry loops.
4. Service installation must use absolute stable executable/module paths,
   private local state and no embedded credentials. No machine wake or quota
   reset manipulation. Only supported local service registration is in scope.
5. Add meaningful offline tests using temp Git/SQLite fixtures and mocked
   Session/native system calls. No real provider/model requests from tests.

Current APIs: `Store.pending/claim/enqueue/finish_job/recover_jobs/stop`,
`Store.session` includes `stopped`, `contract`, `approved_digest`;
`Session.execute(scheduled=True)` and `Session.stop()`.
`scheduler.Capacity(provider)` is the coordinator's quota gate; preserve it.

Run checks when an approved tool allows it; otherwise report precisely that the
coordinator must run them. Do not claim a generated summary is test evidence.
The full suite currently has two known core fixture errors: tests mutate
in-memory Settings instead of writing the settings bound by the new contract.
Codex is fixing those; do not weaken contract verification to accommodate them.

## Remaining coordinator integration

Finish active-budget/phase resume, Gherkin preview/approval, shared status polling,
instruction loading for explicitly selected discovered definitions, durable
Stop versus terminal detach, full verification and independent review. Native
control of externally started desktop/cloud sessions is not established.

## Verified checkpoint

Claude completed the four assigned scheduler/CLI files through the installed
subscription CLI. Codex ran the combined suite: **132 tests passed in 22.978s**,
including a simulated weekly wait that resumes review without repeating the
build or resetting active budget. Both native CLI sign-in checks passed; the
Codex read-only quota adapter returned real five-hour and weekly windows. Raw
account usage, native session identifiers and transcripts are not published.
Claude is reviewing the coordinator integration read-only; findings remain to
be addressed before release readiness. No source commit, push or merge yet.
