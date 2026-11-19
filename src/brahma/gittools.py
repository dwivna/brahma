from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time

from . import ownership
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


# Build caches that checks legitimately create inside an ignored directory (Round 13).
DEFAULT_IGNORED_ALLOWLIST = (".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                             "node_modules", "dist", "*.egg-info")


def _allowed_ignored(path: str, allow, is_dir: bool = False) -> bool:
    """True when `path` lies inside an allowlisted build-cache directory.

    The allowlist names directories: a *file* or symlink that merely carries an allowlisted
    name (for example a symlink called `.venv`) is never tolerated.
    """
    parts = Path(path).parts
    candidates = parts if is_dir else parts[:-1]
    return any(fnmatch.fnmatchcase(part, pattern) for part in candidates for pattern in allow)


# Inside an allowlisted cache directory only these names are still inspected. A broader scan
# would flag ordinary packages (certifi's cacert.pem, botocore/credentials.py).
_CACHE_SENSITIVE_PREFIXES = (".env",)


def _ls_ignored(repo: Path, *extra: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(repo), "ls-files", "-o", "-i", "--exclude-standard", "-z", *extra],
                            capture_output=True, check=True, timeout=60)
    return [name for name in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if name]


def ignored_paths(repo: Path, allow=None) -> list[str]:
    """Git-ignored files that exist, minus the allowlisted build caches.

    `git status` never reports ignored files, so a worker could create or alter `.env` or
    a planted script unseen. Fully ignored directories are listed once; an allowlisted one is
    not ownership-checked but is still scanned for `.env*` files (a planted secret file), and
    any other ignored directory is expanded to its files. Remaining blind spot: other files
    hidden inside an allowlisted directory are not inspected.
    """
    allow = DEFAULT_IGNORED_ALLOWLIST if allow is None else tuple(allow)
    found = []
    for name in _ls_ignored(repo, "--directory"):
        is_dir = name.endswith("/")
        if _allowed_ignored(name.rstrip("/"), allow, is_dir):
            if is_dir:
                found.extend(item for item in _ls_ignored(repo, "--", name)
                             if Path(item).name.startswith(_CACHE_SENSITIVE_PREFIXES))
            continue
        if is_dir:
            found.extend(item for item in _ls_ignored(repo, "--", name)
                         if not _allowed_ignored(item, allow))
        else:
            found.append(name)
    return found


def changed_paths(repo: Path, ignored_allow=None) -> list[str]:
    """Every changed path for ownership review: tracked/untracked changes plus git-ignored files."""
    return sorted(set(status_paths(repo)) | set(ignored_paths(repo, ignored_allow)))


def status_paths(repo: Path) -> list[str]:
    """Changes `git status` reports (never ignored files); these are the only paths git can stage."""
    # NUL delimiters preserve spaces. Rename destinations and originals are both checked.
    result = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            capture_output=True, check=True, timeout=15)
    chunks = result.stdout.decode("utf-8", errors="surrogateescape").split("\0")
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


_FINGERPRINT_CAP = 64 * 1024 * 1024


def _fingerprint(path: Path) -> str:
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    if not path.is_file():
        return "absent"
    info = path.stat()
    if info.st_size > _FINGERPRINT_CAP:  # huge artefacts: size+mtime, not a full read on every check
        return f"large:{info.st_mode}:{info.st_size}:{info.st_mtime_ns}"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            digest.update(chunk)
    return str(path.stat().st_mode) + ":" + digest.hexdigest()


def snapshot_changes(worktree: Path, ignored_allow=None) -> dict[str, str]:
    return {name: _fingerprint(worktree / name) for name in changed_paths(worktree, ignored_allow)}


_SENSITIVE_PREFIXES = (".env", "credentials", ".credentials")


def _sensitive(path: str) -> bool:
    return any(part in (".brahma", "custody", ".git") or part.startswith(_SENSITIVE_PREFIXES)
               or part.endswith((".pem", ".key")) for part in Path(path).parts)


def ownership_changes(worktree: Path, files: list[str], baseline=None, ignored_allow=None) -> tuple[bool, str]:
    current = changed_paths(worktree, ignored_allow)
    if baseline is None:
        paths = current
    else:
        paths = [name for name in sorted(set(current) | set(baseline)) if name not in baseline or
                 _fingerprint(worktree / name) != baseline[name]]
    outside = [name for name in paths if not any(ownership.owns(pattern, name) for pattern in files)]
    sensitive = [name for name in paths if _sensitive(name)]
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


_CHECK_SECRET_MARKERS = ("AUTHTOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "PRIVATE_KEY", "DSN",
                         "WEBHOOK", "KUBECONFIG", "NETRC")
_CHECK_SECRET_NAMES = {"DATABASE_URL", "REDIS_URL", "MONGODB_URI", "DB_PASS", "AWS_PROFILE", "DOCKER_CONFIG",
                       "GIT_CONFIG_GLOBAL", "NPM_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "HF_TOKEN"}


def check_env() -> dict[str, str]:
    """Environment for approved checks: worker-written code runs here, so no credentials.

    The same scrubbing as provider workers, plus agent sockets, secret-looking names and common
    credential-file locations. This is a denylist and cannot be complete: unusual variable names can
    survive, HOME and PATH are kept, and credential *files* under HOME (.aws, .npmrc, .netrc, .ssh, gh
    config) stay readable. A sandbox for checks is not provided (Round 13).
    """
    from .providers import child_env
    env = child_env()
    for name in list(env):
        key = name.upper()
        if (key in _CHECK_SECRET_NAMES or any(marker in key for marker in _CHECK_SECRET_MARKERS)
                or key.endswith(("_KEY", "_PASS", "_PWD")) or key in ("KEY", "PASS")):
            env.pop(name)
    for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO", "GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(name, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


async def run_checks(worktree, checks, cancel, timeout, emit):
    deadline = time.monotonic() + timeout
    commands = [["git", "-c", "core.hooksPath=/dev/null", "diff", "--check"], *checks]
    summaries = []
    for command in commands:
        if cancel.is_set() or time.monotonic() >= deadline:
            return False, "Checks cancelled or time budget exhausted"
        emit(Event(kind="check", text="Running approved check: " + " ".join(command)))
        try:
            process = await asyncio.create_subprocess_exec(*command, cwd=worktree, env=check_env(),
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
    ok, message = ownership_changes(worktree, ["**"])
    if not ok:
        raise ValueError(message)
    # Owned git-ignored files are reviewed above but never staged (`git add` refuses them).
    paths = status_paths(worktree)
    if not paths:
        return git(worktree, "rev-parse", "HEAD")
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
