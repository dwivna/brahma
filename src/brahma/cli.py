from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys

from . import __version__, exitcodes
from .config import Settings, state_home
from .models import Plan
from .store import Store
from .session import Session
from .gittools import repository


def parser():
    result = _Parser(prog="brahma", description="One terminal for supervised Claude Code and Codex development")
    result.add_argument("--version", action="version", version=f"Brahma {__version__}")
    result.add_argument("--repo", type=Path, help="Explicit Git repository (defaults to current repository)")
    result.add_argument("--json", action="store_true", dest="json_output",
                        help="Machine-readable output: every result and error is one JSON object (see exit codes in README)")
    result.add_argument("--demo", action="store_true", help="Run the three-pane interface without provider calls")
    result.add_argument("--resume", metavar="SESSION", help="Reopen a local conversation/checkpoint")
    sub = result.add_subparsers(dest="command")
    sub.add_parser("doctor", help="Read-only CLI and subscription sign-in diagnostics")
    p = sub.add_parser("usage", help="Inspect provider-reported quota without a model request")
    p.add_argument("provider", nargs="?", choices=("codex", "claude"))
    p.add_argument("--json", action="store_true", help="Print the structured observation report")
    p = sub.add_parser("models", help="Discover native models and show task profile selection without generation")
    p.add_argument("provider", nargs="?", choices=("codex", "claude"))
    p.add_argument("--json", action="store_true", help="Print the structured catalog and selection")
    sub.add_parser("sessions", help="List local saved sessions")
    p = sub.add_parser("plan", help="Propose a read-only plan, without executing it")
    p.add_argument("request")
    p.add_argument("--repo", type=Path, dest="target")
    p.add_argument("--output", type=Path, help="Save the plan JSON locally")
    p = sub.add_parser("run", help="Execute an explicitly approved plan in an isolated branch")
    p.add_argument("plan_file", type=Path)
    p.add_argument("--repo", type=Path, dest="target")
    p.add_argument("--approve", action="store_true", help="Approve this plan and the selected repository's configured checks")
    p = sub.add_parser("worker", help="Run approved queued work (foreground scheduler)")
    p.add_argument("--home", type=Path, required=True, help="Brahma state directory")
    p.add_argument("--once", action="store_true", help="Run at most one due job, then exit")
    p = sub.add_parser("service", help="Manage the local background scheduler (macOS launchd)")
    p.add_argument("action", choices=("install", "status", "stop"))
    p = sub.add_parser("memory", help="Inspect or forget what Brahma remembers about one repository")
    p.add_argument("action", choices=("show", "forget"))
    p.add_argument("--repo", type=Path, dest="target", help="Repository (defaults to the current one)")
    p.add_argument("--yes", action="store_true", help="Confirm `forget`; without it nothing is deleted")
    p = sub.add_parser("statusline", help="Claude Code status-line tap: records Claude's own rate-limit numbers for `brahma usage`")
    p.add_argument("--then", metavar="COMMAND", help="Your existing status-line command; it receives the same input and its output is shown")
    p = sub.add_parser("note", help="Leave a note for a session; it is applied at the next checkpoint, never mid-run")
    p.add_argument("session")
    p.add_argument("text")
    p.add_argument("--task", type=int, help="Apply only to this task number (default: every task)")
    p = sub.add_parser("decide", help="Decide a disputed review: side with the reviewer or the author, then work continues")
    p.add_argument("session")
    p.add_argument("--for", dest="side", required=True, choices=("reviewer", "author"))
    p = sub.add_parser("proposals", help="Draft proposals from read-only repository signals; starts nothing, approves nothing")
    p.add_argument("--repo", type=Path, dest="target", help="Repository (defaults to the current one)")
    p.add_argument("--feedback", help="Review feedback you supply (Brahma never goes looking for it)")
    p = sub.add_parser("status", help="Where a session stands: JSON, with an exit code per outcome")
    p.add_argument("session")
    p = sub.add_parser("events", help="Stream a session's events as JSON lines")
    p.add_argument("session")
    p.add_argument("--after", type=int, default=0, help="Only events with a sequence number above this")
    p.add_argument("--follow", action="store_true", help="Keep streaming until the session settles or Ctrl-C")
    p = sub.add_parser("pause", help="Hold a session: approval and queue are kept but nothing runs until `resume`")
    p.add_argument("session")
    p = sub.add_parser("resume", help="Release a paused session; it still needs its existing approval to run")
    p.add_argument("session")
    p = sub.add_parser("stop", help="Stop a session for good: cancels its queue; running again needs a new approval")
    p.add_argument("session")
    p = sub.add_parser("kill", help="Kill switch: halt every worker now and refuse new work (--release to lift)")
    p.add_argument("--release", action="store_true", help="Lift the kill switch; approvals are untouched")
    return result


def readiness(rows: list[dict], snapshots: dict) -> dict:
    """Installation/sign-in readiness is separate from quota; absent quota stays unknown."""
    from . import quota
    result = {}
    for row in rows:
        name = row["provider"]
        ready = bool(row.get("available"))
        decision = quota.eligibility(snapshots.get(name)) if snapshots.get(name) else None
        row["readiness"] = "ready" if ready else row.get("state", "unverified")
        row["quota"] = decision if decision else {"state": "unknown", "next_at": None,
                                                  "reason": "No provider-reported quota observation"}
        result[name] = row["quota"]["state"]
    return result


STATUSLINE_REFRESH_SECONDS = 15.0
STATUSLINE_MAX_INPUT = 1_000_000


def record_claude_statusline(home: Path, payload: str, now: float | None = None) -> bool:
    """Store Claude Code's own `rate_limits` (5h and 7d) from a status-line payload. Never raises.

    This is Claude's documented, credential-free signal: Claude Code hands the numbers to the status-line command.
    Brahma never reads sign-in material or polls an endpoint. Written at most once per refresh interval.
    """
    import time
    from . import quota
    try:
        data = json.loads(payload)
        limits = data.get("rate_limits") if isinstance(data, dict) else None
        if not isinstance(limits, dict) or not {"five_hour", "seven_day"} <= set(limits):
            return False          # a partial payload would read as "available" with the other window unknown
        now = time.time() if now is None else now
        for window in (limits["five_hour"], limits["seven_day"]):
            reset = window.get("resets_at") if isinstance(window, dict) else None
            if isinstance(reset, (int, float)) and not isinstance(reset, bool) and reset > now + 8 * 86400:
                return False      # a forged far-future reset would park the provider for years
        snapshot = quota.normalize_claude({k: v for k, v in limits.items() if k in ("five_hour", "seven_day")}, observed_at=now)
        store = Store(home)
        try:
            previous = store.quota("claude")
            if (previous and previous.get("source") == "claude.statusline"
                    and 0 <= now - previous.get("observed_at", 0) < STATUSLINE_REFRESH_SECONDS):
                return False      # at most one write per interval, whatever the values
            store.observe(snapshot)
        finally:
            store.close()
        return True
    except Exception:  # a status line must never break because of Brahma
        return False


def statusline_tap(home: Path, then: str | None) -> int:
    import shlex
    payload = sys.stdin.read(STATUSLINE_MAX_INPUT)
    record_claude_statusline(home, payload)
    if then:
        try:
            done = subprocess.run(shlex.split(then), input=payload, text=True, capture_output=True, timeout=5)
            sys.stdout.write(done.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return exitcodes.READY


def session_status(store: Store, key: str) -> tuple[int, dict]:
    """One JSON object saying where a session stands; the exit code is the outcome."""
    saved = store.session(key)  # ValueError("Unknown session") becomes exit 20
    job = store.job(key)
    digest, checks = None, None
    if saved["contract"]:
        try:
            from .contracts import read_contract
            contract = read_contract(Path(saved["contract"]))
            digest, checks = contract["digest"], bool(contract["payload"]["checks"])
        except (ValueError, OSError):
            digest = None
    code = exitcodes.for_session(saved, job["state"] if job else None, checks)
    approved = bool(saved["approved_digest"]) and saved["approved_digest"] == digest
    return code, {"ok": code == exitcodes.READY, "session": key, "status": saved["status"],
                  "outcome": exitcodes.TABLE[code], "exit_code": code, "approved": approved,
                  "contract_digest": digest, "paused": bool(saved["paused"]), "stopped": bool(saved["stopped"]),
                  "killed": store.killed, "job": job, "repo": saved["repo"], "worktree": saved["worktree"]}


def stream_events(store: Store, key: str, after: int, follow: bool) -> int:
    """Print events as JSON lines (one object per line); with follow, until the session settles."""
    import time
    store.session(key)
    try:
        while True:
            batch = store.events_after(key, after)
            for event in batch:
                print(json.dumps(event, ensure_ascii=False), flush=True)
                after = event["seq"]
            if not follow:
                break
            code = session_status(store, key)[0]
            if not batch and (code in (exitcodes.READY, exitcodes.CANCELLED, exitcodes.NEEDS_APPROVAL,
                                       exitcodes.NEEDS_CHECKS, exitcodes.BLOCKED) or store.killed):
                break
            if not batch:
                time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    return exitcodes.READY


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def main(argv=None):
    """Run the CLI. Usage errors are exit code 20; anything else that stops a command is 13 (blocked) or 14 (interrupted).
    With --json (global, or a command's own --json) every result and error is exactly one JSON object with `ok` and
    `exit_code`; `events` prints JSON lines."""
    argv = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in argv
    try:
        return _main(argv)
    except SystemExit:
        raise
    except ValueError as error:
        return fail(str(error), exitcodes.USAGE, wants_json)
    except KeyboardInterrupt:
        return fail("Interrupted", exitcodes.CANCELLED, wants_json)
    except Exception as error:      # a provider, Git, database or filesystem problem: report it in the promised shape
        from .safety import redact
        return fail(redact(f"{type(error).__name__}: {error}")[:2000], exitcodes.BLOCKED, wants_json)


def emit(payload: dict, text, code: int, as_json: bool) -> int:
    """Print one result. JSON output is always the envelope: the payload plus `ok` and `exit_code`.
    `text` may be a callable so human formatting is only done for humans."""
    if as_json:
        print(json.dumps({**payload, "ok": code == exitcodes.READY, "exit_code": code}, ensure_ascii=False))
    else:
        text = text() if callable(text) else text
        print(text if text is not None else json.dumps(payload, indent=2, ensure_ascii=False))
    return code


def fail(message: str, code: int, wants_json: bool) -> int:
    if wants_json:
        print(json.dumps({"ok": False, "error": message, "exit_code": code}))
    else:
        print(f"brahma: {message}", file=sys.stderr)
    return code


def _main(argv):
    args = parser().parse_args(argv)
    as_json = args.json_output or bool(getattr(args, "json", False))
    if args.json_output and args.command is None:
        raise ValueError("--json needs a command; the interactive terminal has no JSON mode")
    if args.command == "models":
        from .model_selection import format_models, read_models
        try:
            root = repository(args.repo or Path.cwd())
        except ValueError as error:
            if args.repo:
                return fail(str(error), exitcodes.USAGE, as_json)
            root = None
        try:
            settings = Settings.load(root) if root else Settings()
            names = (args.provider,) if args.provider else ("codex", "claude")
            report = asyncio.run(read_models(settings, names))
        except (ValueError, OSError) as error:
            return fail(str(error), exitcodes.USAGE, as_json)
        return emit({"models": report}, lambda: format_models(report), exitcodes.READY, as_json)
    if args.command == "usage":
        from .usage import format_usage, read_usage
        store = Store(state_home())
        try:
            names = (args.provider,) if args.provider else ("codex", "claude")
            report = asyncio.run(read_usage(store, names))
            return emit({"usage": report}, lambda: format_usage(report), exitcodes.READY, as_json)
        finally:
            store.close()
    if args.command == "doctor":
        from .providers import doctor
        async def inspect():
            return [await doctor(name) for name in ("codex", "claude")]
        rows = asyncio.run(inspect())
        snapshots = {}
        home = state_home()
        if (home / "state.sqlite3").exists():  # Doctor stays read-only: never create state.
            store = Store(home)
            try:
                snapshots = {name: store.quota(name) for name in ("codex", "claude")}
            finally:
                store.close()
        readiness(rows, snapshots)
        payload = {"version": __version__, "providers": rows, "api_fallback": False,
                   "usage_remaining": "unknown; provider-reported limits only"}
        code = exitcodes.READY if all(r.get("available") for r in rows) else exitcodes.SIGN_IN
        return emit(payload, json.dumps(payload, indent=2), code, as_json)
    if args.command == "worker":
        from .scheduler import serve
        try:
            return asyncio.run(serve(args.home.expanduser().resolve(), once=args.once))
        except (ValueError, OSError) as error:
            return fail(str(error), exitcodes.USAGE, as_json)
    if args.command == "service":
        from .scheduler import service
        try:
            home = state_home().resolve()
            message = service(args.action, home)
            queued = None
            if args.action == "status" and (home / "state.sqlite3").exists():
                store = Store(home)
                try:
                    queued = len([j for j in store.pending() if j["state"] == "queued"])
                finally:
                    store.close()
            text = message + ("" if queued is None else
                              f"\nQueued approved jobs: {queued}. Quota is checked from provider signals when work is due; "
                              "unknown quota permits one bounded approved attempt; a reported limit suspends work.")
            return emit({"action": args.action, "message": message, "queued_approved_jobs": queued}, text, exitcodes.READY, as_json)
        except subprocess.SubprocessError as error:
            return fail(str(error) or "The service manager command failed", exitcodes.BLOCKED, as_json)
        except (ValueError, OSError) as error:
            return fail(str(error), exitcodes.USAGE, as_json)
    home = state_home()
    def report(payload: dict, text: str) -> int:
        return emit(payload, text, exitcodes.READY, as_json)

    if args.command == "kill":
        from . import halt
        if args.release:
            changed = halt.release(home)
            return report({"kill": False, "changed": changed},
                          "Kill switch released. Nothing restarts by itself: use `brahma resume`/`run` as before."
                          if changed else "Kill switch was not engaged.")
        halt.engage(home)
        return report({"kill": True, "changed": True},
                      "Kill switch engaged: running workers stop at their next check (within seconds), "
                      "no new work starts, and Git writes are refused. Approvals are kept.")
    if args.command in ("pause", "resume", "stop"):
        store = Store(home)
        try:
            action = {"pause": store.pause, "resume": store.resume, "stop": store.stop}[args.command]
            store.session(args.session)
            action(args.session)
            return report({"session": args.session, "action": args.command},
                          {"pause": f"Paused {args.session}: its approval and queue are kept, nothing runs until `brahma resume`. "
                                    "A running worker stops at its checkpoint.",
                           "resume": f"Resumed {args.session}: it runs again only under its existing approval.",
                           "stop": f"Stopped {args.session}: its queue is cancelled and running it again needs a new approval."}[args.command])
        finally:
            store.close()
    if args.command == "memory":
        from .memory import Memory, repo_id
        root = repository(args.target or args.repo or Path.cwd())
        if args.action == "forget" and not args.yes:
            raise ValueError("Forgetting deletes this repository's memory and learned routing history; add --yes to confirm")
        if not (home / "state.sqlite3").exists():
            return report({"repo_id": repo_id(root), "memory": [], "outcomes": []}, "Nothing is remembered for this repository.")
        store = Store(home)
        try:
            memory = Memory(store)
            identity = repo_id(root)
            if args.action == "forget":
                removed = memory.forget(identity)
                return report(removed, f"Forgot {removed['memory_removed']} memory entries and {removed['outcomes_removed']} "
                                       "routing outcomes. Sessions, approvals, worktrees and checkpoints are untouched.")
            data = memory.inspect(identity)
            return report(data, json.dumps(data, indent=2))
        finally:
            store.close()
    if args.command == "note":
        from . import briefs
        from .models import Event
        if not (home / "state.sqlite3").exists():
            raise ValueError("Unknown session")
        text = briefs.clean_note(args.text)
        store = Store(home)
        try:
            store.session(args.session)
            if args.task is not None and args.task < 1:
                raise ValueError("Task numbers start at 1")
            store.event(args.session, Event("brahma", "user", "user_note", json.dumps({"task": args.task, "text": text}, ensure_ascii=False)))
            return report({"session": args.session, "task": args.task},
                          "Note saved. It is applied at the next checkpoint (not to work already running) and cannot change "
                          "scope, ownership, permissions or checks; those need a newly approved contract.")
        finally:
            store.close()
    if args.command == "decide":
        import time
        from .models import Event
        if not (home / "state.sqlite3").exists():
            raise ValueError("Unknown session")
        store = Store(home)
        try:
            saved = store.session(args.session)
            waiting = [a for a in store.open_attention(args.session) if a["kind"] == "decision"]
            disputes = [e for e in store.events_after(args.session, 0, 5000) if e["kind"] == "disagreement"]
            if not waiting or not disputes:
                raise ValueError("No disputed review is waiting for a decision")
            import hashlib
            latest = json.loads(disputes[-1]["text"])
            index = latest["index"]
            digest = hashlib.sha256(latest["text"].encode()).hexdigest()[:32]   # binds the decision to these exact positions
            store.event(args.session, Event("brahma", "user", "dispute_decision",
                                            json.dumps({"index": index, "for": args.side, "digest": digest})))
            for item in waiting:
                store.resolve_attention(item["id"])
            queued = False
            if saved["approved_digest"] and not saved["stopped"]:
                session = Session(store, Path(saved["repo"]), key=args.session)
                store.enqueue(args.session, session.generation, saved["approved_digest"], time.time(),
                              "Abhinav decided a disputed review")
                queued = True
            return report({"session": args.session, "task": index, "decided_for": args.side, "queued": queued},
                          f"Recorded: sided with the {args.side} on task {index}."
                          + (" Queued to continue under the existing approval." if queued else " Run it again to continue."))
        finally:
            store.close()
    if args.command == "statusline":
        return statusline_tap(home, args.then)
    if args.command == "proposals":
        from . import routing
        from .memory import Memory, repo_id
        root = repository(args.target or args.repo or Path.cwd())
        store = Store(home)
        try:
            drafts = routing.draft_proposals(root, Memory(store), repo_id(root), args.feedback)
        finally:
            store.close()
        return report({"proposals": drafts}, json.dumps(drafts, indent=2) if drafts else "No proposals from the current signals.")
    if args.command in ("status", "events"):
        if not (home / "state.sqlite3").exists():
            raise ValueError("Unknown session")
        store = Store(home)
        try:
            if args.command == "status":
                code, payload = session_status(store, args.session)
                return emit(payload, json.dumps(payload, indent=2), code, as_json)
            return stream_events(store, args.session, args.after, args.follow)
        finally:
            store.close()
    if args.command == "sessions":
        store = Store(home)
        try:
            sessions = store.recent()
        finally:
            store.close()
        return emit({"sessions": sessions}, json.dumps(sessions, indent=2), exitcodes.READY, as_json)
    if args.command in ("plan", "run"):
        store = Store(home)
        try:
            root = repository(args.target or args.repo or Path.cwd())
            def log(event):
                print(f"[{event.provider}/{event.role}] {event.text}", file=sys.stderr, flush=True)
            session = Session(store, root, log)
            if args.command == "plan":
                plan = asyncio.run(session.ask(args.request))
                text = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2)
                if args.output:
                    args.output.write_text(text + "\n", encoding="utf-8")
                saved = store.session(session.key)
                return emit({"session": session.key, "status": saved["status"], "plan": plan.to_dict(),
                             "needs": "answers to the plan's questions" if plan.questions else "a human approval: brahma approve " + session.key},
                            text, exitcodes.READY, as_json)
            if not args.approve:
                raise ValueError("Execution requires --approve; inspect the plan and configured checks first")
            plan = Plan.parse(args.plan_file.read_text(encoding="utf-8"))
            async def approve_and_execute():
                # One event loop for the whole run: the session's cancel Event binds to the loop it first awaits on.
                selection = await session.discover_model_profiles()
                session.set_plan(plan, model_selection=selection)
                return await session.execute()
            result = asyncio.run(approve_and_execute())
            code = exitcodes.for_result_status(result.status)
            print(json.dumps({"ok": result.status == "ready", "session": session.key, "status": result.status,
                              "summary": result.text, "exit_code": code}, indent=2))
            return code
        except (ValueError, OSError) as error:
            return fail(str(error), exitcodes.USAGE, as_json)
        finally:
            store.close()
    from .ui import BrahmaApp
    try:
        root = repository(args.repo or Path.cwd())
    except ValueError:
        root = None
    app = BrahmaApp(root, home=home, resume=args.resume, demo=args.demo)
    try:
        app.run()
    finally:
        app.store.close()
    return 0
