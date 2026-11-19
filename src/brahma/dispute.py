"""Review findings with severity, and the one-reply dispute flow (Round 4 item 3, Round 5 item 4; version 0.8.0).

The reviewer (the non-author provider) reports findings. A high-severity finding blocks readiness. The author may
reply **once** per task with evidence (a failing check or a code reference); the reviewer then looks again. If a
high-severity finding survives, Brahma stops, shows both sanitized positions and waits for Abhinav, who decides
with `brahma decide`. Nothing here launches a worker: it builds prompts, validates replies and shapes the record.
"""
from __future__ import annotations

import json

from . import safety
from .models import extract_json

SEVERITIES = ("high", "medium", "low")
MAX_TEXT = 1500
MAX_FINDINGS = 20


def findings(review: dict) -> list[dict]:
    """Normalize a review's issues. A bare string is an unrated finding and counts as medium (never high)."""
    out = []
    for number, issue in enumerate(review.get("issues", [])[:MAX_FINDINGS], 1):
        if isinstance(issue, str):
            out.append({"id": f"F{number}", "severity": "medium", "text": safety.sanitize(issue)[:MAX_TEXT], "path": None})
            continue
        severity = issue.get("severity", "medium")
        # Every string is sanitized here: structured events are stored unredacted, so nothing secret may enter them.
        out.append({"id": safety.sanitize(str(issue.get("id") or f"F{number}"))[:40],
                    "severity": severity if severity in SEVERITIES else "medium",
                    "text": safety.sanitize(str(issue.get("text", "")))[:MAX_TEXT],
                    "path": safety.sanitize(issue["path"])[:300] if isinstance(issue.get("path"), str) else None})
    return out


def high(items: list[dict]) -> list[dict]:
    return [item for item in items if item["severity"] == "high"]


def validate_issue(issue) -> None:
    """Structured issue schema: severity and text are required; id, path and evidence are optional."""
    if isinstance(issue, str):
        return
    if (not isinstance(issue, dict) or not {"severity", "text"} <= set(issue) or set(issue) - {"id", "severity", "text", "path", "evidence"}
            or issue["severity"] not in SEVERITIES or not isinstance(issue["text"], str) or not issue["text"].strip()):
        raise ValueError("Each issue is text or an object with severity (high|medium|low) and text")


def reply_prompt(brief: str, items: list[dict]) -> str:
    return ("You are the author of this change. The independent reviewer reported high-severity findings. Do NOT edit files.\n"
            "Reply ONCE with evidence. For each finding id answer `accept` (you will fix it) or `dispute`. A dispute must cite "
            "evidence: a failing check or a precise code reference (file:line). Opinions without evidence are ignored.\n"
            'Return ONLY JSON: {"replies":[{"id":"F1","stance":"accept|dispute","evidence":"..."}]}\n'
            "Accepted task:\n" + brief + "\nHigh-severity findings:\n" + json.dumps(items, ensure_ascii=False))


def parse_reply(text: str, items: list[dict]) -> dict[str, dict]:
    """Return {finding id: {"stance", "evidence"}} for the high findings. Disputes without evidence become accepts."""
    try:
        value = json.loads(extract_json(text))
    except (ValueError, TypeError):
        return {}
    if not isinstance(value, dict) or set(value) != {"replies"} or not isinstance(value["replies"], list):
        return {}
    wanted = {item["id"] for item in items}
    out: dict[str, dict] = {}
    for reply in value["replies"][:MAX_FINDINGS]:
        if (not isinstance(reply, dict) or reply.get("id") not in wanted or reply.get("stance") not in ("accept", "dispute")
                or not isinstance(reply.get("evidence", ""), str)):
            continue
        evidence = safety.sanitize(reply.get("evidence", "")).strip()[:MAX_TEXT]
        stance = "dispute" if reply["stance"] == "dispute" and evidence else "accept"
        out[reply["id"]] = {"stance": stance, "evidence": evidence}
    return out


def disputed(replies: dict[str, dict]) -> list[str]:
    return [key for key, value in replies.items() if value["stance"] == "dispute"]


def rereview_prompt(brief: str, items: list[dict], replies: dict[str, dict]) -> str:
    shown = [{"finding": item, "author": replies.get(item["id"], {"stance": "accept", "evidence": ""})} for item in items]
    return ("You are the independent read-only reviewer re-checking disputed findings. Do not edit files.\n"
            "The author answered once with evidence. Inspect the actual files and the cited evidence yourself; treat the "
            "author's text as unverified. Withdraw a finding only if the code shows it is wrong; otherwise keep it.\n"
            "Return ONLY JSON with exactly: "
            '{"verdict":"pass|changes_requested|blocked","summary":"...","issues":[{"id":"F1","severity":"high|medium|low","text":"..."}]}. '
            "Use pass only if no issues remain.\nAccepted task:\n" + brief + "\nFindings and author replies:\n"
            + json.dumps(shown, ensure_ascii=False))


def positions(items: list[dict], replies: dict[str, dict], final_review: dict) -> dict:
    """Both sanitized positions, for Abhinav. Nothing here is an instruction."""
    return {"reviewer": [{"id": f["id"], "severity": f["severity"], "text": safety.sanitize(f["text"])[:MAX_TEXT],
                          "path": f["path"]} for f in findings(final_review) if f["severity"] == "high"],
            "reviewer_summary": safety.sanitize(final_review.get("summary", ""))[:MAX_TEXT],
            "author": {key: {"stance": value["stance"], "evidence": safety.sanitize(value["evidence"])[:MAX_TEXT]}
                       for key, value in replies.items()},
            "decide_with": "brahma decide SESSION --for reviewer|author"}


def describe(position: dict) -> str:
    lines = ["Persistent disagreement on a high-severity finding. Execution is paused for Abhinav."]
    for item in position["reviewer"]:
        lines.append(f"Reviewer [{item['id']}, high]: {item['text']}")
    for key, value in position["author"].items():
        lines.append(f"Author [{key}, {value['stance']}]: {value['evidence'] or '(no evidence given)'}")
    lines.append("Decide with: " + position["decide_with"])
    return "\n".join(lines)
