# Model selection and usage inspection

`brahma models` and `/models` inspect the installed Codex and Claude model catalogs
without generating a model response. Add `codex` or `claude` to inspect one provider.
`brahma --repo /path/to/repo models --json` includes that repository's configured
profiles and structured discovery evidence. These commands do not approve work.

During planning, Brahma checks both native catalogs and matches the routine and
critical preferences configured in `brahma.toml`:

```toml
[brahma]
codex_model = "gpt-6.2-sol"
codex_critical_model = "gpt-6.2-astra"
claude_model = "sonnet"
claude_critical_model = "opus"
```

The planner assigns critical work to architecture, security, authentication,
migrations, concurrency and difficult faults. Bounded, well-understood changes
use the routine profile. Builders and independent reviewers both use the task's
complexity profile. Existing provider quota checks take precedence over provider
preference; pinned provider assignments stay pinned.

The approval contract contains the configured preferences, selected models,
discovery source and reasons. Its display shows both providers' routine and
critical selections. Worker panes identify the model used for each phase. Resume
uses the locked choices; changed configuration requires a new plan and approval.
Verified Claude aliases are pinned to the concrete model ID returned by the
catalog. Unverified aliases remain labelled as such when the catalog or its
concrete model identifiers are unavailable.
Discovery does not change an existing approval or run a model merely to test access.

When a configured version is absent, Brahma first looks for an advertised model
in the same supported family: Sol stays Sol, Astra stays Astra, and Claude
Sonnet/Opus/Haiku each stay in their own family. The native default is preferred
within that family, then version ordering supplies a deterministic choice; this
is a rule, not a benchmark result. A missing routine family can use its configured
critical profile if that is advertised. Critical work cannot silently downgrade
to a routine profile. If no permitted profile is advertised, the affected task blocks
before generation. If discovery is unavailable, a supported configured preference
is explicitly **unverified**; existing quota rules still permit only a bounded
approved attempt. Native rejection checkpoints the work.

The supported automatic profile families are Codex Sol/Astra and Claude
Sonnet/Opus/Haiku. Newly advertised families, `default` aliases, credit models and
unknown model families are not automatically selected. A catalog establishes
advertised CLI support, not remaining quota or included subscription billing.
Claude's current model documentation describes credit billing for some models;
headless calls can charge those credits without an interactive consent screen.
See the [official Claude model documentation](https://code.claude.com/docs/en/model-config#fable-and-usage-credits).

The catalog adapters use Codex `model/list` and Claude's initialize control response;
neither sends a user message, thread/turn request or generation request. Account
identity is discarded. Native calls have deadlines, bounded output and cancellation.
See the [Codex protocol](https://learn.chatgpt.com/docs/app-server#list-models-modellist)
and [Claude initialize response](https://code.claude.com/docs/en/agent-sdk/typescript#sdkcontrolinitializeresponse).

These rules select a suitable approved profile, not a measured universal best
model. No benchmark superiority, learned preference or cost savings is claimed.
The broader outcome-based routing required in decision Round 4 remains a separate
workstream.

Use `brahma usage [codex|claude]` or `/usage [codex|claude]` for subscription usage.
The terminal also recognizes simple account-usage questions. It reports actual
window durations, percentages and reset times only when exposed by the provider.
Codex has a native quota read; Claude currently uses saved stream/status-line
observations. Cached and stale values are labelled last reported; current missing
values stay unknown. Request token counters do not establish remaining allowance.
Inspection never creates tasks, approvals or a worker queue.

The conversation shows one readable plan. Planner JSON, generation IDs and native
session IDs stay in the local event record instead of being repeated in the UI.

## Verification — 19 November 2026

The full offline suite passed: **196 tests in 30.449 seconds**, using
`uv run python -m unittest discover -s tests -v`. It includes the screenshot's
two messages, plan/approval preservation, active-worker inspection, cache and
cancellation behavior, bounded native protocol transcripts, model family selection,
critical-model blocking, resolved IDs across restart, and configuration drift.
An independent review found and verified fixes for mixed-intent interception,
current-directory settings and alias drift; no material findings remained.

Live read-only verification of both installed CLI catalogs also passed. This
performed native initialization and model listing only, with no generated model
response. It caught configured GPT-6.2 preferences missing from the advertised
catalog and verified selection of the available Sol/Astra family versions. No
account identity, credentials, quota amounts or raw native transcripts are recorded
here. Claude's concurrently active checkout was left unchanged.

### Re-review before merge — 19 November 2026

Claude's follow-up commit `b223748` fixes the planner's sentence ordering and
adds a regression test. Independent re-review then found two further defects:
missing Claude concrete IDs were labelled verified, and a local usage reply could
skip pending scheduler events in the terminal. Both new regressions failed on
that commit and pass with the fixes: unresolved aliases remain unverified, and
local callbacks drain unseen stored events in order before advancing the cursor.

The full offline suite now passes **200 tests in 31.263 seconds**. Both reviewers
rechecked their findings and reported no remaining material defects. No live
model calls or user-state inspection were needed for this re-review.
