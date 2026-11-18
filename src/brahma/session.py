from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
from pathlib import Path
import re
import fnmatch
import uuid
import time
from dataclasses import asdict, replace

from .config import Settings
from .models import Event, Plan, Result
from . import gittools
from .store import Store


CONTROL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))|[\x00-\x08\x0b-\x1f\x7f]")


class Session:
    def __init__(self, store: Store, repo: Path, emit=lambda event: None, key: str | None = None):
        self.store, self.repo, self.callback = store, gittools.repository(repo), emit
        self.key = key or store.create(self.repo)
        saved = store.session(self.key)
        if Path(saved["repo"]).resolve() != self.repo:
            raise ValueError("Session belongs to another repository")
        self.settings = Settings.load(self.repo)
        self.cancel = asyncio.Event()
        self.plan = Plan.parse(saved["plan"]) if saved["plan"] else None
        generations = [r["text"] for r in store.events(self.key) if r["kind"] == "plan_created"]
        self.generation = generations[-1] if generations else uuid.uuid4().hex

    def emit(self, event: Event):
        if event.kind == "quota":
            self.store.observe(json.loads(event.text))
        if event.kind == "task_state":
            state = json.loads(event.text)
            state["generation"] = self.generation
            if state.get("state") == "ready":
                worktree = self.store.session(self.key)["worktree"]
                state["snapshot"] = gittools.snapshot_changes(Path(worktree))
                state["head"] = gittools.git(Path(worktree), "rev-parse", "HEAD")
            event.text = json.dumps(state)
        event.text = CONTROL.sub("", event.text)
        self.store.event(self.key, event)
        self.callback(event)

    @contextlib.contextmanager
    def lock(self):
        with (self.store.home / "worker.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Another Brahma worker is active; wait or stop it first") from error
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    async def ask(self, request: str) -> Plan:
        from . import engine
        self.cancel.clear()
        self.emit(Event(role="user", kind="user", text=request))
        history = [r["text"] for r in self.store.events(self.key) if r["kind"] == "user"]
        saved = self.store.session(self.key)
        if saved["status"] == "ready" and self.plan:
            history = ["Previously completed (background, do not redo): " + self.plan.summary, request]
        brief = "\n\nUser message:\n".join(history)
        if self.plan and self.plan.questions:
            brief += "\nOutstanding questions:\n" + "\n".join(self.plan.questions)
        if len(brief) > self.settings.context_chars // 2:
            raise ValueError("Conversation brief is too large; use /new with a concise goal. History is preserved")
        with self.lock():
            active = Path(saved["worktree"]) if saved["worktree"] else self.repo
            plan = await engine.plan(brief, active, self.settings, self.emit, self.cancel)
        self.set_plan(plan)
        self.emit(Event(kind="plan", text=json.dumps(self.plan.to_dict(), ensure_ascii=False)))
        return self.plan

    def set_plan(self, plan: Plan):
        from .contracts import prepare_contract, write_contract
        from .discovery import discover
        self.settings = Settings.load(self.repo)
        self.plan = plan
        self.generation = uuid.uuid4().hex
        contract = None
        if not plan.questions:
            checks = plan.checks or self.settings.checks
            candidate = prepare_contract(plan, self.repo, checks, task_timeout=self.settings.task_timeout,
                                         repair_cycles=self.settings.repair_cycles,
                                         definitions=discover(self.repo).entries,
                                         model_profiles=asdict(self.settings))
            contract = str(write_contract(self.store.home / "contracts" / self.key, candidate))
        self.store.finish_job(self.key, "cancelled")
        self.store.set(self.key, plan=json.dumps(plan.to_dict()), status="awaiting_approval", stopped=0,
                       contract=contract, approved_digest=None)
        self.emit(Event(kind="plan_created", text=self.generation))

    def approved_contract(self):
        from .contracts import read_contract, verify_contract
        from .discovery import discover
        saved = self.store.session(self.key)
        if not saved["contract"]:
            raise ValueError("This checkpoint has no locked contract; create and approve a new plan")
        contract = read_contract(Path(saved["contract"]))
        # Reload settings on every dispatch: configuration changes require approval.
        fresh = Settings.load(self.repo)
        checks = self.plan.checks or fresh.checks
        catalog = discover(self.repo)
        verify_contract(contract, self.plan, self.repo, checks, task_timeout=fresh.task_timeout,
                        repair_cycles=fresh.repair_cycles, definitions=catalog.entries,
                        model_profiles=asdict(fresh))
        return contract, replace(fresh, checks=checks), catalog

    def stop(self):
        self.store.stop(self.key)
        self.cancel.set()

    def queue_resume(self):
        saved = self.store.session(self.key)
        if (saved["stopped"] or not saved["approved_digest"] or
                saved["status"] not in ("cancelled", "interrupted")):
            return
        contract, _, _ = self.approved_contract()
        if saved["approved_digest"] != contract["digest"]:
            return
        self.store.enqueue(self.key, self.generation, contract["digest"], time.time() + 1,
                           "Terminal detached; continue approved checkpoint")

    async def execute(self, *, scheduled: bool = False) -> Result:
        from . import engine
        from .scheduler import Capacity
        if self.plan is None or self.plan.questions:
            raise ValueError("Resolve plan questions before approval")
        if self.store.session(self.key)["status"] == "ready":
            raise ValueError("This plan is already complete; use /new for another goal")
        contract, settings, catalog = self.approved_contract()
        settings._definition_catalog = catalog.entries
        settings._shared_contract = contract
        self.settings = settings
        if scheduled:
            saved = self.store.session(self.key)
            if saved["stopped"] or saved["approved_digest"] != contract["digest"]:
                raise ValueError("Scheduled approval is no longer valid")
        else:
            self.store.set(self.key, stopped=0, approved_digest=contract["digest"])
        self.cancel.clear()
        with self.lock():
            worktree = gittools.create_worktree(self.repo, self.store.home, self.key)
            self.store.set(self.key, worktree=str(worktree), status="running")
            self.emit(Event(kind="approved", text="Approved contract " + contract["digest"] + " in branch brahma/" + self.key))
            # Only fully verified task checkpoints are skipped; interrupted work stays for re-review.
            completed = set()
            progress = {}
            current = gittools.snapshot_changes(worktree)
            head = gittools.git(worktree, "rev-parse", "HEAD")
            current_index = None
            for event in self.store.events(self.key):
                if event["kind"] == "native_session" and current_index in progress and event["role"] in ("builder", "reviewer"):
                    progress[current_index][event["role"] + "_session"] = event["text"]
                if event["kind"] == "task_state":
                    try:
                        state = json.loads(event["text"])
                    except (ValueError, TypeError):
                        continue
                    index = state.get("index")
                    if (state.get("generation") != self.generation or type(index) is not int or
                            not 1 <= index <= len(self.plan.tasks) or
                            self.plan.tasks[index - 1].title != state.get("title")):
                        continue
                    current_index = index
                    if "deadline_epoch" in state:
                        progress[index] = state
                    # A newer attempt invalidates an earlier ready receipt even if
                    # no file changed. Only the latest task state may authorize a skip.
                    completed.discard(index)
                    if state.get("state") == "ready" and state.get("head") == head:
                        snapshot = state.get("snapshot", {})
                        owned = self.plan.tasks[index - 1].files
                        relevant = {p for p in set(snapshot) | set(current)
                                    if any(fnmatch.fnmatchcase(p, glob) for glob in owned)}
                        if all(snapshot.get(p) == current.get(p) for p in relevant):
                            completed.add(index)
                        else:
                            completed.discard(index)
            async def monitor_stop():
                while True:
                    if self.store.session(self.key)["stopped"]:
                        self.cancel.set()
                        return
                    await asyncio.sleep(.2)
            monitor = asyncio.create_task(monitor_stop())
            capacity = Capacity(self.store, self.cancel)
            async def gate(provider):
                self.approved_contract()
                if self.store.session(self.key)["stopped"]:
                    self.cancel.set()
                    return {"state": "unavailable", "next_at": None, "reason": "Stopped by user"}
                return await capacity(provider)
            try:
                result = await engine.execute(self.plan, worktree, self.settings, self.emit, self.cancel,
                                              completed_indices=completed, task_progress=progress, provider_gate=gate)
                if result.status == "ready":
                    ok, message = gittools.ownership_changes(worktree, [p for t in self.plan.tasks for p in t.files])
                    if not ok:
                        result = Result("brahma", "blocked", message)
                    elif self.settings.auto_commit:
                        commit = gittools.commit_changes(worktree, self.plan.summary)
                        result.text += "\nLocal commit: " + commit
                    if result.status == "ready" and self.settings.publish:
                        try:
                            result.text += "\nDraft PR: " + gittools.publish(worktree, self.plan.summary, result.text)
                        except ValueError as error:
                            self.emit(Event(kind="publication", text=str(error)))
                # A durable stop takes precedence over an automatic restart.
                if self.store.session(self.key)["stopped"]:
                    result = Result("brahma", "cancelled", "Stopped by user; checkpoint retained", result.usage)
                self.store.set(self.key, status=result.status)
                if result.status == "waiting":
                    waits = [json.loads(e["text"]) for e in self.store.events(self.key) if e["kind"] == "wait"]
                    latest = waits[-1] if waits else {}
                    next_at = latest.get("next_at")
                    if next_at is not None and next_at <= time.time():
                        next_at = None  # Never spin on an expired/unknown reset.
                    self.store.enqueue(self.key, self.generation, contract["digest"], next_at,
                                       latest.get("reason", "Waiting for observed provider capacity"))
                else:
                    self.store.finish_job(self.key)
                self.emit(Event(kind="summary", text=f"{result.status}: {result.text}", usage=result.usage))
                return result
            except BaseException:
                self.store.set(self.key, status="interrupted")
                raise
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
