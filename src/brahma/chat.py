"""Render stored events for display. Use one ChatRenderer per conversation."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import safety
from .models import Event, extract_json
from .providers import clean_text
from .session import displayable


def _local(value: str | datetime | int | float) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).astimezone()
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone()


def format_time(value: str | datetime) -> str:
    return _local(value).strftime("%H:%M %Z")


def format_datetime(value: str | datetime) -> str:
    return _local(value).strftime("%a %d %b %Y %H:%M %Z")


def format_reset_time(value: str | datetime | int | float | None) -> str:
    """Provider resets can be Unix timestamps or ISO times."""
    if isinstance(value, bool) or not isinstance(value, (str, datetime, int, float)):
        return "unknown"
    try:
        return _local(value).strftime("%a %d %b %Y %H:%M %Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return "unknown"


HIDDEN_KINDS = frozenset({"usage", "native_session", "plan", "plan_created"})
_USER_KINDS = {"approval", "approved", "user", "user_note", "dispute_decision", "capacity_confirmed", "control"}
_PLAIN_KINDS = {
    "message", "progress", "model_selected", "approval", "approved", "user", "capacity_confirmed",
    "publication", "summary", "error", "warning", "information", "enforcement", "restore",
    "note_applied", "checkpoint", "flagged_instruction", "plan_display", "dispute_overruled", "dispute_resolved",
}
_NAMES = {"brahma": "Brahma", "codex": "Codex", "claude": "Claude"}
_OTHER_WORKER = {"Codex": "Claude", "Claude": "Codex"}
REPORT_FIELDS = (
    ("completed", "Completed"), ("evidence", "Evidence"), ("current_work", "Current work"),
    ("next_work", "Next work"), ("where_we_are", "Where we are"), ("needs_you", "Needs you"),
)
_TASK_STATES = {
    "assigned": "was assigned", "building": "is being built", "checking": "is being checked",
    "reviewing": "is under review", "ready": "is ready", "blocked": "is blocked",
    "waiting": "is waiting", "incomplete": "is incomplete", "cancelled": "was cancelled",
    "needs_checks": "needs checks", "needs_decision": "needs your decision",
    "changes_requested": "needs changes",
}
_JSON = json.JSONDecoder()
_JSON_START = re.compile(r'[\[{]')
_ISO_TIME = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})')
_LINE_ESCAPES = str.maketrans({"\n": "\\n", "\r": "\\r", "\u2028": "\\u2028", "\u2029": "\\u2029"})


def command_display(argv: list[str]) -> str:
    return safety.redact(" ".join(_clean(safety.redact(arg).translate(_LINE_ESCAPES)) for arg in argv))


def _object(text: str) -> dict:
    try:
        value = json.loads(extract_json(text))
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _without_json(text: str) -> str:
    parts = []
    start = cursor = 0
    while match := _JSON_START.search(text, cursor):
        try:
            _, end = _JSON.raw_decode(text, match.start())
        except (ValueError, RecursionError):
            cursor = match.end()
            continue
        # Keep the replacement inside a single word so redaction still covers
        # the entire value of a credential containing brackets.
        parts.extend((text[start:match.start()], "(structured-data-omitted)"))
        start = cursor = end
    return "".join(parts) + text[start:]


def _clean(text: str) -> str:
    return "\n".join(displayable(line) for line in clean_text(text).split("\n"))


def _safe(text: str, *, localize_times: bool) -> str:
    text = safety.redact(_without_json(_clean(text)))
    return _ISO_TIME.sub(lambda match: format_reset_time(match[0]), text) if localize_times else text


def _value(value, default: str = "") -> str:
    if isinstance(value, str):
        return " ".join(_safe(value, localize_times=False).split())
    return str(value) if isinstance(value, (int, float)) else default


def _field(row: dict, key: str, default: str = "") -> str:
    return _value(row.get(key), default) or default


def _task(row: dict) -> str:
    number = _field(row, "index")
    return f"Task {number}" if number else "The task"


def _findings(row: dict) -> str:
    issues = row.get("issues", row.get("findings", row.get("notes", [])))
    issues = issues if isinstance(issues, list) else []
    details = []
    for number, issue in enumerate(issues, 1):
        if isinstance(issue, str):
            issue = {"text": issue}
        if isinstance(issue, dict):
            identifier = _field(issue, "id", f"F{number}")
            severity = _field(issue, "severity", "medium")
            path = _field(issue, "path")
            location = f" at {path}" if path else ""
            details.append(f"{identifier} ({severity}){location}: {_field(issue, 'text', 'No detail supplied')}")
    count = len(details)
    return f"{count} finding{'s' if count != 1 else ''}" + (": " + " | ".join(details) if details else "")


@dataclass(frozen=True)
class ChatMessage:
    time: str
    speaker: str
    text: str
    addressee: str | None = None
    date: str | None = None

    @property
    def line(self) -> str:
        speaker = f"{self.speaker} → {self.addressee}" if self.addressee else self.speaker
        return f"{self.time}  {speaker}: {self.text}"

    def __str__(self) -> str:
        return f"{self.date}\n{self.line}" if self.date else self.line


class ChatRenderer:
    """Render events in storage order, retaining only display context."""

    def __init__(self) -> None:
        self._day = None
        self._quota: dict[str, dict] = {}
        self._builder: str | None = None
        self._routed_builder: tuple[str, str | None] | None = None
        self._check_count = 0
        self._typed_checks = False
        self._legacy_check = False
        self._failed_checks: list[str] = []

    def render(self, event: Event, created: str) -> ChatMessage | None:
        kind = event.kind
        if kind in HIDDEN_KINDS:
            return None
        speaker = _NAMES.get(event.provider, "Brahma")
        if kind in _USER_KINDS or (event.provider == "brahma" and event.role == "user"):
            speaker = "You"
        row = _object(event.text)
        if kind == "routing":
            self._routed_builder = (_field(row, "index"), _NAMES.get(_field(row, "provider")))
        elif event.role == "builder" and event.provider in {"codex", "claude"}:
            self._builder = _NAMES[event.provider]
        addressee = None
        if kind in {"origin_baseline", "ready_receipt", "work_report"}:
            speaker = "Brahma"
        if kind == "review_result" and event.provider == "brahma" and event.role == "manager":
            speaker, addressee, text = self._review("Brahma", row, kind)
        elif kind in {"review", "review_verdict", "review_notes"} or (
                kind == "message" and event.role == "reviewer" and "verdict" in row):
            speaker, addressee, text = self._review(speaker, row, kind)
            if kind == "message" and row.get("verdict") == "pass":
                text = "Proposed verdict (unverified). " + text
        elif kind == "task_state":
            addressee, text = self._task_state(row)
        elif kind == "quota":
            text = self._quota_message(event.provider, speaker, row)
            if text is None:
                return None
        elif kind == "check":
            if self._typed_checks or self._legacy_check:
                return None
            self._legacy_check = True
            text = "Check result unverified"
        elif kind == "check_status" and event.provider == "brahma" and event.role == "manager":
            self._typed_checks = True
            text = self._check(row)
        elif kind == "control":
            text = {"stop": "Requested a session stop", "pause": "Paused the session"}.get(
                event.text, "Recorded a session control")
        elif kind == "checks_failing":
            failed = self._failed_checks
            if failed:
                text = f"{_task(row)} still has {len(failed)} failed out of {self._check_count} started checks: " + ", ".join(failed)
            else:
                text = f"{_task(row)} still has failing checks"
        elif kind in _STRUCTURED_RULES:
            text = _STRUCTURED_RULES[kind](row)
        elif kind in _PLAIN_KINDS and event.text.strip():
            text = event.text
        else:
            speaker = "Brahma" if kind not in CHAT_KINDS else speaker
            text = f"{kind.replace('_', ' ')} recorded"
        local = _local(created)
        day = local.date()
        date = local.strftime("%a %d %b %Y") if day != self._day else None
        self._day = day
        if kind not in {"work_report", "check_status"}:
            text = _safe(text, localize_times=kind != "progress").strip()
        if kind not in {"progress", "message", "work_report", "plan_display", "check_status"}:
            text = " ".join(text.split())
        return ChatMessage(local.strftime("%H:%M %Z"), speaker, text or "Message recorded", addressee, date)

    def _task_state(self, row: dict) -> tuple[str | None, str]:
        state = _field(row, "state")
        builder = _NAMES.get(_field(row, "builder"))
        if state == "assigned":
            self._builder = None
            if self._routed_builder and self._routed_builder[0] == _field(row, "index"):
                self._builder = self._routed_builder[1]
            self._routed_builder = None
        self._builder = builder or self._builder
        if state == "checking":
            self._check_count = 0
            self._typed_checks = self._legacy_check = False
            self._failed_checks.clear()
        task = _task(row)
        title = _field(row, "title")
        if title:
            task += f" “{title}”"
        action = _TASK_STATES.get(state, "was updated")
        addressee = self._builder if state == "assigned" else None
        summary = _field(row, "summary")
        return addressee, f"{task} {action}" + (f" — {summary}" if summary else "")

    def _review(self, speaker: str, row: dict, kind: str) -> tuple[str, str | None, str]:
        builder = _NAMES.get(_field(row, "builder")) or self._builder or _OTHER_WORKER.get(speaker)
        verdict = _field(row, "verdict", "pass" if kind == "review_notes" else "recorded").replace("_", " ")
        task = f" for {_task(row).lower()}" if "index" in row else ""
        summary = _field(row, "summary")
        text = f"Review{task}: {verdict} with {_findings(row)}"
        return speaker, builder, text + (f" — {summary}" if summary else "")

    def _quota_message(self, provider: str, speaker: str, row: dict) -> str | None:
        state = {key: value for key, value in row.items() if key not in {"observed_at", "source", "provider"}}
        if self._quota.get(provider) == state:
            return None
        self._quota[provider] = state
        blocked = row.get("blocked")
        status = "exhausted" if blocked is True else "available" if blocked is False else _field(row, "state", "unknown")
        reset = row.get("reset_at")
        detail = f", reset {format_reset_time(reset)}" if reset is not None or blocked is True else ""
        windows = row.get("windows")
        if isinstance(windows, list):
            usage = [f"{_field(window, 'name', 'window')} {_field(window, 'used_percent', 'unknown')}% used"
                     for window in windows if isinstance(window, dict) and window.get("used_percent") is not None]
            if usage:
                detail += f" ({', '.join(usage)})"
        return f"{speaker} quota is {status}{detail}"

    def _check(self, row: dict) -> str:
        argv = row.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            return "Check result unverified"
        command = command_display(argv)
        if row.get("phase") == "start":
            self._check_count += 1
            return f"Running check {self._check_count}: {command} (result unverified)"
        code = row.get("exit_code")
        if row.get("phase") != "complete" or type(code) is not int:
            return "Check result unverified"
        if code:
            self._failed_checks.append(command)
            return f"Check failed with exit {code}: {command}"
        return f"Check passed: {command}"


def _wait(row: dict) -> str:
    provider = _NAMES.get(_field(row, "provider"), "the provider")
    reset = format_reset_time(row.get("next_at", row.get("reset_at")))
    reason = _field(row, "reason")
    return f"Waiting for {provider} (reset {reset})" + (f" — {reason}" if reason else "")


def _origin(row: dict) -> str:
    text = "Recorded the repository's push target for this session"
    return text + (" and this session's result requires manual publication" if row.get("legacy") is True else "")


def report_rows(row: dict) -> list[str]:
    return [f"{label}: {_field(row, key, '—')}" for key, label in REPORT_FIELDS]


def _report(row: dict) -> str:
    return "\n".join(report_rows(row))


def _dispute_reply(row: dict) -> str:
    replies = row.get("replies")
    details = []
    if isinstance(replies, dict):
        details = [f"{identifier} — {_field(reply, 'stance')}: {_field(reply, 'evidence', 'No evidence supplied')}"
                   for identifier, reply in replies.items() if isinstance(reply, dict)]
    return f"Recorded the builder’s reply for {_task(row).lower()}" + (": " + " | ".join(details) if details else "")


def _context_expansion(row: dict) -> str:
    parts = []
    for key in ("accepted", "rejected"):
        paths = row.get(key)
        if isinstance(paths, list) and paths:
            parts.append(f"{key} " + ", ".join(_value(path) for path in paths))
    return f"Recorded expanded context for {_task(row).lower()}" + (": " + " and ".join(parts) if parts else "")


def _outcome(row: dict) -> str:
    provider = _NAMES.get(_field(row, "provider"))
    builder = f" with {provider}" if provider else ""
    status = "ready" if row.get("ready") is True else "without readiness"
    repairs = _field(row, "repairs", "unknown")
    findings = _field(row, "findings", "unknown")
    return f"{_task(row)} ended {status}{builder} after {repairs} repair attempts with {findings} findings"


_STRUCTURED_RULES = {
    "routing": lambda row: f"Chose {_NAMES.get(_field(row, 'provider'), 'a builder')} for {_task(row).lower()} because "
                           + _field(row, "reason", "of the routing policy"),
    "wait": _wait,
    "origin_baseline": _origin,
    "ready_receipt": lambda row: "Recorded the verified readiness receipt",
    "work_report": _report,
    "user_note": lambda row: f"Left a note for {_task({'index': row.get('task')}).lower()}: {_field(row, 'text')}",
    "dispute_decision": lambda row: f"Sided with the {_field(row, 'for', 'review participant')} on {_task(row).lower()}",
    "dispute_reply": _dispute_reply,
    "dispute_void": lambda row: f"Voided the dispute decision for {_task(row).lower()} because "
                                + _field(row, "reason", "the review context changed"),
    "disagreement": lambda row: _field(row, "text", f"{_task(row)} needs a decision on a disputed review"),
    "context_expansion": _context_expansion,
    "signin_lost": lambda row: f"{_NAMES.get(_field(row, 'provider'), 'The provider')} lost sign-in during {_task(row).lower()}",
    "verification_spent": lambda row: f"Verification took {_field(row, 'seconds', 'an unknown number of')} seconds for {_task(row).lower()}",
    "outcome": _outcome,
}

CHAT_KINDS = frozenset(_PLAIN_KINDS | _STRUCTURED_RULES.keys() | {
    "review", "review_verdict", "review_notes", "task_state", "quota", "check", "check_status", "review_result", "checks_failing", "control",
})


def render_chat(events: Iterable[tuple[Event, str]]) -> list[ChatMessage]:
    """Render a conversation from (event, stored UTC timestamp) pairs."""
    renderer = ChatRenderer()
    return [message for event, created in events if (message := renderer.render(event, created)) is not None]
