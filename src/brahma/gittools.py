from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import stat
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

from . import gitsafe, ownership, toolchain
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
            destination = destination.removeprefix("refs/heads/")
            # A brahma/* branch, or an exact commit (40 hex digits) sent to an explicit brahma/* destination.
            exact = bool(re.fullmatch(r"[0-9a-f]{40}", source)) and destination.startswith("brahma/")
            if ref.startswith(("+", ":")) or not (source.startswith("brahma/") or exact) or (
                    destination and not destination.startswith("brahma/")):
                return "Only brahma/* branches may be pushed; default branches are never written"
    if command == "checkout" and any(a in ("-B", "--force", "-f") for a in args):
        return "Forced checkout is blocked"
    return None


from .gitsafe import _GIT, git_env, guarded, run_git  # noqa: F401  (re-exported)


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
    result = subprocess.run([*guarded(repo), *args],
                            text=True, capture_output=True, timeout=60, env=git_env())
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
    from .gitsafe import unsupported_repository
    unsupported = unsupported_repository(repo, base_revision)      # before checkout, which would run a smudge filter
    if unsupported:
        raise ValueError(unsupported)
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked changes exist. Commit/stash them yourself or select a clean worktree first")
    git(repo, "worktree", "add", "-b", "brahma/" + session, str(path), base_revision)
    return path


# Build caches that checks legitimately create inside an ignored directory (Round 13).
DEFAULT_IGNORED_ALLOWLIST = (".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                             "node_modules", "dist", "*.egg-info")


def _ignored_directories(repo: Path) -> set[str]:
    """Directories Git ignores as a whole (listed once by `ls-files --directory`), without the trailing slash."""
    return {name.rstrip("/") for name in _ls_ignored(repo, "--directory") if name.endswith("/")}


def _allowed_ignored(path: str, allow, ignored_dirs: set[str], is_dir: bool = False) -> bool:
    """True when `path` lies inside an allowlisted build-cache directory that Git really ignores (the directory or one
    of its parents is an ignored directory). One rule for the ignored-file scan and the special-file walk: an ordinary
    tracked `dist/` gives no exemption to anything in it.

    The allowlist names directories: a *file* or symlink that merely carries an allowlisted
    name (for example a symlink called `.venv`) is never tolerated.
    """
    parts = Path(path).parts
    for end in range(1, len(parts) + (1 if is_dir else 0)):
        if any(fnmatch.fnmatchcase(parts[end - 1], pattern) for pattern in allow) and \
                any("/".join(parts[:k]) in ignored_dirs for k in range(1, end + 1)):
            return True
    return False


# Inside an allowlisted cache directory only these names are still inspected. A broader scan
# would flag ordinary packages (certifi's cacert.pem, botocore/credentials.py).
_CACHE_SENSITIVE_PREFIXES = (".env",)


def _ls_ignored(repo: Path, *extra: str) -> list[str]:
    result = subprocess.run([*guarded(repo), "ls-files", "-o", "-i", "--exclude-standard", "-z", *extra],
                            capture_output=True, check=True, timeout=60, env=git_env())
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
    listed = _ls_ignored(repo, "--directory")
    ignored_dirs = {name.rstrip("/") for name in listed if name.endswith("/")}

    def kept(item: str) -> bool:
        # A `.env*` file is inspected wherever it is, including inside an allowlisted cache nested in another ignored directory.
        return (not _allowed_ignored(item, allow, ignored_dirs)
                or Path(item).name.casefold().startswith(_CACHE_SENSITIVE_PREFIXES))

    found = []
    for name in listed:
        if name.endswith("/"):
            found.extend(item for item in _ls_ignored(repo, "--", name) if kept(item))
        elif kept(name):
            found.append(name)
    return found


def special_paths(repo: Path, allow=None) -> list[str]:
    """FIFOs, sockets and device nodes in the worktree. Git lists none of them (tracked, untracked or ignored), so they
    are found by a walk that never follows symlinks. `.git` is skipped, and so is an allowlisted cache directory, but only
    when Git really ignores it (it or a parent is an ignored directory): an ordinary `dist/` is walked like any other."""
    allow = DEFAULT_IGNORED_ALLOWLIST if allow is None else tuple(allow)
    root = Path(repo)
    ignored_dirs = _ignored_directories(repo)

    def skipped(relative: str) -> bool:
        return _allowed_ignored(relative, allow, ignored_dirs, is_dir=True)

    found, caches = [], []
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        prefix = base.relative_to(root).as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        caches.extend(base / d for d in dirs if d != ".git" and skipped(prefix + d))
        dirs[:] = [d for d in dirs if d != ".git" and not skipped(prefix + d)]
        for name in files:
            try:
                kind = os.lstat(base / name).st_mode
            except OSError:
                continue
            if not (stat.S_ISREG(kind) or stat.S_ISLNK(kind) or stat.S_ISDIR(kind)):
                found.append((base / name).relative_to(root).as_posix())
    # Inside a skipped cache only sensitive names (`.env*`) are inspected, as in ignored_paths(); Git never lists special
    # files, so a `.env` FIFO or socket there is found here. Only those names are stat'ed.
    for cache in caches:
        if os.path.islink(cache):
            continue                    # a symlink named like a cache is never walked (it may point outside)
        for directory, dirs, files in os.walk(cache, followlinks=False):
            for name in (*files, *dirs):
                if not name.casefold().startswith(_CACHE_SENSITIVE_PREFIXES):
                    continue
                try:
                    kind = os.lstat(Path(directory) / name).st_mode
                except OSError:
                    continue
                if not (stat.S_ISREG(kind) or stat.S_ISLNK(kind) or stat.S_ISDIR(kind)):
                    found.append((Path(directory) / name).relative_to(root).as_posix())
    return found


def changed_paths(repo: Path, ignored_allow=None) -> list[str]:
    """Every changed path for ownership review: tracked/untracked changes, git-ignored files, and special files (which
    Git never lists)."""
    return sorted(set(status_paths(repo)) | set(ignored_paths(repo, ignored_allow)) | set(special_paths(repo, ignored_allow)))


_BLOB_CACHE: dict[tuple, str] = {}


def _hash_paths(repo: Path, names: list[str], *options: str) -> dict[str, str]:
    """`git hash-object` for each path. `--stdin-paths` is line-based and unquotes a leading `"`, so a name containing a
    line break or starting with a quote is hashed on its own as an argument instead; every name must get exactly one
    hash, or nothing is trusted."""
    plain, odd = [], []
    for name in names:                                       # one pass (a set rebuilt per name was quadratic)
        (odd if "\n" in name or "\r" in name or name.startswith('"') else plain).append(name)
    out: dict[str, str] = {}
    for start in range(0, len(plain), 2000):
        chunk = plain[start:start + 2000]
        hashed = subprocess.run([*guarded(repo), "hash-object", *options, "--stdin-paths"],
                                input="\n".join(chunk).encode("utf-8", "surrogateescape"), capture_output=True,
                                check=True, timeout=300, env=git_env()).stdout.decode().split()
        if len(hashed) != len(chunk):
            raise ValueError("File contents could not be hashed one-to-one; nothing is accepted")
        out.update(zip(chunk, hashed))
    for name in odd:
        out[name] = subprocess.run([*guarded(repo), "hash-object", *options, "--", name], capture_output=True,
                                   check=True, timeout=300, env=git_env()).stdout.decode().strip()
    return out


def content_changed_tracked(repo: Path) -> list[str]:
    """Tracked regular files whose current content is not their index blob, found without Git's stat cache.

    Git decides "unchanged" from stat data compared at whole-second resolution, so an edit made in the same second as
    its last refresh, with the modification time put back, is invisible to `git status`. Each file is hashed as
    `git add` would store it; the hash is reused only while device, inode, size, mtime and the nanosecond inode change
    time are all unchanged (any write changes ctime, and an unprivileged process cannot set it back)."""
    listing = subprocess.run([*guarded(repo), "ls-files", "-s", "-z"], capture_output=True, check=True,
                             timeout=60, env=git_env()).stdout.decode("utf-8", "surrogateescape")
    root = Path(repo).resolve()
    if len(_BLOB_CACHE) > 200000:
        _BLOB_CACHE.clear()                    # before deciding what to hash: every needed hash is then recomputed
    expected, keys, missing, mode_changed, symlinks = {}, {}, [], [], []
    for row in listing.split("\0"):
        if "\t" not in row:
            continue
        meta, name = row.split("\t", 1)
        mode, blob, stage = meta.split()
        if stage == "0" and mode == "120000":
            # A tracked symlink must still be a symlink with exactly the committed target bytes, whatever
            # core.symlinks says (with it false, Git accepts a regular file holding the target text as unchanged).
            try:
                target = os.readlink(os.fsencode(root / name))
            except FileNotFoundError:
                continue                       # deleted: status reports it
            except OSError:
                symlinks.append(name)          # not a symlink any more (or unreadable): a change
                continue
            if hashlib.sha1(b"blob %d\0" % len(target) + target).hexdigest() != blob:
                symlinks.append(name)
            continue
        if stage != "0" or mode not in ("100644", "100755"):
            continue                           # conflicts and gitlinks are reported by status itself
        try:
            info = os.lstat(root / name)
        except OSError:
            continue                           # deleted: status reports it
        if not stat.S_ISREG(info.st_mode):
            continue                           # type changed: status reports it
        if bool(info.st_mode & stat.S_IXUSR) != (mode == "100755"):
            mode_changed.append(name)          # executable bit changed, whatever core.fileMode says
        key = (str(root), name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        expected[name], keys[name] = blob, key
        if key not in _BLOB_CACHE:
            missing.append(name)
    for name, blob in _hash_paths(repo, missing, "--no-filters").items():     # raw bytes: what was really checked
        _BLOB_CACHE[keys[name]] = blob
    return sorted({name for name in expected if _BLOB_CACHE.get(keys[name]) != expected[name]}
                  | set(mode_changed) | set(symlinks))


def status_paths(repo: Path) -> list[str]:
    """Changes `git status` reports (never ignored files), plus tracked files whose content differs from the index
    even where Git's stat cache says otherwise; these are the only paths git can stage."""
    # NUL delimiters preserve spaces. Rename destinations and originals are both checked.
    result = subprocess.run([*guarded(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            capture_output=True, check=True, timeout=15, env=git_env())
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
    return sorted(set(paths) | set(content_changed_tracked(repo)))


_HASH_CACHE: dict[tuple, str] = {}
_HASH_CACHE_FROM = 64 * 1024 * 1024     # only large files are cached; small ones are cheaper to hash than to track


def _fingerprint(path: Path) -> str:
    """Content fingerprint of one path. Every file is hashed in full: metadata alone (size, mtime) can be preserved
    across a content change, so it never stands in for contents. A large file's hash is reused only while its inode
    change time (ctime) is unchanged; any write updates ctime and an unprivileged process cannot set it back."""
    try:
        kind = os.lstat(path).st_mode                   # the entry itself, never what a symlink points to
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(kind):
        return "symlink:" + os.readlink(path)
    if stat.S_ISDIR(kind):
        return "absent"                                  # a directory holds no content of its own; its files are listed
    if not stat.S_ISREG(kind):
        return f"special:{kind}"                         # FIFO, socket or device: never verifiable (see unverifiable())
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)   # a FIFO swapped in never blocks us
    except OSError:
        return "unreadable"
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            return f"special:{before.st_mode}"
        key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if before.st_size >= _HASH_CACHE_FROM and key in _HASH_CACHE:
            return str(before.st_mode) + ":" + _HASH_CACHE[key]
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
        return f"changing:{after.st_ctime_ns}"           # written while being read: never equal to a settled fingerprint
    if before.st_size >= _HASH_CACHE_FROM:
        if len(_HASH_CACHE) > 4096:
            _HASH_CACHE.clear()
        _HASH_CACHE[key] = digest.hexdigest()
    return str(before.st_mode) + ":" + digest.hexdigest()


def unverifiable(fingerprint: str | None) -> bool:
    """A fingerprint that cannot show whether content changed: an unreadable file, one written while being read, or a
    special file (FIFO, socket, device). Comparisons fail closed: such a path always counts as changed."""
    return isinstance(fingerprint, str) and (fingerprint == "unreadable" or fingerprint.startswith(("changing:", "special:")))


def _changed_since(worktree: Path, current: list[str], baseline: dict | None) -> list[str]:
    if baseline is None:
        return list(current)
    out = []
    for name in sorted(set(current) | set(baseline)):
        now = _fingerprint(worktree / name)
        if name not in baseline or now != baseline[name] or unverifiable(now) or unverifiable(baseline[name]):
            out.append(name)
    return out


def _never_acceptable(worktree: Path, name: str) -> bool:
    """Changed paths that no ownership can make acceptable: sensitive names, a nested repository or submodule (a
    directory), a symlink leaving the worktree, and special files."""
    path = worktree / name
    try:
        kind = os.lstat(path).st_mode
    except FileNotFoundError:
        return _sensitive(name)
    except OSError:
        return True
    if stat.S_ISLNK(kind):
        return _sensitive(name) or not path.resolve().is_relative_to(worktree.resolve())
    # A regular file Brahma cannot read cannot be verified, owned or not.
    return _sensitive(name) or stat.S_ISDIR(kind) or not stat.S_ISREG(kind) or unverifiable(_fingerprint(path))


def snapshot_changes(worktree: Path, ignored_allow=None) -> dict[str, str]:
    return {name: _fingerprint(worktree / name) for name in changed_paths(worktree, ignored_allow)}


def hidden_index_entries(worktree: Path) -> list[str]:
    """Tracked paths whose changes `git status` would not show: assume-unchanged (lower-case tag) or skip-worktree (S)."""
    result = subprocess.run([*guarded(worktree), "ls-files", "-v", "-z"], capture_output=True, check=True, timeout=60, env=git_env())
    hidden = []
    for row in result.stdout.decode("utf-8", errors="surrogateescape").split("\0"):
        if len(row) > 2 and (row[0].islower() or row[0] == "S"):
            hidden.append(row[2:])
    return sorted(hidden)


def git_identity(worktree: Path) -> dict:
    """The Git metadata ownership is measured against. Taken once when a run starts and compared on every verification."""
    return {"git_dir": git(worktree, "rev-parse", "--absolute-git-dir"),
            "common_dir": git(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            "branch": git(worktree, "branch", "--show-current"),
            "head": git(worktree, "rev-parse", "HEAD"),
            "hidden": hidden_index_entries(worktree)}


def identity_problems(worktree: Path, identity: dict | None) -> list[str]:
    """What moved in the Git metadata since `identity` was taken. Any entry blocks readiness."""
    if not identity:
        return []
    try:
        now = git_identity(worktree)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return ["Git metadata could not be read: " + str(error)]
    problems = []
    for key, label in (("git_dir", "the worktree's Git directory (.git was repointed)"), ("common_dir", "the repository it belongs to"),
                       ("branch", "the checked-out branch"), ("head", "HEAD (an unexpected commit)")):
        if now[key] != identity.get(key):
            problems.append(f"{label} changed")
    if now["hidden"]:      # Brahma clears every flag when a run starts, so any flag present now was set during the run
        problems.append("index flags hide changes to: " + ", ".join(now["hidden"][:20]))
    return problems


def clear_index_flags(worktree: Path) -> list[str]:
    """Make every tracked change visible again before a run takes its baseline: a flag left by an interrupted earlier run
    must not become part of what later runs trust. Returns the paths that were hidden."""
    hidden = hidden_index_entries(worktree)
    if hidden:
        git(worktree, "update-index", "--no-assume-unchanged", "--no-skip-worktree", "--", *hidden)
    return hidden


_SENSITIVE_PREFIXES = (".env", "credentials", ".credentials")


def _sensitive(path: str) -> bool:
    # Case-folded: on macOS's default case-insensitive filesystem `.ENV` and `.Git` are the same files as `.env` and `.git`.
    return any(part in (".brahma", "custody", ".git") or part.startswith(_SENSITIVE_PREFIXES)
               or part.endswith((".pem", ".key")) for part in (piece.casefold() for piece in Path(path).parts))


def ownership_changes(worktree: Path, files: list[str], baseline=None, ignored_allow=None, identity=None) -> tuple[bool, str]:
    moved = identity_problems(worktree, identity)
    if moved:
        return False, "Git metadata changed during the run, so changes cannot be attributed: " + "; ".join(moved)
    paths = _changed_since(worktree, changed_paths(worktree, ignored_allow), baseline)
    outside = [name for name in paths if not any(ownership.owns(pattern, name) for pattern in files)]
    # Sensitive names, nested repositories (directories), escaping symlinks and special files: never accepted.
    sensitive = [name for name in paths if _never_acceptable(worktree, name)]
    if outside or sensitive:
        return False, f"File ownership/sensitive-path review required: {', '.join(sorted(set(outside+sensitive)))}"
    return True, f"{len(paths)} changed paths fit approved ownership"


def violating_paths(worktree: Path, files: list[str], baseline=None, ignored_allow=None) -> list[str]:
    """Paths changed since the prelaunch baseline that fall outside ownership or are sensitive/escaping."""
    paths = _changed_since(worktree, changed_paths(worktree, ignored_allow), baseline)
    return [name for name in paths
            if not any(ownership.owns(pattern, name) for pattern in files) or _never_acceptable(worktree, name)]


def restore_violations(worktree: Path, files: list[str], baseline: dict, ignored_allow=None,
                       identity: dict | None = None) -> tuple[list[str], list[str]]:
    """Undo attributable worker changes that violate ownership, on the isolated branch only.

    A path that was already changed in the prelaunch baseline (a pre-existing edit) cannot be separated from the
    worker's change, so it is never touched and is reported as not restored. A path absent from the baseline was
    clean at launch: tracked files go back to HEAD, new files (including ignored ones) are removed.
    With the run's starting `identity`, a worker that also committed is undone too: when HEAD has moved, the paths its
    commits changed are included and every restore uses the starting revision as the source, so the files go back to
    what they were when the run began (the commits stay in the branch history; readiness is blocked by the moved HEAD).
    Returns (restored, not_restored). Readiness stays blocked either way; this only limits the damage.
    """
    restored, kept = [], []
    root = worktree.resolve()
    from .safety import REDACTED, redact
    # A baseline loaded from storage has credential-shaped path names redacted. A path whose redacted form is in it may
    # have existed at launch, so it is treated as pre-existing and never deleted; every other path is restored as usual.
    redacted_baseline = any(REDACTED in str(name) for name in baseline)
    if identity:
        # Rollback writes through Git. If the repository, its common directory or the branch moved, Git would act on
        # whatever they now point to (perhaps another checkout), so nothing is restored and the run stays blocked.
        try:
            now = git_identity(worktree)
            moved = [field for field in ("git_dir", "common_dir", "branch") if now.get(field) != identity.get(field)]
        except (OSError, ValueError, subprocess.SubprocessError):
            moved = ["git metadata unreadable"]
        if moved:
            return [], ["every change (Git metadata moved: " + ", ".join(moved) + "; nothing was restored)"]
    candidates = list(violating_paths(worktree, files, baseline, ignored_allow))
    source = "HEAD"
    original = (identity or {}).get("head")
    if original and git(worktree, "rev-parse", "HEAD") != original:
        source = original
        committed = subprocess.run([*guarded(worktree), "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", original, "HEAD"],
                                   capture_output=True, check=True, timeout=60, env=git_env()).stdout
        for name in committed.decode("utf-8", errors="surrogateescape").split("\0"):
            if name and name not in candidates and (not any(ownership.owns(p, name) for p in files) or _sensitive(name)):
                candidates.append(name)
    # Exact comparison against the source revision's file list: a worker-chosen name is never a pathspec (`:(glob)**`).
    listing = subprocess.run([*guarded(worktree), "ls-tree", "-r", "--name-only", "-z", source],
                             capture_output=True, check=True, timeout=60, env=git_env()).stdout
    tracked_files = set(listing.decode("utf-8", errors="surrogateescape").split("\0"))
    for name in candidates:
        target = worktree / name
        if redacted_baseline and REDACTED in name and not os.path.lexists(target):
            kept.append(name + " (a launch-time file whose name was stored redacted; it cannot be checked)")
            continue
        if name in baseline or (redacted_baseline and redact(name) in baseline):
            kept.append(name)
            continue
        if any(ownership.owns(pattern, name) for pattern in files) and not _sensitive(name):
            # An owned regular file is a violation only because it cannot be verified (unreadable, still being
            # written). It is the task's own output: readiness stays blocked, but it is never deleted. An owned
            # symlink or special file is still removed below.
            try:
                regular = stat.S_ISREG(os.lstat(target).st_mode)
            except OSError:
                regular = True              # its type cannot be read now (missing, parent replaced): kept, never reset
            if regular:
                kept.append(name)
                continue
        try:
            # The parent folder must be exactly where the name says: a symlinked folder (out of the worktree, or to
            # another folder in it) would redirect the restore to a file the checks above never looked at.
            parent_in_place = target.parent.resolve() == root.joinpath(*Path(name).parts[:-1])
            escapes = not target.resolve(strict=False).is_relative_to(root) and not target.is_symlink()
        except (OSError, RuntimeError):
            parent_in_place, escapes = False, True          # a symlink loop or unreadable folder: never acted on
        if not parent_in_place or escapes:
            kept.append(name)  # never act outside the worktree, or on a path its folder redirects
            continue
        tracked = name in tracked_files
        try:
            if tracked:
                git(worktree, "restore", "--source=" + source, "--staged", "--worktree", "--", name)
            else:
                try:
                    kind = os.lstat(target).st_mode
                except FileNotFoundError:
                    kind = None                                  # already gone: nothing left to undo
                if kind is not None and stat.S_ISDIR(kind):
                    kept.append(name)                            # a nested repository is reported, never deleted
                    continue
                if source != "HEAD":
                    git(worktree, "rm", "-q", "--cached", "--ignore-unmatch", "--", name)   # added by a worker commit
                if kind is not None:
                    target.unlink()                              # regular file, symlink, FIFO, socket or device node
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


async def _end_group(process, grace: float = 1.0) -> None:
    """End every process left in a check's group, even when the check itself already exited successfully."""
    from . import procs
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            pass
        deadline = time.monotonic() + grace
        while procs._group_alive(process.pid) and time.monotonic() < deadline:
            await asyncio.sleep(.05)
        if not procs._group_alive(process.pid):
            break
    if process.returncode is None:
        await process.wait()


async def run_checks(worktree, checks, cancel, timeout, emit, results=None, bound=None):
    """Run approved checks. If `results` is a list, one (argv, status, exit_code) per command is appended as the
    coordinator observed it (status passed|failed|not_run), for handoff packets."""
    deadline = time.monotonic() + timeout
    override = gitsafe.attribute_override(Path(worktree))     # before the implicit `git diff --check` runs
    if override:
        raise toolchain.ToolchainChanged(override)           # an environment change after approval: no repair is spent
    commands = [[*_GIT, "diff", "--check", "--no-ext-diff", "--no-textconv"], *checks]
    summaries = []
    for command in commands:
        if cancel.is_set() or time.monotonic() >= deadline:
            return False, "Checks cancelled or time budget exhausted"
        emit(Event(kind="check", text="Running approved check: " + " ".join(command)))
        environment = check_env()
        if command[0] == "git" and command is commands[0]:
            environment = git_env(environment)        # the implicit check takes no Git config from the environment
        argv, problem = toolchain.verify_before_run(command, worktree, bound, environment)       # includes the implicit git diff --check
        if problem:
            emit(Event(kind="check", text=problem))
            if results is not None:
                results.append((tuple(command), "not_run", None))
            raise toolchain.ToolchainChanged(problem)       # an environment change, not a test failure: no repair is spent
        from . import procs
        try:
            process = await asyncio.create_subprocess_exec(*argv, cwd=worktree, env=environment,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except OSError:
            if results is not None:
                results.append((tuple(command), "failed", None))
            return False, "Approved check could not start: " + command[0]
        record = procs.register(process.pid)
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
            await _end_group(process)          # also any background process the check left behind after exiting
            procs.unregister(record)
            if not running.done():
                running.cancel()
    return True, "\n".join(summaries)


def _staged_differs(worktree: Path, paths: list[str]) -> list[str]:
    """Staged paths whose index blob is not the file's raw bytes: the commit must contain exactly what was checked."""
    staged = subprocess.run([*guarded(worktree), "ls-files", "-s", "-z", "--", *paths],
                            capture_output=True, check=True, timeout=60, env=git_env()).stdout
    index = {}
    for row in staged.decode("utf-8", "surrogateescape").split("\0"):
        if "\t" in row:
            meta, name = row.split("\t", 1)
            index[name] = meta.split()[1]
    regular = [name for name in index if (worktree / name).is_file() and not (worktree / name).is_symlink()]
    if not regular:
        return []
    raw = _hash_paths(worktree, regular, "--no-filters")
    return [name for name in regular if index[name] != raw[name]]


def commit_changes(worktree: Path, summary: str, identity: dict | None = None) -> str:
    """Commit on the session's own branch only. With `identity`, the branch, HEAD and gitdir must still be the verified ones."""
    if not git(worktree, "branch", "--show-current").startswith("brahma/"):
        raise ValueError("Refusing to commit: the worktree is not on a Brahma branch")
    ok, message = ownership_changes(worktree, ["**"], identity=identity)
    if not ok:
        raise ValueError(message)
    # Owned git-ignored files are reviewed above but never staged (`git add` refuses them).
    paths = status_paths(worktree)
    if not paths:
        return git(worktree, "rev-parse", "HEAD")
    git(worktree, "add", "--", *paths)       # attributes come from the empty tree: no filter or conversion applies
    staged_differs = _staged_differs(worktree, paths)
    if staged_differs:
        git(worktree, "restore", "--staged", "--", *paths)          # unstage; the files themselves are untouched
        raise ValueError("Not committed: the staged content differs from the checked files (" + ", ".join(staged_differs[:5])
                         + "); Git converted them (attributes such as eol or ident) or kept a stale index entry")
    git(worktree, "commit", "--no-gpg-sign", "-m", "Brahma: " + summary[:120])
    return git(worktree, "rev-parse", "HEAD")


def _run_publication_step(argv: list[str], worktree: Path, stopped, *, input_text: str = "", env=None,
                          timeout: float = 120) -> subprocess.CompletedProcess:
    """One external write of a publication (push or PR creation), stopped at once if `stopped()` reports a stop,
    pause or kill switch while it runs. Runs in its own process group so the whole step is ended."""
    process = subprocess.Popen(argv, cwd=worktree, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, env=env, start_new_session=True)
    deadline = time.monotonic() + timeout
    finished = False
    pending = input_text or None    # communicate() takes the input once; a retry after a timeout continues sending it
    try:
        while True:
            try:
                out, err = process.communicate(pending, timeout=0.25)
                finished = True
                return subprocess.CompletedProcess(argv, process.returncode, out, err)
            except subprocess.TimeoutExpired:
                pending = None
                try:
                    reason = stopped()
                except Exception as error:          # a stop check that cannot answer is treated as a stop
                    reason = "the stop check failed (" + type(error).__name__ + ")"
                if reason or time.monotonic() > deadline:
                    raise ValueError("Publication stopped: " + (reason or "it took too long")
                                     + "; check the remote branch before publishing again") from None
    finally:
        if not finished:                            # any way out but a normal finish ends the whole step
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.communicate()


def publish_lock_path(worktree: Path) -> Path:
    """One lock per worktree, in Brahma's own state directory."""
    from .config import state_home
    return state_home() / "publish-locks" / (hashlib.sha256(str(Path(worktree).resolve()).encode()).hexdigest()[:32] + ".lock")


def publish(worktree: Path, summary: str, body: str, expected_head: str, expected_uncommitted: dict | None = None,
            ignored_allow=None, stopped=None, lock_wait: float = 300) -> str:
    """Push exactly the commit that was verified ready, and open a draft PR. Anything added after readiness (a commit, an
    uncommitted or ignored edit) means this is no longer the verified result, so nothing is pushed.

    The push sends the verified commit itself (its SHA), never whatever the branch name points to by then. Publications
    of one worktree are serialized, and `stopped()` (kill switch, stop or pause) is checked before every external write
    and while each one runs."""
    import fcntl
    # The lock lives in Brahma's own state directory (never in the worktree or its Git directory, which a worker can
    # write), one per worktree, opened without following a link and only if it is a regular file.
    stopped = stopped or (lambda: None)
    try:
        lock = publish_lock_path(worktree)
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("Not published: the publication lock is not a regular file")
            deadline = time.monotonic() + lock_wait
            while True:     # wait for another publication of this worktree, but never past a stop or the deadline
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    reason = stopped() or ("another publication of this worktree is still running"
                                           if time.monotonic() > deadline else None)
                    if reason:
                        raise ValueError("Not published: " + reason) from None
                    time.sleep(0.25)
            return _publish(worktree, summary, body, expected_head, expected_uncommitted, ignored_allow, stopped)
        finally:
            os.close(descriptor)
    except (OSError, sqlite3.Error) as error:
        # The caller handles ValueError; an unreadable state directory or lock must refuse, not crash the run.
        raise ValueError(f"Not published: {type(error).__name__}: {error}") from None


def _publish(worktree, summary, body, expected_head, expected_uncommitted, ignored_allow, stopped) -> str:
    from . import halt, safety
    from .config import state_home

    def halted() -> str | None:
        return "Kill switch engaged" if halt.engaged(state_home()) else stopped()
    reason = halted()
    if reason:
        raise ValueError("Not published: " + reason)
    summary, body = safety.redact(summary), safety.redact(body)     # never send a secret-shaped string to GitHub
    from .providers import find_executable
    gh = find_executable("gh")
    if not gh:
        raise ValueError("Draft PR publishing needs GitHub CLI: install gh and run gh auth login; local branch is preserved")
    branch = git(worktree, "branch", "--show-current")
    if not branch.startswith("brahma/"):
        raise ValueError("Only Brahma-owned branches may be published")
    if not expected_head or git(worktree, "rev-parse", "HEAD") != expected_head:
        raise ValueError("Not published: the branch has moved since it was verified ready; run and verify it again")
    if status_paths(worktree) or hidden_index_entries(worktree):
        raise ValueError("Not published: the worktree has changes that were not part of the verified, committed result")
    now = snapshot_changes(worktree, ignored_allow)
    if (expected_uncommitted is None or now != expected_uncommitted
            or any(unverifiable(value) for value in now.values())):
        # Ignored files (a new `.env`, a planted script) and special files are compared with the verified receipt too.
        raise ValueError("Not published: ignored or special files changed after the work was verified ready")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_head):
        raise ValueError("Not published: the verified commit is not a full commit id")
    push = ("push", "origin", f"{expected_head}:refs/heads/{branch}")    # the verified commit, to its own brahma/* branch
    reason = refuse(push) or halted()
    if reason:
        raise ValueError("Not published: " + reason)
    pushed = _run_publication_step([*guarded(worktree), *push], worktree, halted, env=git_env())
    if pushed.returncode:
        raise ValueError(pushed.stderr.strip() or "Push failed; nothing was published")
    try:                                    # from here on, every failure says that the commit is already on the remote
        reason = halted()
        if reason:
            raise ValueError(f"Not published as a PR: {reason}")
        result = _run_publication_step([gh, "pr", "create", "--draft", "--head", branch, "--title", summary[:120],
                                        "--body-file", "-"], worktree, halted, input_text=body, timeout=60)
        if result.returncode:
            raise ValueError("Draft PR creation failed. Check gh auth and repository access")
    except Exception as error:
        text = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) \
            else f"Not published as a PR ({type(error).__name__}: {error})"
        raise ValueError(f"{text}. The verified commit was pushed to {branch}") from None
    return result.stdout.strip()
