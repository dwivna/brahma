"""Per-repository memory and measured routing history (Round 4), kept outside the repository.

Memory records decisions, conventions, check commands and past failures; outcomes record measured results by
provider and task type and are the only inputs to learned routing. Both are keyed by a canonical repository
identity, are inspectable, and can be forgotten for one repository without touching worktrees, approvals or
run checkpoints. Nothing here is committed or sent anywhere. Measured usage and estimates are separate fields.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .handoff import looks_secret
from .store import Store

KINDS = ("decision", "convention", "check", "failure")
MAX_VALUE = 2000
MIN_SAMPLES = 5  # below this, routing must say "no data yet" instead of presenting a learned preference


def repo_id(repo: Path) -> str:
    """Canonical identity: the repository's real common Git directory, so every worktree of one repo agrees."""
    from .gitsafe import run_git
    common = run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        raise ValueError("Memory needs a Git repository")
    return hashlib.sha256(str(Path(common.stdout.strip()).resolve()).encode()).hexdigest()[:16]


class Memory:
    def __init__(self, store: Store):
        self.store = store

    def remember(self, repo: str, kind: str, key: str, value: str) -> None:
        if kind not in KINDS:
            raise ValueError("Memory kind must be one of " + ", ".join(KINDS))
        if not key.strip() or len(key) > 200 or not value.strip() or len(value) > MAX_VALUE:
            raise ValueError("Memory needs a short key and a bounded value")
        if looks_secret(key) or looks_secret(value):
            raise ValueError("Memory never stores secrets")
        self.store.db.execute("INSERT INTO memory VALUES (?,?,?,?,?) ON CONFLICT(repo_id,kind,key) DO UPDATE SET "
                              "value=excluded.value, created=excluded.created", (repo, kind, key, value, self.store.now()))
        self.store.db.commit()

    def recall(self, repo: str, kind: str | None = None) -> list[dict]:
        rows = self.store.db.execute("SELECT kind,key,value,created FROM memory WHERE repo_id=?" + (" AND kind=?" if kind else "")
                                     + " ORDER BY kind,key", (repo, kind) if kind else (repo,))
        return [dict(zip(("kind", "key", "value", "created"), row)) for row in rows]

    def record_outcome(self, repo: str, *, provider: str, task_type: str, checks_passed: bool, repairs: int, findings: int,
                       ready: bool, session: str | None = None, task_index: int | None = None, seconds: float | None = None,
                       usage_measured: dict | None = None, usage_estimated: dict | None = None) -> None:
        """Record one measured result. Measured usage (provider-reported) and estimates are stored apart."""
        if provider not in ("codex", "claude"):
            raise ValueError("Unknown provider")
        if not task_type.strip() or repairs < 0 or findings < 0:
            raise ValueError("Invalid outcome")
        self.store.db.execute(
            "INSERT INTO outcomes(repo_id,session,task_index,provider,task_type,checks_passed,repairs,findings,ready,seconds,"
            "usage_measured,usage_estimated,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (repo, session, task_index, provider, task_type, int(checks_passed), repairs, findings, int(ready), seconds,
             json.dumps(usage_measured) if usage_measured is not None else None,
             json.dumps(usage_estimated) if usage_estimated is not None else None, self.store.now()))
        self.store.db.commit()

    def routing_history(self, repo: str, task_type: str | None = None) -> dict:
        """Per-provider results for this repository. `learned` is True only with enough samples."""
        rows = self.store.db.execute(
            "SELECT provider,task_type,COUNT(*),SUM(ready),SUM(checks_passed),AVG(repairs),AVG(findings) FROM outcomes "
            "WHERE repo_id=?" + (" AND task_type=?" if task_type else "") + " GROUP BY provider,task_type",
            (repo, task_type) if task_type else (repo,)).fetchall()
        result = {}
        for provider, kind, count, ready, passed, repairs, findings in rows:
            result.setdefault(provider, {})[kind] = {
                "samples": count, "ready_rate": ready / count, "checks_passed_rate": passed / count,
                "avg_repairs": repairs, "avg_findings": findings, "learned": count >= MIN_SAMPLES,
                "note": "learned from measured outcomes" if count >= MIN_SAMPLES else "no data yet: too few samples"}
        return result

    def inspect(self, repo: str) -> dict:
        outcomes = self.store.db.execute(
            "SELECT provider,task_type,checks_passed,repairs,findings,ready,seconds,usage_measured,usage_estimated,created "
            "FROM outcomes WHERE repo_id=? ORDER BY id", (repo,)).fetchall()
        names = ("provider", "task_type", "checks_passed", "repairs", "findings", "ready", "seconds", "usage_measured",
                 "usage_estimated", "created")
        return {"repo_id": repo, "memory": self.recall(repo), "outcomes": [dict(zip(names, row)) for row in outcomes]}

    def forget(self, repo: str) -> dict:
        """Remove this repository's memory and learned routing history. Nothing else is touched: sessions,
        approvals, contracts, jobs, task checkpoints, worktrees and other repositories' memory all remain."""
        with self.store.db:
            memories = self.store.db.execute("DELETE FROM memory WHERE repo_id=?", (repo,)).rowcount
            outcomes = self.store.db.execute("DELETE FROM outcomes WHERE repo_id=?", (repo,)).rowcount
        return {"repo_id": repo, "memory_removed": memories, "outcomes_removed": outcomes}
