"""Read-only Git queries that repository or environment configuration cannot turn into running a program.

Every Git call Brahma makes uses this prefix and environment: no hooks, no fsmonitor helper, no stale untracked cache, and
no Git configuration, repository, index or object-store location inherited from the environment. Kept separate from
gittools (which can write) so read-only modules can depend on it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


# Never let repository configuration run code or hide paths while Brahma inspects a worktree: no hooks, no fsmonitor
# helper, no stale untracked cache, and Git's full change detection. `core.trustctime=false`, `core.checkStat=minimal`
# or `core.ignoreStat=true` would let a same-size edit with its modification time put back go unreported; the inode
# change time (ctime) cannot be put back by an unprivileged process, so with these pinned every edit is seen.
_GIT = ("git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false", "-c", "core.trustctime=true", "-c", "core.checkStat=default",
        "-c", "core.ignoreStat=false", "-c", "diff.ignoreSubmodules=none")


# Environment variables that would inject Git configuration (which can name a program to run) or point Git at another
# repository, index or object store. Brahma's own Git calls and the implicit `git diff --check` never inherit them.
_GIT_ENV_OVERRIDES = ("GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                      "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
                      "GIT_EXTERNAL_DIFF", "GIT_EXEC_PATH", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM")


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for name in list(env):
        if name in _GIT_ENV_OVERRIDES or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(name)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def run_git(repo: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """A read-only Git query with the same protections as git(): for modules that need the raw result."""
    return subprocess.run([*_GIT, "-C", str(repo), *args], text=True, capture_output=True, timeout=timeout, env=git_env())


def _filter_drivers(repo: Path) -> set[str]:
    """Names of filter drivers that name a program (clean, smudge or process) in any Git configuration scope."""
    listed = run_git(repo, "config", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$")
    names = set()
    for line in listed.stdout.splitlines():
        key = line.split(" ", 1)[0]
        if line.split(" ", 1)[1:] and line.split(" ", 1)[1].strip():
            names.add(key[len("filter."):key.rindex(".")])
    return names


def filtered_paths(repo: Path, paths: list[str]) -> list[str]:
    """Paths whose `filter` attribute selects a configured driver: Git would run a helper program on them (clean on add
    or status, smudge on checkout) and their committed bytes would differ from the files that were checked."""
    drivers = _filter_drivers(repo)
    if not drivers or not paths:
        return []
    result = subprocess.run([*_GIT, "-C", str(repo), "check-attr", "-z", "--stdin", "filter"],
                            input="\0".join(paths).encode("utf-8", "surrogateescape"), capture_output=True,
                            timeout=60, env=git_env())
    if result.returncode:
        raise ValueError("Git attributes could not be read: " + result.stderr.decode(errors="replace")[:300])
    fields = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    return sorted({fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] in drivers})


def unsupported_repository(repo: Path, revision: str = "HEAD") -> str | None:
    """Repository features Brahma 1.0 cannot verify, or None. Submodules: their contents are another repository's and
    are not ownership-checked. Filter drivers (Git LFS and similar): Git runs their helper programs outside the approved
    checks, and the committed bytes differ from the checked files."""
    listing = subprocess.run([*_GIT, "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", revision],
                             capture_output=True, timeout=60, env=git_env())
    if listing.returncode:
        raise ValueError("The repository revision could not be listed: " + listing.stderr.decode(errors="replace")[:300])
    entries = [row for row in listing.stdout.decode("utf-8", "surrogateescape").split("\0") if row]
    if any(row.startswith("160000 ") for row in entries) or (repo / ".gitmodules").exists():
        return "This repository uses Git submodules, which Brahma 1.0 does not support (their contents cannot be verified)"
    filtered = filtered_paths(repo, [row.split("\t", 1)[1] for row in entries if "\t" in row])
    if filtered:
        return ("This repository uses a Git filter driver (for example Git LFS) on " + ", ".join(filtered[:5])
                + ("…" if len(filtered) > 5 else "") + "; Brahma 1.0 does not support filter drivers")
    return None
