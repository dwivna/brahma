"""Plain-language status board and decision-only attention, derived from the shared session record (0.10.0).

Everything here is read from the persisted store, so a foreground run, a scheduler run and a reopened terminal all see
the same board. No raw worker transcript is ever read: only structured state (task_state, wait, quota, routing, outcome,
attention signals). Measured usage and estimates are shown separately; unknown stays "unknown".
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import exitcodes

TASK_SECONDS = 3600
# The only things allowed to interrupt Abhinav (Round 11).
NOTIFY_KINDS = ("approval", "disagreement", "checks_failing", "signin_lost", "ready")
SIGNALS = ("checks_failing", "signin_lost")
NEXT_STEP = {
    "assigned": "Starting the builder", "building": "Builder is working", "checking": "Running the approved checks",
    "reviewing": "Independent review is running", "changes_requested": "Repairing from the findings",
    "waiting": "Waiting for provider capacity; resumes by itself", "ready": "Ready for your review",
    "needs_checks": "Configure checks, then approve again", "needs_decision": "Waiting for your decision",
    "blocked": "Blocked; see the reason", "incomplete": "Needs a new approved plan", "cancelled": "Stopped; checkpoint kept",
}


@dataclass
class TaskRow:
    index: int
    title: str
    worker: str
    phase: str
    active_minutes: float | None      # None = not started; never grows while waiting for quota
    repairs: int
    next_step: str
    blocker: str = ""


@dataclass
class Board:
    session: str
    status: str
    paused: bool
    stopped: bool
    tasks: list[TaskRow] = field(default_factory=list)
    capacity: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    attention: list[dict] = field(default_factory=list)

    def lines(self, width: int = 100) -> list[str]:
        head = f"{self.status}" + (" · PAUSED" if self.paused else "") + (" · STOPPED" if self.stopped else "")
        out = [f"Session {self.session}: {head}"]
        for t in self.tasks:
            active = "not started" if t.active_minutes is None else f"{t.active_minutes:.0f} of 60 min active"
            out.append(f"  {t.index}. {t.title[:40]} · {t.worker} · {t.phase} · {active} · repairs {t.repairs}/3")
            out.append(f"     next: {t.next_step}" + (f" · {t.blocker}" if t.blocker else ""))
        for name, cap in self.capacity.items():
            out.append(f"  {name}: {cap}")
        out.append(f"  usage (measured): {self.usage['measured']} · estimated: {self.usage['estimated']}")
        for item in self.attention:
            out.append(f"  ! {item}")
        return [line[:width] for line in out]


def _clean(text: object, limit: int = 160) -> str:
    from .safety import sanitize
    return " ".join(sanitize(str(text)).split())[:limit]


def _when(epoch) -> str:
    if not isinstance(epoch, (int, float)):
        return "reset unknown"
    return "resets " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


def capacity(store, events: list[dict]) -> dict:
    """Per provider: waiting (with reset if reported), available, or unknown. Never guessed."""
    from . import quota
    waiting = {}
    for event in events:
        if event["kind"] == "wait":
            try:
                value = json.loads(event["text"])
                waiting[value["provider"]] = value
            except (ValueError, TypeError, KeyError):
                continue
    result = {}
    for name in ("codex", "claude"):
        snapshot = store.quota(name)
        state = quota.eligibility(snapshot) if snapshot else None
        if state is None:
            result[name] = "unknown (no provider-reported quota)"
            continue
        if state["state"] == "waiting":
            result[name] = f"waiting, {_when(state.get('next_at'))}"
        elif name in waiting and waiting[name].get("state") == "waiting" and state["state"] != "available":
            result[name] = f"waiting, {_when(waiting[name].get('next_at'))}"
        else:
            result[name] = state["state"]
    return result


def usage(events: list[dict]) -> dict:
    """Measured token counters and estimates stay separate; raw transcripts are not read."""
    measured = {"input": 0, "output": 0}
    seen = False
    estimated = "none recorded"
    for event in events:
        if event["kind"] == "usage" and isinstance(event.get("usage"), dict):
            seen = True
            measured["input"] += int(event["usage"].get("input_tokens", 0) or 0)
            measured["output"] += int(event["usage"].get("output_tokens", 0) or 0)
        if event["kind"] == "outcome":
            try:
                value = json.loads(event["text"]).get("usage_estimated")
                if value:
                    estimated = _clean(json.dumps(value), 80)
            except (ValueError, TypeError, AttributeError):
                pass
    return {"measured": f"in {measured['input']:,} / out {measured['output']:,} tokens" if seen else "unknown",
            "estimated": estimated}


def build(store, key: str) -> Board:
    from .cli import session_status
    code, status = session_status(store, key)
    saved = store.session(key)
    events = store.events_after(key, 0, 20000)
    latest: dict[int, dict] = {}
    for event in events:
        if event["kind"] == "task_state":
            try:
                row = json.loads(event["text"])
            except (ValueError, TypeError):
                continue
            if type(row.get("index")) is int:
                latest[row["index"]] = row
    tasks = []
    for index in sorted(latest):
        row = latest[index]
        state = str(row.get("state", "unknown"))
        remaining = row.get("remaining_seconds")
        active = None if not isinstance(remaining, (int, float)) else max(0.0, (TASK_SECONDS - remaining) / 60)
        attempt = row.get("attempt", 0) if type(row.get("attempt", 0)) is int else 0
        blocker = _clean(row.get("summary", "")) if state in ("blocked", "incomplete", "needs_decision", "waiting") else ""
        tasks.append(TaskRow(index, _clean(row.get("title", "?"), 60), str(row.get("builder") or "not chosen yet"), state, active,
                             max(0, attempt - 1), NEXT_STEP.get(state, "In progress"), blocker))
    attention = [_clean(a["text"], 200) for a in store.open_attention(key)]
    return Board(key, status["outcome"], bool(saved["paused"]), bool(saved["stopped"]), tasks,
                 capacity(store, events), usage(events), attention)


def pending(store, key: str) -> list[tuple[str, str, str]]:
    """Permitted attention events as (dedup key, kind, text). Routine progress never appears here."""
    from .cli import session_status
    code, status = session_status(store, key)
    out = []
    events = store.events_after(key, 0, 20000)
    floor = max([e["seq"] for e in events if e["kind"] == "plan_created"], default=0)   # signals belong to the current plan
    if code == exitcodes.NEEDS_APPROVAL and not [a for a in store.open_attention(key) if a["kind"] == "decision"]:
        if status["contract_digest"] and not status["approved"]:
            out.append((f"approval:{status['contract_digest']}", "approval", "A plan needs your approval"))
    for item in store.open_attention(key):
        if item["kind"] == "decision":
            out.append((f"disagreement:{item['id']}", "disagreement", _clean(item["text"], 200)))
    for event in events:
        if event["kind"] in SIGNALS and event["seq"] >= floor:
            try:
                value = json.loads(event["text"])
            except (ValueError, TypeError):
                continue
            label = {"checks_failing": "Checks are still failing after the repairs",
                     "signin_lost": f"{_clean(value.get('provider', 'a provider'), 20)} sign-in was lost; run its login"}
            out.append((f"{event['kind']}:{event['seq']}", event["kind"], label[event["kind"]]))
    if code == exitcodes.READY and status["contract_digest"]:
        out.append((f"ready:{status['contract_digest']}:{store.session(key)['status']}", "ready", "Work is ready for your review"))
    return [item for item in out if item[1] in NOTIFY_KINDS]


def fresh(store, key: str) -> list[tuple[str, str]]:
    """Attention not yet shown, claimed durably so polling and reopening show each event once."""
    return [(kind, text) for dedup, kind, text in pending(store, key) if store.claim_notice(key, dedup)]
