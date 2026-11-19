# Changelog

Brahma uses SemVer 0.x (decision Round 10): MINOR for new capabilities or any change to commands, JSON output,
state schema or contract format; PATCH for fixes. Each merged PR bumps the version; a tag is a separate release
decided by Abhinav. See `docs/decisions.md`.

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
