"""Worker process lifecycle across coordinator crashes (review round 8, finding 17).

Workers and checks run in their own process groups so a stop can kill the whole tree. If the coordinator itself dies, those
groups can survive it, and the worker lock dies with the coordinator. So every launch is recorded under `<state>/workers/`
with its coordinator's PID and both start times, and `reap()` stops recorded groups whose coordinator is gone (checking start
times, so a reused PID is never signalled). Session.lock() reaps right after taking the lock and starts nothing if a survivor
cannot be stopped; the scheduler reaps when it starts; the kill switch reaps every recorded group at once.

(Children deliberately do not inherit the lock descriptor: a long-lived grandchild such as a build daemon would hold it for
hours.)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

_home: Path | None = None


def holding(home: Path | None) -> None:
    """Set (or clear, with None) the state directory launches are recorded in while the worker lock is held."""
    global _home
    _home = Path(home) if home is not None else None


def _started(pid: int) -> str | None:
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _group_alive(pgid: int) -> bool:
    """True while any process is still in the group (zombies awaiting their parent count as gone)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                                 # macOS answers EPERM for a group of zombies; ask ps
    try:
        done = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True, timeout=5)
        states = [parts[1] for parts in (line.split() for line in done.stdout.splitlines())
                  if len(parts) >= 2 and parts[0] == str(pgid)]
        return any(not state.startswith("Z") for state in states) if states else True
    except (OSError, subprocess.SubprocessError):
        return True


def register(pid: int) -> Path | None:
    """Record a launched process group (its leader is `pid`). Never raises: bookkeeping must not stop work."""
    if _home is None:
        return None
    try:
        folder = _home / "workers"
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / f"{pid}.json"
        path.write_text(json.dumps({"pid": pid, "coordinator": os.getpid(), "started": _started(pid),
                                    "coordinator_started": _started(os.getpid()), "at": time.time()}))
        return path
    except OSError:
        return None


def unregister(record: Path | None) -> None:
    if record is not None:
        try:
            record.unlink()
        except OSError:
            pass


def reap(home: Path, *, everything: bool = False, grace: float = 2.0) -> tuple[list[int], list[int]]:
    """Stop recorded worker groups whose coordinator has died (or all of them, for the kill switch).
    Returns (stopped, survivors): survivors are groups that are still alive after SIGKILL."""
    folder = Path(home) / "workers"
    stopped, survivors = [], []
    if not folder.is_dir():
        return stopped, survivors
    for record in sorted(folder.glob("*.json")):
        try:
            data = json.loads(record.read_text())
            pid, coordinator = int(data["pid"]), int(data["coordinator"])
        except (OSError, ValueError, KeyError, TypeError):
            unregister(record)
            continue
        if _alive(pid) and data.get("started") and _started(pid) not in (None, data["started"]):
            unregister(record)               # the PID now belongs to an unrelated process: never signal it
            continue
        if not _group_alive(pid):
            unregister(record)               # the whole group is gone (a PID is not reused while its group exists)
            continue
        orphan = not _alive(coordinator) or (data.get("coordinator_started") and _started(coordinator) != data["coordinator_started"])
        if not (everything or orphan):
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, sig)
            except ProcessLookupError:
                break
            except PermissionError:
                pass                         # only zombies left, or not ours: decided by _group_alive below
            deadline = time.monotonic() + grace
            while _group_alive(pid) and time.monotonic() < deadline:
                time.sleep(.05)
            if not _group_alive(pid):
                break
        if _group_alive(pid):
            survivors.append(pid)            # keep the record: it is still running
            continue
        stopped.append(pid)
        unregister(record)
    return stopped, survivors
