# Prompt and context efficiency — required behavior

Abhinav's requirement is recorded in Round 3 of `decisions.md`. This document is
an engineering outline, not an implementation or a cost benchmark.

The coordinator should send a small, explicit task brief rather than replaying
the whole user conversation to both providers. A brief must retain:

- The approved goal and locked contract identity, including scenario IDs.
- Relevant decisions in the user's words, repository instructions and constraints.
- The worker's exact ownership, dependencies, permitted skills and agent roles.
- Check commands, actual results, unresolved review issues and known failures.
- Native session identity, checkpoint provenance and remaining approved budget.

Stable instructions should precede task-specific context. Read files and diffs
when needed; send the relevant portions with provenance. Avoid duplicate source
dumps, repeated reviews of unchanged material and unsupported retries. The
manager's structured ledger is the common state, while each provider retains its
native context where supported.

Compaction must preserve the contract, constraints and unresolved evidence. It
must not convert a failed check to success, drop a user stop, expand permissions,
erase ownership or treat stale results as current. Preserve original events for
local inspection; do not replace source evidence with an untraceable summary.

Track actual provider-reported input/output and cached-token fields when exposed.
Character budgets are not token counts. Any local tokenizer count must identify
the model/encoding and remain an estimate if exact compatibility is unverified.
Unknown quota, cost and cached usage remain unknown. Keep brief construction,
compaction and review costs in the total rather than hiding coordination overhead.

Evaluate with completed accepted scenarios, actual checks, repair/reopen rate,
reported tokens and wall/active time on comparable tasks. Token reductions alone
do not prove better value. The existing prototype has bounded context and usage
parsing; automatic compaction and benchmarked efficiency are not implemented.
