"""Quota-aware provider routing and read-only proposal drafts (Round 4; version 0.7.0).

Routing:  capacity first, learning second.  A provider whose quota is exhausted is never chosen over an eligible
one, whatever its record. Among eligible providers a learned ranking is used only when *both* have enough measured
outcomes for the task type in this repository; otherwise Brahma says "no data yet" and falls back to a plain,
documented default (available before unknown, then codex before claude). That default is not a claim about which
model is better.

Proposals:  read-only signals (recorded check failures, stale local brahma branches, feedback the user supplied)
become *draft* proposals with draft Gherkin. This module never starts a worker and never creates or refreshes an
approval; it imports no provider or engine code.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .memory import MIN_SAMPLES

PROVIDERS = ("codex", "claude")
STALE_BRANCH_DAYS = 30
ELIGIBLE = ("available", "unknown")
DEFAULT_ORDER_NOTE = "default order (available before unknown, then codex before claude); not a learned preference"


def task_type(files: list[str]) -> str:
    """A coarse, stable task type from the owned paths: tests, docs or code."""
    stems = [f.rstrip("/").lower() for f in files] or ["code"]
    if all(f.startswith(("tests/", "test/")) or "/test_" in f or f.startswith("test_") for f in stems):
        return "tests"
    if all(f.startswith("docs/") or f.endswith((".md", ".rst", ".txt")) for f in stems):
        return "docs"
    return "code"


@dataclass(frozen=True)
class Decision:
    provider: str
    mode: str                 # pinned | learned | default | wait | none
    reason: str
    table: dict = field(default_factory=dict)   # per-provider facts shown to the user, never hidden

    def to_dict(self) -> dict:
        return {"provider": self.provider, "mode": self.mode, "reason": self.reason, "table": self.table}


def _score(entry: dict) -> tuple:
    # Higher ready rate first, then fewer repair cycles, then fewer review findings (all measured).
    return (-entry["ready_rate"], entry["avg_repairs"], entry["avg_findings"])


def choose(eligibility: dict[str, dict], history: dict, kind: str, pinned: str | None = None) -> Decision:
    """Pick the provider for an auto task.

    `eligibility` maps provider -> {"state": available|unknown|waiting|blocked|unavailable, "next_at", "reason"}.
    `history` is Memory.routing_history(repo) (provider -> task_type -> stats). Pinned providers are honoured as is:
    the caller still applies the normal capacity gate and wait.
    """
    table = {}
    for name in PROVIDERS:
        state = eligibility.get(name, {"state": "unknown", "reason": "No eligibility information"})
        stats = history.get(name, {}).get(kind)
        table[name] = {"quota": state["state"], "samples": stats["samples"] if stats else 0,
                       "learned": bool(stats and stats["learned"]),
                       "history": (f"{stats['samples']} runs, {stats['ready_rate']:.0%} ready" if stats and stats["learned"]
                                   else "no data yet" if not stats else f"no data yet ({stats['samples']} of {MIN_SAMPLES} runs)")}
    if pinned in PROVIDERS:
        return Decision(pinned, "pinned", f"{pinned} is pinned by the approved contract", table)
    eligible = [n for n in PROVIDERS if table[n]["quota"] in ELIGIBLE]
    if not eligible:
        waiting = [n for n in PROVIDERS if table[n]["quota"] == "waiting"]
        if waiting:
            pick = min(waiting, key=lambda n: eligibility[n].get("next_at") or float("inf"))
            return Decision(pick, "wait", f"no provider has capacity now; durable wait for {pick}'s reset", table)
        return Decision(PROVIDERS[0], "none", "no permitted provider can run", table)
    learned = [n for n in eligible if table[n]["learned"]]
    if len(eligible) > 1 and len(learned) == len(eligible):
        pick = min(eligible, key=lambda n: _score(history[n][kind]))
        return Decision(pick, "learned", f"best measured record for {kind} tasks among providers with capacity", table)
    if len(eligible) == 1:
        return Decision(eligible[0], "default", f"only {eligible[0]} has capacity; history not consulted", table)
    pick = min(eligible, key=lambda n: (table[n]["quota"] != "available", PROVIDERS.index(n)))
    return Decision(pick, "default", "no data yet: " + DEFAULT_ORDER_NOTE, table)


# ---- proposal drafts (read-only) -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    source: str          # check_failure | stale_branch | feedback
    title: str
    evidence: str
    gherkin: str
    approved: bool = False   # always False: a proposal is never an approval


def _gherkin(title: str, given: str, then: str) -> str:
    return f"Feature: {title}\n  Scenario: {title}\n    Given {given}\n    When the owner approves this contract\n    Then {then}"


def from_check_failures(failures: list[str]) -> list[Proposal]:
    out = []
    for text in failures[:5]:
        line = " ".join(text.split())[:200]
        out.append(Proposal("check_failure", "Repair a failing check", line,
                            _gherkin("Repair a failing check", f"the recorded check failure: {line}",
                                     "the configured checks pass and no unrelated file changes")))
    return out


def from_feedback(feedback: str | None) -> list[Proposal]:
    """Explicitly supplied review feedback only: Brahma never goes looking for it."""
    if not feedback or not feedback.strip():
        return []
    text = " ".join(feedback.split())[:400]
    return [Proposal("feedback", "Address supplied review feedback", text,
                     _gherkin("Address supplied review feedback", f"this feedback: {text}",
                              "the feedback is resolved within the approved file ownership"))]


def stale_branches(repo: Path, now: float | None = None, days: int = STALE_BRANCH_DAYS) -> list[Proposal]:
    """Local brahma/* branches untouched for a while (a read-only listing; nothing is deleted or merged)."""
    result = subprocess.run(["git", "--literal-pathspecs", "-C", str(repo), "for-each-ref",
                             "--format=%(refname:short)%09%(committerdate:unix)", "refs/heads/brahma/"],
                            text=True, capture_output=True, timeout=30)
    if result.returncode:
        return []
    now = time.time() if now is None else now
    out = []
    for line in result.stdout.splitlines():
        name, _, stamp = line.partition("\t")
        try:
            age = (now - int(stamp)) / 86400
        except ValueError:
            continue
        if age >= days:
            out.append(Proposal("stale_branch", f"Decide what to do with stale branch {name}",
                                f"{name} has had no commit for {int(age)} days",
                                _gherkin(f"Resolve stale branch {name}", f"branch {name} untouched for {int(age)} days",
                                         "the owner has reviewed it and chosen to keep, finish or abandon it")))
    return out


def recorded_check_failures(memory, repo_id: str) -> list[str]:
    """Check failures already recorded in this repository's memory (kind 'failure', key starting 'check:')."""
    return [m["value"] for m in memory.recall(repo_id, "failure") if m["key"].startswith("check:")]


def draft_proposals(repo: Path, memory, repo_id: str, feedback: str | None = None) -> list[dict]:
    """All draft proposals for a repository. Starts nothing, approves nothing."""
    drafts = (from_check_failures(recorded_check_failures(memory, repo_id)) + stale_branches(repo) + from_feedback(feedback))
    return [dict(source=p.source, title=p.title, evidence=p.evidence, gherkin=p.gherkin, approved=p.approved, status="draft")
            for p in drafts]
