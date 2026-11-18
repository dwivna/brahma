"""Local durable queue. It schedules approved contracts, never account resets."""
from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import time

from .models import Event
from .store import Store

RETRY_SECONDS = 15      # Back-off while another approved worker holds the global lock.
POLL_SECONDS = 5
STOP_GRACE = 10.0       # Time given to child work to checkpoint after a service stop.


class Capacity:
    def __init__(self, store: Store, cancel: asyncio.Event):
        self.store, self.cancel = store, cancel

    async def __call__(self, provider: str) -> dict:
        from . import providers, quota
        snapshot = self.store.quota(provider)
        if snapshot:
            decision = quota.eligibility(snapshot)
            if decision["state"] == "waiting":
                return decision
        auth = await providers.doctor(provider)
        if not auth["available"]:
            return {"state": "unavailable", "next_at": None,
                    "reason": (auth.get("error") or "Not available") + ". " + auth.get("action", "")}
        if provider == "codex":
            fresh = await quota.probe_codex(self.cancel)
            # A failed recheck must not erase an observed block with no reset.
            if snapshot and snapshot.get("blocked") and not snapshot.get("reset_at") and fresh.get("blocked") is None:
                return {"state": "waiting", "next_at": None, "reason": "Quota blocked; reset time unavailable"}
            self.store.observe(fresh)
            return quota.eligibility(fresh)
        return quota.eligibility(snapshot) if snapshot else {
            "state": "unknown", "next_at": None, "reason": "Signed in; remaining Claude allowance not yet reported"}


def _note(store: Store, session, key: str, text: str) -> None:
    """Record a scheduler event; failing to record must never break the daemon loop."""
    try:
        if session is not None:
            session.emit(Event(kind="error", text=text))
        else:
            store.event(key, Event(kind="error", text=text))
    except Exception:
        pass


async def tick(store: Store, now: float | None = None, stop: asyncio.Event | None = None,
               grace: float = STOP_GRACE) -> bool:
    """Claim at most one due job and run it to completion, checkpoint or stop.

    Only an existing approval (digest and plan generation recorded when the user approved)
    can run; nothing here creates or refreshes an approval. Session's global lock
    serializes this against every other worker.
    """
    from .session import Session
    job = store.claim(time.time() if now is None else now)
    if not job:
        return False
    key = job["session"]
    session = None
    try:
        row = store.session(key)
        session = Session(store, Path(row["repo"]), key=key)
    except Exception as error:
        store.finish_job(key, "blocked")
        _note(store, None, key, "Scheduled task blocked: " + str(error))
        return True
    if row["status"] == "ready":
        store.finish_job(key)
        return True
    if (row["stopped"] or not row["contract"] or row["approved_digest"] != job["digest"]
            or session.generation != job["generation"]):
        store.finish_job(key, "cancelled")
        return True

    work = asyncio.ensure_future(session.execute(scheduled=True))
    waiter = asyncio.ensure_future(stop.wait()) if stop is not None else None
    interrupted = False

    async def settle():
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)

    try:
        await asyncio.wait({work, *([waiter] if waiter else [])}, return_when=asyncio.FIRST_COMPLETED)
        if not work.done():
            # Service stop: let child work stop cleanly and keep its checkpoint.
            interrupted = True
            session.cancel.set()
            await asyncio.wait({work}, timeout=grace)
        await settle()
    except asyncio.CancelledError:
        # Outer cancellation: ask child work to checkpoint, then force it, then re-raise.
        session.cancel.set()
        try:
            await asyncio.wait({work}, timeout=grace)
        finally:
            await settle()
            _requeue_interrupted(store, session, job)
        raise
    finally:
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    error = None if work.cancelled() else work.exception()
    result = None if work.cancelled() or error else work.result()
    if interrupted and error is None and (result is None or result.status == "cancelled"):
        _requeue_interrupted(store, session, job)
        return True
    if error is None and result is not None:
        if result.status != "waiting":
            store.finish_job(key)
        return True
    if isinstance(error, ValueError) and "Another Brahma worker" in str(error):
        store.enqueue(key, session.generation, job["digest"], time.time() + RETRY_SECONDS,
                      "Another approved worker is active")
    elif isinstance(error, ValueError):
        store.finish_job(key, "blocked")
        _note(store, session, key, "Scheduled task blocked: " + str(error))
    else:
        store.finish_job(key, "blocked")
        _note(store, session, key, "Scheduled task failed; checkpoint preserved: " + str(error))
    return True


def _requeue_interrupted(store: Store, session, job: dict) -> None:
    """A service stop is not a user stop: keep the approved job for the next daemon start.

    Store.enqueue is a no-op for a durably stopped/paused session, so a user stop is never undone.
    """
    try:
        if store.session(session.key)["status"] in ("waiting", "blocked", "incomplete", "ready"):
            return
        store.enqueue(session.key, session.generation, job["digest"], time.time(),
                      "Service stopped; approved work resumes when the service restarts")
    except Exception:
        pass


async def serve(home: Path, *, once: bool = False) -> int:
    home = Path(home)
    store = Store(home)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        descriptor = os.open(home / "scheduler.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a") as guard:
            try:
                fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("brahma: another scheduler already owns this state directory", file=sys.stderr)
                return 0
            store.recover_jobs()
            while not stop.is_set():
                try:
                    await tick(store, stop=stop)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # Keep the daemon alive; the job was already checkpointed.
                    print("brahma: scheduler tick failed: " + str(error), file=sys.stderr, flush=True)
                if once:
                    break
                try:
                    await asyncio.wait_for(stop.wait(), POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
    finally:
        store.close()
        for sig in installed:
            loop.remove_signal_handler(sig)
    return 0


LABEL = "dev.dwivna.brahma"


def service_plist(home: Path) -> dict:
    home = Path(home).expanduser().resolve()
    # Absolute Python/module paths remain valid outside an interactive shell.
    # Only PATH (to find the signed-in CLIs) is inherited; no credentials are embedded.
    return {"Label": LABEL,
            "ProgramArguments": [str(Path(sys.executable).absolute()), "-m", "brahma", "worker", "--home", str(home)],
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                     "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            "WorkingDirectory": str(home),
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "StandardOutPath": str(home / "service.log"), "StandardErrorPath": str(home / "service-error.log")}


def service(action: str, home: Path, *, agents: Path | None = None, run=subprocess.run) -> str:
    if action not in ("install", "status", "stop"):
        raise ValueError("Unknown service action")
    if sys.platform != "darwin":
        raise ValueError("Automatic service installation currently supports macOS; run `brahma worker` on other systems")
    home = Path(home).expanduser().resolve()
    path = (agents or Path.home() / "Library/LaunchAgents") / (LABEL + ".plist")
    target = f"gui/{os.getuid()}"
    if action == "install":
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(plistlib.dumps(service_plist(home)))
        os.replace(temporary, path)
        run(["launchctl", "bootout", target + "/" + LABEL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            run(["launchctl", "bootstrap", target, str(path)], check=True, capture_output=True)
        except subprocess.CalledProcessError as error:
            raise ValueError("launchctl could not register the service: "
                             + (error.stderr or b"").decode("utf-8", "replace").strip()[:300]) from None
        return "Brahma service installed; only approved queued contracts can run while this computer is awake."
    if action == "stop":
        result = run(["launchctl", "bootout", target + "/" + LABEL], capture_output=True, text=True)
        return "Brahma service stopped; checkpoints retained." if result.returncode == 0 else "Brahma service is not running."
    result = run(["launchctl", "print", target + "/" + LABEL], capture_output=True, text=True)
    return "Brahma service is registered." if result.returncode == 0 else "Brahma service is not registered; run `brahma service install`."
