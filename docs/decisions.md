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
