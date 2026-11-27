"""Owner reports derived from the coordinator's persisted plan and event history."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from .dispute import findings
from .store import Store


REPORT_STATES = {"ready", "blocked", "incomplete", "waiting", "needs_decision", "cancelled"}


def latest_report(store: Store, key: str) -> dict[str, str] | None:
    event = store.latest_event(key, "work_report")
    return json.loads(event["text"]) if event else None


def _action(key, state, reason, wait, control, signin):
    from .chat import format_reset_time

    if state == "needs_decision":
        return f"Resolve the disputed review with brahma decide {key} --for reviewer|author."
    if state in {"blocked", "cancelled", "waiting"}:
        if control == "stop":
            return "Approve a new plan to restart the stopped work."
        if "kill switch" in reason.lower():
            return f"Release the kill switch, then run brahma resume {key}."
        if control == "pause" or "pause" in reason.lower() or state == "cancelled":
            return f"Run brahma resume {key} to continue."
    if state == "incomplete":
        return "Create and approve a new plan for the unfinished work."
    if state in {"waiting", "blocked"} and wait and wait.get("state") in {"waiting", "blocked"}:
        return f"Wait for {wait['provider'].title()} capacity. Reset {format_reset_time(wait.get('next_at'))}."
    if state == "blocked" and signin:
        return f"Restore {signin.title()} CLI sign-in, then resume session {key}."
    if state == "blocked" and wait and wait.get("state") == "unavailable":
        return f"Restore {wait['provider'].title()} availability. {wait.get('reason', '')}".strip()
    if "toolchain changed" in reason.lower():
        return f"Inspect the changed toolchain and approve a new plan. {reason}"
    if state in {"blocked", "waiting"}:
        return f"Resolve the blocker and review the checkpoint. {reason}".strip()
    if state == "needs_checks":
        return "Configure checks and approve a new plan."
    return "None"


def build_report(store: Store, key: str) -> dict[str, str]:
    from .chat import command_display

    floor = store.latest_seq(key, "plan_created")
    events = store.events_since(key, floor)
    plan = json.loads(store.session(key)["plan"] or "{}")
    tasks = plan.get("tasks", [])
    states = {}
    checks = defaultdict(list)
    reviews = defaultdict(list)
    passed_reviews = set()
    index = 0
    wait = receipt = pending_check = signin = None
    typed_check = False
    summary = publication = control = stranded_commit = ""
    for event in events:
        kind, text = event["kind"], event["text"]
        if event["provider"] != "brahma":
            continue
        if kind == "review_result" and event["role"] == "manager":
            review = json.loads(text)
            i = review["index"]
            counts = Counter(issue["severity"] for issue in findings(review))
            detail = ", ".join(f"{n} {level}" for level, n in counts.items()) or "no findings"
            reviews[i].append(f"{review['reviewer'].title()} {review['verdict']} ({detail}, model judgement).")
            if review["verdict"] == "pass":
                passed_reviews.add(i)
            else:
                passed_reviews.discard(i)
        elif kind == "dispute_overruled":
            reviews[index].append("Owner decision: " + text)
        elif kind == "task_state":
            row = json.loads(text)
            index = row["index"]
            states[index] = row
            if row["state"] in {"building", "changes_requested"}:
                passed_reviews.discard(index)
            summary = ""
            if row["state"] == "assigned":
                wait = None
                signin = None
                control = ""
            pending_check = None
            typed_check = False
        elif kind == "check_status" and event["role"] == "manager":
            row = json.loads(text)
            command = command_display(row["argv"])
            typed_check = True
            if row["phase"] == "start":
                checks[index].append(f"{command} (result unverified)")
                pending_check = row["argv"]
            elif row["phase"] == "complete" and type(row.get("exit_code")) is int:
                result = f"{command} => exit {row['exit_code']}"
                if pending_check == row["argv"]:
                    checks[index][-1] = result
                else:
                    checks[index].append(result)
                pending_check = None
        elif kind == "check" and not typed_check:
            if not checks[index] or checks[index][-1] != "Legacy check result unverified":
                checks[index].append("Legacy check result unverified")
        elif kind == "wait":
            wait = json.loads(text)
        elif kind == "signin_lost":
            signin = json.loads(text)["provider"]
        elif kind == "control":
            control = text
        elif kind == "ready_receipt":
            receipt = json.loads(text)
        elif kind == "publication":
            publication = text
        elif kind == "error":
            landed = re.match(r"The verified work was committed as ([0-9a-f]{40,64}) on ", text)
            if landed:
                stranded_commit = landed.group(1)
        elif kind == "summary":
            summary = text

    row = states.get(index, {})
    state = summary.partition(":")[0] if summary else row.get("state", "pending")
    ready = [i for i, item in states.items() if item["state"] == "ready"]

    def label(i):
        return f"Task {i} {states[i]['title'] if i in states else tasks[i - 1]['title']}"

    evidence = []
    scope = list(states) if summary and states else [index]
    for i in scope:
        if i:
            evidence.append(f"Task {i}:")
        evidence.append("; ".join(checks[i]) or "No checks were run on record.")
        if states.get(i, {}).get("state") == "needs_checks":
            evidence.append("No project checks configured.")
        evidence.extend(reviews[i] or ["Independent review unverified."])
        if i in ready:
            evidence.append("Ownership verified at task readiness.")
    evidence.append("Behaviour beyond the recorded checks remains unverified.")
    completed = ", ".join(label(i) for i in ready) or "No task recorded ready"
    commit = "No local commit recorded."
    if stranded_commit:
        commit = f"Local commit {stranded_commit}. Ready receipt unavailable."
    if receipt:
        commit = (f"Verified commit {receipt['head']}." if receipt["committed"]
                  else f"Uncommitted changes at {receipt['head']}.")
    if summary and receipt and summary.splitlines()[-1].startswith("Draft PR: "):
        publication = summary.splitlines()[-1]
    remaining = next((i for i in range(1, len(tasks) + 1) if i not in ready), None)
    position = f"{len(ready)} of {len(tasks)} tasks ready. "
    position += f"Run {state}." if summary else f"Task {index} {state}."
    if receipt:
        position += " Ready receipt recorded."
    for i in range(1, len(tasks) + 1):
        if i not in ready and i not in passed_reviews:
            position += f" Independent review outstanding for {label(i)}."
    reason = row.get("summary", "")
    if summary and state in {"blocked", "cancelled", "waiting"}:
        reason = summary.partition(":")[2].strip()
    return {
        "completed": f"{completed}. {commit}",
        "evidence": " ".join(evidence),
        "current_work": (f"{label(index)} ({state})." if index and not summary
                         and state not in REPORT_STATES and state != "needs_checks" else "None"),
        "next_work": label(remaining) if remaining else "None",
        "where_we_are": position + " " + (publication or "No publication recorded."),
        "needs_you": _action(key, state, reason, wait, control, signin),
    }
