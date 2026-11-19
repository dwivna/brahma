from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import time

from . import ownership, toolchain
from .models import Event


DEFAULT_BRANCHES = ("main", "master", "develop", "trunk", "production", "release")
_FORBIDDEN_COMMANDS = ("merge", "rebase", "reset", "cherry-pick", "revert", "filter-branch", "filter-repo",
                       "update-ref", "replace", "gc", "prune", "reflog", "am", "pull")
_FORCE_FLAGS = ("--force", "-f", "--force-with-lease", "--force-if-includes", "--delete", "-d", "--mirror",
                "--prune", "--all", "--tags", "--follow-tags", "--no-verify")


def refuse(args: tuple[str, ...]) -> str | None:
    """Hard blocks (Round 7/11): history rewriting, default-branch writes, merges and force pushes.

    Brahma only ever commits to its own `brahma/*` branch and pushes that branch. Anything else is
    refused here, in code, whatever a plan or a model asks for. Returns the reason, or None if allowed.
    """
    # Skip the values of git's own global options (`-C dir`, `-c k=v`) so they are never mistaken for the command.
    cleaned, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            skip = True
        else:
            cleaned.append(a)
    args = tuple(cleaned)
    words = [a for a in args if not a.startswith("-")]
    command = words[0] if words else ""
    if command in _FORBIDDEN_COMMANDS:
        return f"git {command} is blocked: Brahma never rewrites history or merges"
    if command == "commit" and any(a.startswith("--amend") for a in args):
        return "git commit --amend is blocked: Brahma never rewrites history"
    if command == "branch" and any(a in ("-d", "-D", "-m", "-M", "-f", "--delete", "--move", "--force") for a in args):
        return "Deleting, renaming or forcing branches is blocked"
    if command == "tag" and len(args) > 1:
        return "Creating or deleting tags is blocked: releases are Abhinav's decision"
    if command == "push":
        if any(a in _FORCE_FLAGS or (a.startswith("--force")) or a.startswith("--delete") for a in args):
            return "Force, delete, mirror or bulk pushes are blocked"
        refs = words[1:]
        targets = refs[1:] if refs else []  # first word is the remote
        if not targets:
            return "Push needs an explicit brahma/* branch"
        for ref in targets:
            source, _, destination = ref.partition(":")
            if ref.startswith(("+", ":")) or not source.startswith("brahma/") or (
                    destination and not destination.startswith("brahma/")):
                return "Only brahma/* branches may be pushed; default branches are never written"
    if command == "checkout" and any(a in ("-B", "--force", "-f") for a in args):
        return "Forced checkout is blocked"
    return None


_READ_ONLY = frozenset({"status", "diff", "rev-parse", "log", "show", "ls-files", "ls-tree", "cat-file", "check-ignore",
                        "symbolic-ref", "merge-base", "rev-list", "for-each-ref", "describe", "blame", "grep", "config"})


def _is_write(args: tuple[str, ...]) -> bool:
    """Anything not on the read-only list counts as a write, so the kill switch fails closed."""
    words = [a for a in args if not a.startswith("-")]
    if not words:
        return True
    if words[0] == "branch" and "--show-current" in args:
        return False
    return words[0] not in _READ_ONLY


def git(repo: Path, *args: str) -> str:
    reason = refuse(args)
    if reason:
        raise ValueError(reason)
    if _is_write(args):
        from . import halt
        from .config import state_home
        if halt.engaged(state_home()):
            raise ValueError("Kill switch engaged: coordinator Git writes are refused")
    result = subprocess.run(["git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
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
    result = subprocess.run(["git", "--literal-pathspecs", "-C", str(repo), "ls-files", "-o", "-i", "--exclude-standard", "-z", *extra],
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
    result = subprocess.run(["git", "--literal-pathspecs", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
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


def violating_paths(worktree: Path, files: list[str], baseline=None, ignored_allow=None) -> list[str]:
    """Paths changed since the prelaunch baseline that fall outside ownership or are sensitive/escaping."""
    current = changed_paths(worktree, ignored_allow)
    paths = current if baseline is None else [name for name in sorted(set(current) | set(baseline)) if name not in baseline
                                              or _fingerprint(worktree / name) != baseline[name]]
    bad = []
    for name in paths:
        link_escape = (worktree / name).is_symlink() and not (worktree / name).resolve().is_relative_to(worktree.resolve())
        if not any(ownership.owns(pattern, name) for pattern in files) or _sensitive(name) or link_escape:
            bad.append(name)
    return bad


def restore_violations(worktree: Path, files: list[str], baseline: dict, ignored_allow=None) -> tuple[list[str], list[str]]:
    """Undo attributable worker changes that violate ownership, on the isolated branch only.

    A path that was already changed in the prelaunch baseline (a pre-existing edit) cannot be separated from the
    worker's change, so it is never touched and is reported as not restored. A path absent from the baseline was
    clean at launch: tracked files go back to HEAD, new files (including ignored ones) are removed.
    Returns (restored, not_restored). Readiness stays blocked either way; this only limits the damage.
    """
    restored, kept = [], []
    root = worktree.resolve()
    # Exact comparison against the tracked list: a worker-chosen file name is never a pathspec (`:(glob)**`, `*`).
    listing = subprocess.run(["git", "--literal-pathspecs", "-C", str(worktree), "ls-files", "-z"],
                             capture_output=True, check=True, timeout=60).stdout
    tracked_files = set(listing.decode("utf-8", errors="surrogateescape").split("\0"))
    for name in violating_paths(worktree, files, baseline, ignored_allow):
        target = worktree / name
        if name in baseline:
            kept.append(name)
            continue
        if not (target.parent.resolve() / target.name).resolve(strict=False).is_relative_to(root) and not target.is_symlink():
            kept.append(name)  # never act outside the worktree
            continue
        tracked = name in tracked_files
        try:
            if tracked:
                git(worktree, "restore", "--source=HEAD", "--staged", "--worktree", "--", name)
            elif target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                kept.append(name)
                continue
            restored.append(name)
        except (OSError, ValueError):
            kept.append(name)
    return restored, kept


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
    env = child_env()          # already strict: workers and checks share one scrub
    env["GIT_TERMINAL_PROMPT"] = "0"
    from .toolchain import trusted_path
    env["PATH"] = trusted_path(env)       # a relative PATH entry would resolve inside the worker-writable worktree
    # Disable, rather than try to enumerate, inputs that redirect Maven's JVM or startup script: mavenrc files are shell code that can
    # override JAVA_HOME, MAVEN_BASEDIR chooses which .mvn/ is read, MAVEN_DEBUG_OPTS goes straight to the JVM.
    env["MAVEN_SKIP_RC"] = "1"
    for name in ("MAVEN_BASEDIR", "MAVEN_DEBUG_OPTS"):
        env.pop(name, None)
    if env.get("GRADLE_USER_HOME") and not env["GRADLE_USER_HOME"].startswith("/"):
        env.pop("GRADLE_USER_HOME")       # a relative value would resolve inside the worker-writable worktree
    return env


def scrub_secrets(env: dict[str, str]) -> dict[str, str]:
    """Remove secret-looking names, agent sockets and credential-file locations from an environment (in place)."""
    for name in list(env):
        key = name.upper()
        if (key in _CHECK_SECRET_NAMES or any(marker in key for marker in _CHECK_SECRET_MARKERS)
                or key.endswith(("_KEY", "_PASS", "_PWD", "_PASSWD", "_PAT", "_AUTH")) or key in ("KEY", "PASS")
                or key.startswith(("NPM_CONFIG_", "PG")) and any(m in key for m in ("PASS", "AUTH", "TOKEN"))):
            env.pop(name)
    for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO", "GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(name, None)
    return env


async def run_checks(worktree, checks, cancel, timeout, emit, results=None, bound=None):
    """Run approved checks. If `results` is a list, one (argv, status, exit_code) per command is appended as the
    coordinator observed it (status passed|failed|not_run), for handoff packets."""
    deadline = time.monotonic() + timeout
    commands = [["git", "-c", "core.hooksPath=/dev/null", "diff", "--check"], *checks]
    summaries = []
    for command in commands:
        if cancel.is_set() or time.monotonic() >= deadline:
            return False, "Checks cancelled or time budget exhausted"
        emit(Event(kind="check", text="Running approved check: " + " ".join(command)))
        environment = check_env()
        argv, problem = toolchain.verify_before_run(command, worktree, bound, environment)       # includes the implicit git diff --check
        if problem:
            emit(Event(kind="check", text=problem))
            if results is not None:
                results.append((tuple(command), "not_run", None))
            raise toolchain.ToolchainChanged(problem)       # an environment change, not a test failure: no repair is spent
        try:
            process = await asyncio.create_subprocess_exec(*argv, cwd=worktree, env=environment,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except OSError:
            if results is not None:
                results.append((tuple(command), "failed", None))
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
                if results is not None:
                    results.append((tuple(command), "not_run", None))
                await _stop(process)
                await running
                return False, "Checks cancelled or timed out"
            summary = f"{' '.join(command)} => exit {process.returncode}\n" + output.decode(errors="replace")
            summaries.append(summary)
            emit(Event(kind="check", text=summary[-16000:]))
            if results is not None:
                results.append((tuple(command), "failed" if process.returncode else "passed", process.returncode))
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
    from . import safety
    summary, body = safety.redact(summary), safety.redact(body)     # never send a secret-shaped string to GitHub
    if not shutil.which("gh"):
        raise ValueError("Draft PR publishing needs GitHub CLI: install gh and run gh auth login; local branch is preserved")
    branch = git(worktree, "branch", "--show-current")
    if not branch.startswith("brahma/"):
        raise ValueError("Only Brahma-owned branches may be published")
    git(worktree, "push", "--set-upstream", "origin", f"{branch}:{branch}")  # explicit destination: never a default branch
    result = subprocess.run(["gh", "pr", "create", "--draft", "--head", branch,
                             "--title", summary[:120], "--body-file", "-"], cwd=worktree,
                             input=body, text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError("Draft PR creation failed; pushed branch is preserved. Check gh auth and repository access")
    return result.stdout.strip()
