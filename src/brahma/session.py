from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import sqlite3
import subprocess
from pathlib import Path
import re
import uuid
import time
from dataclasses import asdict, replace

from . import safety, ownership
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
        if event.kind == "disagreement":
            self.store.attention(self.key, "decision", "Disputed high-severity review finding: decide with `brahma decide "
                                 + self.key + " --for reviewer|author`")
        if event.kind == "outcome":
            self._record_outcome(json.loads(event.text))
        if event.kind == "check":
            self._record_check_failure(event.text)
        if event.kind == "task_state":
            state = json.loads(event.text)
            state["generation"] = self.generation
            if state.get("state") == "ready":
                worktree = self.store.session(self.key)["worktree"]
                state["snapshot"] = gittools.snapshot_changes(Path(worktree), self.settings.ignored_allowlist)
                state["head"] = gittools.git(Path(worktree), "rev-parse", "HEAD")
            event.text = json.dumps(state)
        event.text = CONTROL.sub("", event.text)
        self.store.event(self.key, event)
        self.callback(event)
        if event.provider in ("codex", "claude") and event.kind not in ("enforcement", "usage", "quota", "task_state", "wait"):
            # Worker output is data. If it tries to direct Brahma, show the user a redacted flag; never act on it.
            flags = safety.flag_embedded_instructions(event.text)
            if flags:
                note = Event("brahma", "safety", "flagged_instruction",
                             "Embedded instruction in worker output (ignored, not acted on): " +
                             "; ".join(f"{f.kind}: \u201c{f.excerpt}\u201d" for f in flags[:5]))
                self.store.event(self.key, note)
                self.callback(note)

    def _notes(self):
        """Notes Abhinav left for this session; applied by the engine at its next checkpoint, never mid-run."""
        from .briefs import Note
        notes = []
        events = self.store.events_after(self.key, 0, 5000)
        floor = max([e["seq"] for e in events if e["kind"] == "plan_created"], default=0)   # notes belong to one plan
        for event in events:
            if event["kind"] != "user_note" or event["seq"] < floor:
                continue
            try:
                data = json.loads(event["text"])
                notes.append(Note(event["seq"], data.get("task") if type(data.get("task")) is int else None, str(data["text"])))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        return notes

    def _memory(self):
        from .memory import Memory, repo_id
        return Memory(self.store), repo_id(self.repo)

    def _record_outcome(self, data: dict) -> None:
        """Measured result of one task into per-repository routing history (never blocks the run)."""
        try:
            memory, repo = self._memory()
            memory.record_outcome(repo, provider=data["provider"], task_type=data["task_type"],
                                  checks_passed=data["checks_passed"], repairs=data["repairs"], findings=data["findings"],
                                  ready=data["ready"], session=self.key, task_index=data["index"], seconds=data["seconds"],
                                  usage_measured=data.get("usage_measured"))
        except (ValueError, KeyError, OSError, sqlite3.Error):
            pass

    def _record_check_failure(self, text: str) -> None:
        """A check that exited non-zero is remembered so a repair can be proposed (never started) later."""
        match = re.search(r"^(.*?) => exit (\d+)", text, re.M)
        if not match or match.group(2) == "0":
            return
        try:
            memory, repo = self._memory()
            memory.remember(repo, "failure", "check:" + match.group(1)[:150], text[:600])
        except (ValueError, OSError, sqlite3.Error):
            pass

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

    async def discover_model_profiles(self) -> dict:
        from .model_catalog import discover_models, select_profiles
        settings = Settings.load(self.repo)
        catalogs = await asyncio.gather(*(discover_models(name, self.cancel)
                                          for name in ("codex", "claude")))
        if self.cancel.is_set():
            raise ValueError("Model discovery cancelled")
        return select_profiles(settings, {row["provider"]: row for row in catalogs})

    @staticmethod
    def selected_settings(settings: Settings, selection: dict | None) -> Settings:
        if selection is None:
            return settings
        names = ("codex_model", "codex_critical_model", "claude_model", "claude_critical_model")
        configured, selected = selection.get("configured"), selection.get("selected")
        if (configured != {name: getattr(settings, name) for name in names}
                or not isinstance(selected, dict) or set(selected) != set(names)
                or any(value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200)
                       for value in selected.values())):
            raise ValueError("Model settings changed; discover and approve a new plan")
        result = replace(settings, **{name: value or getattr(settings, name) for name, value in selected.items()})
        result._model_selection = selection
        return result

    async def ask(self, request: str) -> Plan:
        from . import engine
        if self.store.killed:
            raise ValueError("Kill switch engaged: no planner starts. Lift it with `brahma kill --release`.")
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
            from .scheduler import Capacity
            monitor = asyncio.create_task(self._watch_halt(since=saved))
            try:
                selection = await self.discover_model_profiles()
                settings = self.selected_settings(Settings.load(self.repo), selection)
                plan = await engine.plan(brief, active, settings, self.emit, self.cancel,
                                         provider_gate=Capacity(self.store, self.cancel))
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            reason = self._new_halt(saved)
            if reason:
                raise ValueError(reason + " during planning; no plan was recorded")
        self.set_plan(plan, model_selection=selection)
        self.emit(Event(kind="plan", text=json.dumps(self.plan.to_dict(), ensure_ascii=False)))
        return self.plan

    def _toolchain_basis(self) -> tuple[Path | None, str]:
        """Where to fingerprint the toolchain from. A follow-on plan uses the existing worktree only when `git status` is clean (committed
        content); a dirty worktree may hold unreviewed worker edits, so the repository is used. Git-ignored files are not seen by that
        status; they are still verified against this fingerprint immediately before every check, and a mismatch refuses the check."""
        tree = self.store.session(self.key)["worktree"]
        if tree:
            try:
                if not gittools.status_paths(Path(tree)):
                    return Path(tree), "worktree"
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
        return None, "repo"

    def set_plan(self, plan: Plan, *, model_selection: dict | None = None):
        from .contracts import prepare_contract, write_contract
        from .discovery import discover
        settings = Settings.load(self.repo)
        profiles = asdict(settings)
        settings = self.selected_settings(settings, model_selection)
        if model_selection is not None:
            profiles["selection"] = model_selection
        catalog = discover(self.repo)
        contract = None
        basis_directory, basis_label = self._toolchain_basis()       # decided once for this plan
        if not plan.questions:
            checks = plan.checks or settings.checks
            candidate = prepare_contract(plan, self.repo, checks, task_timeout=settings.task_timeout,
                                         repair_cycles=settings.repair_cycles,
                                         definitions=catalog.entries,
                                         model_profiles=profiles,
                                         base_revision=(gittools.git(Path(self.store.session(self.key)["worktree"]), "rev-parse", "HEAD")
                                                        if self.store.session(self.key)["worktree"] else None),
                                         # A follow-on plan runs in the existing worktree, so its toolchain is read from there.
                                         toolchain_directory=basis_directory, toolchain_basis=basis_label)
            # Check required context before asking for approval; never silently truncate it.
            from .engine import _task_context, _brief, _BOUNDARIES
            settings._definition_catalog = catalog.entries
            settings._shared_contract = candidate
            for task in plan.tasks:
                builders = ("codex", "claude") if task.provider == "auto" else (task.provider,)
                for builder in builders:
                    reviewer = "claude" if builder == "codex" else "codex"
                    for provider, role in ((builder, "builder"), (reviewer, "reviewer")):
                        required = _BOUNDARIES + _brief(task) + _task_context(self.repo, task, settings, provider, role)
                        if len(required) > settings.context_chars:
                            raise ValueError("Required contract context exceeds budget before approval; split this plan")
            contract = str(write_contract(self.store.home / "contracts" / self.key, candidate))
        self.settings = settings
        self.plan = plan
        self.generation = uuid.uuid4().hex
        self.store.finish_job(self.key, "cancelled")
        self.store.set(self.key, plan=json.dumps(plan.to_dict()), status="awaiting_approval", stopped=0,
                       contract=contract, approved_digest=None)
        for stale in self.store.open_attention(self.key):   # attention belongs to the plan generation that raised it
            self.store.resolve_attention(stale["id"])
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
        profiles = asdict(fresh)
        selection = contract["payload"]["models"].get("selection")
        selected = self.selected_settings(fresh, selection)
        if selection is not None:
            profiles["selection"] = selection
        checks = self.plan.checks or fresh.checks
        catalog = discover(self.repo)
        basis = (contract["payload"].get("toolchain") or {}).get("basis", "repo")
        verify_contract(contract, self.plan, self.repo, checks, task_timeout=fresh.task_timeout,
                        repair_cycles=fresh.repair_cycles, definitions=catalog.entries,
                        model_profiles=profiles, base_revision=contract["payload"]["base_revision"],
                        toolchain_directory=Path(saved["worktree"]) if basis == "worktree" and saved["worktree"] else None,
                        toolchain_basis=basis)
        selected.checks = checks
        selected.bound_toolchain = contract["payload"].get("toolchain")    # re-verified before every check
        return contract, selected, catalog

    def stop(self):
        self.store.stop(self.key)
        self.cancel.set()

    def pause(self):
        """Distinct from stop: the approval and queued job survive; `resume` lets them run again."""
        self.store.pause(self.key)
        self.cancel.set()

    def _new_halt(self, since: dict) -> str | None:
        """A halt that applies to planning: the kill switch, or a stop/pause issued after planning began (a stop left from
        an earlier plan does not block asking for a new one)."""
        if self.store.killed:
            return "Kill switch engaged"
        now = self.store.session(self.key)
        if now["stopped"] and not since["stopped"]:
            return "Stopped by user"
        if now["paused"] and not since["paused"]:
            return "Paused by user"
        return None

    async def _watch_halt(self, since: dict, interval: float = .2) -> None:
        """Durable controls from any process (`brahma kill|stop|pause`) cancel the planner within moments."""
        while True:
            if self._new_halt(since):
                self.cancel.set()
                return
            await asyncio.sleep(interval)

    def halted(self) -> str | None:
        """Why work must not continue right now, or None."""
        if self.store.killed:
            return "Kill switch engaged"
        saved = self.store.session(self.key)
        if saved["stopped"]:
            return "Stopped by user"
        if saved["paused"]:
            return "Paused by user"
        return None

    def confirm_capacity(self, provider: str):
        if provider not in ("claude", "codex"):
            raise ValueError("Choose claude or codex")
        from .quota import unknown
        saved = self.store.session(self.key)
        if saved["status"] != "waiting" or saved["stopped"]:
            raise ValueError("Capacity confirmation only resumes an approved waiting session; stopped tasks require approval")
        contract, _, _ = self.approved_contract()
        if saved["approved_digest"] != contract["digest"]:
            raise ValueError("This contract has no current approval")
        waits = [json.loads(e["text"]) for e in self.store.events(self.key) if e["kind"] == "wait"]
        if not waits or waits[-1].get("provider") != provider:
            raise ValueError("This session is waiting for a different provider")
        self.store.observe(unknown(provider, "user.confirmed"))
        self.emit(Event(role="user", kind="capacity_confirmed", text="User confirms capacity to retry " + provider + "; remaining allowance still unmeasured"))
        self.store.enqueue(self.key, self.generation, contract["digest"], time.time(), "User confirmed provider capacity; one bounded continuation")

    def queue_resume(self):
        saved = self.store.session(self.key)
        if (saved["stopped"] or saved["paused"] or not saved["approved_digest"] or
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
        if self.store.killed:
            raise ValueError("Kill switch engaged: no work starts. Release it with `brahma kill --release`")
        if self.plan is None or self.plan.questions:
            raise ValueError("Resolve plan questions before approval")
        if self.store.session(self.key)["status"] == "ready":
            raise ValueError("This plan is already complete; use /new for another goal")
        contract, settings, catalog = self.approved_contract()
        settings._definition_catalog = catalog.entries
        settings._shared_contract = contract
        settings._session_key = self.key
        settings._packet_home = self.store.home
        settings._notes = self._notes      # a loader: the engine re-reads notes at each attempt
        try:
            memory, repo = self._memory()
            settings._routing_history = memory.routing_history(repo)
        except (ValueError, sqlite3.Error):
            settings._routing_history = {}
        self.settings = settings
        if scheduled:
            saved = self.store.session(self.key)
            if saved["stopped"] or saved["paused"] or saved["approved_digest"] != contract["digest"]:
                raise ValueError("Scheduled approval is no longer valid")
        else:
            self.store.set(self.key, stopped=0, paused=0, approved_digest=contract["digest"])
        self.cancel.clear()
        with self.lock():
            worktree = gittools.create_worktree(self.repo, self.store.home, self.key, contract["payload"]["base_revision"])
            if gittools.git(worktree, "rev-parse", "HEAD") != contract["payload"]["base_revision"]:
                raise ValueError("Worktree base changed outside this approval; review a new plan")
            revealed = gittools.clear_index_flags(worktree)
            if revealed:
                self.emit(Event(kind="enforcement", text="Cleared Git index flags that hid changes to: " + ", ".join(revealed[:20])
                                + ". Those changes are now visible to ownership verification."))
            identity = gittools.git_identity(worktree)
            if (identity["hidden"] or identity["branch"] != "brahma/" + self.key
                    or not Path(identity["git_dir"]).resolve().is_relative_to(Path(identity["common_dir"]).resolve() / "worktrees")):
                raise ValueError("Worktree Git identity does not match this session; it was not run")
            self.settings._git_identity = identity
            self.store.set(self.key, worktree=str(worktree), status="running")
            self.emit(Event(kind="approved", text="Approved contract " + contract["digest"] + " in branch brahma/" + self.key))
            # Only fully verified task checkpoints are skipped; interrupted work stays for re-review.
            completed = set()
            progress = {}
            current = gittools.snapshot_changes(worktree, self.settings.ignored_allowlist)
            head = gittools.git(worktree, "rev-parse", "HEAD")
            current_index = None
            decisions: dict[int, str] = {}
            for event in self.store.events(self.key):
                if event["kind"] == "dispute_decision":
                    try:
                        said = json.loads(event["text"])
                        if said.get("for") in ("reviewer", "author") and type(said.get("index")) is int:
                            decisions[said["index"]] = (said["for"], said.get("digest"))
                    except (ValueError, TypeError, AttributeError):
                        pass
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
                                    if any(ownership.owns(glob, p) for glob in owned)}
                        if all(snapshot.get(p) == current.get(p) for p in relevant):
                            completed.add(index)
                        else:
                            completed.discard(index)
            for index, (side, digest) in decisions.items():
                # A decision applies only to the exact dispute it was made on (same positions), in this plan generation.
                waiting = (progress.get(index) or {}).get("dispute")
                if waiting and waiting.get("state") == "escalated" and digest and digest == waiting.get("digest"):
                    progress[index]["dispute_decision"] = side
            async def monitor_stop():
                while True:
                    if self.halted():
                        self.cancel.set()
                        return
                    await asyncio.sleep(.2)
            monitor = asyncio.create_task(monitor_stop())
            capacity = Capacity(self.store, self.cancel)
            async def gate(provider):
                self.approved_contract()
                reason = self.halted()
                if reason:
                    self.cancel.set()
                    return {"state": "unavailable", "next_at": None, "reason": reason}
                return await capacity(provider)
            try:
                result = await engine.execute(self.plan, worktree, self.settings, self.emit, self.cancel,
                                              completed_indices=completed, task_progress=progress, provider_gate=gate)
                if result.status == "ready" and self.halted():
                    result = Result("brahma", "cancelled", self.halted() + "; checkpoint retained", result.usage)
                if result.status == "ready":
                    ok, message = gittools.ownership_changes(worktree, [p for t in self.plan.tasks for p in t.files],
                                                             ignored_allow=self.settings.ignored_allowlist, identity=identity)
                    if not ok:
                        result = Result("brahma", "blocked", message)
                    elif self.settings.auto_commit:
                        commit = gittools.commit_changes(worktree, self.plan.summary, identity)
                        result.text += "\nLocal commit: " + commit
                    if result.status == "ready" and self.settings.publish and not self.halted():
                        try:
                            result.text += "\nDraft PR: " + gittools.publish(worktree, self.plan.summary, result.text)
                        except ValueError as error:
                            self.emit(Event(kind="publication", text=str(error)))
                # A durable stop takes precedence over an automatic restart.
                reason = self.halted()
                if reason:
                    result = Result("brahma", "cancelled", reason + "; checkpoint retained", result.usage)
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
