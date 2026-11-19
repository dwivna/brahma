"""Bind the approved checks to the toolchain that will run them (found by live run 3: a service-run `mvn` picked a
different JDK than the terminal's, and nothing in the approval said which one was approved).

The contract records, for every distinct check program, the file it resolves to, the SHA-256 of that file, and what it
reports as its own version (for Maven this includes the JDK it runs on). Verification re-probes and compares, so a changed
JDK, a swapped binary or a different PATH makes the approval fail closed with a message naming what moved.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

PROBE_SECONDS = 20
MAX_HASH_BYTES = 256 * 1024 * 1024
# Programs whose own version output does not come from `--version`.
_VERSION_FLAGS = {"java": ["-version"], "javac": ["-version"], "go": ["version"]}
# Variables that decide which toolchain a check picks up; recorded by name and value (no secrets live here).
_TOOL_VARIABLES = ("JAVA_HOME", "MAVEN_HOME", "M2_HOME", "GRADLE_HOME", "NODE_VERSION", "VIRTUAL_ENV", "GOROOT")


def _hash(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_HASH_BYTES:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _resolve(program: str, repo: Path, env: dict[str, str]) -> tuple[Path | None, bool]:
    """Return (resolved file, lives_in_repo). Programs inside the repository (./mvnw) are hashed, never executed."""
    if "/" in program:
        candidate = (repo / program) if not os.path.isabs(program) else Path(program)
        try:
            candidate = candidate.resolve(strict=True)
        except OSError:
            return None, False
        return candidate, candidate.is_relative_to(repo.resolve())
    found = shutil.which(program, path=env.get("PATH", os.defpath))
    if not found:
        return None, False
    resolved = Path(found).resolve()
    return resolved, resolved.is_relative_to(repo.resolve())


def _version(program: str, resolved: Path, env: dict[str, str]) -> str:
    flags = _VERSION_FLAGS.get(Path(program).name, ["--version"])
    try:
        process = subprocess.run([str(resolved), *flags], capture_output=True, text=True, timeout=PROBE_SECONDS,
                                 env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = (process.stdout + "\n" + process.stderr).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()][:6]
    return " | ".join(lines)[:600] if process.returncode == 0 and lines else "unknown"


def probe(checks: list[list[str]], repo: Path, env: dict[str, str] | None = None) -> dict:
    """Describe the toolchain the checks would use right now. Deterministic for an unchanged machine."""
    if env is None:
        from .gittools import check_env
        env = check_env()
    repo = Path(repo)
    programs: dict[str, dict] = {}
    for command in checks:
        if not command or command[0] in programs:
            continue
        program = command[0]
        resolved, in_repo = _resolve(program, repo, env)
        if resolved is None:
            programs[program] = {"path": None, "sha256": None, "version": "not found"}
            continue
        programs[program] = {"path": str(resolved), "sha256": _hash(resolved),
                             "version": "inside repository; not executed" if in_repo else _version(program, resolved, env)}
    variables = {name: env[name] for name in _TOOL_VARIABLES if name in env}
    return {"programs": programs, "environment": variables}


def describe(toolchain: dict) -> list[str]:
    """Readable lines for the approval screen."""
    lines = []
    for program, info in sorted(toolchain.get("programs", {}).items()):
        lines.append(f"Toolchain {program}: {info['path'] or 'NOT FOUND'}; {info['version']}")
    for name, value in sorted(toolchain.get("environment", {}).items()):
        lines.append(f"Toolchain {name}={value}")
    return lines


def differences(approved: dict, current: dict) -> list[str]:
    """Plain statements of what moved between the approved toolchain and this machine's."""
    moved = []
    for program in sorted(set(approved.get("programs", {})) | set(current.get("programs", {}))):
        before, after = approved.get("programs", {}).get(program), current.get("programs", {}).get(program)
        if before != after:
            was = (before or {}).get("version", "absent")
            now = (after or {}).get("version", "absent")
            where = ""
            if (before or {}).get("path") != (after or {}).get("path"):
                where = f" ({(before or {}).get('path')} -> {(after or {}).get('path')})"
            moved.append(f"{program}: approved '{was}', now '{now}'{where}")
    for name in sorted(set(approved.get("environment", {})) | set(current.get("environment", {}))):
        before, after = approved.get("environment", {}).get(name), current.get("environment", {}).get(name)
        if before != after:
            moved.append(f"{name}: approved {before!r}, now {after!r}")
    return moved
