"""Kill switch (Round 7/11): one global flag that stops all Brahma work, independent of the database.

The flag is a plain file in the state directory so it works when SQLite is locked or damaged and can be
set by anything that can create a file. While it exists no worker starts, running workers are cancelled
at their next check, and Git writes by the coordinator are refused. Releasing it is a separate, explicit
action; it never resumes work by itself and approvals are untouched.
"""
from __future__ import annotations

import os
from pathlib import Path

NAME = "KILL"


def flag(home: Path) -> Path:
    return Path(home) / NAME


def engaged(home: Path) -> bool:
    return os.path.lexists(flag(home))


def engage(home: Path) -> None:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    flag(home).write_text("Brahma kill switch engaged. Remove with `brahma kill --release`.\n")
    _count(home)


def _count(home: Path) -> None:
    # A durable count of engagements that release never resets: work can tell that the switch was engaged while it ran,
    # even when it was lifted again before the work looked (a run's final checks block its halt monitor). Best effort:
    # failing to count must never stop the switch itself (or the reaping that follows) from working.
    try:
        (Path(home) / (NAME + ".count")).write_text(str(epoch(home) + 1) + "\n")
    except OSError:
        pass


def epoch(home: Path) -> int:
    """How many times the kill switch has been engaged (0 when never, or unreadable)."""
    try:
        return int((Path(home) / (NAME + ".count")).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def kill_everything(home: Path) -> tuple[list[int], list[int]]:
    """The kill switch, as every interface applies it: engage the flag, then stop every recorded worker and check process
    group at once, including groups whose coordinator already died (nobody would be watching the flag for them).
    Returns (stopped, survivors) process-group ids."""
    engage(home)
    from . import procs
    return procs.reap(Path(home), everything=True)


def release(home: Path) -> bool:
    try:
        flag(home).unlink()
    except FileNotFoundError:
        return False
    _count(home)        # also counts a switch engaged without engage() (the flag file created by hand)
    return True
