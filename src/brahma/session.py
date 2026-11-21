from __future__ import annotations

import asyncio
import threading
import contextlib
import fcntl
import os
import json
import math
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


# Bidi overrides, zero-width and other format characters (Unicode Cf) can make a line read differently from what it holds.
def displayable(text: str) -> str:
    """One line safe to show a person who is about to approve it: escapes and controls (C0, C1, DEL), line breaks, tabs and
    Unicode format characters are removed, so no field can fake another line or reorder what is shown."""
    import unicodedata
    text = CONTROL.sub("", text)
    return "".join(ch for ch in text if not (unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") or "\x80" <= ch <= "\x9f"))


CONTROL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))|[\x00-\x08\x0b-\x1f\x7f]")


def approval_view(contract: dict) -> list[str]:
    """Everything a person must see before approving, rendered from the locked contract itself (not from session state),
    one sanitised line per item. Used by the CLI and the terminal UI alike."""
    from . import toolchain
    from .config import run_settings_line
    payload = contract["payload"]
    plan = Plan.parse(json.dumps(payload["plan"]))
    lines = [f"Contract {contract['digest']}", f"Repository: {payload['repository']}",
             f"Base revision: {payload['base_revision']}", "Goal: " + plan.summary]
    for number, task in enumerate(plan.tasks, 1):
        lines += [f"{number}. {task.title} [{task.provider}/{task.complexity}]", "   Owns: " + ", ".join(task.files),
                  "   Acceptance: " + "; ".join(task.acceptance), "   Instructions to the worker:"]
        lines += ["      " + line for line in task.instructions.splitlines() if line.strip()]
        for kind, ids in (("skills", task.allowed_skills), ("agents", task.allowed_agents)):
            if ids:
                lines.append(f"   Loads {kind}: " + ", ".join(ids))
    for identifier, entry in sorted(payload.get("definitions", {}).items()):
        lines.append(f"Definition {identifier}: {entry['scope']} {entry['path']} sha256:{entry['sha256'][:16]}")
    instructions = payload.get("instructions", {})
    lines.append("Repository instructions: " + (", ".join(sorted(instructions)) if instructions else "none"))
    lines.append("Gherkin feature:")
    lines += ["   " + line for line in payload["feature"].splitlines() if line.strip()]
    lines.append("Checks: " + (json.dumps(payload["checks"]) if payload["checks"] else "none (cannot become ready)"))
    lines += toolchain.describe(payload.get("toolchain", {}))
    models = payload["models"]
    lines.append(run_settings_line(models))
    risky = sorted({c[0].rsplit("/", 1)[-1] for c in payload["checks"]
                    if c and c[0].rsplit("/", 1)[-1] in ("sh", "bash", "zsh", "curl", "wget", "nc", "ssh", "scp")})
    if risky:
        lines.append("WARNING: approved checks run these shell or network programs on this computer: " + ", ".join(risky))
    selection = models.get("selection")
    for provider in ("codex", "claude"):
        for suffix, purpose in (("_model", "routine"), ("_critical_model", "critical")):
            field = provider + suffix
            detail = selection["profiles"][field] if selection else None
            chosen = detail["selected"] if detail else models.get(field)
            state = ("advertised" if detail and detail["verified"] else
                     "unavailable" if detail and detail["available"] is False else "unverified")
            lines.append(f"Model: {provider} {purpose} = {chosen or 'unavailable'} ({state})")
    budget = payload["task_budget"]
    lines.append(f"Budget: {budget['active_seconds'] / 60:g} active minutes per task, at most {budget['repair_cycles']} "
                 "repairs; quota waiting excluded")
    return [safety.redact(displayable(line)) for line in lines]


def verification_charges(events: list[dict], generation: str) -> dict[int, float]:
    """Resume-verification seconds per task not yet reflected in its latest budget checkpoint. A task checkpoint that
    carries a budget starts from the already charged time, so only charges recorded after it still apply: each
    verification is deducted exactly once, whether the task stays complete or is worked on again."""
    charges: dict[int, float] = {}
    for event in events:
        if event["kind"] not in ("verification_spent", "task_state"):
            continue
        try:
            row = json.loads(event["text"])
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict) or row.get("generation") != generation or type(row.get("index")) is not int:
            continue
        if event["kind"] == "task_state":
            if "deadline_epoch" in row:
                charges.pop(row["index"], None)
        elif isinstance(row.get("seconds"), (int, float)) and not isinstance(row["seconds"], bool) and math.isfinite(row["seconds"]):
            charges[row["index"]] = charges.get(row["index"], 0.0) + max(0.0, row["seconds"])
    return charges


FINISH_WAIT = 180      # seconds a cancellation waits for a final step in flight


async def _finish_in_thread(step):
    """Run a blocking step in a thread. A cancellation waits for the step to finish (up to FINISH_WAIT, so quitting can
    never hang on a stalled step) before it continues, so a commit does not land after the run has unwound and released
    its worktree lock. The step is carried as `finished` (None if it was still running), so the caller can record it."""
    task = asyncio.ensure_future(asyncio.to_thread(step))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        deadline = time.monotonic() + FINISH_WAIT
        while not task.done() and time.monotonic() < deadline:
            try:
                await asyncio.wait({task}, timeout=deadline - time.monotonic())
            except asyncio.CancelledError:
                pass
        cancelled.finished = task if task.done() else None
        raise


def _best_effort(record) -> None:
    """A record made while an exception or cancellation propagates: a failure here must never replace it."""
    try:
        record()
    except Exception:
        pass

class Session:
    def __init__(self, store: Store, repo: Path, emit=lambda event: None, key: str | None = None):
        self.store, self.repo, self.callback = store, gittools.repository(repo), emit
        self.key = key or store.create(self.repo)
        saved = store.session(self.key)
        if Path(saved["repo"]).resolve() != self.repo:
            raise ValueError("Session belongs to another repository")
        self.settings = Settings.load(self.repo)
        self.cancel = asyncio.Event()
        # Set only when Brahma is closing (terminal UI quit, service stop) and never cleared. Unlike `cancel`, which a
        # pause, kill or stop also sets and which can therefore be stale, it means exactly one thing; publication
        # (which runs in a thread and must not start or continue while closing) reads only this.
        self.closing = threading.Event()
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
        if event.kind == "verification_spent":
            event.text = json.dumps(dict(json.loads(event.text), generation=self.generation))
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

    def _decisions(self) -> dict[int, tuple[str, str | None]]:
        """The person's dispute decisions that still apply: each is bound to this plan generation and to the newest
        escalation of its task, so an earlier decision can never settle a later or another plan's dispute."""
        decisions: dict[int, tuple[str, str | None]] = {}
        escalations: dict[int, int] = {}          # the newest dispute escalation per task, in this plan only
        plan_floor = self.store.latest_seq(self.key, "plan_created")
        for event in self.store.events_since(self.key, 1):
            if event["kind"] == "disagreement" and event["seq"] >= plan_floor:
                try:
                    escalated = json.loads(event["text"]).get("index")
                except (ValueError, TypeError, AttributeError):
                    escalated = None
                if type(escalated) is int:
                    escalations[escalated] = event["seq"]
                    decisions.pop(escalated, None)          # a new escalation voids any earlier decision for the task
            if event["kind"] == "dispute_decision":
                try:
                    said = json.loads(event["text"])
                    index = said.get("index")
                    if (said.get("for") in ("reviewer", "author") and type(index) is int
                            and said.get("generation") == self.generation
                            and type(said.get("escalation")) is int and said["escalation"] == escalations.get(index)):
                        decisions[index] = (said["for"], said.get("digest"))
                except (ValueError, TypeError, AttributeError):
                    pass
        return decisions

    def _notes(self):
        """Notes Abhinav left for this session; applied by the engine at its next checkpoint, never mid-run."""
        from .briefs import Note
        notes = []
        floor = self.store.latest_seq(self.key, "plan_created")     # notes belong to one plan, however long the history
        events = self.store.events_since(self.key, floor)
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
        """The one-worker lock. A coordinator that died leaves its lock free but its worker groups may survive it: those are
        stopped (procs.reap) before anything new starts, and if one cannot be stopped nothing starts."""
        from . import procs
        descriptor = os.open(self.store.home / "worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(descriptor)
            raise ValueError("Another Brahma worker is active; wait or stop it first") from error
        try:
            _, survivors = procs.reap(self.store.home)
            if survivors:
                raise ValueError("A worker left by a coordinator that stopped is still running (process groups "
                                 + ", ".join(map(str, survivors)) + ") and could not be stopped; nothing was started")
            procs.holding(self.store.home)
            try:
                yield
            finally:
                procs.holding(None)
        finally:
            os.close(descriptor)

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
            saved = dict(saved, control=self.store.last_control(self.key))
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
        from .contracts import prepare_contract, refuse_credentials, write_contract
        from .discovery import discover
        settings = Settings.load(self.repo)
        # Before anything is stored: a proposal with open questions is saved and its questions go into the next
        # planner prompt, so it is checked exactly like one that is ready for approval.
        refuse_credentials(plan, plan.checks or settings.checks)
        profiles = asdict(settings)
        settings = self.selected_settings(settings, model_selection)
        if model_selection is not None:
            profiles["selection"] = model_selection
        catalog = discover(self.repo)
        contract = None
        basis_directory, basis_label = self._toolchain_basis()       # decided once for this plan
        if not plan.questions:
            checks = plan.checks or settings.checks
            existing = self.store.session(self.key)["worktree"]
            candidate = prepare_contract(plan, self.repo, checks, task_timeout=settings.task_timeout,
                                         repair_cycles=settings.repair_cycles,
                                         definitions=catalog.entries,
                                         model_profiles=profiles,
                                         base_revision=gittools.git(Path(existing), "rev-parse", "HEAD") if existing else None,
                                         # A follow-on plan runs in the existing worktree: its instructions are read there.
                                         instruction_root=Path(existing) if existing else None,
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
                        # Context is checked in the checkout the run will use (the existing worktree for a follow-on plan).
                        required = _BOUNDARIES + _brief(task) + _task_context(Path(existing) if existing else self.repo,
                                                                              task, settings, provider, role)
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
        instruction_root = None
        if contract["payload"].get("instruction_basis") == "worktree":
            if not saved["worktree"]:
                raise ValueError("This approval reads instructions from a worktree that no longer exists; approve a new plan")
            instruction_root = Path(saved["worktree"])
        verify_contract(contract, self.plan, self.repo, checks, task_timeout=fresh.task_timeout,
                        repair_cycles=fresh.repair_cycles, definitions=catalog.entries,
                        model_profiles=profiles, base_revision=contract["payload"]["base_revision"],
                        instruction_root=instruction_root,
                        # Once this exact contract is approved, instruction files the plan owns may change in the run.
                        approved_instructions=(contract["payload"].get("instructions")
                                               if saved["approved_digest"] == contract["digest"] else None),
                        toolchain_directory=Path(saved["worktree"]) if basis == "worktree" and saved["worktree"] else None,
                        toolchain_basis=basis)
        selected.checks = checks
        selected.bound_toolchain = contract["payload"].get("toolchain")    # re-verified before every check
        return contract, selected, catalog

    def _approval_target(self, expected: str | None = None) -> tuple[dict, str]:
        if self.store.killed:
            raise ValueError("Kill switch engaged: nothing can be approved until it is lifted")
        if self.plan is None or self.plan.questions:
            raise ValueError("Resolve plan questions before approval")
        if self.store.session(self.key)["status"] == "ready":
            raise ValueError("This plan is already complete; use /new for another goal")
        contract, _, _ = self.approved_contract()
        if expected is not None and contract["digest"] != expected:
            raise ValueError("The contract changed after you read it; nothing was approved. Read it again.")
        # Only values Brahma controls go into the system prompt; the repository name is reduced to a safe, quoted label.
        label = re.sub(r"[^A-Za-z0-9._-]", "_", self.repo.name)[:40] or "repository"
        return contract, (f"approve contract {contract['digest'][:12]} ({len(self.plan.tasks)} task(s)) "
                          f"in repository '{label}'")

    def approval_summary(self) -> tuple[str, list[str]]:
        """(digest, lines) for the contract that approve() would approve now."""
        contract, _ = self._approval_target()
        return contract["digest"], approval_view(contract)

    def _record_approval(self, digest: str) -> str:
        again, _, _ = self.approved_contract()           # nothing may change while the prompt was open
        if again["digest"] != digest or self.store.killed:
            raise ValueError("The contract or the kill switch changed while approval was being confirmed; nothing was approved")
        self.store.set(self.key, stopped=0, paused=0, approved_digest=digest)
        job = self.store.job(self.key)
        if job and job["state"] == "held":
            self.store.finish_job(self.key, "superseded")
        self.emit(Event(role="user", kind="approval", text="Approved by the device owner: contract " + digest))
        return digest

    def approve(self, expected_digest: str | None = None) -> str:
        """Human-only: approve the current locked contract. The device owner confirms with Touch ID or the login password
        (presence.require); there is no other way to create an approval. Returns the approved digest."""
        from . import presence
        contract, reason = self._approval_target(expected_digest)
        presence.require(reason)
        return self._record_approval(contract["digest"])

    async def approve_async(self, expected_digest: str | None = None) -> str:
        """approve() for an event loop: only the system prompt waits in a thread; the database stays on this one."""
        from . import presence
        contract, reason = self._approval_target(expected_digest)
        await asyncio.to_thread(presence.require, reason)
        return self._record_approval(contract["digest"])

    def ready_receipt(self) -> dict:
        """The verified-ready result of the current approval, or ValueError. Publication is bound to it."""
        saved = self.store.session(self.key)
        if saved["status"] != "ready" or not saved["approved_digest"]:
            raise ValueError("Only a ready, verified plan can be published")
        if self.store.killed or saved["stopped"] or saved["paused"]:
            raise ValueError("Not published: the kill switch is engaged or the session is stopped or paused")
        receipts = []
        for event in self.store.events(self.key):
            if event["kind"] == "ready_receipt":
                try:
                    receipts.append(json.loads(event["text"]))
                except (ValueError, TypeError):
                    continue
        if not receipts or receipts[-1].get("digest") != saved["approved_digest"] or not isinstance(receipts[-1].get("head"), str):
            raise ValueError("No readiness receipt for the current approval; run and verify it again")
        if not receipts[-1].get("committed"):
            raise ValueError("The ready result was not committed (auto_commit is off); commit it yourself or enable auto_commit")
        if not isinstance(receipts[-1].get("uncommitted"), dict) or not isinstance(receipts[-1].get("ignored_allowlist"), list):
            raise ValueError("The readiness receipt does not record the verified ignored files; run and verify it again")
        return receipts[-1]

    def _publish_arguments(self, receipt: dict, body: str) -> tuple[tuple, threading.Event]:
        """The arguments of gittools.publish, and the event that abandons it. Read on the caller's thread: the store's
        SQLite connection belongs to the thread that opened it."""
        worktree = Path(self.store.session(self.key)["worktree"])
        home, key = self.store.home, self.key
        closing = self.closing
        abandoned = threading.Event()       # set when the awaiting task is cancelled

        def stopped() -> str | None:
            # Called from the publication thread: no use of this session's own database connection. Closing (a quit or
            # service stop, or the awaiting task cancelled) is checked first and needs no I/O, so it is seen at once
            # even when the state database is busy; then a durable stop, pause or kill switch.
            if abandoned.is_set() or closing.is_set():
                return "Cancelled (Brahma is closing)"
            return Store.halt_reason(home, key)
        return ((worktree, self.plan.summary, body, receipt["head"], receipt["uncommitted"], receipt["ignored_allowlist"],
                 stopped), abandoned)

    def publish_ready(self, receipt: dict, body: str) -> str:
        """Publish exactly what the readiness receipt verified: its commit, and its ignored and special files."""
        arguments, _ = self._publish_arguments(receipt, body)
        return gittools.publish(*arguments)

    async def publish_ready_in_thread(self, receipt: dict, body: str) -> str:
        """publish_ready for an event loop: the state is read here, only the Git/GitHub work runs in a thread."""
        arguments, abandoned = self._publish_arguments(receipt, body)
        try:
            return await asyncio.to_thread(gittools.publish, *arguments)
        except asyncio.CancelledError:
            abandoned.set()                 # the thread ends its running step at the next poll; nothing new starts
            raise

    def _job_held(self) -> bool:
        job = self.store.job(self.key)
        return bool(job and job["state"] == "held")

    def begin_closing(self):
        """Brahma is closing (terminal UI quit, service stop): not durable, never cleared on this object. Running work
        checkpoints; publication stops at its next check and starts nothing new."""
        self.closing.set()
        self.cancel.set()

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
        fresh = self.store.controls_after(self.key, since.get("control", 0))   # a repeated stop/pause also counts
        if now["stopped"] and ("stop" in fresh or not since["stopped"]):
            return "Stopped by user"
        if now["paused"] and ("pause" in fresh or not since["paused"]):
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
        # Running never approves: only Session.approve() (a person, confirmed by the OS) records an approval.
        saved = self.store.session(self.key)
        if saved["approved_digest"] != contract["digest"]:
            raise ValueError("Scheduled approval is no longer valid" if scheduled else
                             "This contract has not been approved by a person; approve it first (`brahma approve "
                             + self.key + "` or /approve)")
        if saved["stopped"] or saved["paused"]:
            raise ValueError("Scheduled approval is no longer valid" if scheduled else
                             "This session is stopped or paused; `brahma resume " + self.key + "`, or approve a new plan")
        if self._job_held() and not scheduled:
            raise ValueError("This session's work is held (pause or kill switch); `brahma resume " + self.key + "` queues it again")
        # Taken before any blocking step: a stop or pause recorded after this was made during the run.
        run_control = self.store.last_control(self.key)
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
            decisions = self._decisions()
            # One ledger: a resume verification is charged to the task's remaining approved time, once. A later
            # checkpoint of that task already starts from the charged budget, so only charges after it still apply.
            charges = verification_charges(self.store.events(self.key), self.generation)
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
                                    if any(ownership.owns(glob, p) for glob in owned)}
                        if all(snapshot.get(p) == current.get(p) and not gittools.unverifiable(current.get(p))
                               for p in relevant):
                            completed.add(index)
                        else:
                            completed.discard(index)
            for index, spent in charges.items():
                left = (progress.get(index) or {}).get("remaining_seconds")
                if isinstance(left, (int, float)) and not isinstance(left, bool) and math.isfinite(left):
                    progress[index] = dict(progress[index], remaining_seconds=max(0.0, left - spent))
            for index, (side, digest) in decisions.items():
                # A decision applies only to the exact dispute it was made on (same positions), in this plan generation.
                waiting = (progress.get(index) or {}).get("dispute")
                if waiting and waiting.get("state") == "escalated" and digest and digest == waiting.get("digest"):
                    progress[index]["dispute_decision"] = side
            seen_halt: list[str] = []
            async def monitor_stop():
                while True:
                    reason = self.halted()
                    if reason:
                        seen_halt.append(reason)
                        self.cancel.set()
                        return
                    await asyncio.sleep(.2)
            monitor = asyncio.create_task(monitor_stop())
            receipt = None      # the verified result, set as soon as it exists: from then on the run is complete
            outcome = None      # what publication did, once it was decided (the text recorded in the run)

            def halt_during_run() -> str | None:
                """A stop, pause or kill switch now in force or seen during this run, even if lifted since: the current
                halt, what the halt monitor saw, a job held by a kill switch release, and the durable stop/pause records
                (which cover the moments the monitor cannot sample). Recorded in seen_halt, so the end of the run holds
                or re-queues the work."""
                now = self.halted()
                if now and now not in seen_halt:
                    seen_halt.append(now)
                if not seen_halt:
                    if self._job_held():
                        seen_halt.append("Kill switch engaged")
                    else:
                        actions = self.store.controls_after(self.key, run_control)
                        if actions:
                            seen_halt.append("Stopped by user" if "stop" in actions else "Paused by user")
                return now or (seen_halt[0] if seen_halt else None)
            capacity = Capacity(self.store, self.cancel)
            async def gate(provider):
                self.approved_contract()
                reason = self.halted()
                if reason:
                    seen_halt.append(reason)
                    self.cancel.set()
                    return {"state": "unavailable", "next_at": None, "reason": reason}
                return await capacity(provider)
            try:
                result = await engine.execute(self.plan, worktree, self.settings, self.emit, self.cancel,
                                              completed_indices=completed, task_progress=progress, provider_gate=gate)
                if result.status == "ready" and self.halted():
                    result = Result("brahma", "cancelled", self.halted() + "; checkpoint retained", result.usage)
                if result.status == "ready":
                    # The final steps run in threads, so the halt monitor keeps sampling and the loop stays responsive.
                    # A cancellation waits for a step in flight to finish (_finish_in_thread): the run never unwinds,
                    # and releases its worktree lock, while a commit is still landing.
                    def final_checks():
                        owned = gittools.ownership_changes(worktree, [p for t in self.plan.tasks for p in t.files],
                                                           ignored_allow=self.settings.ignored_allowlist,
                                                           identity=identity)
                        try:
                            unchanged = result.tree is not None and engine._tree(worktree, self.settings) == result.tree
                        except (OSError, ValueError, subprocess.SubprocessError):
                            unchanged = False
                        return owned, unchanged
                    (ok, message), same = await _finish_in_thread(final_checks)
                    halt_reason = halt_during_run() if ok and same else None
                    if not ok:
                        result = Result("brahma", "blocked", message)
                    elif not same:
                        # Readiness covers exactly what was checked and reviewed; never commit or publish anything else.
                        result = Result("brahma", "blocked", "Files changed after they were checked and reviewed; "
                                        "nothing was committed. Resume to check and review them again.", result.usage)
                    elif halt_reason:
                        # A stop, pause or kill switch during the run (even if lifted since): nothing is committed.
                        result = Result("brahma", "cancelled", halt_reason + "; checkpoint retained", result.usage)
                    if result.status == "ready":
                        def commit_and_verify():
                            made = (gittools.commit_changes(worktree, self.plan.summary, identity)
                                    if self.settings.auto_commit else None)
                            # What was verified, bound to the exact commit: publication may push this and nothing else.
                            return made, {"digest": contract["digest"],
                                          "head": gittools.git(worktree, "rev-parse", "HEAD"),
                                          "committed": not gittools.status_paths(worktree),
                                          # Ignored and special files as verified: publication refuses if any changed.
                                          "uncommitted": gittools.snapshot_changes(worktree,
                                                                                   self.settings.ignored_allowlist),
                                          "ignored_allowlist": list(self.settings.ignored_allowlist)}
                        try:
                            made, receipt = await _finish_in_thread(commit_and_verify)
                        except asyncio.CancelledError as cancelled:
                            step = cancelled.finished
                            if step is None:
                                _best_effort(lambda: self.emit(Event(kind="error", text=(
                                    "Brahma closed while the final commit step was still running; check the worktree "
                                    f"({worktree}) before resuming"))))
                            elif not step.cancelled() and step.exception() is None:
                                made, receipt = step.result()   # landed while Brahma was closing: complete
                                if made:
                                    result.text += "\nLocal commit: " + made
                                _best_effort(lambda: self.emit(Event(kind="ready_receipt", text=json.dumps(receipt))))
                            elif not step.cancelled():
                                _best_effort(lambda: self.emit(Event(kind="error", text="The final commit failed while "
                                                                     "Brahma was closing: " + str(step.exception()))))
                            raise
                        if made:
                            result.text += "\nLocal commit: " + made
                        self.emit(Event(kind="ready_receipt", text=json.dumps(receipt)))
                    if receipt is not None and self.settings.publish:
                        try:
                            if not receipt["committed"]:
                                raise ValueError("Not published: the verified work is not committed (auto_commit is off)")
                            if self.closing.is_set():
                                raise ValueError("Brahma is closing; the verified commit is ready for /publish")
                            # A stop, pause or kill switch in force or seen during this run, even if lifted since, never
                            # leads to an external write.
                            seen = halt_during_run()
                            if seen:
                                raise ValueError(f"a halt was seen during this run ({seen}); the verified commit is "
                                                 "ready for /publish")
                            outcome = "Publishing: Brahma closed during publication. Check the remote branch and any " \
                                      "draft PR, then use /publish"
                            outcome = "Draft PR: " + await self.publish_ready_in_thread(receipt, result.text)
                            result.text += "\n" + outcome
                        except Exception as error:
                            # Any failure of publication is reported, never raised: the run is verified and committed,
                            # so it ends ready and /publish can try again. Only a cancellation propagates.
                            text = str(error) if type(error) is ValueError else \
                                f"Not published ({type(error).__name__}: {error}). Check the remote branch, then use /publish"
                            outcome = text if text.startswith("Not published") else "Not published: " + text
                            self.emit(Event(kind="publication", text=outcome))
                            result.text += "\n" + outcome
                # A durable stop takes precedence over an automatic restart, until the verified result exists: from then
                # on the run is complete (its commit may have moved the branch, so it could not be resumed), and a halt
                # only prevented its publication.
                reason = self.halted() if receipt is None else None
                if reason:
                    result = Result("brahma", "cancelled", reason + "; checkpoint retained", result.usage)
                self.store.set(self.key, status=result.status)
                stopped = self.store.session(self.key)["stopped"]
                if reason and not stopped:
                    # A pause or the kill switch keeps the approved continuation; only `resume` queues it again.
                    self.store.hold(self.key, self.generation, contract["digest"], reason + "; resume to continue")
                elif seen_halt and result.status == "cancelled" and not stopped:
                    if set(seen_halt) == {"Paused by user"} and not self._job_held():
                        # Only a pause was seen, and `resume` has already released it: continue as the person asked.
                        self.store.enqueue(self.key, self.generation, contract["digest"], time.time(),
                                           "Resumed after a pause")
                    else:
                        # A kill switch or stop was seen during this run: lifting it never restarts work; only
                        # `resume` does (a stopped session needs a new approval).
                        self.store.hold(self.key, self.generation, contract["digest"],
                                        f"{seen_halt[0]} during the run; resume to continue")
                elif result.status == "waiting":
                    waits = [json.loads(e["text"]) for e in self.store.events(self.key) if e["kind"] == "wait"]
                    latest = waits[-1] if waits else {}
                    next_at = latest.get("next_at")
                    if next_at is not None and next_at <= time.time():
                        next_at = None  # Never spin on an expired/unknown reset.
                    self.store.enqueue(self.key, self.generation, contract["digest"], next_at,
                                       latest.get("reason", "Waiting for observed provider capacity"))
                elif result.status == "ready":
                    self.store.close_finished_job(self.key)     # complete: closed even if held (`resume` must not re-run it)
                elif not self._job_held():
                    # Any other outcome keeps a hold decided during the run (a kill switch release): `resume` continues it.
                    self.store.finish_job(self.key)
                self.emit(Event(kind="summary", text=f"{result.status}: {result.text}", usage=result.usage))
                return result
            except BaseException as error:
                # Records made while something propagates are best effort: they must never replace it.
                if receipt is not None:
                    # The verified result exists: the run is complete and stays ready, so it is not requeued (it could not
                    # resume from its own commit) and /publish still works. Say only what is true.
                    ended = ("Brahma closed" if isinstance(error, asyncio.CancelledError)
                             else f"The run ended with {type(error).__name__}")
                    if outcome and outcome.startswith("Publishing:"):
                        note = outcome.replace("Publishing: ", "Not published: ", 1)      # closed while publishing
                    elif outcome:
                        note = f"{ended} after publication was decided ({outcome})"
                    elif not receipt["committed"]:
                        note = f"{ended} after the verified result was recorded; it is not committed, nothing was pushed"
                    elif self.settings.publish:
                        note = f"Not published: {ended} before publication; nothing was pushed. Use /publish"
                    else:
                        note = f"{ended} after the verified result was recorded"
                    _best_effort(lambda: self.store.set(self.key, status="ready"))
                    _best_effort(lambda: self.store.close_finished_job(self.key))
                    _best_effort(lambda: self.emit(Event(kind="publication", text=note)))
                    _best_effort(lambda: self.emit(Event(kind="summary", text=f"ready: {result.text}\n{note}",
                                                         usage=result.usage)))
                else:
                    _best_effort(lambda: self.store.set(self.key, status="interrupted"))
                raise
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
