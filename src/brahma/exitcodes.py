"""Process exit codes (Round 9/11, version 0.4.0). One distinct code per outcome.

The table is documented in the README and versioned: changing a code is a MINOR bump.
It is the *proposed starting table* from decision Round 11; Abhinav has not yet confirmed the final one.
"""
from __future__ import annotations

READY = 0
WAITING = 10          # waiting for provider capacity, or queued to run
NEEDS_APPROVAL = 11   # a human must review and approve the contract
NEEDS_CHECKS = 12     # no approved checks are configured, so nothing can be marked ready
BLOCKED = 13          # stopped by a policy gate, a failed check or a provider problem
CANCELLED = 14        # stopped, paused or killed by the user
USAGE = 20            # bad arguments, unknown session, unreadable input
SIGN_IN = 30          # a provider CLI is missing or not signed in

TABLE = {READY: "ready", WAITING: "waiting or queued", NEEDS_APPROVAL: "needs human approval",
         NEEDS_CHECKS: "needs approved checks", BLOCKED: "blocked", CANCELLED: "stopped, paused or killed",
         USAGE: "usage error", SIGN_IN: "provider CLI missing or not signed in"}


def for_result_status(status: str) -> int:
    """Exit code for an engine/session result status."""
    return {"ready": READY, "waiting": WAITING, "cancelled": CANCELLED, "interrupted": CANCELLED}.get(status, BLOCKED)


def for_session(saved: dict, job_state: str | None, has_checks: bool | None) -> int:
    """Exit code describing where a stored session stands (used by `brahma status`)."""
    if saved["stopped"] or saved["paused"]:
        return CANCELLED
    if saved["status"] == "ready":
        return READY
    if has_checks is False:
        return NEEDS_CHECKS
    if not saved["approved_digest"] or saved["status"] in ("chat", "awaiting_approval"):
        return NEEDS_APPROVAL
    if saved["status"] == "waiting" or job_state in ("queued", "running"):
        return WAITING
    return for_result_status(saved["status"])
