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
# helper, no stale untracked cache.
_GIT = ("git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false")


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
