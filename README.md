# Brahma

One terminal conversation for approved development work through Claude Code and Codex.

**Development prototype.** Python 3.12+, Textual, asyncio and SQLite. Uses existing
subscription CLI sign-in; it does not configure paid API access. Provider-owned
limits still apply. No measured cost-savings claim is made.

Decisions are in [docs/decisions.md](docs/decisions.md), starting at round 1.

From this checkout, with Python and [uv](https://docs.astral.sh/uv/) installed:

```sh
uv sync
uv run brahma --demo
uv run brahma doctor
uv run brahma --repo /path/to/your/repository
```

The terminal has A (Brahma), A1 (Codex) and A2 (Claude), with one input and one
active worker. Describe the goal, inspect the proposed plan, then approve it.
Workers edit an isolated Git branch; they do not automatically merge or release.
Use `/stop` or Escape to request cancellation, `/sessions` to list checkpoints,
and `/resume ID` to reopen one. `/doctor` retries CLI installation and sign-in
checks. Demo mode performs no provider requests, edits or publication.

Checks are explicit argv arrays in the selected repository's `brahma.toml`:

```toml
[brahma]
checks = [["python", "-m", "unittest", "discover", "-s", "tests", "-v"]]
```

Choose the commands appropriate to your repository and runtime. Without
configured checks, a task stays `needs_checks`. A claimed test pass from a model
does not replace an actual check. The prototype requires the other provider's
independent review before reporting ready.

Session state and event logs are local, outside the selected repository. Do not
commit that state or raw transcripts. `BRAHMA_STATE_DIR` can select its location.

The implementation includes hash-bound Gherkin contracts, separate file ownership,
actual check commands, provider quota observations, durable reset waits and native
session IDs for worker continuation. The active 60-minute task budget excludes
quota waits; repair counts persist. `/skills` and `/agents` inventory user/project
definitions; explicitly approved definitions load as bounded worker instructions.
Native plugins and automatic child-agent spawning stay disabled in this version.

The scheduler resumes existing approvals only. Use `brahma service install` to
register the macOS background service, `brahma service status` to inspect it, and
`brahma pause SESSION` to durably prevent a session's automatic restart. `/stop`
also cancels future restarts for the selected session. Closing the terminal can
hand an approved checkpoint to the local service. The computer must be awake.
Linux users can run `brahma worker` under their own service manager.

Codex quota polling was verified against its native read-only endpoint. Claude
supplies stream limit events; a complete independently polled five-hour/weekly
snapshot is not established for its headless mode. Missing capacity remains
unknown, with one bounded approved attempt; a known rejection without a reset
stays waiting for fresh information. Brahma cannot alter provider quotas or buy
reset/usage credits. Read the [provider evidence](docs/provider-capabilities.md).

Native continuation is for Brahma-managed worker sessions. Arbitrary external
CLI/desktop/cloud session takeover is not implemented. Automatic conversation
compaction and cost savings have not been benchmarked. Global installation and
GitHub source publication are being finalized after independent review.

For a local editable installation exposing the terminal command:

```sh
uv tool install --editable .
brahma --demo
```

Run local verification with:

```sh
uv run python -m unittest discover -s tests -v
```
