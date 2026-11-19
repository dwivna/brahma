from __future__ import annotations

import asyncio
from collections import deque
import json
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from .config import Settings, state_home
from .gittools import repository
from .models import Event, Plan, Task
from .session import CONTROL, Session
from .store import Store
from .usage import format_usage, read_usage, usage_request


HELP = """Type your goal here. Brahma proposes a plan; /approve runs only that plan in an isolated branch.
/repo PATH   select a Git repository       /new   start a fresh conversation
/doctor      check signed-in CLIs           /status   checkpoint and reported usage
/usage [codex|claude]   inspect reported quota and resets without a model request
/models [codex|claude]  discover advertised models and task profiles without generation
/approve     execute the displayed plan     /stop or Escape   stop the active process group
/pause   hold   /unpause   release a held session   /kill   global kill switch
/note [N:] TEXT   guidance for the next checkpoint (never changes scope, ownership or checks)
/capacity claude|codex   confirm you checked capacity when no reset was reported
/skills or /agents   inspect available definitions
/sessions    show saved conversations       /resume ID   reopen a checkpoint
/publish     push the completed branch and create a draft PR (requires gh)
No automatic merges, releases or paid API fallback. Approved queues can continue through the local service after quit. /stop cancels future restarts.
Configure explicit test commands in brahma.toml. Without them a task stays needs_checks."""


class ConversationLog(RichLog):
    def on_resize(self, event):
        super().on_resize(event)
        # Pane resize is delivered after its own layout, including the stacked layout.
        self.call_after_refresh(self.app.redraw_log, self.id)


class BrahmaApp(App):
    TITLE = "BRAHMA"
    SUB_TITLE = "One conversation · two coding partners · one active worker"
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("escape", "stop", "Stop"), ("ctrl+n", "fresh", "New chat")]
    CSS = """
    Screen { background: #0b1220; color: #dbe8f5; }
    Header { background: #152238; color: #e8c27a; }
    #status { height: 2; padding: 0 1; color: #8da7bd; }
    #board { height: auto; max-height: 9; margin: 0 1; padding: 0 1; color: #b9d0e4; }
    #size-notice { display: none; height: 1fr; padding: 1; color: #efcc87; }
    Screen.too-small #size-notice { display: block; }
    Screen.too-small #manager, Screen.too-small #workers, Screen.too-small #controls { display: none; }
    #manager { height: 2fr; border: round #cda766; border-title-color: #f0d9a0; margin: 0 1; padding: 0 1; }
    #workers { height: 2fr; margin: 0 1; }
    #workers.narrow { layout: vertical; }
    #codex { width: 1fr; height: 1fr; border: round #529aca; border-title-color: #89c9f3; padding: 0 1; }
    #claude { width: 1fr; height: 1fr; border: round #a77e6c; border-title-color: #e9bba7; padding: 0 1; }
    #controls { height: 3; margin: 0 1; }
    #prompt { width: 1fr; border: round #435d75; }
    Button { min-width: 10; margin-left: 1; }
    Footer { background: #152238; }
    """

    def __init__(self, repo: Path | None = None, *, home: Path | None = None,
                 resume: str | None = None, demo: bool = False):
        super().__init__()
        self.repo = repo
        self.store = Store(home or state_home())
        self.session: Session | None = None
        self.resume_key = resume
        self.demo = demo
        self.demo_plan = None
        self.job: asyncio.Task | None = None
        self.inputs = 0
        self.outputs = 0
        self.have_usage = False
        self.checking_auth = False
        self.usage_job: asyncio.Task | None = None
        self.usage_cancel = asyncio.Event()
        self._seen_events = 0
        self._pane_entries = {key: deque(maxlen=200) for key in ("manager", "codex", "claude")}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Starting Brahma…", id="status")
        yield Static("", id="board", markup=False)
        yield Static("Terminal too small. Resize to at least 80×24. Escape stops; Ctrl+Q quits.",
                     id="size-notice", markup=False)
        yield ConversationLog(id="manager", min_width=1, wrap=True, markup=False, highlight=False, max_lines=1200)
        with Horizontal(id="workers"):
            yield ConversationLog(id="codex", min_width=1, wrap=True, markup=False, highlight=False, max_lines=800)
            yield ConversationLog(id="claude", min_width=1, wrap=True, markup=False, highlight=False, max_lines=800)
        with Horizontal(id="controls"):
            yield Input(placeholder="Describe your task, or /help", id="prompt")
            yield Button("Approve", id="approve", variant="primary", disabled=True)
            yield Button("Pause", id="pause")
            yield Button("Resume", id="resume")
            yield Button("Stop", id="stop", variant="warning")
        yield Footer()

    def on_mount(self):
        for key, title in (("manager", "A · BRAHMA — shared conversation"),
                           ("codex", "A1 · CODEX"), ("claude", "A2 · CLAUDE")):
            self.query_one("#" + key, RichLog).border_title = title
        try:
            if self.resume_key:
                saved = self.store.session(self.resume_key)
                self.select_repo(Path(saved["repo"]), self.resume_key)
            elif self.repo:
                self.select_repo(self.repo)
            else:
                self.write(Event(text="Select a repository with /repo PATH. No worker runs until a plan is approved."))
        except (ValueError, OSError) as error:
            self.write(Event(kind="error", text=str(error)))
        self.write(Event(text="Brahma is ready. " + HELP))
        if self.demo:
            self.write(Event(text="DEMO — simulated messages only; no provider requests or repository edits."))
            self.write(Event("codex", "builder", text="Ready for a bounded implementation assignment."))
            self.write(Event("claude", "reviewer", text="Ready for independent review after the builder finishes."))
        else:
            self.job = asyncio.create_task(self.handle("/doctor"))
        self.refresh_status()
        self.set_interval(1, self.poll_events)
        self.query_one("#prompt", Input).focus()

    def on_resize(self, event):
        matches = self.query("#workers")
        if len(matches):
            matches.first().set_class(event.size.width < 85, "narrow")
            self.screen.set_class(event.size.width < 80 or event.size.height < 24, "too-small")
            self.refresh_status()

    def redraw_log(self, target: str):
        log = self.query_one("#" + target, RichLog)
        log.clear()
        for entry in self._pane_entries[target]:
            log.write(entry)

    def write(self, event: Event, *, count_usage: bool = True):
        if count_usage and event.kind == "usage":
            self.have_usage = True
            self.inputs += int(event.usage.get("input_tokens", 0) or 0)
            self.outputs += int(event.usage.get("output_tokens", 0) or 0)
        # The event log retains the protocol; the conversation shows the readable result.
        if (event.kind in ("plan", "plan_created", "native_session", "usage")
                or (event.role == "planner" and event.kind == "message")):
            self.refresh_status()
            return
        target = event.provider if event.provider in ("codex", "claude") else "manager"
        text = CONTROL.sub("", event.text)
        color = "#efcc87" if target == "manager" else "#8ec9ee" if target == "codex" else "#e6b6a0"
        rendered = Text(f"{event.role} · {text}", style=color)
        self._pane_entries[target].append(rendered)
        self.query_one("#" + target, RichLog).write(rendered)
        self.refresh_status()

    async def show_usage(self, names: tuple[str, ...]):
        if self.demo:
            self.write(Event(text="DEMO — quota inspection is skipped; no provider requests are made."))
            return
        self.usage_cancel.clear()
        key = self.session.key if self.session else None
        report = await read_usage(self.store, names, cancel=self.usage_cancel)
        event = Event(kind="information", text=format_usage(report))
        # Operational questions never enter the planning history or alter its approval.
        if self.session and self.session.key == key:
            self.session.emit(event)
        else:
            self.write(event)

    async def inspect_usage(self, names: tuple[str, ...]):
        try:
            await self.show_usage(names)
        except Exception as error:
            self.write(Event(kind="error", text=str(error)))
        finally:
            self.usage_job = None

    def on_session_event(self, event: Event):
        if self.session:
            self.drain_session_events()
        else:
            self.write(event)

    def drain_session_events(self):
        # Local callbacks and scheduler polling share one ordered cursor. A callback
        # must render pending background records before advancing past its own event.
        events = self.store.events(self.session.key)
        for row in events[self._seen_events:]:
            self.write(Event(row["provider"], row["role"], row["kind"], row["text"], row["usage"]))
        self._seen_events = len(events)
        self.refresh_status()

    def poll_events(self):
        if self.session and not self.demo:
            self.drain_session_events()

    def refresh_status(self):
        selected = self.session.repo.name if self.session else "No repository selected"
        status = self.store.session(self.session.key)["status"] if self.session else "idle"
        usage = f"reported tokens in {self.inputs:,} / out {self.outputs:,}" if self.have_usage else "tokens not yet reported"
        mode = "DEMO" if self.demo else "LIVE"
        first = Text(f"{mode} · {status} · {usage}")
        configured = self.session.settings if self.session else Settings()
        second = Text(f"API fallback OFF · {configured.task_timeout // 60}m/task · {min(3, configured.repair_cycles)} repairs · {selected}")
        width = max(1, self.size.width - 2)
        first.truncate(width, overflow="ellipsis")
        second.truncate(width, overflow="ellipsis")
        self.query_one("#status", Static).update(Text.assemble(first, "\n", second))
        self.refresh_board()
        self.query_one("#approve", Button).disabled = bool(self.job and not self.job.done()) or not (
            (self.demo and self.demo_plan) or
            (self.session and self.session.plan and not self.session.plan.questions and status != "ready"))

    def refresh_board(self):
        """Render the board from the shared record and notify only for permitted, not-yet-shown attention."""
        widget = self.query_one("#board", Static)
        if not self.session or self.demo:
            widget.update("")
            return
        try:
            from . import board
            widget.update(Text("\n".join(board.build(self.store, self.session.key).lines(max(20, self.size.width - 4)))))
            for kind, text in board.fresh(self.store, self.session.key):
                self.notify(text, title="Brahma needs you" if kind != "ready" else "Brahma", timeout=15)
        except Exception as error:   # the board is a view; it must never break the terminal
            widget.update(Text(f"Board unavailable: {type(error).__name__}"))

    def select_repo(self, path: Path, key: str | None = None):
        root = repository(path)
        selected = Session(self.store, root, self.on_session_event, key)
        # Validate first, then replace the displayed conversation and its counters.
        self.session = selected
        self._seen_events = len(self.store.events(selected.key))
        for entries in self._pane_entries.values():
            entries.clear()
        for log in self.query(RichLog):
            log.clear()
        self.inputs = self.outputs = 0
        self.have_usage = False
        self.demo_plan = None
        self.write(Event(text=f"Selected {root}. Session {self.session.key}. Model routing is configurable in brahma.toml."))
        if key:
            events = self.store.events(key)
            for event in events:
                if event["kind"] == "usage":
                    self.have_usage = True
                    self.inputs += int(event["usage"].get("input_tokens", 0) or 0)
                    self.outputs += int(event["usage"].get("output_tokens", 0) or 0)
            for event in events[-100:]:
                self.write(Event(event["provider"], event["role"], event["kind"], event["text"], event["usage"]),
                           count_usage=False)
            if self.session.plan:
                self.show_plan(self.session.plan)
        self.refresh_status()

    def inspect(self, value: str):
        if value == "/help":
            self.write(Event(text=HELP))
        else:
            self.write(Event(text=json.dumps(self.store.session(self.session.key) if self.session else {"status": "idle"})))

    def show_plan(self, plan: Plan):
        lines = [plan.summary]
        for n, task in enumerate(plan.tasks, 1):
            lines.extend((f"{n}. {task.title} [{task.provider}/{task.complexity}]",
                          "   Files: " + ", ".join(task.files), "   Acceptance: " + "; ".join(task.acceptance)))
        if plan.questions:
            lines.extend(["Decision needed:", *plan.questions])
        else:
            if self.session:
                saved = self.store.session(self.session.key)
                if saved["contract"]:
                    from .contracts import read_contract
                    try:
                        contract = read_contract(Path(saved["contract"]))
                    except ValueError as error:
                        # An old-format or damaged stored contract must not crash the terminal on restore.
                        contract = None
                        lines.append("Stored contract cannot be used: " + str(error) + ". Describe the goal again to create a new plan.")
                if saved["contract"] and contract is not None:
                    lines.extend(("Contract: " + contract["digest"], "Feature file: " + str(Path(saved["contract"]).with_suffix(".feature")),
                                  contract["payload"]["feature"],
                                  "Checks: " + json.dumps(contract["payload"]["checks"]),
                                  "Budget: 60 active minutes/task; at most 3 repairs; quota waiting excluded."))
                    models = contract["payload"]["models"]
                    # Locked settings that widen what a run does must be visible at approval, not only inside the digest.
                    default = Settings()
                    wide = [f"{name}={models.get(name)!r}" for name in ("publish", "auto_commit", "strict_enforcement", "ignored_allowlist")
                            if models.get(name, getattr(default, name)) != getattr(default, name)]
                    lines.append("Run settings changed from the defaults: " + ", ".join(wide) if wide else "Run settings: defaults (no publication, no auto-commit)")
                    risky = sorted({c[0].rsplit("/", 1)[-1] for c in contract["payload"]["checks"]
                                    if c and c[0].rsplit("/", 1)[-1] in ("sh", "bash", "zsh", "curl", "wget", "nc", "ssh", "scp")})
                    if risky:
                        lines.append("WARNING: approved checks run these shell or network programs on this computer: " + ", ".join(risky))
                    selection = models.get("selection")
                    for provider in ("codex", "claude"):
                        for suffix, purpose in (("_model", "routine"), ("_critical_model", "critical")):
                            field = provider + suffix
                            detail = selection["profiles"][field] if selection else None
                            chosen = detail["selected"] if detail else models.get(field)
                            state = "advertised" if detail and detail["verified"] else "unavailable" if detail and detail["available"] is False else "unverified"
                            lines.append(f"Model: {provider} {purpose} = {chosen or 'unavailable'} ({state})")
            lines.append("/approve locks this complete contract and permits queued resumption. /stop cancels restarts.")
        if self.session:
            lines.append("Approved checks: " + ("; ".join(" ".join(c) for c in (plan.checks or self.session.settings.checks)) or
                        "none configured — outcome cannot be marked ready"))
        self.write(Event(kind="plan_display", text="\n".join(lines)))

    async def on_input_submitted(self, event: Input.Submitted):
        value = event.value.strip()
        if not value:
            return
        if value == "/unpause" or value == "/note" or value.startswith("/note "):
            event.input.value = ""
            if value == "/unpause":
                self.action_unpause()
            else:
                self.action_note(value[5:].strip())
            return
        if value in ("/stop", "/pause", "/quit", "/kill"):
            event.input.value = ""
            if value == "/quit":
                await self.action_quit()
            elif value == "/pause":
                self.action_pause()
            elif value == "/kill":
                self.action_kill()
            else:
                self.action_stop()
            return
        names = usage_request(value)
        if names is not None:
            event.input.value = ""
            if self.usage_job and not self.usage_job.done():
                self.write(Event(text="A usage check is already running."))
            else:
                self.usage_job = asyncio.create_task(self.inspect_usage(names))
            return
        if self.job and not self.job.done():
            if value in ("/status", "/help"):
                event.input.value = ""
                self.inspect(value)
            else:
                self.write(Event(text="One worker is already active. Use /stop before another request; your input is retained."))
            return
        event.input.value = ""
        self.job = asyncio.create_task(self.handle(value))
        self.refresh_status()

    async def handle(self, value: str):
        try:
            names = usage_request(value)
            if names is not None:
                await self.show_usage(names)
            elif value == "/help":
                self.inspect(value)
            elif value == "/models" or value.startswith("/models "):
                names = tuple(value.split()[1:]) or ("codex", "claude")
                if len(names) > 2 or any(name not in ("codex", "claude") for name in names):
                    raise ValueError("Use /models [codex|claude]")
                if self.demo:
                    self.write(Event(text="DEMO — model discovery is skipped; no provider requests are made."))
                else:
                    from .model_selection import format_models, read_models
                    settings = Settings.load(self.session.repo) if self.session else Settings()
                    self.checking_auth = True
                    try:
                        report = await read_models(settings, names)
                        self.write(Event(kind="information", text=format_models(report)))
                    finally:
                        self.checking_auth = False
            elif value.startswith("/repo "):
                self.select_repo(Path(value[6:].strip()).expanduser())
            elif value == "/new":
                if self.session:
                    self.select_repo(self.session.repo)
            elif value == "/doctor":
                if self.demo:
                    self.write(Event(text="DEMO — sign-in checks are skipped. Start live mode to check installed CLIs."))
                else:
                    from .providers import doctor
                    self.write(Event(text="Checking installed CLIs and subscription sign-in; no model requests."))
                    self.checking_auth = True
                    try:
                        for name in ("codex", "claude"):
                            result = await doctor(name)
                            if result.get("available"):
                                detail = "Ready: installed and signed in with a subscription."
                            else:
                                detail = result.get("error") or "Subscription availability could not be verified."
                            action = result.get("action")
                            self.write(Event(name, "doctor", text=detail + ("\n" + action if action else "")))
                    finally:
                        self.checking_auth = False
            elif value in ("/skills", "/agents"):
                if not self.session:
                    raise ValueError("Select a repository first")
                from .discovery import discover
                catalog = discover(self.session.repo)
                kind = "skill" if value == "/skills" else "agent"
                for entry in catalog.entries:
                    if entry.kind == kind:
                        self.write(Event(text=f"{entry.id} [{entry.scope}; discovered, native loading disabled] {entry.description}"))
                for warning in catalog.errors:
                    self.write(Event(kind="warning", text=warning))
            elif value.startswith("/capacity "):
                if self.demo:
                    raise ValueError("Demo mode cannot change quota state or enqueue work")
                if not self.session:
                    raise ValueError("Select a waiting session first")
                self.session.confirm_capacity(value.split(maxsplit=1)[1])
                self.write(Event(text="Your capacity confirmation queued one bounded continuation of the existing contract."))
            elif value == "/sessions":
                for row in self.store.recent():
                    self.write(Event(text=f"{row['id']}  {row['status']}  {row['repo']}"))
            elif value.startswith("/resume "):
                key = value[8:].strip()
                self.select_repo(Path(self.store.session(key)["repo"]), key)
            elif value == "/status":
                self.inspect(value)
            elif value == "/publish":
                if self.demo:
                    raise ValueError("Demo mode cannot publish a branch or create a PR")
                from .gittools import publish
                if not self.session or self.store.session(self.session.key)["status"] != "ready":
                    raise ValueError("Only a ready, verified plan can be published")
                if self.store.killed or self.store.session(self.session.key)["stopped"] or self.store.session(self.session.key)["paused"]:
                    raise ValueError("Not published: the kill switch is engaged or the session is stopped or paused")
                saved = self.store.session(self.session.key)
                self.write(Event(kind="publication", text=await asyncio.to_thread(
                    publish, Path(saved["worktree"]), self.session.plan.summary, "Brahma's approved plan passed configured checks and independent review.")))
            elif value == "/approve":
                if self.demo:
                    self.write(Event(text="DEMO complete: execution is simulated, no changes were made."))
                elif not self.session:
                    raise ValueError("Select a repository first")
                else:
                    await self.session.execute()
            elif value.startswith("/"):
                raise ValueError("Unknown command. Use /help")
            elif self.demo:
                self.write(Event(role="user", text=value))
                self.demo_plan = Plan("Demo: implement, verify, independently review", [Task(
                    "Example task", value, ["src/**", "tests/**"], ["Configured checks pass"])])
                self.show_plan(self.demo_plan)
            elif not self.session:
                raise ValueError("Select a Git repository with /repo PATH first")
            else:
                self.show_plan(await self.session.ask(value))
        except Exception as error:
            self.write(Event(kind="error", text=str(error)))
        finally:
            self.job = None
            self.refresh_status()

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "stop":
            self.action_stop()
        elif event.button.id == "pause":
            self.action_pause()
        elif event.button.id == "resume":
            self.action_unpause()
        elif not self.job or self.job.done():
            self.job = asyncio.create_task(self.handle("/approve"))
            self.refresh_status()

    def action_pause(self):
        self.usage_cancel.set()
        if self.session and not self.demo:
            self.session.pause()
        self.write(Event(text="Paused: the approval and queue are kept and nothing runs until `brahma resume "
                         + (self.session.key if self.session else "SESSION") + "`. An active worker stops at its checkpoint."))

    def action_unpause(self):
        """Same durable control as `brahma resume`: the approval is unchanged and nothing new is approved."""
        if not self.session or self.demo:
            self.write(Event(text="No live session to resume."))
            return
        # Never lift a halt that is stronger than a pause: the kill switch and a stop stay in force.
        if self.store.killed or self.store.session(self.session.key)["stopped"]:
            self.write(Event(kind="error", text="Not resumed: the kill switch is engaged or the session was stopped. "
                             "Use `brahma kill --release` or start a new approved run."))
            return
        self.store.resume(self.session.key)
        # Do not clear the cancel event while a worker is still winding down; execute() starts each run with its own state.
        if not (self.job and not self.job.done()):
            self.session.cancel.clear()
        self.write(Event(text="Resumed: the session runs again only under its existing approval."))
        self.refresh_status()

    def action_note(self, text: str):
        """Same record as `brahma note`: applied at the next checkpoint, never to running work."""
        import json
        from . import briefs
        if not self.session or self.demo:
            self.write(Event(text="No live session to leave a note for."))
            return
        task = None
        head, _, rest = text.partition(":")
        if rest and head.strip().isdigit():
            task, text = int(head), rest
        try:
            note = briefs.clean_note(text)
            if task is not None and task < 1:
                raise ValueError("Task numbers start at 1")
        except ValueError as error:
            self.write(Event(kind="error", text=str(error)))
            return
        self.store.event(self.session.key, Event("brahma", "user", "user_note", json.dumps({"task": task, "text": note}, ensure_ascii=False)))
        self.write(Event(text="Note saved for the next checkpoint; it cannot change scope, ownership, permissions or checks."))

    def action_kill(self):
        from . import halt
        self.usage_cancel.set()
        halt.engage(self.store.home)
        if self.session and not self.demo:
            self.session.cancel.set()
        self.write(Event(text="Kill switch engaged: all workers stop, no new work starts, Git writes are refused. "
                         "Lift it with `brahma kill --release`."))

    def action_stop(self):
        self.usage_cancel.set()
        active = self.job is not None and not self.job.done()
        if self.session and not self.demo:
            self.session.stop()
        if active and self.checking_auth:
            self.job.cancel()
        self.write(Event(text="Stop requested; waiting for active work to end. Checkpoint is retained."
                         if active else "No active work to stop. Any queued restart for this session is cancelled."))

    def action_fresh(self):
        if self.job and not self.job.done():
            self.write(Event(text="Stop the active worker before starting a fresh conversation."))
        elif self.session:
            self.select_repo(self.session.repo)

    async def action_quit(self):
        self.usage_cancel.set()
        if self.usage_job:
            self.usage_job.cancel()
            await asyncio.gather(self.usage_job, return_exceptions=True)
        # Closing the terminal checkpoints active work; explicit Stop is durable cancellation.
        if self.checking_auth and self.job:
            self.job.cancel()
        elif self.session and self.job and not self.job.done():
            self.session.cancel.set()
        job = self.job
        if job and not job.done() and job is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(job, return_exceptions=True)), 6)
            except asyncio.TimeoutError:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        if self.session and not self.demo:
            try:
                self.session.queue_resume()
            except (ValueError, OSError):
                pass  # Invalidated authority remains checkpointed; never broaden it on exit.
        self.exit()
