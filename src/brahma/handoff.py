"""Typed handoff packets (Round 5): small, bounded, read-only, and free of history.

A packet carries only what a worker needs for one phase: the locked Gherkin slice for its task, the owned
paths, the approved check commands, a diff or commit reference, coordinator-verified check results and
structured open findings. It never carries an author summary, conversation history, raw transcripts or
secrets; workers read files and Git themselves. Packets are written by the coordinator outside the
repository and outside the database; a worker is given a path to a read-only file and never the store.

This module deliberately does not import the store.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

PHASES = ("build", "repair", "review")
SEVERITIES = ("high", "medium", "low")
CHECK_STATUSES = ("passed", "failed", "not_run")
MAX_TEXT = 2000
MAX_ITEMS = 50
MAX_PACKET_BYTES = 64 * 1024
FORBIDDEN_FIELDS = ("summary", "author_summary", "history", "conversation", "transcript", "messages", "notes")
_REF = re.compile(r"^[0-9a-f]{7,40}(\.\.[0-9a-f]{7,40})?$")
_SECRET = re.compile(
    r"(?i)(api[_-]?key|secret|token|passwd|password|authorization|bearer)\s*[:=]\s*\S{6,}"
    r"|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")


def looks_secret(text: str) -> bool:
    """True when text appears to contain a credential. Used for packets and repository memory."""
    return bool(_SECRET.search(text))


def _text(value, name: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Packet field {name} must be non-empty text")
    if len(value) > limit:
        raise ValueError(f"Packet field {name} is too long")
    if looks_secret(value):
        raise ValueError(f"Packet field {name} appears to contain a secret")
    return value


def _list(value, name: str) -> list:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise ValueError(f"Packet field {name} must be a list of at most {MAX_ITEMS} items")
    return value


@dataclass(frozen=True)
class Packet:
    """Immutable; every field is validated on construction through :func:`parse`."""
    session: str
    task_index: int
    phase: str
    gherkin: tuple[str, ...]
    owned_paths: tuple[str, ...]
    checks: tuple[tuple[str, ...], ...]
    diff_ref: str | None
    check_results: tuple[tuple[tuple[str, ...], str, int | None], ...]
    findings: tuple[tuple[str, str, str | None, str], ...]  # (id, severity, path, text)

    def to_dict(self) -> dict:
        return {"version": 1, "session": self.session, "task_index": self.task_index, "phase": self.phase,
                "gherkin": list(self.gherkin), "owned_paths": list(self.owned_paths),
                "checks": [list(c) for c in self.checks], "diff_ref": self.diff_ref,
                "check_results": [{"argv": list(a), "status": s, "exit_code": e} for a, s, e in self.check_results],
                "findings": [{"id": i, "severity": v, "path": p, "text": t} for i, v, p, t in self.findings]}


_FIELDS = {"version", "session", "task_index", "phase", "gherkin", "owned_paths", "checks", "diff_ref",
           "check_results", "findings"}


def parse(data: dict) -> Packet:
    """Validate untrusted packet data into an immutable Packet; reject anything outside the schema."""
    from . import ownership
    if not isinstance(data, dict):
        raise ValueError("Packet must be an object")
    for name in data:
        if name in FORBIDDEN_FIELDS:
            raise ValueError(f"Packet field {name} is not allowed: handoffs carry no author summary or history")
    unknown = set(data) - _FIELDS
    if unknown:
        raise ValueError("Unknown packet fields: " + ", ".join(sorted(map(str, unknown))))
    if data.get("version") != 1:
        raise ValueError("Unsupported packet version")
    phase = data.get("phase")
    if phase not in PHASES:
        raise ValueError("Packet phase must be one of " + ", ".join(PHASES))
    index = data.get("task_index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        raise ValueError("Packet task_index must be a positive integer")
    session = _text(data.get("session"), "session", 64)
    gherkin = tuple(_text(item, "gherkin") for item in _list(data.get("gherkin"), "gherkin"))
    if not gherkin:
        raise ValueError("Packet needs the locked Gherkin slice for its task")
    owned = tuple(_text(item, "owned_paths", 200) for item in _list(data.get("owned_paths"), "owned_paths"))
    if not owned:
        raise ValueError("Packet needs the task's owned paths")
    for pattern in owned:
        ownership.validate(pattern)
    checks = []
    for argv in _list(data.get("checks"), "checks"):
        checks.append(tuple(_text(word, "checks", 500) for word in _list(argv, "checks")))
    diff_ref = data.get("diff_ref")
    if diff_ref is not None and (not isinstance(diff_ref, str) or not _REF.match(diff_ref)):
        raise ValueError("Packet diff_ref must be a commit or commit range, not free text")
    if phase == "review" and diff_ref is None:
        raise ValueError("A review packet needs the diff reference")
    results = []
    for item in _list(data.get("check_results"), "check_results"):
        if not isinstance(item, dict) or set(item) - {"argv", "status", "exit_code"} or item.get("status") not in CHECK_STATUSES:
            raise ValueError("Each check result needs argv, status and an optional exit_code only")
        code = item.get("exit_code")
        if code is not None and (not isinstance(code, int) or isinstance(code, bool)):
            raise ValueError("Check exit_code must be an integer")
        results.append((tuple(_text(w, "check_results", 500) for w in _list(item.get("argv"), "check_results")),
                        item["status"], code))
    findings = []
    for item in _list(data.get("findings"), "findings"):
        if not isinstance(item, dict) or set(item) - {"id", "severity", "path", "text"} or item.get("severity") not in SEVERITIES:
            raise ValueError("Each finding needs id, severity, text and an optional path only")
        path = item.get("path")
        if path is not None:
            ownership.validate(_text(path, "findings.path", 200))
        findings.append((_text(item.get("id"), "findings.id", 64), item["severity"], path, _text(item.get("text"), "findings.text")))
    packet = Packet(session, index, phase, gherkin, owned, tuple(checks), diff_ref, tuple(results), tuple(findings))
    if len(json.dumps(packet.to_dict())) > MAX_PACKET_BYTES:
        raise ValueError("Packet is too large; hand off less")
    return packet


def materialize(home: Path, packet: Packet) -> Path:
    """Write the packet as a read-only file under `home/packets/SESSION/`, and return its path.

    The directory is outside the repository and holds no database. The file is mode 0444 so a worker given its
    path can read but not edit it; replacing it takes a coordinator call. The packet itself is immutable.
    """
    directory = Path(home) / "packets" / packet.session
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"task-{packet.task_index}-{packet.phase}.json"
    if path.exists() or path.is_symlink():
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR) if not path.is_symlink() else None
        path.unlink()
    text = json.dumps(packet.to_dict(), ensure_ascii=False, indent=2) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)
    os.chmod(path, 0o444)
    return path


def load(path: Path) -> Packet:
    """Read and re-validate a packet file (what a worker-side reader uses)."""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Packet path must not be a symlink")
    if path.stat().st_size > MAX_PACKET_BYTES * 2:
        raise ValueError("Packet is too large")
    return parse(json.loads(path.read_text(encoding="utf-8")))
