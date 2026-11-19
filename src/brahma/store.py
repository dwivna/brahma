from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 3  # 1 = sessions/events/jobs/quotas; 2 adds the shared record; 3 adds notices (shown-once attention)


class Store:
    def __init__(self, home: Path):
        self.home = home
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(home / "state.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, repo TEXT, created TEXT, status TEXT, plan TEXT, worktree TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, session TEXT, created TEXT, provider TEXT, role TEXT, kind TEXT, text TEXT, usage TEXT)")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(sessions)")}
        for name, kind in (("stopped", "INTEGER DEFAULT 0"), ("contract", "TEXT"), ("approved_digest", "TEXT"),
                           ("paused", "INTEGER DEFAULT 0")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {kind}")
        self.db.execute("CREATE TABLE IF NOT EXISTS jobs (session TEXT PRIMARY KEY, generation TEXT NOT NULL, digest TEXT NOT NULL, next_at REAL, state TEXT NOT NULL, reason TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS quotas (provider TEXT PRIMARY KEY, snapshot TEXT NOT NULL)")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.db.close()
            raise ValueError(f"State format {version} is newer than this Brahma reads ({SCHEMA_VERSION}); "
                             "upgrade Brahma rather than reinterpret it")
        self._migrate_shared_record()
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def _migrate_shared_record(self) -> None:
        """Additive, idempotent tables for the shared record (Round 5). Existing data is never touched."""
        self.db.execute("CREATE TABLE IF NOT EXISTS task_states (session TEXT NOT NULL, task_index INTEGER NOT NULL, "
                        "phase TEXT NOT NULL, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, "
                        "updated TEXT NOT NULL, PRIMARY KEY(session, task_index))")
        # Per-repository memory: decisions, conventions, check commands, past failures. Keyed by canonical repo id.
        self.db.execute("CREATE TABLE IF NOT EXISTS memory (repo_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, "
                        "value TEXT NOT NULL, created TEXT NOT NULL, PRIMARY KEY(repo_id, kind, key))")
        # Measured outcomes by provider and task type: the only inputs to learned routing. Measured usage and
        # estimated usage are separate columns and are never merged.
        self.db.execute("CREATE TABLE IF NOT EXISTS outcomes (id INTEGER PRIMARY KEY, repo_id TEXT NOT NULL, session TEXT, "
                        "task_index INTEGER, provider TEXT NOT NULL, task_type TEXT NOT NULL, checks_passed INTEGER NOT NULL, "
                        "repairs INTEGER NOT NULL, findings INTEGER NOT NULL, ready INTEGER NOT NULL, seconds REAL, "
                        "usage_measured TEXT, usage_estimated TEXT, created TEXT NOT NULL)")
        self.db.execute("CREATE INDEX IF NOT EXISTS outcomes_repo ON outcomes(repo_id)")
        # Interrupts and decision-only attention events for the status view.
        self.db.execute("CREATE TABLE IF NOT EXISTS attention (id INTEGER PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL, "
                        "text TEXT NOT NULL, created TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS notices (session TEXT NOT NULL, key TEXT NOT NULL, created TEXT NOT NULL, "
                        "PRIMARY KEY (session, key))")

    def create(self, repo: Path) -> str:
        key = uuid.uuid4().hex[:12]
        self.db.execute("INSERT INTO sessions(id,repo,created,status,plan,worktree) VALUES (?,?,?,?,?,?)", (key, str(repo), self.now(), "chat", None, None))
        self.db.commit()
        return key

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # Event kinds whose text is machine-readable JSON read back by the coordinator: not rewritten by redaction.
    STRUCTURED_EVENTS = frozenset({"quota", "task_state", "wait", "plan_created", "routing", "outcome", "dispute_reply", "disagreement", "dispute_decision",
                               "user_note", "context_expansion", "checks_failing", "signin_lost"})

    def event(self, session: str, event) -> None:
        if event.kind == "task_state" and isinstance(event.text, str):
            # Structured, but its free-text fields carry check output and reviewer text: redact those fields only.
            from . import safety
            try:
                row = json.loads(event.text)
                for name in ("summary", "feedback"):
                    if isinstance(row.get(name), str):
                        row[name] = safety.redact(row[name])
                event.text = json.dumps(row, ensure_ascii=False)
            except (ValueError, TypeError, AttributeError):
                event.text = safety.redact(event.text)
        if event.kind not in self.STRUCTURED_EVENTS and isinstance(event.text, str):
            from . import safety
            event.text = safety.redact(event.text)  # the one chokepoint before events reach logs and screens
        self.db.execute("INSERT INTO events(session,created,provider,role,kind,text,usage) VALUES(?,?,?,?,?,?,?)",
                        (session, self.now(), event.provider, event.role, event.kind, event.text,
                         json.dumps(event.usage, ensure_ascii=False)))
        self.db.commit()

    def set(self, session: str, **values) -> None:
        if not values or set(values) - {"status", "plan", "worktree", "stopped", "paused", "contract", "approved_digest"}:
            raise ValueError("Invalid session fields")
        self.db.execute("UPDATE sessions SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                        (*values.values(), session))
        self.db.commit()

    def session(self, key: str) -> dict:
        row = self.db.execute("SELECT id,repo,created,status,plan,worktree,stopped,contract,approved_digest,paused FROM sessions WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("Unknown session")
        return dict(zip(("id", "repo", "created", "status", "plan", "worktree", "stopped", "contract", "approved_digest",
                         "paused"), row))

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

    def pause(self, session: str) -> None:
        """Hold a session: its approval and queued job are kept, but nothing runs until resume()."""
        self.session(session)
        self.db.execute("UPDATE sessions SET paused=1 WHERE id=?", (session,))
        self.db.commit()

    def resume(self, session: str) -> None:
        self.session(session)
        self.db.execute("UPDATE sessions SET paused=0 WHERE id=?", (session,))
        self.db.commit()

    @property
    def killed(self) -> bool:
        from . import halt
        return halt.engaged(self.home)

    def finish_job(self, session: str, state: str = "done") -> None:
        self.db.execute("UPDATE jobs SET state=? WHERE session=?", (state, session))
        self.db.commit()

    def pending(self) -> list[dict]:
        return [dict(zip(("session", "generation", "digest", "next_at", "state", "reason"), row)) for row in
                self.db.execute("SELECT session,generation,digest,next_at,state,reason FROM jobs WHERE state IN ('queued','running') ORDER BY next_at")]

    def claim(self, now: float) -> dict | None:
        if self.killed:
            return None
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT session,generation,digest,next_at,state,reason FROM jobs WHERE state='queued' AND next_at IS NOT NULL AND next_at<=? AND session IN (SELECT id FROM sessions WHERE stopped=0 AND COALESCE(paused,0)=0) ORDER BY next_at LIMIT 1", (now,)).fetchone()
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

    def events_after(self, key: str, after: int = 0, limit: int = 500) -> list[dict]:
        """Events with their sequence numbers, oldest first, for streaming callers."""
        rows = self.db.execute("SELECT seq,created,provider,role,kind,text,usage FROM events WHERE session=? AND seq>? "
                               "ORDER BY seq LIMIT ?", (key, after, limit))
        return [dict(zip(("seq", "created", "provider", "role", "kind", "text", "usage"),
                         (*row[:6], json.loads(row[6])))) for row in rows]

    def job(self, key: str) -> dict | None:
        row = self.db.execute("SELECT generation,digest,next_at,state,reason FROM jobs WHERE session=?", (key,)).fetchone()
        return dict(zip(("generation", "digest", "next_at", "state", "reason"), row)) if row else None

    # ---- shared record: task state and attention ------------------------------------------------------
    def set_task_state(self, session: str, task_index: int, phase: str, state: str, attempt: int = 0) -> None:
        self.session(session)
        self.db.execute("INSERT INTO task_states VALUES (?,?,?,?,?,?) ON CONFLICT(session,task_index) DO UPDATE SET "
                        "phase=excluded.phase,state=excluded.state,attempt=excluded.attempt,updated=excluded.updated",
                        (session, task_index, phase, state, attempt, self.now()))
        self.db.commit()

    def task_states(self, session: str) -> list[dict]:
        return [dict(zip(("task_index", "phase", "state", "attempt", "updated"), row)) for row in
                self.db.execute("SELECT task_index,phase,state,attempt,updated FROM task_states WHERE session=? "
                                "ORDER BY task_index", (session,))]

    def attention(self, session: str, kind: str, text: str) -> int:
        if kind not in ("decision", "interrupt", "blocked"):
            raise ValueError("Unknown attention kind")
        cursor = self.db.execute("INSERT INTO attention(session,kind,text,created) VALUES (?,?,?,?)",
                                 (session, kind, text[:500], self.now()))
        self.db.commit()
        return cursor.lastrowid

    def open_attention(self, session: str | None = None) -> list[dict]:
        query = "SELECT id,session,kind,text,created FROM attention WHERE resolved=0"
        rows = self.db.execute(query + (" AND session=?" if session else "") + " ORDER BY id", (session,) if session else ())
        return [dict(zip(("id", "session", "kind", "text", "created"), row)) for row in rows]

    def claim_notice(self, session: str, key: str) -> bool:
        """True exactly once per (session, key), across polls, restarts and reopened terminals."""
        cursor = self.db.execute("INSERT OR IGNORE INTO notices VALUES (?,?,?)", (session, key[:200], self.now()))
        self.db.commit()
        return cursor.rowcount == 1

    def resolve_attention(self, attention_id: int) -> None:
        self.db.execute("UPDATE attention SET resolved=1 WHERE id=?", (attention_id,))
        self.db.commit()

    def close(self):
        self.db.close()
