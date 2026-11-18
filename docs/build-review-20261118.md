# Build and review evidence — 18 November 2026

The user-selected clone is the active project. Work is on
`dev/brahma-prototype`; Anvi's school worktree remains clean.

Claude Code implemented the scheduler/CLI and their offline tests. Codex
integrated contracts, active-budget checkpoints, native worker continuation and
terminal controls. The initial Claude launch found an invalid empty MCP config;
using `{"mcpServers":{}}` fixed it and the argument shape is regression-tested.

Claude's independent read-only review raised seven findings. Source-independent
resume, explicit provider matching for definitions, textual limits overriding
stale availability, planner capacity gates, check display, preapproval context
validation and shutdown requeue behavior were addressed. Follow-up review of a
compact snippet packet also led to atomic plan validation and a stored-state
shutdown guard. It did not constitute a complete second review of all code.

Some follow-up suggestions were rejected with evidence: builders are forbidden
to commit and an unexpected worktree HEAD should invalidate authority; the
contract resolves a missing base argument to a concrete Git revision;
`Session.execute` explicitly restores its catalog/shared-feature context; quota
events are persisted by `Session.emit`; waiting is a serialized result rather
than a sleeping model task. The integration regressions exercise these paths.
No unconditional independent release approval is claimed.

## Measured verification

- Complete local suite: **137 tests passed in 26.232 seconds** on Python 3.14.
- Weekly-reset simulation resumes the review phase without rebuilding, preserving
  active time and repair allowance despite a source commit and tracked user edits.
- Unknown-reset rejection stays blocked without repeated model probing; explicit
  user capacity confirmation queues one continuation and cannot undo Stop.
- Contract/check/definition changes invalidate approval; required oversized
  context fails before approval without replacing the prior plan.
- Both installed CLIs passed subscription sign-in checks outside the sandbox.
- Codex native quota read returned five-hour and weekly windows without a model
  turn. Account percentages and raw transcripts are deliberately not published.
- The real Claude worker completed its assigned files, and a separate read-only
  review returned findings. Claude did not execute tests; Codex ran them.
- Editable `brahma` installation reports version 0.1.0. Its real terminal demo
  started and exited successfully without provider requests.
- The macOS service was registered with an empty approved queue.

## Practical limits

Native continuation covers Brahma-managed workers. Arbitrary existing desktop,
cloud or external terminal sessions are not controllable in this prototype.
Discovered skill/agent definitions load only when explicitly selected and pinned
to their provider; the other vendor receives them as read-only review references.
Native plugins and nested agent processes are not enabled. Complete independent
Claude headless quota polling and measured cost savings remain unestablished.
No Anvi training, wake/close, vault access or sealed-pool inspection occurred.
