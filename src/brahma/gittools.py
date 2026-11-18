from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time

from .models import Event


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
                            text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


def repository(path: Path) -> Path:
    if not path.expanduser().is_dir():
        raise ValueError("Select an existing Git repository with --repo or /repo PATH")
    return Path(git(path.expanduser().resolve(), "rev-parse", "--show-toplevel"))


def create_worktree(repo: Path, home: Path, session: str, base_revision: str = "HEAD") -> Path:
    path = home / "worktrees" / session
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or repository(path) != path.resolve():
            raise ValueError("Saved worktree is not a valid owned Git worktree")
        left = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        right = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if left != right or git(path, "branch", "--show-current") != "brahma/" + session:
            raise ValueError("Saved worktree identity does not match this session")
        return path
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked changes exist. Commit/stash them yourself or select a clean worktree first")
    git(repo, "worktree", "add", "-b", "brahma/" + session, str(path), base_revision)
    return path


def changed_paths(repo: Path) -> list[str]:
    # NUL delimiters preserve spaces. Rename destinations and originals are both checked.
    result = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            capture_output=True, check=True, timeout=15)
    chunks = result.stdout.decode("utf-8", errors="strict").split("\0")
    paths, index = [], 0
    while index < len(chunks) and chunks[index]:
        row = chunks[index]
        paths.append(row[3:])
        if "R" in row[:2] or "C" in row[:2]:
            index += 1
            if index < len(chunks) and chunks[index]:
                paths.append(chunks[index])
        index += 1
    return sorted(set(paths))


def _fingerprint(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    if not path.is_file():
        return "absent"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return str(path.stat().st_mode) + ":" + digest.hexdigest()


def snapshot_changes(worktree: Path) -> dict[str, str]:
    return {name: _fingerprint(worktree / name) for name in changed_paths(worktree)}


def ownership_changes(worktree: Path, files: list[str], baseline=None) -> tuple[bool, str]:
    current = changed_paths(worktree)
    if baseline is None:
        paths = current
    else:
        paths = [name for name in sorted(set(current) | set(baseline)) if name not in baseline or
                 _fingerprint(worktree / name) != baseline[name]]
    outside = [name for name in paths if not any(fnmatch.fnmatchcase(name, pattern) or
                (pattern.endswith("/") and name.startswith(pattern)) for pattern in files)]
    sensitive = [name for name in paths if any(part == ".env" or part.startswith(".env.") or
                 part in (".brahma", "custody", ".git") or part.endswith((".pem", ".key"))
                 for part in Path(name).parts)]
    sensitive.extend(name for name in paths if (worktree / name).is_symlink() and
                     not (worktree / name).resolve().is_relative_to(worktree.resolve()))
    if outside or sensitive:
        return False, f"File ownership/sensitive-path review required: {', '.join(sorted(set(outside+sensitive)))}"
    return True, f"{len(paths)} changed paths fit approved ownership"


async def _stop(process):
    if process.returncode is not None:
        # No leader remains to checkpoint. Kill pipe-holding descendants in one
        # signal, avoiding a second signal to a group already reaped by the OS.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), 2)
    except asyncio.TimeoutError:
        pass
    finally:
        # A leader may exit while descendants still hold the output pipe open.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def run_checks(worktree, checks, cancel, timeout, emit):
    deadline = time.monotonic() + timeout
    commands = [["git", "-c", "core.hooksPath=/dev/null", "diff", "--check"], *checks]
    summaries = []
    for command in commands:
        if cancel.is_set() or time.monotonic() >= deadline:
            return False, "Checks cancelled or time budget exhausted"
        emit(Event(kind="check", text="Running approved check: " + " ".join(command)))
        try:
            process = await asyncio.create_subprocess_exec(*command, cwd=worktree,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except OSError:
            return False, "Approved check could not start: " + command[0]
        output = bytearray()
        async def drain():
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > 16000:
                    del output[:-16000]
            await process.wait()
        running = asyncio.create_task(drain())
        stopped = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait((running, stopped), timeout=max(0, deadline-time.monotonic()),
                                         return_when=asyncio.FIRST_COMPLETED)
            if running not in done or cancel.is_set():
                await _stop(process)
                await running
                return False, "Checks cancelled or timed out"
            summary = f"{' '.join(command)} => exit {process.returncode}\n" + output.decode(errors="replace")
            summaries.append(summary)
            emit(Event(kind="check", text=summary[-16000:]))
            if process.returncode:
                return False, "\n".join(summaries)
        finally:
            stopped.cancel()
            if process.returncode is None:
                await _stop(process)
            if not running.done():
                running.cancel()
    return True, "\n".join(summaries)


def commit_changes(worktree: Path, summary: str) -> str:
    paths = changed_paths(worktree)
    if not paths:
        return git(worktree, "rev-parse", "HEAD")
    ok, message = ownership_changes(worktree, ["**"])
    if not ok:
        raise ValueError(message)
    git(worktree, "add", "--", *paths)
    git(worktree, "commit", "-m", "Brahma: " + summary[:120])
    return git(worktree, "rev-parse", "HEAD")


def publish(worktree: Path, summary: str, body: str) -> str:
    import shutil
    if not shutil.which("gh"):
        raise ValueError("Draft PR publishing needs GitHub CLI: install gh and run gh auth login; local branch is preserved")
    branch = git(worktree, "branch", "--show-current")
    if not branch.startswith("brahma/"):
        raise ValueError("Only Brahma-owned branches may be published")
    git(worktree, "push", "--set-upstream", "origin", branch)
    result = subprocess.run(["gh", "pr", "create", "--draft", "--head", branch,
                             "--title", summary[:120], "--body-file", "-"], cwd=worktree,
                             input=body, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError("Draft PR creation failed; pushed branch is preserved. Check gh auth and repository access")
    return result.stdout.strip()
