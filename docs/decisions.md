# Brahma decisions — Abhinav's register

This register belongs to Brahma. Its rounds start at 1, independently of Anvi.
Developer coordination is external to the intelligence being developed.

## Round 1 — 18 November 2026

Abhinav: **“Q1 - A, Q2 - A, Q3 - A.”**

1. **Existing signed-in CLIs:** use Claude Code and Codex subscriptions. No automatic paid API fallback;
   normal provider limits apply. Savings and extra quota are not claimed without measurements.
2. **Three-pane terminal:** A is Brahma's shared conversation; A1 is Codex; A2 is Claude.
3. **Bounded execution:** assign, monitor and exchange reviews automatically within an approved plan,
   one worker at a time. Abhinav retains decisions and stop control.

Abhinav also asked for a separate GitHub repository/project named `brahma`, a suitable stack and an
installed `brahma` terminal command. Framework selection was delegated to the builder.

## Round 2 — 18 November 2026

Abhinav: **“Q1 - B ( created the repo - https://github.com/dwivna/brahma.git ), Q2 A, Q3 A, Q4, B.”**

1. **Public:** use the public `dwivna/brahma` repository and a linked Projects board named `brahma`.
2. **Git authority:** branches, commits and draft PRs are allowed for approved work; merges and releases remain Abhinav's.
3. **Repository scope:** any explicitly selected Git repository, Anvi first; each project's instructions remain applicable.
4. **Run boundary:** sixty minutes per task, at most three repair cycles. Exhaustion checkpoints work;
   it never skips tests or turns unfinished work into completed work.

Abhinav: **“Brahma should have his own decesion and round for brahma start from 1, so what all decesion
you added in anvi decesion remove and all brahma doc keep it in brahma repo.”**

The earlier Brahma entry was removed from Anvi's decision register. These two rounds retain the decisions here.

## Round 3 — 18 November 2026

Abhinav:

> Brahma will create a feature gherkin file after locking it using grill me then share the gherkin file to claude and codex to complete the implementation, clear seperation of work should be taken care by brahma for claude and codex, a common sharing system between brahma, codex and claude must exist grill me more on this

> brahma should be able to pause and resume any task, any session of claude and codex, should be able to available subagents and skills of claude and codex at project and user level

> brahma must be aware of usgae limit, resets, 5 hour limit, weekly limit, availability and should assigned task to claude and codex based on that, brahma should automatic start work if usage got reset, brahma should have control of codex and claude limits and reset so that brahma can smartly and effectively usage codex and claude

> i hope brahma is doing basic check if claude/codex available on user system as cli, and it's logged in otherwise flag the user that claude/codex not installed or logged in, ask user to do so

> brahma must be expert in prompting, tokenizing, compacting and saving cost, he should be able to maximize the output of work and should be easy to use

These are required capabilities, not a report that they exist in the prototype.
Provider-owned quotas and reset schedules cannot be changed by Brahma. Brahma can
observe exposed capacity, route approved work, wait, checkpoint and resume through
supported controls. Missing, stale or unexposed quota information must stay unknown;
request token counts do not establish remaining subscription capacity.

The four questions sent in this round are:

1. Lock Gherkin, ownership, allowed skills/agents, checks and budget together (A)
   or approve assignments separately after locking Gherkin (B).
2. Resume approved work through a local background service while the computer
   is awake (A), or only while the terminal remains open (B).
3. Count active execution time, excluding quota waits, toward sixty minutes (A),
   or count all elapsed time (B). Repair allowance is unchanged.
4. For verified sign-in but unknown remaining quota, allow one bounded approved
   work attempt (A), or require manual capacity confirmation (B).

Abhinav: **“Q1 - A, Q2 - A, Q3 - A, Q4 - A.”**

1. **Complete contract:** one approval locks Gherkin scenarios, separate file
   ownership, allowed skills/subagents, actual check commands and task budget.
   Scope or permission expansion requires a new approval.
2. **Durable local scheduling:** a background service may resume approved queued
   work after capacity returns, while the computer is awake. Stop cancels future
   restarts. Closing the terminal need not cancel an approved queue.
3. **Active-time budget:** count active execution toward sixty minutes per task;
   exclude quota waiting, including a weekly wait. Preserve the repair allowance
   and remaining active time across checkpoints and restarts. This clarifies the
   run boundary recorded in Round 2.
4. **Unknown capacity:** verified sign-in with unknown remaining allowance permits
   one bounded approved work attempt. Observe its real signals; checkpoint on
   limits instead of repeatedly probing with model requests.

Control of externally started sessions and the native customization boundary may
require a later round; no unrestricted control of other sessions is inferred.

## Engineering choices, not additional user decisions

Python 3.12+, Textual, asyncio subprocesses, SQLite and uv. CLI workers inherit existing sign-in;
model names are configurable. Initial Codex profiles use locally advertised GPT-6.2-Sol/Astra;
Claude uses its documented Sonnet/Opus aliases. No credentials or run transcripts are published.

## Round 4 — 18 November 2026

Abhinav: **“Q1 - A, Q2 - A, Q3 - A, Q4 - A.”** (all recommended options)

1. **Routing:** learned and quota-aware. Quota state overrides. Outcomes (checks passed, repair cycles,
   review findings, by task type) are recorded locally per repository. With too few samples Brahma says
   "no data yet" and uses simple strengths rules; it never presents a guess as a learned preference.
2. **Proactivity:** Brahma may draft proposed plans from repository signals (failing checks, stale branches,
   open review feedback). It never starts work without approval; approval still locks the complete contract.
3. **Review:** the non-author model reviews the diff against the locked Gherkin (not the author's summary),
   configured checks must pass, and an unresolved high-severity finding blocks "ready".
4. **Memory:** local per-repository memory outside the repository: decisions, conventions, check commands,
   past failures, model performance. Inspectable and deletable (a forget-this-repository command is
   required); never committed or sent anywhere.

These are required capabilities, not a report that they exist in the prototype.

## Round 5 — 18 November 2026 (shared workspace)

Abhinav: all four recommended options.

1. **Handoff:** a small typed packet per handoff: locked Gherkin slice, owned files, diff/commit reference,
   check results, open findings. Workers read files and Git themselves; no free-form history or
   author-written summary is forwarded.
2. **Shared record:** Brahma's local state (SQLite and event log) outside the repository. Brahma writes a
   read-only per-task packet for each worker; workers never write the shared record directly.
3. **Context size:** minimal by default: task slice, owned paths, check commands. Compact at checkpoints.
   Report measured usage separately from estimates. If a worker reports missing context, widen once.
4. **Disagreement:** the author may reply once with evidence (a failing check or code reference). If the
   reviewer still disagrees, Brahma stops, shows both positions and waits for Abhinav.

These are required capabilities, not a report that they exist in the prototype.

## Round 6 — 18 November 2026 (interface)

Abhinav: Q1 B, Q2 A, Q3 A, Q4 A.

1. **Doors:** the terminal conversation plus a desktop/web view. Every door must lead to the same session
   and memory; the second interface may not hold separate state or approvals. Voice is not in this round.
2. **Status:** a plain-language live board per task: who is working, phase, active time of sixty minutes,
   repair cycles, provider capacity (known / unknown / waiting with reset), next step and anything needing
   Abhinav. Measured usage is shown separately from estimates; raw transcripts are not shown.
3. **Interrupts:** Stop cancels and checkpoints; Pause keeps the task for a later resume; a note is applied at
   the next checkpoint. Any change of scope, files or permissions needs a new approval. No live injection
   into a running worker.
4. **Attention:** notify only for decisions and blocks: approval needed, escalated disagreement, checks
   failing after repairs, lost sign-in, work ready for review. Routine progress stays on the board.

These are required capabilities, not a report that they exist in the prototype.

## Round 7 — 18 November 2026 (safety limits)

Abhinav: hard stops A/B/C (not D), enforcement A, untrusted text A, kill switch A.

1. **Hard stops, even inside an approved contract:** (a) merge, release or push to the default branch;
   (b) edits outside the locked file ownership; (c) reading or sending credentials or secrets, including
   Claude/Codex sign-in material and environment files, which are also redacted from logs and packets.
   These are enforced in code, not only requested in prompts. Deleting or rewriting history was **not**
   selected as a hard stop; until Abhinav decides otherwise it stays a flagged action needing explicit
   approval, and it is never done automatically.
2. **Enforcement:** restrict before, verify after. Workers run on an isolated branch or worktree with each
   provider CLI's strictest working sandbox and permission settings; Brahma then inspects the diff and Git
   state. Violations are reverted on the branch and block "ready". Brahma states what it cannot prevent.
3. **Untrusted text:** repo files, issues, PR comments, web pages and worker output are data, never
   instructions. Only Abhinav and the locked contract direct work. Embedded instructions are shown to
   Abhinav as flagged text and ignored for action. A repository's instruction files apply only when selected.
4. **Kill switch:** one command stops all workers and the service, cancels future restarts, checkpoints
   and writes a stop record. Nothing is deleted. Resuming needs Abhinav's explicit action.

These are required capabilities, not a report that they exist in the prototype.

## Round 7 addendum — 18 November 2026

Abhinav chose a **hard block** for deleting or rewriting Git history (force-push, branch deletion, history
rewrites). This supersedes the flagged-only wording in Round 7 item 1; there is no approval path in this
version. Abhinav also approved the Rounds 4-7 plan in `docs/plans/rounds-4-7.plan.json` as drafted (with this
amendment), executed on an isolated branch; merge remains Abhinav's.

## Round 8 — 19 November 2026 (model selection)

Abhinav:

> brahma should be able to check and select the available best and suitable model for codex and claude based on work

This extends the routing requirement in Round 4. Implementation evidence and
the limits of advertised model availability are documented in
[model-selection.md](model-selection.md). The request is a target; it does not
establish measured superiority, included billing entitlement or quota availability.

## Round 9 — 19 November 2026 (humans and AI callers)

Abhinav: AI interface B, priority A, autonomy A, measures A + B + C (not D).

1. **AI interface:** a stable JSON command-line interface only: JSON output on every command, fixed exit codes
   and a streamed event log, so an AI agent shells out. No MCP server and no local HTTP API in this phase.
2. **Priority:** human first, agent-safe. The terminal conversation stays the primary door; the agent door is a
   second door over the same session, memory and approvals and may never carry weaker safety.
3. **Autonomy of an AI caller:** may propose plans, read status, usage and models, and run contracts a human
   has already approved. It may not approve its own contract, widen scope or permissions, merge or publish.
4. **Success measures:** verified-ready rate per provider and task type; repair cycles and active time per
   task; provider-reported usage per task, kept separate from estimates. Human interventions per task were not
   selected. No savings or superiority claim is made without these measurements.

These are required capabilities, not a report that they exist in the prototype.

## Round 10 — 19 November 2026 (versioning and releases)

Abhinav: scheme A, bump B, gate A + B + C (not D), breaking changes B.

1. **Scheme:** SemVer 0.x. MINOR for new capabilities or any change to commands, JSON output, state schema or
   contract format; PATCH for fixes. 1.0.0 is reserved for a stable, independently reviewed JSON interface
   and safety limits.
2. **When the version changes:** every merged PR bumps the version in the same PR (not only at release).
3. **Release gate:** the full offline suite passes on the exact commit; an independent reviewer who did not
   write the change reads the diff since the last tag, and open HIGH findings block the release; a smoke test
   of the installed tool (doctor, usage, models, demo start, with no model requests) passes. Abhinav's own
   explicit approval was not selected as a gate here; merges and releases remain Abhinav's under Round 2.
4. **Breaking state or format changes:** best effort; fix reports as they appear.

**Open tensions, to settle in the next round (not decided):**

- *Per-PR bump vs release gate.* If every PR bumps the version, a "release" (tag) must be a separate act. The
  gate in item 3 applies to tagging, not to each merge.
- *Best effort vs approval integrity.* Round 3 binds an approval to a contract digest. Best effort may not
  let an old-format approved contract be silently reinterpreted; a minimal schema-version check that refuses
  an unknown format is proposed, pending Abhinav's decision. AI callers that parse JSON (Round 9) also need
  breaking JSON changes to be visible by version.

The Rounds 4-7 plan referenced in the Round 7 addendum is `docs/plans/rounds-4-7.plan.json`.

## Round 11 — 19 November 2026 (independent review, priorities and approval)

Abhinav: fix first A, approval C, exit codes A, versioning A.

An independent read-only review and checks run on `master` (a75917e) found, among others: checks run with the
owner's full environment; git-ignored files such as `.env` are invisible to the ownership gate (verified);
`src/*.py` accepts nested files; read-side secret protection is prompt-only; a transient "overloaded" reply is
classified as a quota limit; the Round 7 hard stops and kill switch are not implemented.

1. **Order of work:** close the enforcement holes before new features: a patch-level change that fixes the
   checks environment, ignored-file detection, ownership glob semantics and the "overloaded" misclassification,
   each with a test. Rounds 4-7 task work waits for it.
2. **Approval for AI callers:** a human-signed policy approval. A human signs a policy once (paths, checks,
   budget, risk class); contracts that match it may be approved without a fresh prompt. This amends AGENTS.md
   "Plans need user approval" and refines Round 9 item 3: the AI caller still cannot approve for itself, because
   only the human-signed policy can authorize. **Not yet designed; no code may implement it until settled:**
   who signs and how the AI is prevented from signing or editing the policy (for example a TTY-only step or a
   key the agent cannot read); what a policy may never cover (the Round 7 hard stops, merge, release, scope or
   permission expansion); expiry and revocation; the digest bound to each approval; and an audit record.
3. **Exit codes:** a distinct code per outcome, documented and versioned; changing one is a MINOR bump.
   Proposed starting table: 0 ready, 10 waiting/queued, 11 needs approval, 12 needs checks, 13 blocked,
   14 cancelled, 20 usage error, 30 sign-in or CLI missing. Final table to be confirmed.
4. **Versioning (resolves the Round 10 tensions):** each PR bumps the version; a tag is a separate release
   decided by Abhinav and subject to the Round 10 gate. State and contracts carry a schema version; Brahma
   refuses to run an approved contract of an unknown format and never reinterprets it; otherwise best effort.

These are required capabilities, not a report that they exist in the prototype.

## Round 12 — 19 November 2026 (signed-policy approval design)

Abhinav: Q1 A, Q2 A, Q3 A, Q4 A (all recommended). This designs Round 11 item 2; it is not implemented.

1. **Signing:** a human-presence step. `brahma policy sign` needs an interactive terminal and a macOS
   Keychain/Touch ID or password prompt at signing time. The agent may read policies but cannot create or edit
   them. An AI caller runs as the same OS user, so no key or file it can read is a barrier. Non-macOS systems
   have no signing path until Abhinav decides a fallback; there, policy approval stays unavailable and direct
   human approval is required.
2. **Scope:** narrow and routine only. A policy names the repository, owned-path globs, exact check commands,
   a task budget and permitted providers, and covers only `routine` complexity. `critical` tasks and anything
   outside the listed paths, checks, budget or providers always need a fresh human approval. The complexity
   label is assigned by the planner and is not trusted alone; the signed paths, checks and budget are the
   binding limits.
3. **Lifetime:** default expiry 24 hours, maximum 7 days, with an optional use-count cap. `brahma policy revoke`
   and the kill switch cancel a policy at once. Editing the policy, changing the repository's checks, or a
   change of Brahma's contract format invalidates it. Re-signing is always a human step.
4. **Boundaries:** any mismatch (a path, check, budget, provider, complexity or changed digest) stops and asks
   Abhinav with the exact difference; Brahma never auto-narrows a contract. Every policy approval writes an
   audit record (policy id, contract digest, time) and appears on the status board. The Round 7 hard stops and
   merge/release can never be covered by a policy.

AGENTS.md is amended ("plans need user approval, or a valid human-signed policy") only when this is
implemented and tested, so it does not describe behaviour that does not yet exist.

These are required capabilities, not a report that they exist in the prototype.

## Round 13 — 19 November 2026 (hole-closing patch scope)

Abhinav: Q1 A, Q2 A, Q3 A, Q4 A (all recommended). Implements Round 11 item 1; not yet built.

1. **Check environment:** run configured checks with the same scrubbed environment workers receive (no tokens or
   provider credentials), keeping only what tools need. This does **not** stop a test from reading files in the
   owner's home directory; a sandbox or container for checks is deferred and is not claimed.
2. **Ignored files:** include git-ignored files in the change check. A default allowlist of build caches
   (`.venv`, `__pycache__`, `.pytest_cache`, `node_modules`, `dist`) is tolerated; any other ignored file a
   worker creates or edits counts as outside ownership; sensitive names (`.env*`, `*.pem`, `*.key`,
   credentials) always block. The allowlist is configurable in `brahma.toml` and locked into the contract.
3. **Ownership patterns:** gitignore-style. `*` stays within one directory, `**` crosses directories, and a
   directory path (`src/` or `src/**`) owns its subtree. The planner prompt and plan parser are updated to
   match. This changes what existing contracts mean, so it is a MINOR version bump (0.2.0) and a contract of
   the old pattern format is refused rather than reinterpreted (Round 11 item 4).
4. **Provider "overloaded":** a transient error, not quota. Retry up to three times with growing delays (about
   one, five and ten minutes) within the same attempt, without spending a repair cycle or changing quota state.
   If it still fails, checkpoint as blocked "provider overloaded" with the reason, never as a quota limit.

Out of scope for this patch (still open): read-side secret enforcement and log redaction, the kill switch and
Pause, the history-rewrite hard block, the agent JSON interface, and signed-policy approval.

These are required capabilities, not a report that they exist in the prototype.

## Round 14 — 20 November 2026 (1.0.0 release decision)

Abhinav: 1.0.0 locks the first version of Brahma; nothing with a known bug, security gap or vulnerability lands in it.
Codex reviews independently, and every finding is fixed and closed before release (this replaces an earlier
"cap the review at round 7"). Q1 A, Q2 A (both recommended).

1. **Gate:** the Round 10 gate in full. The suite passes on the release commit, an independent Codex review of the whole
   codebase (not only the last diff) ends with no open findings, and the installed-tool smoke test passes.
2. **Signed-policy approval (Q1 A):** not in 1.0.0. Every contract in 1.0.0 needs direct human approval, which is the safe
   default; Round 12 is implemented later as a MINOR release.
3. **Read access (Q2 A):** documented as a limit, not built. Checks and workers are not sandboxed for reads: they can read
   files the owner's user can read. Secret protection on reads is a prompt instruction because neither provider CLI can
   enforce it. Writes stay enforced by ownership verification.
4. **Versioning:** SemVer from 1.0.0. A breaking change to commands, JSON output, exit codes, state schema or contract format
   needs a MAJOR bump.
5. **Human-only actions (review round 8, finding 1; Abhinav chose "Touch ID / password"):** approving a contract, deciding a
   disputed review, publishing and lifting the kill switch require macOS device-owner authentication (LocalAuthentication
   policy 2, run by `/usr/bin/osascript`). There is no flag, variable or setting that skips it; non-macOS systems refuse.
   Running never approves. CLI approval first prints the whole contract in the person's terminal, reads the digest prefix
   from `/dev/tty`, and binds the approval to that digest. Verified 20 November 2026: from Abhinav's own Terminal the dialog
   appeared and returned success; from an agent's shell macOS cancels it before it appears (LAError -4). Limit: it does not
   stop a process that can rewrite Brahma or drive the screen.
6. **Lifting the kill switch and pausing hold work** (findings 8, 16): held jobs run again only after `brahma resume`.

## Round 15 — 21 November 2026 (1.0.0 ship rule)

Abhinav, after Codex review round 15 (asked: "wait for zero findings, or ship with MEDIUM/LOW listed as known limits?";
answered "ok continue, ship 1.0.0", then "ok go ahead" to the stated interpretation). This amends Round 14, item 1.

1. **Gate:** HIGH findings from the independent Codex review still block 1.0.0 and are fixed and re-reviewed. MEDIUM and
   LOW findings open at release are listed in the CHANGELOG 1.0.0 "Known limits" with their scenario, and are fixed in
   later releases. The suite on the release commit, the installed-tool smoke test (doctor, usage, models, demo start,
   no model requests) and one real `brahma approve` by Abhinav still apply.
2. **Unchanged:** merging to master and tagging `v1.0.0` are Abhinav's explicit decisions (Round 2); nothing is merged
   or tagged without his instruction.

## Round 16 — 21 November 2026 (reviewer when Codex is out of usage)

Codex review round 17 stopped at its usage limit ("try again at Nov 25th, 2026 9:40 PM") without findings or a
verdict. Abhinav, asked how to proceed, answered: "You take charge of it, today is last day to finish work on brahma,
complete it". This amends Round 15, item 1, for 1.0.0 only.

1. **Substitute reviewer:** the changes Codex has not reviewed (`0901567..` the release commit) are reviewed by a fresh
   Claude review with no authoring context (`/code-review` at high effort), plus the automated security review. Its
   HIGH findings block 1.0.0 exactly as Codex's would; MEDIUM and LOW go to the CHANGELOG "Known limits".
2. **Codex still reviews:** when Codex usage resets, round 17 runs on the released commit. Any HIGH finding there is
   fixed in a 1.0.x patch release, not waved through.
3. **Unchanged:** the suite and smoke test on the release commit, one real `brahma approve` by Abhinav, and merge/tag
   only on Abhinav's explicit instruction.

## Rounds 17–20 — 21 November 2026 (1.1.0: conversation as chat, local times, Brahma's work reports)

Asked by Abhinav: "internally they communicate via json but when user see them in terminal it should show to user in
chat format - Example 3 person doing chat brahma, codex, claude. Display everywhere times and date in IST format or as
per system", and "add same reporting skill to brahma that currently claude and codex is follwing for reporting work".
Answers below are the options Abhinav chose, quoted by question.

**Round 17**
1. Q1 (where the chat appears): "CLI and TUI". One renderer gives `brahma run` output and the terminal UI the same chat.
   Stored events, `brahma events` and every `--json` output stay as they are.
2. Q2 (layout): "One shared timeline", e.g. `14:38 IST  Brahma → Claude: Task 1 is yours (critical, Opus 5.5).` An
   arrow names who a message is for; Codex and Claude still never message each other directly (Brahma relays a
   reviewer's findings in the builder's next brief).
3. Q3 (technical events): "Translate, hide repeats". Each known kind becomes one plain sentence; a quota event is shown
   only when it changes; usage goes to the status bar; raw JSON stays in `brahma events` and `--json`.
4. Q4 (times): "System timezone". Every displayed time uses the computer's timezone with its abbreviation (IST on
   Abhinav's Mac); the full date on the first message and when the day changes. Stored times stay UTC.

**Round 18**
1. Q1 (worker verbosity): "Progress summaries". Workers speak in their progress messages and results only.
2. Q2 (the owner in the chat): "Yes, as 'You'". Approval, notes, decisions and stop/pause appear as messages from You.
3. Q3 (release): "1.1.0 now; security 1.2.0". The chat view is 1.1.0; the planned security skills move to 1.2.0.
4. Q4 (builder): "Through Brahma". Claude Code prepares the contract; Abhinav approves it; Brahma runs it.

**Round 19**
1. Q1 (who reports in the six fields Completed, Evidence, Current work, Next work, Where we are, Needs you): "Brahma
   only". Workers reply as now.
2. Q2 (sharing the rule): "Repo skill, mirrored". One `work-report` skill, identical in `.claude/skills` and
   `.agents/skills`; Brahma's own reports follow the same rules.
3. Q3 (other skills for workers): "Only those the plan names". Unchanged: a task gets a skill only through its approved
   `allowed_skills`.
4. Q4 (when to run): "Approve after both reset". Observed on 21 November: Codex's weekly window 100% used until
   Wed 25 Nov 2026 21:40 IST; Claude's 95% used until Wed 25 Nov 2026 13:30 IST. The contract is approved after both.

**Round 20**
1. Q1 (workers and the skill): "Not given to workers". The skill serves Brahma's report rules and interactive sessions.
2. Q2 (when Brahma reports): "Each task end + run end": after a task ends ready, blocked, incomplete, waiting or
   needing a decision, and once when the run ends.
3. Q3 (where reports live): "Chat + stored + status". A Brahma chat message, a stored `work_report` event with the six
   fields as JSON, and an additive `report` field in `brahma --json status`.
4. Q4 (wording): "Generic, same rules". Same fields, order and rules, addressed to the owner, without a name or project.

## Round 21 — 21 November 2026 (is Brahma worth it: measure, benchmark, next features)

Abhinav asked Claude, as a worker of Brahma, about its benefits, whether it reduces or increases the owner's effort,
benchmarks, future value and features. Facts recorded at the time from Brahma's state: 20 sessions, none ran to ready
(8 planning only, 8 never approved, 3 incomplete, 3 blocked, 1 interrupted); 6 tasks reached ready; 23 of 24 reviews
requested changes; repository memory for learned routing held 0 items; no benchmark existed. Claude's view: the value
is cross-vendor review and unattended runs on subscriptions, but it was not yet measurably reducing effort; as a
builder in Brahma, Claude cannot run tests (Read, Glob, Grep, Edit, Write only), and medium/low findings also force
repairs. Codex was out of usage and was not asked.

1. Q1 (the deciding metric): "Your minutes per accepted change" — the owner's time (planning, approving, reading,
   deciding, fixing) per change that is merged.
2. Q2 (benchmark): "Fixed suite, A/B" — a fixed task suite in a sample repository, run through Brahma and through an
   interactive session, comparing owner minutes, wall time, quota and escaped defects, before each release.
3. Q3 (features after 1.1.0): "Claude builder runs checks", "Benchmark + effort log", "Calibrated review" (only high
   and medium findings force a repair; low findings become notes). Not chosen: notifications.
4. Q4 (Codex's view): "Yes, after its reset" — the same questions go to Codex, read-only, after Wed 25 Nov 2026 21:40
   IST; both answers are recorded side by side.

## Round 22 — 21 November 2026 (order and shape of the next features)

1. Q1 (order): "Measure first". 1.2.0 benchmark and effort log (a baseline measured on 1.1.0); 1.3.0 the Claude builder
   runs checks; 1.4.0 calibrated review; the security skills follow. **This amends Round 18, item 3**, which had put the
   security skills in 1.2.0.
2. Q2 (how the Claude builder runs checks): "Approved checks only". Brahma gives the builder one tool that runs exactly
   the contract's approved check commands in the worktree and returns the result; no general shell.
3. Q3 (measuring the owner's minutes): "Automatic + your estimate". Brahma logs the owner's touch points automatically
   (approve, decide, notes, resume, time the UI is focused on a session) and asks one optional question at merge for
   minutes spent outside Brahma; observed and estimated minutes stay separate.

## Round 23 — 22 November 2026 (the 1.2.0 benchmark)

1. Q1 (repository): "Purpose-built fixture". A small Python repository kept inside Brahma, created as its own Git
   repository from a fixed template before each run, with seeded tasks; no private data.
2. Q2 (suite): "6 mixed tasks": 2 bug fixes, 1 test addition, 1 refactor, 1 docs change, 1 small feature. Actual usage
   per run is unknown until measured.
3. Q3 (accepted): "Hidden tests + your review". Each task has acceptance tests the workers never see (kept in Brahma's
   `bench/`, outside the fixture repository given to workers), run after the change, plus the owner's brief review
   deciding merge; that review time counts in the metric.
4. Q4 (interactive arm): "brahma bench timer". The owner runs `brahma bench start TASK` / `stop` while driving an
   interactive session; Brahma records the minutes and outcome the same way as for its own arm.

The 1.2.0 contract is drafted after 1.1.0 merges, because it builds on 1.1.0's chat and report modules.
Codex's questions for Round 21, item 4 are in `docs/plans/round-21-codex-questions.md`.

## Rounds 24–25 — 24 November 2026 (two independent sessions of one provider when the other is unavailable)

Asked by Abhinav: "When claude / Codex not available then start two different independent session from claude if codex
is not available or codex if claude is not avialable in brahma". Until now the reviewer is always the other provider
and a task waits when it has no capacity (README: "The other provider reviews the actual changes"); Round 16 allowed a
fresh Claude review in Codex's place for 1.0.0 only. A task's provider and model are part of the approved contract, so
the fallback must be approved too.

**Round 24**
1. Q1 (authority): "brahma.toml default". A repository setting turns the fallback on; every contract shows it on the
   approval screen, so the owner approves it with the contract. Without the setting, tasks wait as before.
2. Q2 (roles): "Reviewer and pinned builder". If one provider is unavailable, the other builds a task pinned to the
   unavailable one, and a separate session of the same provider reviews it.
3. Q3 (independence): "Fresh session, other model". A new session with no shared history, briefed only with the
   contract, the diff and Brahma's own check output, using a different model of that provider.
4. Q4 (switching back): "Next task returns". The task in progress finishes with its fallback pair; the next task uses
   cross-vendor review again.

**Round 25**
1. Q1 (trust): "Mark it, publish as usual". The report, chat and readiness receipt state that an independent session
   of the same provider reviewed it, and which provider was unavailable; publication follows the contract.
2. Q2 (unavailable): "Quota, sign-in or install". Exhausted quota (waiting or blocked), signed out, or not installed. A
   transient "overloaded" reply keeps the existing retries first.
3. Q3 (model): "The other tier". The second session uses the provider's other configured tier (critical reviews
   routine and the reverse); if both tiers name the same model, it is used and the report says so.
4. Q4 (release): "1.2.0, before benchmark". **This amends Round 22, item 1:** 1.2.0 same-provider fallback; 1.3.0
   benchmark and effort log (Round 23); 1.4.0 the Claude builder runs approved checks; 1.5.0 calibrated review; then
   the security skills. The benchmark records same-provider and cross-vendor reviews separately.
   The README's "the other provider reviews" promise is updated with the fallback.
