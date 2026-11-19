# Changelog

Brahma uses SemVer 0.x (decision Round 10): MINOR for new capabilities or any change to commands, JSON output,
state schema or contract format; PATCH for fixes. Each merged PR bumps the version; a tag is a separate release
decided by Abhinav. See `docs/decisions.md`.

## 0.10.0

Terminal status board and decision-only attention (plan task 5).

**Added**
- A plain-language board in the terminal, built from the shared session record (so foreground runs, scheduler runs and a
  reopened terminal agree): per task the current worker, phase, active minutes out of 60, repair cycles, next step and any
  blocker; provider capacity as known, unknown or waiting with its reset; measured usage shown separately from estimates.
  Unknown stays "unknown". Quota waits do not add active time. No raw transcript is read; all text is sanitized.
- Terminal controls on the same durable state as the CLI: Pause and Resume buttons, `/unpause`, and `/note [N:] text`
  (same record as `brahma note`). Stop is unchanged.
- Notifications only for approval needed, escalated disagreement, checks still failing after repairs, lost sign-in, and
  ready for review. Each is shown once, recorded durably (`notices` table, schema 3), so polling and reopening do not repeat it.
- The engine records `checks_failing` and `signin_lost` signals; the builder prompt now teaches the `MISSING_CONTEXT:` line.

**Changed**
- State schema 3 adds the `notices` table (additive). An older Brahma refuses a schema-3 store rather than guess.

## 0.9.0

Packet-driven briefs, bounded context expansion and scoped notes (finishes plan task 3).

**Fixed (found by the automated security review of 0.8.0)**
- A reviewer's `blocked` verdict on the dispute re-review fell through to readiness. A `blocked` re-review now blocks the
  task, and only an explicit `pass` can reach readiness (allow-list instead of deny-list).
- Finding `id` and `path` are sanitized as well as the text, because dispute events are stored unredacted.

- Context expansion (found by the automated review of this branch): files are read by walking the path one component at a time from a
  directory descriptor with `O_NOFOLLOW` (no re-resolved path string, so a swapped parent link cannot redirect it) and judged from
  the descriptor (regular file, single link, same device), the sensitive-name check is case-folded, only the sanitized file
  names appear in events. Worker-created files remain readable by design; only the worktree is ever read.

**Added**
- Builder and reviewer briefs carry the validated handoff packet for their phase (Gherkin slice, owned paths, approved
  checks, diff reference, coordinator-verified check results), written read-only outside the repository. If a packet
  cannot be built the brief falls back to the task alone.
- One bounded context expansion: a builder line `MISSING_CONTEXT: a.py, b.py` makes Brahma add up to five named files
  (12000 characters each, redacted; no symlinks, outside paths or sensitive names) and relaunch once. It is recorded in
  the checkpoint and never repeats after a restart.
- `brahma note SESSION "text" [--task N]`: a sanitized note applied at the next checkpoint, never to a running worker.
  Notes are guidance and cannot change scope, ownership, permissions or checks.

**Not delivered**
- Pause, stop and note handling is unchanged during checks and publication beyond the existing kill and pause gates.

## 0.8.0

Review findings with severity and the one-reply dispute flow (decisions Round 4 item 3 and Round 5 item 4; second part of
plan task 3).

**Added**
- Reviewers report issues as objects with `severity` (high, medium, low) and text. A bare string is an unrated
  finding and counts as medium. A high-severity finding blocks readiness.
- When the reviewer reports a high-severity finding the author (the builder's provider, read-only, role `reply`) may
  reply **once per task**, with evidence (a failing check or a file:line reference). A dispute without evidence counts
  as acceptance. The reviewer then re-inspects with the author's evidence. If it withdraws, the task proceeds to the
  normal readiness gates (checks on the reviewed diff, ownership, postflight). If it still reports a high finding,
  Brahma stops with status `needs_decision` (exit code 11), records an attention item and shows both sanitized
  positions.
- `brahma decide SESSION --for reviewer|author`: Abhinav's decision. `reviewer` sends the task back to repair;
  `author` records an overrule and lets the task continue to the readiness gates (checks and ownership still run). The
  approved job is re-queued; a decision cannot approve anything new.
- The reply allowance and the dispute state are in the checkpoint: after a restart there is no second reply and no
  provider call while a dispute waits for a decision.

**Not yet delivered (rest of plan task 3)**
- Worker briefs from handoff packets, one bounded context expansion, scoped notes at the next checkpoint, and
  session-level note handling.

## 0.7.0

Quota-aware routing, outcome recording and draft proposals (decision Round 4; first part of plan task 3).

**Added**
- `routing.py`. For `auto` tasks the builder is chosen capacity first: a provider whose quota is exhausted is never
  chosen over one with capacity, whatever its record. Among providers with capacity a learned ranking (ready rate,
  then repairs, then review findings, by task type: tests, docs or code, in this repository) is used only when both
  have at least 5 measured runs. Otherwise the choice says "no data yet" and uses a plain default (available before
  unknown, then codex before claude), which is not a claim about which model is better. Pinned providers are
  honoured. The decision and its per-provider table are recorded as a `routing` event.
- Measured outcomes (ready, checks passed, repair cycles, review findings, seconds, provider-reported usage) are
  recorded into per-repository memory when a task finishes, and checks that exit non-zero are remembered.
- `brahma proposals`: draft proposals (with draft Gherkin) from recorded check failures, stale local `brahma/*`
  branches and feedback you pass with `--feedback`. It starts no worker, creates no approval and deletes nothing.

- `brahma statusline [--then COMMAND]`: a Claude Code status-line tap. Claude has no usage query a program may call,
  and reading its sign-in token to poll an endpoint is not allowed, so before this `brahma usage` showed Claude only
  from old limit events. Claude Code hands its own `rate_limits` (5-hour and 7-day percentages and resets) to the
  configured status-line command; the tap records them for `brahma usage` and then runs your existing status-line
  command with the same input, so your line looks the same. It never fails the status line. It needs a one-time
  change of the `statusLine` command in Claude's settings (see README); until then Claude's usage stays "unknown".

**Not yet delivered (rest of plan task 3)**
- Worker briefs are not yet generated from handoff packets; packets are not yet read by the engine.
- The one-reply author dispute and escalation of persistent reviewer disagreement, severity tracking, one bounded
  context expansion and scoped notes are not built. Review still follows the earlier changes-requested loop.

## 0.6.0

Safety boundaries (decision Round 7; task 2 of the Rounds 4-7 plan). Partly delivered, see "Not enforced".

**Added**
- `safety.py`: one place that redacts secrets (events are redacted before they are stored; structured JSON events
  are left parseable), flags embedded instructions in worker output as redacted, never-acted-on warnings, and
  reports what each provider launch can and cannot enforce. An `enforcement` event is recorded on every launch.
- `strict_enforcement` setting (off by default): refuse to start any worker whose provider cannot enforce
  protected reads. Both supported CLIs currently cannot, so turning it on stops all workers by design.
- Postflight restore: an out-of-ownership, sensitive or symlink-escaping change made by a worker is reverted on the
  isolated branch (tracked files to HEAD, new files removed, never following a link out of the worktree). A path
  already edited before launch is never touched. Readiness stays blocked after the rollback.
- `run_checks` can return the coordinator-observed exit status per command, in the form handoff packets use.
- `publish` pushes an explicit `brahma/X:brahma/X` refspec, so no configured upstream can reach a default branch.

**Fixed (independent security review of 0.5.0)**
- Packet session ids must be real session ids and the packet directory must stay inside the packets folder
  (a planted symlink or a `../` id can no longer redirect a write or unlink).
- The secret detector now sees compound names, quoted values, `Bearer`/`Basic` tokens and `github_pat_` tokens, and
  scans command arguments both per word and joined.

- Git is run with literal pathspecs everywhere, and "tracked" is decided by exact comparison, so a worker-chosen
  file name such as `:(glob)**` or `*` can no longer be read as a pattern by the restore or staging commands.

**Not enforced (stated plainly)**
- Reads of secret files or paths outside the worktree by a worker are not prevented by either CLI's launch settings;
  only reported. Credential files under `$HOME` stay readable. No sandbox for checks.

## 0.5.0

Shared record, handoff packets and repository memory (decision Rounds 4 and 5; first task of the Rounds 4-7 plan).

**Added**
- State schema 2 (additive migration; existing databases open with their data intact). Brahma refuses a state
  database from a newer version rather than reinterpret it. New tables: task state, attention events,
  repository memory and measured outcomes.
- Handoff packets (`handoff.py`): a validated, bounded, immutable packet of the locked Gherkin slice, owned
  paths, check commands, diff reference, coordinator-verified check results and open findings. Author summaries,
  history, transcripts, unknown fields and secrets are rejected. Packets are written read-only (mode 0444) outside
  the repository; the module has no access to the store.
- Repository memory (`memory.py`), keyed by the canonical repository identity (stable across worktrees):
  decisions, conventions, check commands, past failures and measured outcomes by provider and task type. Routing
  history reports "no data yet" below 5 samples. Measured usage and estimates are separate fields.
- `brahma memory show` and `brahma memory forget --yes`: forget removes one repository's memory and routing
  history only; sessions, approvals, worktrees and checkpoints are untouched.

**Not yet wired**
- The engine does not yet read packets, record outcomes or use the routing history; that is the next task of the plan.

## 0.4.0

JSON command-line interface and exit codes (decisions Rounds 9 and 11).

**Added**
- `--json`: every command prints one JSON object with `ok` and `exit_code`, and errors are JSON too.
- `brahma status SESSION`: where a session stands (status, approval, contract digest, pause/stop/kill, queued
  job); the exit code is the outcome.
- `brahma events SESSION [--after N] [--follow]`: a streamed event log as JSON lines with sequence numbers.
- There is deliberately no `approve` command: an AI caller may not approve its own contract (Round 9 item 3).
  The human-signed policy approval of Round 11/12 is still not built.

**Breaking**
- Exit codes are now the documented table (README): 0 ready, 10 waiting, 11 needs approval, 12 needs checks,
  13 blocked, 14 cancelled/paused/killed, 20 usage error, 30 provider CLI missing or not signed in. Previously
  errors were 2 and `brahma doctor` failures were 1. This is the *proposed starting table*; the final table is
  not yet confirmed by Abhinav.

## 0.3.0

Kill switch, distinct Pause/Stop, and Git hard blocks (decisions Rounds 6, 7 and 11).

**Added**
- `brahma kill` engages a global kill switch (a `KILL` file in the state directory, so it works even if the
  database is locked). Running workers are cancelled within seconds, no worker starts, and coordinator Git
  writes are refused. Approvals and queued jobs are kept. `brahma kill --release` lifts it; nothing restarts
  by itself. In the terminal: `/kill`.
- **Pause is now different from Stop.** `brahma pause SESSION` (terminal `/pause`) holds a session: its
  approval and queued job are kept and a running worker stops at its checkpoint. `brahma resume SESSION`
  releases it under the existing approval. `brahma stop SESSION` (terminal `/stop`) cancels the queue and
  running again needs a new approval; `resume` cannot undo a stop.
- Git hard blocks in code, whatever a plan or model asks: no merge, rebase, reset, cherry-pick, amend, pull,
  tag, branch delete/rename, force or delete push, and no push of anything but `brahma/*` branches (default
  branches such as `master`, `main`, `develop` are never written).

**Changed**
- The state schema gains a `paused` column (added automatically; existing state keeps working).
- A pause or kill during a scheduled run keeps the approved job queued instead of marking it done; a stop
  marks it cancelled.

## 0.2.0

Hardening patch (decision Round 13, from the independent review recorded in Round 11).

**Breaking**
- **Contract format 2.** A contract made by 0.1.x (format 1) is refused with an explanation and must be
  re-planned and re-approved; an old approval is never reinterpreted.
- **Ownership patterns are gitignore-style.** `*`, `?` and `[...]` no longer cross `/` (`src/*.py` no longer owns
  `src/a/b.py`). `**` as a whole segment crosses directories. `src`, `src/` and `src/**` each own their subtree
  (a bare directory used to own nothing). Trailing `/` is now accepted in plans.

**Fixed**
- Approved checks run with a scrubbed environment (no tokens, secret-looking names, provider credentials or
  SSH/GPG agent sockets). They previously inherited the owner's full environment. This is a denylist and can
  miss unusual names; `HOME` is kept and credential files under it stay readable. A sandbox for checks is not
  provided.
- Git-ignored files are now part of the change and ownership check. Previously a worker could create or alter
  `.env` or a planted script unseen. Build caches (`.venv`, `__pycache__`, `.pytest_cache`, `node_modules`,
  `dist`, `*.egg-info`, ...) are tolerated through the new `ignored_allowlist` setting in `brahma.toml`, which is
  locked into the contract. Sensitive names (`.env*`, `*.pem`, `*.key`, `credentials*`) block everywhere
  except inside an allowlisted cache directory, where only `.env*` is still checked (a venv legitimately
  contains `cacert.pem` and `credentials.py`); other files hidden in a cache directory are not inspected. A
  file or symlink that only has an allowlisted name is not tolerated. Owned ignored files no longer break the
  auto-commit (they are reviewed but not staged).
- A transient "overloaded" reply is no longer treated as a quota limit. It is retried up to three times (about
  1, 5 and 10 minutes) inside the same attempt without using a repair cycle or touching quota state, and then
  stops as `blocked` (provider overloaded) instead of stranding the queue.
- Ownership matching uses no regular expressions: a hostile planner pattern can no longer hang the
  coordinator (ReDoS) or raise, and a stray `[` can no longer swallow a `/` and make two tasks own one file.
  Patterns are limited to 200 characters and 16 segments.
- An "overloaded" classification needs a provider-shaped error (HTTP 529 / `overloaded_error`), not a model's
  own sentence that contains the word; a retry only waits if a useful part of the time budget remains.
- A stored session with an old-format contract no longer crashes the terminal; it explains and asks to re-plan.
- A bare directory owner now pulls in `AGENTS.md`/`CLAUDE.md` inside that directory.
- Invalid UTF-8 file names no longer raise; very large ignored files are fingerprinted by size and mtime.

**Internal**
- One version string: the CLI client info reported to Codex and Claude now uses `__version__`.

## 0.1.0

Initial prototype: locked contracts, one worker at a time on an isolated branch, real checks, independent review,
quota-aware waits, a local scheduler service and a three-pane terminal.
