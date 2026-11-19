"""Bind the approved checks to the toolchain that will run them (found by live run 3: a service-run `mvn` picked a
different JDK than the terminal's, and nothing in the approval said which one was approved).

Nothing is executed to learn this. A plan is proposed by a model and the toolchain is read while it is still unapproved, so
probing by running `<program> --version` would let an unapproved program run (and `mvn --version` reads repository
`.mvn/jvm.config`). Instead the contract records, for every distinct check program, the regular file it resolves to and that
file's SHA-256, plus for Java-family programs the JDK's `java` binary hash and its `release` file version. Relative PATH
entries are never searched, so a worker-created `.venv/bin` shim cannot take over a command.

The same fingerprint is re-taken immediately before each check runs, against the directory the check will actually run in,
and the verified absolute path is what gets executed. Limits, stated plainly: the hash covers the launched file, not
everything it loads (Maven's jars, a Python environment's packages); programs that delegate to another runtime
(`uv run`, `npx`, `sh -c`, `docker`) are recorded as launchers whose delegated runtime is NOT bound, and the approval screen warns.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
from pathlib import Path

MAX_HASH_BYTES = 128 * 1024 * 1024
_JAVA_FAMILY = {"java", "javac", "mvn", "mvnw", "gradle", "gradlew", "ant", "sbt", "kotlinc"}
_LAUNCHERS = {"uv", "uvx", "npx", "pipx", "poetry", "env", "sh", "bash", "zsh", "dash", "xargs", "docker", "podman",
              "nix", "nix-shell", "make", "time", "timeout", "nohup", "sudo"}
# Variables that decide which toolchain a program picks up, and only for the programs they steer.
_JAVA_VARIABLES = ("JAVA_HOME", "MAVEN_HOME", "M2_HOME", "GRADLE_HOME")
_VERSION_TEXT = re.compile(r"[0-9A-Za-z._+-]{1,40}")


def _fingerprint(path: Path) -> str | None:
    """SHA-256 of a regular file; None for anything else, unreadable, or larger than the bound. Never follows into devices."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_HASH_BYTES:
            return None
        digest, remaining = hashlib.sha256(), MAX_HASH_BYTES + 1
        with path.open("rb") as handle:
            while remaining > 0 and (chunk := handle.read(min(1 << 20, remaining))):
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest() if remaining > 0 else None
    except OSError:
        return None


def trusted_path(env: dict[str, str]) -> str:
    """PATH without relative or empty entries: a relative entry resolves inside the worktree, which workers can write."""
    return os.pathsep.join(entry for entry in env.get("PATH", os.defpath).split(os.pathsep) if entry and os.path.isabs(entry))


def resolve(program: str, directory: Path, env: dict[str, str]) -> tuple[Path | None, bool]:
    """(absolute file, lives_in_directory). A program written with a slash is relative to `directory`; a bare name is found only
    on the absolute entries of PATH."""
    directory = Path(directory).resolve()
    if "/" in program:
        candidate = Path(program) if os.path.isabs(program) else directory / program
    else:
        found = shutil.which(program, path=trusted_path(env))
        if not found:
            return None, False
        candidate = Path(found)
    try:
        candidate = candidate.resolve(strict=True)
    except OSError:
        return None, False
    return candidate, candidate.is_relative_to(directory)


def _java_runtime(env: dict[str, str]) -> dict:
    home = env.get("JAVA_HOME", "")
    java = None
    if home and os.path.isabs(home):
        java = Path(home) / "bin" / "java"
    else:
        found = shutil.which("java", path=trusted_path(env))
        java = Path(found) if found else None
    if java is None:
        return {"path": None, "sha256": None, "release": None}
    try:
        java = java.resolve(strict=True)
    except OSError:
        return {"path": str(java), "sha256": None, "release": None}
    release = None
    for candidate in (java.parent.parent / "release", Path(home) / "release" if home else None):
        if candidate is not None and candidate.is_file() and candidate.stat().st_size < 65536:
            try:
                for line in candidate.read_text(errors="replace").splitlines():
                    if line.startswith("JAVA_VERSION="):
                        value = line.split("=", 1)[1].strip().strip('"')
                        release = value if _VERSION_TEXT.fullmatch(value) else "unrecognised"
                        break
            except OSError:
                pass
            if release:
                break
    return {"path": str(java), "sha256": _fingerprint(java), "release": release}


def fingerprint_program(program: str, directory: Path, env: dict[str, str]) -> dict:
    """Describe one program as it would run from `directory`. Pure reads: nothing is executed."""
    directory = Path(directory).resolve()
    resolved, in_repo = resolve(program, directory, env)
    name = Path(program).name
    if resolved is None:
        return {"path": None, "sha256": None, "launcher": name in _LAUNCHERS, "runtime": None, "variables": {}}
    path = resolved.relative_to(directory).as_posix() if in_repo else str(resolved)
    info = {"path": path, "in_repo": in_repo, "sha256": _fingerprint(resolved), "launcher": name in _LAUNCHERS,
            "runtime": None, "variables": {}}
    if name in _JAVA_FAMILY:
        info["runtime"] = _java_runtime(env)
        info["variables"] = {key: env[key] for key in _JAVA_VARIABLES if key in env}
    elif name == "go" and "GOROOT" in env:
        info["variables"] = {"GOROOT": env["GOROOT"]}
    return info


def probe(checks: list[list[str]], directory: Path, env: dict[str, str] | None = None) -> dict:
    """The toolchain the checks would use right now. Deterministic for an unchanged machine; executes nothing."""
    if env is None:
        from .gittools import check_env
        env = check_env()
    programs: dict[str, dict] = {}
    for command in checks:
        if command and command[0] not in programs:
            programs[command[0]] = fingerprint_program(command[0], directory, env)
    return {"programs": programs}


def describe(toolchain: dict) -> list[str]:
    """Readable approval lines, including the file hash so the approval shows what it binds."""
    lines = []
    for program, info in sorted(toolchain.get("programs", {}).items()):
        where = info.get("path") or "NOT FOUND"
        short = (info.get("sha256") or "unhashed")[:12]
        line = f"Toolchain {program}: {where} sha256:{short}"
        runtime = info.get("runtime")
        if runtime:
            line += f"; JDK {runtime.get('release') or 'version unknown'} at {runtime.get('path') or 'none found'} sha256:{(runtime.get('sha256') or 'unhashed')[:12]}"
        for name, value in sorted(info.get("variables", {}).items()):
            line += f"; {name}={value}"
        lines.append(line)
        if info.get("launcher"):
            lines.append(f"WARNING: {program} launches other programs; the runtime it delegates to is not bound by this approval.")
        if info.get("path") and not info.get("sha256"):
            lines.append(f"WARNING: {program} could not be fingerprinted (not a regular file or too large); it is not bound.")
    return lines


def differences(approved: dict, current: dict) -> list[str]:
    """Plain statements of what moved. Hashes are shortened; nothing from program output is ever included."""
    def label(info: dict | None) -> str:
        if not info:
            return "absent"
        text = f"{info.get('path') or 'not found'} sha256:{(info.get('sha256') or 'unhashed')[:12]}"
        runtime = info.get("runtime")
        if runtime:
            text += f"; JDK {runtime.get('release') or '?'} {runtime.get('path')} sha256:{(runtime.get('sha256') or 'unhashed')[:12]}"
        for name, value in sorted(info.get("variables", {}).items()):
            text += f"; {name}={value}"
        return text
    moved = []
    before_all, after_all = approved.get("programs", {}), current.get("programs", {})
    for program in sorted(set(before_all) | set(after_all)):
        if before_all.get(program) != after_all.get(program):
            moved.append(f"{program}: approved [{label(before_all.get(program))}], now [{label(after_all.get(program))}]")
    return moved


def verify_before_run(command: list[str], directory: Path, bound: dict | None, env: dict[str, str]) -> tuple[list[str], str | None]:
    """Re-take the fingerprint where the check will really run. Returns (argv to execute, problem). The argv carries the verified
    absolute path, so the program that was hashed is the one that runs. With nothing bound the command runs unchanged."""
    if not bound:
        return command, None
    expected = bound.get("programs", {}).get(command[0])
    if expected is None:
        return command, f"Check program {command[0]} is not part of the approved toolchain"
    actual = fingerprint_program(command[0], directory, env)
    if actual != expected:
        return command, "Toolchain changed since approval, so the check was not run: " + "; ".join(
            differences({"programs": {command[0]: expected}}, {"programs": {command[0]: actual}}))
    resolved, _ = resolve(command[0], directory, env)
    return [str(resolved), *command[1:]], None
