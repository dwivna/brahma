# Changelog

Brahma uses SemVer 0.x (decision Round 10): MINOR for new capabilities or any change to commands, JSON output,
state schema or contract format; PATCH for fixes. Each merged PR bumps the version; a tag is a separate release
decided by Abhinav. See `docs/decisions.md`.

## 0.2.0

Hardening patch (decision Round 13, from the independent review recorded in Round 11).

**Breaking**
- **Contract format 2.** A contract made by 0.1.x (format 1) is refused with an explanation and must be
  re-planned and re-approved; an old approval is never reinterpreted.
- **Ownership patterns are gitignore-style.** `*`, `?` and `[...]` no longer cross `/` (`src/*.py` no longer owns
  `src/a/b.py`). `**` as a whole segment crosses directories. `src`, `src/` and `src/**` each own their subtree
  (a bare directory used to own nothing). Trailing `/` is now accepted in plans.

**Fixed**
- Approved checks run with the same scrubbed environment as workers (no tokens or provider credentials) and
  without SSH/GPG agent sockets. They previously inherited the owner's full environment. A check can still read
  files in the owner's home directory; a sandbox for checks is not provided.
- Git-ignored files are now part of the change and ownership check. Previously a worker could create or alter
  `.env` or a planted script unseen. Build caches (`.venv`, `__pycache__`, `.pytest_cache`, `node_modules`,
  `dist`, `*.egg-info`, ...) are tolerated through the new `ignored_allowlist` setting in `brahma.toml`, which is
  locked into the contract. Sensitive names (`.env*`, `*.pem`, `*.key`, `credentials*`) always block.
- A transient "overloaded" reply is no longer treated as a quota limit. It is retried up to three times (about
  1, 5 and 10 minutes) inside the same attempt without using a repair cycle or touching quota state, and then
  stops as `blocked` (provider overloaded) instead of stranding the queue.

**Internal**
- One version string: the CLI client info reported to Codex and Claude now uses `__version__`.

## 0.1.0

Initial prototype: locked contracts, one worker at a time on an isolated branch, real checks, independent review,
quota-aware waits, a local scheduler service and a three-pane terminal.
