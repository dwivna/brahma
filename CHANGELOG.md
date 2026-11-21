# Changelog

Brahma uses SemVer from 1.0.0 (decision Round 14; 0.x before): MAJOR for any breaking change to commands, JSON output,
exit codes, state schema or contract format; MINOR for new capabilities and backward-compatible additions; PATCH for
fixes. Each merged PR bumps the version; a tag is a separate release decided by Abhinav. See `docs/decisions.md`.

## 1.0.0

First release (docs/decisions.md, Round 14). Includes the fixes from independent review round 8 and the automated
security reviews that followed.

**Changed (breaking against 0.13.0)**
- **Approval is human-only.** `brahma approve SESSION [--queue]`, `run PLAN --approve` and `/approve` require Touch ID or the
  login password (macOS LocalAuthentication). The CLI shows the whole contract first, reads the digest prefix from the
  terminal and binds the approval to it. `run --session` runs an approved session and never approves. `decide`,
  `/publish` and `kill --release` need the same confirmation. Other systems refuse these actions.
- **JSON:** every command, with the global `--json` or its own, returns one object with `ok` and `exit_code`; `models`
  and `usage` reports are nested under their name. Exit codes are locked; `needs_checks` exits 12.
- **Kill switch and pause hold work:** lifting the switch, or pausing a running session, leaves its jobs `held` until
  `brahma resume`; `resume` is refused while the switch is engaged.

**Fixed**
- Dispute decisions are bound to the exact findings and worktree state they were made on.
- Ownership is verified against the Git identity taken at run start (branch, HEAD, Git directory); a worker commit, a
  branch switch, hidden index flags or repository fsmonitor config cannot hide a change. Every worker exit is verified.
- Check output is redacted before the reviewer sees it; nested `.env` files in ignored caches are detected.
- Publication pushes only the commit recorded when the run became ready, from a clean worktree.
- Worker and check process groups left by a dead coordinator are stopped before new work starts.
- The state directory and database are private (0700/0600, symlinks refused); the schema migration is atomic.
- The approval screen and board show the real locked settings and budget; a planner run halts on an external kill or stop.
- The README no longer claims writes are confined to the worktree.
- Independent review round 9: Brahma's Git calls (and the implicit `git diff --check`) take no Git configuration or
  repository location from the environment and never run a repository fsmonitor helper; a background process left by
  a passing check is ended; a worker that raises is still ownership-verified and restored; a malformed provider event
  is a worker error, not a crash; held work does not run from `run --session` until `resume`; a pause followed by a
  quick resume keeps the work queued; automatic publication works and pushes the verified commit; the approval display
  shows the base revision, the Gherkin feature, loaded definitions and instructions; command output is redacted; the
  Codex builder banner no longer claims writes are confined.
- `brahma approve` failed at the digest prompt with "File or stream is not seekable": the terminal was opened as a
  read-write text stream, which requires seeking. It is now read as unbuffered bytes; a test drives a real pseudo-terminal.
- Review of the round-16 fixes (decision Round 16, while Codex was out of usage): draft PR creation no longer fails
  when `gh` takes longer than a quarter second (its body was re-sent to stdin, which Python refuses); a baseline saved
  with a redacted path name never deletes a file whose name it may stand for on resume; an owned output Brahma cannot verify (unreadable, special,
  still being written) blocks readiness but is never deleted; automatic publication runs off the event loop, so Stop and
  Kill in the terminal UI take effect during a push; publication reads only durable controls, so a lifted kill switch
  or a resumed pause no longer refuses `/publish`; a publication waiting for another one of the same worktree stops on
  a stop (and gives up after five minutes); quitting the terminal UI or stopping the service ends a running
  publication (once the closing task is cancelled); the run stays ready with its verified commit and says to use
  `/publish`; a Stop or Pause during publication is named as such; every error after the push says the commit was
  pushed; an owned output missing for a moment is kept; an unusable state directory or lock refuses publication instead of crashing the run; the state database is
  opened by a properly escaped URI; the test suite never touches the real Brahma state directory.
- Independent review round 16: publication pushes the verified commit itself (`<sha>:refs/heads/brahma/...`), is
  serialized per worktree, and checks the kill switch, stop, pause and cancellation before every external write and
  while the push or PR creation runs (which are ended at once); fingerprint-map path keys are redacted like any text;
  `.env*` special files inside ignored caches are found; an owned file Brahma cannot read is not accepted.
- Independent review round 15: `/publish` reads session state on the event loop and publishes in a worker thread;
  an approved plan may edit or delete the instruction files it owns without invalidating its own contract (others must
  still match); the ignored-file scan exempts cache-named directories only when Git ignores them (one rule with the
  special-file walk); unreadable, changing and special files always count as changed and are never accepted, and a
  readiness tree holding one never matches; proposals with open questions are checked for credentials before they are
  saved, exported or reused; Brahma's Git calls ignore replacement objects (grafts are refused), never sign commits and
  never start background gc or maintenance.
- The background worker loads all of Brahma at start and restarts itself (through the service's KeepAlive) when the
  installed code changes, between jobs. Found in real use: an approved run resumed by a worker started before an
  upgrade failed with an ImportError, because one new module was loaded beside old ones already in memory.
- Independent review round 14: `$GIT_DIR/info/attributes` (read by Git whatever `--attr-source` says) must be empty,
  or the repository is refused at approval and every Brahma Git call stops; system attributes are off; tracked symlinks
  are verified as symlinks with their committed target (`core.symlinks=true` pinned); an allowlisted cache name is
  skipped by the special-file walk only when Git ignores it; the hash cache is evicted before it is consulted; `/publish`
  passes the verified ignored-file snapshot (and the receipt records its allowlist); follow-on plans validate context and
  verify their approval against the worktree's instructions (contract format 4 records where instructions are read);
  resume verification time is deducted once from the task's remaining budget whatever happens next; a JVM startup
  variable that sets a Gradle property marks the Gradle JDK unbound.
- Independent review round 13: ownership compares raw file bytes and executable bits (line-ending conversion and
  `core.fileMode=false` no longer hide a change); Brahma's Git calls read attributes from the empty tree, so no filter
  driver program can run during inspection, restoration or commit, whatever `.gitattributes` a worker writes; FIFOs,
  sockets and device nodes, which Git never lists, are found by a filesystem walk and treated as violations; ownership is
  verified after checks whatever their outcome, before a repair builder can start; stored usage keeps only counters;
  publication compares ignored and special files with the ready receipt; resume verification is charged to the remaining
  approved budget; file-fingerprint maps keep their path keys; follow-on plans fingerprint instructions from the
  worktree they run in; hashing dispatch is linear.
- Independent review round 12: a dispute decision is bound to its plan generation and to the escalation it answers;
  credential-field redaction matches whole name segments (file paths used as keys keep their fingerprints, `token` numbers
  are hidden, usage counters kept); resume checks verify and restore ownership on every exit, including failure and
  exceptions; ownership is verified between overload retries inside one provider call, and every retry's usage is kept;
  a review with more findings than a dispute handles is repaired whole, never truncated; Brahma compares tracked file
  contents itself rather than trusting Git's whole-second stat cache, and pins Git's strict change detection; repositories
  with submodules or configured filter drivers are refused at approval and before checkout, and a commit must store
  exactly the checked bytes; a changed directory (nested repository) is never accepted; rollback refuses to act through
  moved Git metadata, and removes special files before reporting them restored; provider and `gh` programs are found on
  absolute PATH entries only and launched by that absolute path; `brahma events` prints every stored event; instruction,
  configuration and contract files are read without ever blocking on a FIFO.
- Independent review round 11: a value stored under a credential-named field (`password`, `api_key`, ...) is hidden at
  any depth, while provider token counters are kept; every string in a plan, including its Gherkin feature, is checked
  for credentials before approval; every approved instruction file must be present in the run's checkout (one that is
  only untracked in the source refuses the run instead of silently dropping its rules), and a bare owned directory brings
  its own instruction files; on resume, checks that change reviewed files accept nothing (the affected tasks are checked
  and reviewed again on the next run; a check that rewrites owned files differently on every run therefore cannot reach
  readiness on resume), and ownership failures found on resume are restored; the board, notes and dispute decisions read
  the newest plan however long the history; each provider invocation's usage is counted once, never merged with an equal
  one; every file is content-hashed for ownership and readiness (size and modification time no longer stand in for files
  over 64 MiB); the terminal UI redacts everything it renders; ownership is verified and restored after every worker
  exit (the first builder before a context-expansion builder, the reviewer, and the dispute reply and re-review); special
  files (FIFOs, devices) are never opened blocking; the background service passes its state directory to everything
  that checks the kill switch.
- Independent review round 10: every Git query Brahma makes ignores a redirected repository, index or configuration
  from the environment; secrets nested anywhere in stored events (including dispute reviews) and in `brahma events`
  output are redacted; repository instructions and selected definitions are redacted before they reach a worker, and a
  plan or check containing a credential-shaped value is refused before approval; workers receive repository instructions
  only if they match the approved fingerprints (an instruction file the plan owns may change within that plan, marked);
  the terminal UI shows the same complete contract view as the CLI (including worker instructions and models) and
  approves only the digest it displayed; readiness, the local commit and publication are bound to the exact files the
  checks and review covered (files changed during review are checked and reviewed again); `/kill` stops orphaned
  worker groups like `brahma kill`; a repeated stop during planning is seen; an unowned edit that a worker also
  committed is restored from the run's starting revision; sensitive names are matched case-insensitively (`.ENV`);
  `worker` and `statusline` refuse `--json` with a JSON error; a scheduled run paused and quickly resumed keeps its
  continuation; a Claude limit without a reset time waits for a newer observation or capacity confirmation; the live
  board's active time advances between checkpoints and shows only the current plan; measured time and usage per task
  include earlier continuations.

**Known limits shipped with 1.0.0 (not hidden by the version number)**
- Toolchain binding covers the check program, its JDK and the startup files and variables listed under 0.13.0. Other
  environment inputs that Maven or Gradle read outside the checkout are not all bound. The round-7 findings of this kind
  were fixed ("disable rather than enumerate", Gradle fail-closed) and independent review round 10 confirmed them closed
  within that stated coverage. Worker edits to files it does not own are restored and block readiness regardless.
- Hashing and executing a program are two steps, not atomic. Fingerprints cover the launched file, not everything it loads.
  Checks are not sandboxed.
- Human-only actions need macOS Touch ID or the login password; they protect Brahma's own commands. A process running as
  the same user can still write Brahma's state database (including an approval) or edit Brahma itself.
- Independent review: Codex reviewed up to `0901567` (round 16). Its round 17 could not run (usage limit until
  25 November 2026), so the later changes were reviewed by fresh Claude reviews (`/code-review`, high), each set of
  fixes reviewed again; those reviews are not Codex reviews. Codex reviews the released commit when it can; a HIGH
  finding there is fixed in a 1.0.x patch (decision Round 16).
- Open findings from that review (MEDIUM/LOW): the sensitive-file scan walks every ignored cache (`node_modules`,
  `.venv`, ...) on each ownership check, which is slow in a very large cache; repositories using SHA-256 object format
  cannot be published (commit ids must be 40-hex; SHA-256 repositories are unsupported in general, see README); a file
  whose *name* looks like a credential is stored redacted, so readiness and publication fail closed while it exists;
  a changed file is hashed twice per ownership check; during publication every quarter-second stop check opens the
  state database afresh, so a write-locked database delays a stop by up to 30 seconds.
- Not implemented: signed-policy approval (Round 12 design only), read-side secret enforcement, and confinement of the
  provider CLIs.

## 0.13.0

**Added**
- **Toolchain binding.** The approved contract now records, for every configured check program, where it is invoked from, the
  regular file it resolves to and that file's SHA-256; for Java-family programs (`mvn`, `gradle`, `java`, ...) also the JDK's `java` binary hash and the
  version from its `release` file, plus `JAVA_HOME`/`MAVEN_HOME`/`M2_HOME`/`GRADLE_HOME`. **Nothing is executed to learn this**
  (probing by running `--version` would run unapproved programs at plan time and `mvn --version` reads repository
  `.mvn/jvm.config`). Execution refuses to start if anything moved, naming what changed.
- **Verified again before every check, where the check runs.** The fingerprint is re-taken against the worktree immediately before
  each check and the verified absolute path is what executes, so a binary swapped after approval, or a program inside the worktree
  a worker edited (`./mvnw`), is refused before it runs. Relative and empty `PATH` entries are no longer searched (a worker-created
  `.venv/bin` shim cannot take over a command).
- Launchers that delegate to another runtime (`uv`, `npx`, `sh`, `docker`, `make`, ...) are flagged on the approval screen: their
  delegated runtime is not bound. Fingerprints cover the launched file, not everything it loads (Maven's jars, Python packages).
- **Build tools** (`mvn`, `gradle`, `ant`, `sbt`, and their wrappers) also bind the startup files they read first (`.mvn/jvm.config`,
  `.mvn/maven.config`, `.mvn/extensions.xml`, the Maven and Gradle wrapper files, `gradle.properties`, `.sdkmanrc`) found in a bounded
  walk of the checkout, so `mvn -f service/pom.xml` is covered. Symlinked files and symlinked `.mvn`/`gradle` directories are bound
  through the content they resolve to and their link target; creating or retargeting one is a change. Plain `java` is not bound to
  them. Not walked: pruned directories (`target`, `build`, `node_modules`, `.venv`, ...). This is defence in depth: a worker's edit to
  any file it does not own is already restored and blocks readiness through ownership verification.
- The implicit `git diff --check` that runs before every check is bound and verified like any other program (every contract binds `git`).
- JVM and build-tool startup variables are bound by value: `JAVA_TOOL_OPTIONS`, `_JAVA_OPTIONS`, `JDK_JAVA_OPTIONS`, `JAVA_OPTS`,
  `MAVEN_OPTS`, `MAVEN_ARGS`, `GRADLE_OPTS` (a `-javaagent:` in any of them changes what runs, and they survive the environment scrub).
  A secret-shaped value is stored as a digest, never as text. Symlinked directories are followed during the startup-file walk (cycle
  safe, bounded). A Gradle project whose `gradle.properties` sets `org.gradle.java.home` is reported as having an unbound JDK.
- Startup files outside the checkout, which ownership verification cannot see, are bound too: Maven's `/etc/mavenrc`,
  `/usr/local/etc/mavenrc` and `~/.mavenrc` (shell code that can override `JAVA_HOME`) and Gradle's user-level `gradle.properties`
  (`$GRADLE_USER_HOME` or `~/.gradle`). An absent file is recorded as absent, so creating one is a change. A present `mavenrc`, or any
  `org.gradle.java.home`, marks the JDK as not bound. The walk of the checkout follows symlink aliases by their own logical names and
  skips only cycles back to an ancestor.
- Inputs that redirect Maven's startup are disabled for checks instead of enumerated: `MAVEN_SKIP_RC=1` is set (no `mavenrc` is read),
  and `MAVEN_BASEDIR` and `MAVEN_DEBUG_OPTS` are removed. Gradle is reported as having an unbound JDK whenever its command line
  (any option that takes a value: `-D`, `-P`, `-g`, `-p`, `-c`, `-I`, `--init-script`, ...), a relative `GRADLE_USER_HOME`, or any
  `gradle.properties` that mentions `java.home`, contains a backslash or is larger than 64 KB could redirect it; a redirect added after
  approval is refused.
- A `java` that is not inside a real JDK (the macOS `/usr/bin/java` stub, a version-manager shim that reads `.java-version`) is a
  selector and is always reported as not bound. A Java build tool run without `JAVA_HOME` chooses its own
  JDK, which Brahma cannot know without running it, so the approval says the JDK is not bound; set `JAVA_HOME` to bind it.
- A toolchain change found after approval stops the run (`blocked`, exit 13) and spends neither an attempt nor active time: the task is
  checkpointed as inactive `waiting` at the same attempt in the check phase, so restoring the environment and resuming re-runs the checks.
- The programs are executed by their invocation path, never the resolved symlink target, so virtualenv and version-manager shims keep
  working; `..` in a path is never collapsed. A follow-on plan is fingerprinted in the existing worktree only if `git status` is clean;
  a dirty worktree falls back to the repository. Git-ignored files are not seen by that status, but are verified before every check
  and any difference is refused before the check runs.
- Stated limit: hashing and executing are two steps, not one atomic step; a process a worker leaves running could change a file in
  between. Checks are not sandboxed and this is accepted for 1.0.0.
- The approval screen shows each program's path and short hash, and the JDK.

**Fixed**
- README no longer says secret redaction is not implemented; it now states the actual coverage and its limit.

**Changed**
- Contract format is now 3. Contracts approved under format 2 are not honoured; approve a new plan.

## 0.12.3

**Fixed** (found by live run 5)
- A reviewer's `pass` that carried only low or medium severity notes was rejected as an invalid review, so a good review forced
  repair after repair until the attempts ran out. A pass may now carry rated low or medium notes, which are recorded (sanitized) in a
  `review_notes` event for Abhinav. A pass that carries a high finding or an unrated issue is still invalid and is repaired.

## 0.12.2

**Fixed** (found by the second live run)
- The launchd service did not inherit `JAVA_HOME`, so a run resumed by the service after a quota wait used a different default JDK
  than the terminal that approved it. A Maven project using Lombok then failed to compile (`cannot find symbol: log`) and the
  failure consumed all three repair attempts. `brahma service install` now records `JAVA_HOME` (only when it is an absolute, existing
  directory; no credentials). Reinstall the service to pick it up.

**Known, not changed**
- The approved contract does not bind the toolchain. A check that fails only because the service environment differs from the terminal
  still counts as a failed check and uses a repair attempt.

## 0.12.1

**Fixed** (found by the first real run against a live repository)
- `brahma run --approve` blocked every run immediately with "Event is bound to a different event loop": model discovery
  and execution used two `asyncio.run` calls, and the session's cancel Event bound to the first loop. They now share one loop.
  A regression test fails on the old code with that exact error.

## 0.12.0

Fixes from the second independent review (providers, quota, contracts, scheduler, config, terminal).

**Fixed**
- Medium: builder workers inherited credentials the checks already stripped (`PGPASSWORD`, `*_SECRET_KEY`, `*_PASSWD`, `*_PAT`,
  `SSH_AUTH_SOCK`, `AWS_PROFILE`, ...). Workers and checks now share one scrub (`gittools.scrub_secrets`). Still a denylist; credential
  files under HOME stay readable, and the docstring now says so.
- Medium: the approval screen did not show locked settings that widen a run. It now lists `publish`, `auto_commit`,
  `strict_enforcement` and a non-default `ignored_allowlist`, warns when an approved check runs a shell or network program (`sh`,
  `bash`, `curl`, `ssh`, ...), and the status line shows the configured time budget and repair count instead of fixed text.
- The Claude status-line tap writes at most once per refresh interval whatever the values, ignores payloads missing either window
  (they read as "available" with the other window unknown) and ignores a reset more than 8 days away (a forged reset could park the
  provider for years).
- Model names from `brahma.toml` must be 1 to 100 characters of letters, digits and `. _ : / [ ] -` with no leading dash.
- The capacity-error pattern no longer matches a stray "529" in prose.

**Known, not changed**
- Approved checks still run unsandboxed on worker-written code (documented). A crashed Brahma can leave an orphaned worker that a
  restarted job might overlap. The contract hashes `AGENTS.md` and `CLAUDE.md` from the main checkout rather than the base
  revision. An unrecognised provider quota value raises rather than degrading (fails closed). A PID-reuse window exists in worker
  cleanup. Relative `PATH` entries are not rejected. Whether the provider CLIs confine writes to the worktree, resume keeps its
  sandbox flags, or a repo-level Codex config loads was not verified.

## 0.11.1

Closes the last open part of plan task 3: pause, stop and kill during checks and publication.

**Added (tests only; the guards shipped in 0.11.0)**
- A kill, stop or pause that arrives after the engine reports ready prevents both the local commit and the publication, and
  the session ends `cancelled`. Without the 0.11.0 guard these tests fail.
- A stop during a running check cancels the check and the session.

## 0.11.0

Fixes from the independent review of 0.3.0 to 0.10.0, plus the confirmed exit-code table.

**Confirmed**
- The exit-code table (0 ready, 10 waiting, 11 needs a human decision, 12 needs checks, 13 blocked, 14 cancelled/paused/killed,
  20 usage error, 30 provider missing or not signed in) was confirmed by Abhinav on 2026-11-19.

**Fixed**
- High: `task_state` rows stored check output and reviewer text unredacted (and fed them into the next builder prompt). Their
  `summary` and `feedback` fields are now redacted at the store and at the source.
- A `blocked` dispute re-review that carried a high issue escalated and could be overruled. It now blocks the task.
- Dispute decisions are bound to the exact escalated positions (digest), an unconsumed decision is cleared when the fresh review
  has no high finding, and attention raised by an earlier plan is resolved when a new plan is made.
- Withdrawing or overruling the high findings no longer drops the medium and low ones; they are still repaired.
- The kill switch now does what the docs say: coordinator Git writes are refused while it is engaged (unlisted commands count
  as writes), commit and publish re-check it, `/publish` checks it, and the planner will not start under it.
- Notes: counted as applied only after a builder received them, re-read at each attempt, and only notes left after the current plan
  apply. Board signals from earlier plans no longer notify when an old session is opened.
- The spent author reply is persisted immediately. `git` global options (`-C`, `-c`) can no longer hide a blocked command.
  Redaction now covers `sk_live_`, JWTs, `AIza`, `glpat-`, `npm_` and `hf_` tokens, and PR text is redacted before it goes to GitHub.

**Known, not changed**
- Packets containing a secret-shaped string silently fall back to a task-only brief; packet files are read-only only
  nominally for the same user; `max_tokens=100000`-style names are over-redacted; concurrent first-run table creation by the
  status-line tap and the UI is unverified.

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

**Fixed (automated review of this branch)**
- Terminal Resume refuses to run under the kill switch or on a stopped session, and does not clear the cancel signal while a worker is still winding down.

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
