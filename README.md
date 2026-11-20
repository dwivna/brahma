# Brahma

**One conversation for development work through Claude Code and Codex.**

Brahma is a local command-line coordinator. Describe a feature, settle the open
questions, review its Gherkin contract, then approve a bounded implementation.
Brahma assigns work, runs checks, asks the other provider to review the changes,
and saves checkpoints when work stops or a provider reaches its limit.

**Status: first release, version 1.0.0.** This README describes the
merged CLI. Known limits are listed under [Release limits](#release-limits-100). [Planned capabilities](#development-status) are listed separately.
The coordinator runs locally; model requests go through your installed provider
CLIs to their hosted services. Cost savings have not been benchmarked.

[Quick start](#quick-start) · [Workflow](#your-first-feature) ·
[Grill me skill](#the-brahma-grill-me-skill) · [Commands](#command-reference) ·
[Configuration](#configuration) · [Limits and recovery](#usage-limits-and-recovery)

## What Brahma does

- Presents one shared input and separate Codex and Claude output panes.
- Checks whether each CLI is installed and signed in with a supported subscription.
- Binds approval to Gherkin, file ownership, checks, selected definitions, model
  configuration and execution budgets using a hashed contract.
- Runs one provider worker at a time in an isolated Git worktree. The other
  provider reviews the actual changes before work can be reported ready.
- Executes checks itself; a model's claim that tests passed is insufficient.
- Retains task phases, native worker session IDs, repair counts and remaining
  active time across quota waits.
- Can schedule approved continuations through a local macOS background service.

The three panes are output views, not three independently interactive shells.

## Requirements

| Requirement | Purpose |
| --- | --- |
| Python 3.12+ | Runtime; the initial full suite was verified on Python 3.14. |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | CLI installation and environment management. |
| Git | Repository inspection, worktrees and branches. |
| [Codex CLI](https://developers.openai.com/codex/cli) | Planning, implementation or review through a subscription login. |
| [Claude Code](https://code.claude.com/docs/en/setup) | Planning, implementation or review through a subscription login. |
| Terminal at least 80 × 24 | The three-pane interface. |
| [GitHub CLI](https://cli.github.com/) — optional | Draft PR publication; requires `gh auth login`. |

Both coding providers are needed to finish implementation plus independent
review. Missing installation or sign-in is reported; Brahma does not install or
authenticate for you. API-key or unrecognized authentication is refused.

macOS is the primary verified environment. The code uses POSIX process/file
controls and provides a foreground scheduler for Linux. Native Windows support
has not been established. Automatic service installation uses macOS `launchd`.

## Quick start

Install from this repository:

```sh
git clone https://github.com/dwivna/brahma.git
cd brahma
uv tool install --editable .
brahma --version
brahma --demo
```

The executable is `brahma`; the distribution is `brahma-orchestrator`. This
editable installation follows the checkout. Do not assume a package named
`brahma` on a public package index is this project. See [uv's tool installation
guide](https://docs.astral.sh/uv/guides/tools/) for executable/PATH setup.

Demo mode makes no provider requests, authentication probes, repository edits or
publication. It can create local session state.

Sign in with your subscriptions, then check readiness:

```sh
codex login
claude auth login
brahma doctor
brahma --repo /absolute/path/to/your/repository
```

Normal startup also checks installation and sign-in. Fix the reported issue in
your terminal and use `/doctor` to retry. Sign-in does not establish remaining
quota or guarantee that the configured model is available.

For development without a global command installation:

```sh
uv sync
uv run brahma --demo
```

## Your first feature

1. **Select a repository** with `--repo PATH` or `/repo PATH`. Otherwise, the CLI
   uses the current Git repository. Initial worktree creation requires a clean
   tracked source tree; commit or stash your own changes first.
2. **Describe the outcome.** For example: “Add CSV export with a header row;
   preserve column order and test empty results.” Ask to be grilled first when
   important behavior is undecided.
3. **Answer the questions** in the shared input. Recommendations and answers do
   not authorize an unseen implementation contract.
4. **Inspect the contract:** Gherkin, separate owned paths, actual checks, the toolchain
   they will run on (program path and SHA-256, the JDK's hash and version, `JAVA_HOME`), selected
   definitions and model/budget settings. The UI shows its hash and `.feature` path.
5. **Use `/approve`.** macOS asks you to confirm with Touch ID or your login password; nothing is approved
   without that. Brahma then works in its own `brahma/SESSION` branch/worktree, runs checks and requests the other
   provider's review. By default, readiness makes a local commit on that branch; publication is off.
6. **Inspect the result.** `ready` requires checks and review. `needs_checks`,
   `waiting`, `blocked`, `incomplete` and `cancelled` retain unfinished work.

For example, a contract can describe an empty export without prescribing code:

```gherkin
Feature: CSV export
  Scenario: No records
    Given the columns are id and name
    When I export an empty result
    Then the CSV contains the header row only
```

The Gherkin is a review specification, not automatically an executable BDD suite.
Your check commands must verify the behavior. Changes to scope, checks,
definitions, governing instructions or configuration can invalidate approval and
require a new plan.

## The Brahma “grill me” skill

[`brahma-grill-me`](.agents/skills/brahma-grill-me/SKILL.md) turns an incomplete idea
into a proposed feature contract. It finds project facts, asks unresolved user
decisions in rounds of up to four questions, explains tradeoffs and gives feedback
on answers. It preserves decisions and proposes Gherkin, owned tasks and checks.
It does not approve or execute the contract for the user.

### Install in a project

From this checkout, copy the regular files to your selected repository:

```sh
project=/absolute/path/to/your/repository
mkdir -p "$project/.agents/skills" "$project/.claude/skills"
cp -R .agents/skills/brahma-grill-me "$project/.agents/skills/"
cp -R .claude/skills/brahma-grill-me "$project/.claude/skills/"
```

The Python tool installation does not install skills into other projects. Use
copies: Brahma skips symlinked definitions. Personal targets for use across
projects are `~/.agents/skills/` and `~/.claude/skills/`. In Brahma's inventory,
project definitions override matching personal IDs.

### Use the skill

In **Brahma's shared input**, after installing the project skill:

```text
Use .agents/skills/brahma-grill-me/SKILL.md to grill me about CSV export.
Ask about unsettled behavior before proposing implementation.
```

Questions use Brahma's planner format and appear in the shared conversation.
Answer there. `/skills` lists discovered definitions. There is no built-in Brahma
`/grill-me` or `/brahma-grill-me` command in this version.

In the **providers' own interactive clients**, native invocations are:

| Client | Prompt |
| --- | --- |
| Codex | `$brahma-grill-me Help me define CSV export.` |
| Claude Code | `/brahma-grill-me Help me define CSV export.` |

See the official [Codex](https://developers.openai.com/codex/skills) and
[Claude Code](https://code.claude.com/docs/en/skills) skill guides. Brahma-managed
workers use restricted native settings. Explicitly selected provider-specific
definitions require a pinned matching task provider; inventory alone grants no
execution permission.

## Command reference

### Inside the terminal

| Command | Purpose |
| --- | --- |
| `/help` | Show terminal commands. |
| `/repo PATH`, `/new` | Select a repository or start a fresh conversation. |
| `/doctor`, `/status` | Retry sign-in checks or inspect session/checkpoint status. |
| `/models [codex\|claude]` | Inspect advertised models and routine/critical profiles without generation. |
| `/usage [codex\|claude]` | Inspect provider-reported quota and resets without creating a plan. |
| `/approve` | Approve (Touch ID or your password) and execute the displayed contract. |
| `/skills`, `/agents` | Inventory user/project definitions; discovery is not activation. |
| `/sessions`, `/resume ID` | List sessions or open a checkpoint without executing it. |
| `/stop`, Escape | Stop this session for good: cancels its queue; running again needs a new approval. |
| `/pause` | Hold this session: its approval and an unfinished run are kept; `brahma resume ID` queues it again. |
| `/kill` | Global kill switch: halt every worker and refuse new work until `brahma kill --release`. |
| `/capacity codex`, `/capacity claude` | Record your capacity confirmation and queue one approved waiting continuation. |
| `/publish` | After Touch ID or your password, push exactly the commit that was verified ready and open a draft PR with `gh`. A branch that moved, or a worktree with changes, is not published. |
| `/quit`, Ctrl+Q | Close the UI; approved queued work can continue via the service. |

Ctrl+N starts a fresh conversation. The same controls exist in the shell:
`brahma pause|resume|stop SESSION` and `brahma kill [--release]`. The kill switch
is a `KILL` file in the state directory; while it exists no worker starts, running
workers stop within seconds (including workers whose coordinator has died), and coordinator Git writes are refused.
Releasing it needs Touch ID or your password and never restarts anything by itself: queued work is held until
`brahma resume SESSION`.

Brahma's Git guard blocks, in code, every merge, rebase, reset, amend, pull, tag,
branch deletion, force or delete push, and any push that is not a `brahma/*` branch.

### From your shell

```sh
brahma --help
brahma doctor
brahma models
brahma usage
brahma sessions
brahma --resume SESSION_ID

# Proposal only; consumes provider usage.
brahma plan "Add CSV export" --repo /path/to/repository --output /tmp/export.plan.json

# Inspect and resolve the plan first; creates a new execution session. You confirm with Touch ID or your password.
brahma run /tmp/export.plan.json --repo /path/to/repository --approve

# Approve a session's displayed contract (Touch ID or password), then run it; --queue hands it to the service.
brahma approve SESSION_ID [--queue]
brahma run --session SESSION_ID       # runs an already approved session; never approves

# Hold, release or stop a session; global kill switch.
brahma pause SESSION_ID
brahma resume SESSION_ID
brahma stop SESSION_ID
brahma kill            # brahma kill --release to lift
brahma note SESSION "text" [--task N]   # guidance applied at the next checkpoint
brahma decide SESSION --for reviewer|author   # settle a disputed review (Touch ID or password)
brahma proposals        # draft proposals from check failures, stale branches, feedback (starts nothing)
brahma memory show     # what Brahma remembers about this repository
brahma memory forget --yes   # delete its memory and routing history only
```

### For scripts and AI callers

Add `--json` before any command (or a command's own `--json`) and every result or error is exactly one JSON object
with `ok` and `exit_code`; `brahma events` prints one JSON object per line. `models` and `usage` put their report
under `models` / `usage`; `plan` returns the session key and what it needs next.

```sh
brahma --json status SESSION_ID          # where it stands; exit code is the outcome
brahma events SESSION_ID --after 120 --follow   # stream events (JSON lines)
```

An AI caller may plan, read status, usage and models, and run contracts a person has already approved
(`brahma run --session`). Approving a contract, deciding a disputed review, publishing and lifting the kill switch are
human-only: each asks the device owner for Touch ID or the login password at that moment, and refuses otherwise. A
program started by an agent cannot pass that prompt. This does not stop a process that can rewrite Brahma's own source
or drive the screen; on systems other than macOS, human-only actions are refused.

Exit codes (locked at 1.0.0; changing one is a MAJOR version bump):

| Code | Meaning |
| --- | --- |
| `0` | Ready (or the command succeeded) |
| `10` | Waiting for provider capacity, or queued to run |
| `11` | Needs a human decision: contract approval, or a disputed review (`brahma decide`) |
| `12` | Needs approved checks, so nothing can be marked ready |
| `13` | Blocked (policy gate, failed check, provider or service problem) |
| `14` | Stopped, paused or killed by the user |
| `20` | Usage error: bad arguments, unknown session or unreadable input |
| `30` | A provider CLI is missing or not signed in (`brahma doctor`) |

For an existing checkpoint, use `brahma --resume SESSION_ID`, inspect it, then
`/approve`, or `brahma approve SESSION_ID` and `brahma run --session SESSION_ID`.
Use the terminal for multi-round questioning: a shell `plan` invocation produces
one proposal and may still contain unresolved questions.

## Configuration

Create `brahma.toml` in the selected repository. Current defaults:

```toml
[brahma]
planner = "codex"
codex_model = "gpt-6.2-sol"
codex_critical_model = "gpt-6.2-astra"
claude_model = "sonnet"
claude_critical_model = "opus"
task_timeout = 3600
repair_cycles = 3
context_chars = 24000
auto_commit = true
publish = false
checks = []
# Build caches a check may create inside git-ignored directories (name patterns, no slashes).
ignored_allowlist = [".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", "dist", "*.egg-info"]
# Refuse to start a worker when its provider CLI cannot enforce protected reads (see Limits). Off by default.
strict_enforcement = false
```

Use `brahma models` or `/models` to check your CLI's advertised models. Before
approval, Brahma matches these preferences to available models in the same supported
family and locks verified concrete model IDs into the contract. Critical work cannot
downgrade to a routine profile; missing permitted models block generation. If native
discovery or a concrete ID is unavailable, the choice stays explicitly unverified;
a subsequent provider rejection checkpoints the attempt. See
[model selection](docs/model-selection.md) for the rules and their limits.
A `critical` task uses the critical-model profile.
`task_timeout` allows 1–3600 active seconds per task; `repair_cycles`
allows 0–3 repairs after the initial attempt. `context_chars` allows
2,000–100,000 **characters**, not tokens.

Supply project checks in the plan or configuration. For example, replace
`checks = []` in a Python project's configuration with:

```toml
checks = [["python", "-m", "unittest", "discover", "-s", "tests", "-v"]]
```

Commands must work inside the isolated worktree with its available runtime.
They are argv arrays, not shell strings; pipes, `&&` and redirection are not
expanded. Brahma also runs `git diff --check`. Without project check commands,
the outcome stays `needs_checks` even after a positive model review.

Checks run with the same scrubbed environment as workers, plus extra scrubbing:
API keys, tokens, secret-looking names and SSH/GPG agent sockets are removed. This
is a denylist, so an unusually named secret variable can survive. `HOME` and `PATH`
are kept, and a check can still read credential files under your home directory
(`.aws`, `.npmrc`, `.netrc`, `.ssh`, GitHub CLI config). Brahma does not sandbox checks.

### Ownership patterns

Each task owns the paths listed in its plan. Patterns are gitignore-style and
anchored at the repository root: `*`, `?` and `[...]` stay within one directory,
`**` as a whole segment crosses directories, and `src`, `src/` or `src/**` each
own everything below `src`. So `src/*.py` owns `src/a.py` but not `src/a/b.py`.
Git-ignored files count as changes: a worker that creates or edits an ignored
file (for example `.env` or a planted script) is outside its ownership, except
build caches in `ignored_allowlist`. Names like `.env*`, `*.pem`, `*.key` and
`credentials*` always block, even inside an owned path. Inside an allowlisted cache
directory only `.env*` files are still checked; other files hidden there are not
inspected. The allowlist names directories: a file or symlink that merely has an
allowlisted name is not tolerated.

## Usage limits and recovery

Readiness, quota observations and reported token usage are separate facts.
Use `brahma usage` or `/usage` for a readable report. Cached observations are
labelled last reported; missing or stale current capacity stays unknown.

- **Codex:** its native read-only interface can provide quota windows/reset times,
  including five-hour and weekly windows when exposed.
- **Claude:** stream limit events can provide availability and resets. Claude Code offers no
  usage query to programs, so live five-hour/weekly numbers come only from its status-line
  payload. Point Claude's status line at Brahma's tap once (it keeps your current line):

  ```json
  { "statusLine": { "type": "command",
      "command": "brahma statusline --then \"bash ~/.claude/statusline-command.sh\"" } }
  ```

  (in `~/.claude/settings.json`; use the full path to `brahma` if it is not on Claude's `PATH`, and
  drop `--then` if you have no status line of your own). Numbers then update whenever Claude Code
  refreshes its status line in any session, and `brahma usage` labels them with their age. Without
  the tap, Claude usage is shown only from saved limit events and stays unknown after a reset.
- **Unknown capacity:** a signed-in provider may receive one bounded approved work
  attempt. A reported limit checkpoints work instead of repeatedly probing models.
- **Known reset:** the scheduler waits, refreshes supported signals and resumes
  the approved phase. A reset is a recheck opportunity, not guaranteed capacity.
  Waiting does not replenish repair counts or active time.
- **Missing reset:** work remains waiting. After personally checking capacity,
  `/capacity PROVIDER` queues one bounded continuation. This records your
  confirmation rather than a measurement, and cannot undo Stop.

Brahma cannot change quotas, reset provider clocks, purchase usage credits or
promise free usage. Subscription sign-in alone does not establish that extra
usage credits are disabled. See [provider evidence](docs/provider-capabilities.md).

### Background scheduling

On macOS:

```sh
brahma service install
brahma service status
brahma service stop
```

Only approved queued contracts run. The computer must be awake; Brahma does not
wake it. Keep the installation and, for editable installs, its checkout in place.
Installation registers the service for future logins. `service stop` unloads it
but leaves its registration file/checkpoints, so it can load at a later login.

Foreground scheduling on a POSIX system requires a state directory:

```sh
brahma worker --home "$HOME/.local/state/brahma"
# At most one due job:
brahma worker --home "$HOME/.local/state/brahma" --once
```

Use the same state directory for the terminal and scheduler. A worker lock
prevents overlapping Brahma workers using that directory.

## State, privacy and boundaries

Brahma states what it can and cannot prevent. On every worker launch it records an `enforcement` event. Neither
supported CLI is launched with a control that stops it **reading** secret files or paths outside the worktree, so
protected reads are reported as *not enforced*. Codex runs with its `workspace-write` sandbox; Claude is launched without
a shell tool. Neither is a verified path boundary, so Brahma does not claim writes are confined to the worktree.
What Brahma enforces is verification after every worker run, whatever its outcome: changes are measured against the
Git identity taken when the run started (branch, HEAD, Git directory, no hidden index flags), an out-of-ownership or
sensitive change is restored on the isolated branch (never a path you had already edited) and readiness stays blocked.
A change outside the worktree (your home directory, another repository) is not detected.
Set `strict_enforcement = true` to make Brahma refuse to start workers until reads can be enforced. Events and
logs are redacted before they are stored; text a worker prints that tries to direct Brahma (read `.env`, push,
merge) is shown to you as a redacted flag and never acted on.

State defaults to `~/.local/state/brahma`. Override it consistently when needed:

```sh
export BRAHMA_STATE_DIR="$HOME/.local/state/brahma-work"
brahma --repo /path/to/repository
```

It contains `state.sqlite3` (sessions, events, quota observations and jobs),
`contracts/SESSION/HASH.json` and `.feature` artifacts, `worktrees/SESSION/`, and
service diagnostics. Keep state, raw transcripts and sign-in material out of
Git and public bug reports.

Local storage is not offline AI: selected context goes through the provider CLIs.
Review selected definitions and check commands. Brahma combines restricted
CLI settings with post-write ownership/sensitive-path checks. It is not a complete
per-file security boundary: reads of files outside the worktree are guarded only by
the provider CLIs' settings and prompts. Secret redaction covers common credential
shapes (API keys, bearer tokens, cloud and Git host tokens, private keys, `NAME=value`
environment lines) in events, logs and packets; it is pattern based, so an unusual
secret format can pass it. Approved checks run worker-written code with a scrubbed
environment but without a sandbox. Brahma is not a hardened sandbox: review what you approve.

Native continuation covers Brahma-managed workers, not arbitrary external
terminal/desktop/cloud sessions. Agent definitions load as bounded instructions;
they do not automatically spawn child processes.

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| `brahma` not found | Use `uv tool dir --bin` to locate the executable directory and add it to `PATH`. |
| Missing CLI or sign-in | Follow `brahma doctor`'s instructions, then retry. |
| No repository selected | Use `--repo PATH` or `/repo PATH`. |
| Tracked changes block startup | Commit/stash your changes or select a clean worktree. |
| `needs_checks` | Add real project checks, review the new contract and approve it. |
| Contract changed | Review the changed plan/settings/definitions before a new approval. |
| Queue does not resume | Inspect `/status`, service status, Stop state, sign-in and reported resets. |
| Draft publishing fails | Check `gh auth status`, remote access and the retained branch. |

## Release limits (1.0.0)

These are deliberate limits of the first release, not hidden defects:

- **No sandbox.** Workers and approved checks run as you. They can read anything your user can read (secret protection
  on reads is a prompt instruction; neither provider CLI can enforce it), and a check can change files outside the
  worktree. Brahma verifies and restores changes *inside* the worktree only.
- **Toolchain binding** covers each check program, the JDK it selects when that is knowable, the startup files and the
  variables listed in the changelog. Maven rc files are disabled for checks and Gradle options that can move the JDK
  or user home mark the JDK unbound. Fingerprints cover the launched file, not everything it loads, and hashing and
  running are two steps.
- **Human-only actions** (approve, decide, publish, lift the kill switch) need macOS Touch ID or the login password.
  They are refused on other systems. The prompt stops an agent that runs commands; it does not stop a process that can
  rewrite Brahma itself or drive the screen.
- **Not implemented:** signed-policy approval (every 1.0.0 approval needs a person), read-side secret enforcement, and
  confinement of the provider CLIs.

## Development status

The [initial build report](docs/build-review-20261118.md) records historical
verification; it does not certify every later change. Ongoing work includes learned
routing and inspectable repository memory, smaller typed handoff packets, a
desktop/web view sharing the same state, note controls, a human-signed approval policy for AI
callers and stronger enforcement. Automatic compaction and savings need
measurement.

The [decision register](docs/decisions.md) separates requirements from delivered
capabilities. [GitHub Projects](https://github.com/users/dwivna/projects/3) tracks work.

## Contributing

Read [AGENTS.md](AGENTS.md) and the relevant decisions before behavior changes.
Keep tasks bounded and ownership clear; never commit credentials or local task state.

```sh
uv sync
uv run python -m unittest discover -s tests -v
```

Tests use fixtures and mocked providers, not live-provider benchmarks. Report
actual checks and remaining gaps in PRs. The canonical grill-me skill is
`.agents/skills/brahma-grill-me/SKILL.md`; keep its Claude copy byte-identical.
For [bug reports](https://github.com/dwivna/brahma/issues), provide sanitized
versions, commands and reproduction steps rather than databases or transcripts.

## License

[MIT](LICENSE).
