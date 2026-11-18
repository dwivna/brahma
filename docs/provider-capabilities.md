# Provider controls — evidence and implementation boundary

Investigated 18 November 2026. These observations describe provider interfaces;
they do not assert the prototype has implemented them. Local CLI versions can
change: the latest read-only help probe returned Codex 0.157.1 and Claude Code
2.1.327. Earlier build probes had returned Codex 0.170.0 at another executable.
Capability detection must use the executable actually selected at run time.

## Startup readiness

Implemented: read-only CLI discovery and subscription sign-in checks before each
provider request. Missing executables request installation using official docs;
signed-out executables request `codex login` or `claude auth login`. Unrecognized
or failed status checks remain unverified and block dispatch. No automatic
installation, login, credentials export or paid API fallback occurs.

Startup presentation and retries are being added to the terminal. Authentication
readiness is separate from quota availability. A valid subscription login does
not prove remaining allowance, connectivity, model entitlement or that account
usage credits are disabled. Keychain access can also differ inside a sandbox.

## Quota observations and resets

| Provider | Supported signal | Boundary |
| --- | --- | --- |
| Codex | App-server `account/rateLimits/read` and `account/rateLimits/updated`; `usedPercent`, `windowDurationMins`, `resetsAt`; optional additional buckets. | Documented, not runtime-probed in this investigation. Read each returned duration rather than assuming all buckets are five-hour or weekly. Missing values remain unknown. |
| Claude Code | Stream `rate_limit_event` with status and optional `resetsAt`, utilization and overage metadata. | Event observations do not supply a complete independent quota polling endpoint. |
| Claude Code status line | `rate_limits.five_hour` and `seven_day` with percentages and reset epochs. | Available in supported sessions after the first response; reliable collection in headless `-p` runs has not been established. |

Sources: [Codex app-server](https://developers.openai.com/codex/app-server),
[Claude SDK reference](https://code.claude.com/docs/en/agent-sdk/typescript),
[Claude status-line rate limits](https://code.claude.com/docs/en/statusline#rate-limit-usage).

Claude's built-in automatic wait applies to interactive sessions, with documented
exceptions for background and `-p` runs, resets more than 24 hours away, and after
three automatic continuations. It does not implement Brahma's requested durable
headless scheduler. [Native wait behavior](https://code.claude.com/docs/en/interactive-mode#wait-for-a-usage-limit-to-reset).

Required scheduler behavior, pending implementation and the open Round 3 answers:

- Persist timestamped observations, quota bucket identity, reset times, affected
  task, native session ID, approval identity and the next eligible action.
- Display five-hour and weekly windows only when observed, with source and age;
  do not convert token counts into subscription capacity or report unknown as zero.
- Route within approved ownership, model requirements and review gates. Switching
  vendors never substitutes a builder's self-review for independent review.
- Checkpoint on a reported limit. At its reported reset, refresh supported status
  before continuing. A reset timestamp is an opportunity to recheck, not proof of
  capacity. Do not spin model requests to poll a limit.
- Keep cancellation durable: a stopped task must not restart on a reset. Preserve
  repair counts, approved scope and the chosen time-budget policy across restart.
- Never purchase usage credits, consume earned-reset credits, change account
  limits, switch accounts to evade limits or claim control of provider resets.

Subscription accounts can have extra usage credits enabled. Authentication alone
does not establish zero additional spending. Brahma must not label that state
verified-free or silently authorize paid continuation.
[Claude usage credits](https://code.claude.com/docs/en/costs#add-usage-credits-to-your-subscription).

## Native sessions, agents and skills

Codex documents `thread/resume`, `turn/interrupt`, and `skills/list` scoped by
working directories. Claude documents native session resume, SDK `interrupt()`,
`supportedAgents()` and `supportedCommands()`. Interrupt ends an active turn;
resume starts another using retained context. OS suspension does not reliably
stop remote generation or quota use.

User/project definitions can be discovered from documented directories, but
discovery, native loading and permission to execute are distinct states. The
prototype's restricted provider commands currently suppress customizations;
discovery must not be presented as enabled execution. Claude `agents --json`
lists session/background-agent metadata rather than custom role definitions.
Only explicitly selected external sessions should be imported; arbitrary desktop
or cloud sessions cannot be claimed controllable without a verified native path.

Sources: [Codex app-server](https://developers.openai.com/codex/app-server),
[Codex custom agents](https://developers.openai.com/codex/subagents#custom-agents),
[Claude agents](https://code.claude.com/docs/en/sub-agents#choose-the-subagent-scope),
[Claude skills](https://code.claude.com/docs/en/skills#choose-where-skills-load),
[Claude headless session controls](https://code.claude.com/docs/en/headless#stop-a-run-with-sigterm).

This investigation used local version/help commands and official documentation.
It did not inspect credentials, personal configuration values or transcripts,
query live quotas, issue model requests, change accounts or control Anvi.
