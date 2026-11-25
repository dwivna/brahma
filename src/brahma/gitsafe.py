"""Read-only Git queries that repository or environment configuration cannot turn into running a program.

Every Git call Brahma makes uses this prefix and environment: no hooks, no fsmonitor helper, no stale untracked cache, and
no Git configuration, repository, index or object-store location inherited from the environment. Kept separate from
gittools (which can write) so read-only modules can depend on it.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


# Never let repository configuration run code or hide paths while Brahma inspects a worktree: no hooks, no fsmonitor
# helper, no stale untracked cache, and Git's full change detection. `core.trustctime=false`, `core.checkStat=minimal`
# or `core.ignoreStat=true` would let a same-size edit with its modification time put back go unreported; the inode
# change time (ctime) cannot be put back by an unprivileged process, so with these pinned every edit is seen.
# Attributes are read from the empty tree (and no global attributes file): no filter driver program can run on any
# Brahma Git call (status, add, diff, checkout), whatever .gitattributes a worker writes, and no line-ending or ident
# conversion applies, so Git compares and stores exactly the bytes that were checked. File modes are always compared.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"     # SHA-1 repositories only (SHA-256 ones are refused)
# Replacement objects (`refs/replace/`) are never used: one could swap the tree of the recorded starting commit while
# HEAD, branch and Git directory all stay the same, making unowned edits look clean.
_GIT = ("git", "--literal-pathspecs", "--no-replace-objects", f"--attr-source={EMPTY_TREE}", "-c", "core.attributesFile=/dev/null",
        "-c", "core.autocrlf=false", "-c", "core.fileMode=true", "-c", "core.symlinks=true", "-c", "core.hooksPath=/dev/null",
        "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "core.trustctime=true",
        "-c", "core.checkStat=default", "-c", "core.ignoreStat=false", "-c", "diff.ignoreSubmodules=none",
        # No signing helper (gpg, ssh-keygen, x509) and no automatic background gc/maintenance: Brahma's commits run
        # no program the approval did not cover.
        "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false", "-c", "push.gpgSign=false", "-c", "gc.auto=0",
        "-c", "maintenance.auto=false",
        # No remote that is a command (ext::), whatever any configuration scope allows.
        "-c", "protocol.ext.allow=never")


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
    env["GIT_ATTR_NOSYSTEM"] = "1"           # no system-wide attributes file: attributes come only from the empty tree
    env["GIT_NO_REPLACE_OBJECTS"] = "1"      # also for any Git a check runs: replacement objects never apply
    return env


def pinned_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """git_env plus the protected settings of the Git prefix as environment configuration, for a program that runs Git
    itself (gh): its Git calls get the same pins (no hooks, no fsmonitor, ...) as Brahma's own, and no pager."""
    env = git_env(base)
    pairs = [_GIT[i + 1] for i, part in enumerate(_GIT[:-1]) if part == "-c"] + ["core.pager=cat"]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for number, pair in enumerate(pairs):
        key, _, value = pair.partition("=")
        env[f"GIT_CONFIG_KEY_{number}"], env[f"GIT_CONFIG_VALUE_{number}"] = key, value
    return env


# Repository-local settings that can redirect a push or run a command during publication and cannot be pinned without
# breaking ordinary setups (keys as `git config --list` prints them: section and name lowercased).
_PUBLICATION_RISKS = re.compile(r"(?s)^(core\.(sshcommand|askpass|gitproxy)|credential\..*|protocol\..*|gpg\..*|push\.gpgsign|"
                                r"remote\..*\.(pushurl|receivepack|uploadpack|proxy|vcs)|url\..*\.(insteadof|pushinsteadof)|"
                                r"include\..*|includeif\..*|ssh\..*|http\..*)$")


def publication_risks(repo: Path) -> list[str]:
    """Settings in the repository's own configuration (local or worktree scope, which a process able to write the
    repository's Git directory could plant) that could redirect a push or run a command while publishing."""
    try:
        listing = subprocess.run([*_GIT, "-C", str(repo), "config", "--show-scope", "--list", "-z"], capture_output=True,
                                 check=True, timeout=30, env=git_env()).stdout.decode("utf-8", errors="surrogateescape")
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"Not published: the repository's Git configuration could not be read ({error})") from None
    fields = listing.split("\0")
    found = []
    for scope, entry in zip(fields[0::2], fields[1::2]):
        key = entry.split("\n", 1)[0]
        if scope in ("local", "worktree") and _PUBLICATION_RISKS.match(key) and key not in found:
            found.append(key)
    return found


def attribute_override(repo: Path) -> str | None:
    """`$GIT_DIR/info/attributes` is read by every Git command whatever `--attr-source` says, so a filter or conversion
    attribute there (written after approval) would run a program or change bytes on Brahma's own Git calls. Brahma
    1.0 refuses to operate while that file has any content."""
    key = str(Path(repo).absolute())
    # Looked up on every call, never cached: a repointed `.git` file must not leave an old directory being checked.
    # rev-parse reads no attributes. Both the worktree's own Git directory and the shared one are checked.
    found = subprocess.run(["git", "-C", key, "rev-parse", "--absolute-git-dir", "--git-common-dir"], text=True,
                           capture_output=True, timeout=30, env=git_env())
    lines = found.stdout.splitlines()
    if found.returncode or len(lines) != 2:
        return None                          # not a repository: the Git command itself reports that
    common = Path(lines[1]) if Path(lines[1]).is_absolute() else Path(key) / lines[1]
    for directory in (Path(lines[0]), common.resolve()):
        for name, what in (("attributes", "sets Git attributes for this repository; Brahma 1.0 does not support "
                                          "repository-local attributes (they can run a filter program or change "
                                          "committed bytes)"),
                           ("grafts", "rewrites commit history (grafts); Brahma 1.0 does not support it")):
            path = directory / "info" / name
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError:
                return f"{path} could not be inspected"
            if info.st_size or not os.path.isfile(path) or os.path.islink(path):
                return f"{path} {what}. Empty or remove that file"
    return None


def guarded(repo: Path) -> list[str]:
    """The protected Git command prefix for `repo`, after refusing repository-local attribute overrides."""
    problem = attribute_override(repo)
    if problem:
        raise ValueError(problem)
    return [*_GIT, "-C", str(repo)]


def run_git(repo: Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """A read-only Git query with the same protections as git(): for modules that need the raw result."""
    return subprocess.run([*guarded(repo), *args], text=True, capture_output=True, timeout=timeout, env=git_env())


def _filter_drivers(repo: Path) -> set[str]:
    """Names of filter drivers that name a program (clean, smudge or process) in any Git configuration scope."""
    listed = run_git(repo, "config", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$")
    names = set()
    for line in listed.stdout.splitlines():
        key = line.split(" ", 1)[0]
        if line.split(" ", 1)[1:] and line.split(" ", 1)[1].strip():
            names.add(key[len("filter."):key.rindex(".")])
    return names


def filtered_paths(repo: Path, paths: list[str], revision: str | None = None) -> list[str]:
    """Paths whose `filter` attribute selects a configured driver: Git would run a helper program on them (clean on add
    or status, smudge on checkout) and their committed bytes would differ from the files that were checked."""
    drivers = _filter_drivers(repo)
    if not drivers or not paths:
        return []
    # With `revision`, the attributes are those of that revision (what a checkout of it would apply), not the working tree.
    source = [f"--source={revision}"] if revision else []
    result = subprocess.run([*guarded(repo), "check-attr", *source, "-z", "--stdin", "filter"],
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
    override = attribute_override(repo)
    if override:
        return override
    if run_git(repo, "rev-parse", "--show-object-format").stdout.strip() not in ("", "sha1"):
        return "This repository uses the SHA-256 object format, which Brahma 1.0 does not support"
    listing = subprocess.run([*guarded(repo), "ls-tree", "-r", "-z", "--full-tree", revision],
                             capture_output=True, timeout=60, env=git_env())
    if listing.returncode:
        raise ValueError("The repository revision could not be listed: " + listing.stderr.decode(errors="replace")[:300])
    entries = [row for row in listing.stdout.decode("utf-8", "surrogateescape").split("\0") if row]
    gitmodules = run_git(repo, "cat-file", "-e", f"{revision}:.gitmodules").returncode == 0
    if any(row.startswith("160000 ") for row in entries) or gitmodules or (repo / ".gitmodules").exists():
        return "This repository uses Git submodules, which Brahma 1.0 does not support (their contents cannot be verified)"
    filtered = filtered_paths(repo, [row.split("\t", 1)[1] for row in entries if "\t" in row], revision)
    if filtered:
        return ("This repository uses a Git filter driver (for example Git LFS) on " + ", ".join(filtered[:5])
                + ("…" if len(filtered) > 5 else "") + "; Brahma 1.0 does not support filter drivers")
    return None
