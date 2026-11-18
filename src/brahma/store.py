from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, home: Path):
        self.home = home
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(home / "state.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, repo TEXT, created TEXT, status TEXT, plan TEXT, worktree TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, session TEXT, created TEXT, provider TEXT, role TEXT, kind TEXT, text TEXT, usage TEXT)")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        for name, kind in (("stopped", "INTEGER DEFAULT 0"), ("contract", "TEXT"), ("approved_digest", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {kind}")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (session TEXT PRIMARY KEY, generation TEXT NOT NULL, digest TEXT NOT NULL, next_at REAL, state TEXT NOT NULL, reason TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS quotas (provider TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
        self.db.commit()

    def create(self, repo: Path) -> str:
        key = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO sessions(id,repo,created,status,plan,worktree) VALUES (?,?,?,?,?,?)", (key, str(repo), self.now(), "chat", None, None))
        self.db.commit()
        return key

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def event(self, session: str, event) -> None:
        self.db.execute("INSERT INTO events(session,created,provider,role,kind,text,usage) VALUES(?,?,?,?,?,?,?)",
                        (session, self.now(), event.provider, event.role, event.kind, event.text,
                         json.dumps(event.usage, ensure_ascii=False)))
        self.db.commit()

    def set(self, session: str, **values) -> None:
        if not values or set(values) - {"status", "plan", "worktree", "stopped", "contract", "approved_digest"}:
            raise ValueError("Invalid session fields")
        self.db.execute("UPDATE sessions SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                        (*values.values(), session))
        self.db.commit()

    def session(self, key: str) -> dict:
        row = self.db.execute("SELECT id,repo,created,status,plan,worktree,stopped,contract,approved_digest FROM sessions WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Unknown session")
        return dict(zip(("id", "repo", "created", "status", "plan", "worktree", "stopped", "contract", "approved_digest"), row))

    def observe(self, snapshot: dict) -> None:
        if snapshot.get("provider") not in ("codex", "claude"):
            raise ValueError("Unknown quota provider")
        self.db.execute("INSERT INTO quotas VALUES (?,?) ON CONFLICT(provider) DO UPDATE SET snapshot=excluded.snapshot",
                        (snapshot["provider"], json.dumps(snapshot)))
        self.db.commit()

    def quota(self, provider: str) -> dict | None:
        row = self.db.execute("SELECT snapshot FROM quotas WHERE provider=?", (provider,)).fetchone()
        return json.loads(row[0]) if row else None

    def enqueue(self, session: str, generation: str, digest: str, next_at: float | None, reason: str) -> None:
        if self.session(session)["stopped"]:
            return
        self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?) ON CONFLICT(session) DO UPDATE SET generation=excluded.generation,digest=excluded.digest,next_at=excluded.next_at,state=excluded.state,reason=excluded.reason",
                        (session, generation, digest, next_at, "queued", reason))
        self.db.commit()

    def stop(self, session: str) -> None:
        self.db.execute("UPDATE sessions SET stopped=1 WHERE id=?", (session,))
        self.db.execute("UPDATE jobs SET state='cancelled' WHERE session=?", (session,))
        self.db.commit()

    def finish_job(self, session: str, state: str = "done") -> None:
        self.db.execute("UPDATE jobs SET state=? WHERE session=?", (state, session))
        self.db.commit()

    def pending(self) -> list[dict]:
        return [dict(zip(("session", "generation", "digest", "next_at", "state", "reason"), row)) for row in
                self.db.execute("SELECT session,generation,digest,next_at,state,reason FROM jobs WHERE state IN ('queued','running') ORDER BY next_at")]

    def claim(self, now: float) -> dict | None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT session,generation,digest,next_at,state,reason FROM jobs WHERE state='queued' AND next_at IS NOT NULL AND next_at<=? AND session IN (SELECT id FROM sessions WHERE stopped=0) ORDER BY next_at LIMIT 1", (now,)).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='running' WHERE session=? AND state='queued'", (row[0],))
        return dict(zip(("session", "generation", "digest", "next_at", "state", "reason"), row)) if row else None

    def recover_jobs(self) -> None:
        self.db.execute("UPDATE jobs SET state='queued' WHERE state='running'")
        self.db.commit()

    def recent(self, limit: int = 20) -> list[dict]:
        return [dict(zip(("id", "repo", "status", "created"), r)) for r in
                self.db.execute("SELECT id,repo,status,created FROM sessions ORDER BY created DESC LIMIT ?", (limit,))]

    def events(self, key: str) -> list[dict]:
        return [dict(zip(("provider", "role", "kind", "text", "usage"), (*row[:4], json.loads(row[4]))))
                for row in self.db.execute("SELECT provider,role,kind,text,usage FROM events WHERE session=? ORDER BY seq", (key,))]

    def close(self):
        self.db.close()
