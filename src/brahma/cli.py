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
    """Run the CLI. Errors become exit code 20 (plain text, or one JSON object with --json)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in argv
    try:
        return _main(argv)
    except SystemExit:
        raise
    except ValueError as error:
        return fail(str(error), exitcodes.USAGE, wants_json)


def fail(message: str, code: int, wants_json: bool) -> int:
    if wants_json:
        print(json.dumps({"ok": False, "error": message, "exit_code": code}))
    else:
        print(f"brahma: {message}", file=sys.stderr)
    return code


def _main(argv):
    args = parser().parse_args(argv)
    if args.command == "models":
        from .model_selection import format_models, read_models
        try:
            root = repository(args.repo or Path.cwd())
        except ValueError as error:
            if args.repo:
                return fail(str(error), exitcodes.USAGE, args.json_output)
            root = None
        try:
            settings = Settings.load(root) if root else Settings()
            names = (args.provider,) if args.provider else ("codex", "claude")
            report = asyncio.run(read_models(settings, names))
        except (ValueError, OSError) as error:
            return fail(str(error), exitcodes.USAGE, args.json_output)
        print(json.dumps(report, indent=2) if args.json else format_models(report))
        return 0
    if args.command == "usage":
        from .usage import format_usage, read_usage
        store = Store(state_home())
        try:
            names = (args.provider,) if args.provider else ("codex", "claude")
            report = asyncio.run(read_usage(store, names))
            print(json.dumps(report, indent=2) if args.json else format_usage(report))
            return 0
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
        print(json.dumps({"version": __version__, "providers": rows, "api_fallback": False,
                          "usage_remaining": "unknown; provider-reported limits only"}, indent=2))
        return exitcodes.READY if all(r.get("available") for r in rows) else exitcodes.SIGN_IN
    if args.command == "worker":
        from .scheduler import serve
        try:
            return asyncio.run(serve(args.home.expanduser().resolve(), once=args.once))
        except (ValueError, OSError) as error:
            return fail(str(error), exitcodes.USAGE, args.json_output)
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
            if args.json_output:
                print(json.dumps({"ok": True, "action": args.action, "message": message,
                                  "queued_approved_jobs": queued, "exit_code": 0}))
            else:
                print(message)
                if queued is not None:
                    print(f"Queued approved jobs: {queued}. Quota is checked from provider signals when work is due; "
                          "unknown quota permits one bounded approved attempt; a reported limit suspends work.")
            return exitcodes.READY
        except subprocess.SubprocessError as error:
            return fail(str(error) or "The service manager command failed", exitcodes.BLOCKED, args.json_output)
        except (ValueError, OSError) as error:
            return fail(str(error), exitcodes.USAGE, args.json_output)
    home = state_home()
    def report(payload: dict, text: str) -> int:
        print(json.dumps({"ok": True, **payload, "exit_code": 0}) if args.json_output else text)
        return exitcodes.READY

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
    if args.command in ("status", "events"):
        if not (home / "state.sqlite3").exists():
            raise ValueError("Unknown session")
        store = Store(home)
        try:
            if args.command == "status":
                code, payload = session_status(store, args.session)
                print(json.dumps(payload, indent=None if args.json_output else 2))
                return code
            return stream_events(store, args.session, args.after, args.follow)
        finally:
            store.close()
    if args.command == "sessions":
        store = Store(home)
        try:
            sessions = store.recent()
            print(json.dumps({"ok": True, "sessions": sessions, "exit_code": 0}) if args.json_output
                  else json.dumps(sessions, indent=2))
        finally:
            store.close()
        return 0
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
                print(text)
                return 0
            if not args.approve:
                raise ValueError("Execution requires --approve; inspect the plan and configured checks first")
            plan = Plan.parse(args.plan_file.read_text(encoding="utf-8"))
            selection = asyncio.run(session.discover_model_profiles())
            session.set_plan(plan, model_selection=selection)
            result = asyncio.run(session.execute())
            code = exitcodes.for_result_status(result.status)
            print(json.dumps({"ok": result.status == "ready", "session": session.key, "status": result.status,
                              "summary": result.text, "exit_code": code}, indent=2))
            return code
        except (ValueError, OSError) as error:
            return fail(str(error), exitcodes.USAGE, args.json_output)
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
