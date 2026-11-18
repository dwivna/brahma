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
