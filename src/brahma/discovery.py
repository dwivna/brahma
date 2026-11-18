"""Bounded, read-only customization inventory; discovery never enables a tool."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import fnmatch
import hashlib
import itertools
import os
from pathlib import Path
import re
import stat
import tomllib
from typing import Iterable

MAX_DEFINITION_BYTES = 65536
MAX_DEFINITIONS = 256


@dataclass(frozen=True)
class Definition:
    provider: str
    kind: str
    name: str
    description: str
    scope: str
    path: str
    sha256: str
    root: str
    state: str = "discovered"
    enabled: bool = False

    @property
    def id(self) -> str:
        return f"{self.provider}/{self.kind}/{self.name}"

    def to_dict(self) -> dict:
        return {"id": self.id, **asdict(self)}


@dataclass
class Catalog:
    entries: list[Definition] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _safe_file(path: Path, root: Path) -> bytes:
    root, path = root.absolute(), path.absolute()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError("Definition escapes its discovery root") from error
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Symlink definitions are not imported")
    # Walk every directory through no-follow descriptors so replacing a parent
    # with a symlink after discovery cannot redirect an approved read.
    directory_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                              dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK,
                     dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Definitions must be regular files")
        content = stream.read(MAX_DEFINITION_BYTES + 1)
    if len(content) > MAX_DEFINITION_BYTES:
        raise ValueError("Definition exceeds inventory size limit")
    return content


def fingerprint(entry: Definition) -> str:
    return hashlib.sha256(_safe_file(Path(entry.path), Path(entry.root))).hexdigest()


def selected_instructions(entries: Iterable[Definition], identifiers: Iterable[str], provider: str,
                          max_chars: int = 12000) -> str:
    """Load only explicitly approved definitions as bounded worker instructions.

    This is not native plugin/skill activation or permission for a child agent.
    References in the selected text are retained, never recursively followed.
    """
    if provider not in ("claude", "codex") or type(max_chars) is not int or max_chars < 1:
        raise ValueError("Invalid instruction-loading provider or budget")
    available = {entry.id: entry for entry in entries}
    output = []
    seen = set()
    for identifier in identifiers:
        if not isinstance(identifier, str) or identifier not in available:
            raise ValueError("Approved definition is not in the current inventory")
        if identifier in seen:
            continue
        seen.add(identifier)
        entry = available[identifier]
        if entry.provider != provider:
            continue
        content = _safe_file(Path(entry.path), Path(entry.root))
        digest = hashlib.sha256(content).hexdigest()
        if digest != entry.sha256:
            raise ValueError(f"Definition changed since approval: {identifier}")
        text = content.decode("utf-8")
        output.append(f"\nApproved {identifier} [sha256:{digest}]\n"
                      "State: instruction-loaded; native customization disabled; no child spawning.\n"
                      f"{text}\nEnd approved {identifier}\n")
        if sum(len(part) for part in output) > max_chars:
            raise ValueError("Required selected instructions exceed the context budget")
    return "".join(output)


def _metadata(path: Path, content: bytes) -> tuple[str, str]:
    text = content.decode("utf-8")
    if path.suffix == ".toml":
        data = tomllib.loads(text)
        name = data.get("name", path.stem)
        description = data.get("description", "")
    else:
        name = path.parent.name if path.name == "SKILL.md" else path.stem
        description = ""
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                match = re.match(r"^(name|description):\s*(.*?)\s*$", line)
                if match:
                    key, val = match.groups()
                    val = val.strip("\"'")
                    if key == "name":
                        name = val
                    elif val not in ("|", ">"):
                        description = val
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name):
        raise ValueError("Definition has an unsupported name")
    if not isinstance(description, str):
        raise ValueError("Definition description must be text")
    # Descriptions are plain metadata, never interpreted as commands or markup.
    description = re.sub(r"[\x00-\x1f\x7f]", " ", description)[:500]
    return name, description


def discover(repo: Path, user_home: Path | None = None,
             exclusions: Iterable[str] = ()) -> Catalog:
    """Inventory supported directories only. Project definitions shadow user IDs.

    This does not parse user configuration, transcripts, credentials or plugins.
    Exclusions are relative to the selected project/user root, not process cwd.
    """
    repo = repo.resolve()
    home = (user_home or Path.home()).resolve()
    patterns = tuple(exclusions)
    if any(not isinstance(pattern, str) for pattern in patterns):
        raise ValueError("Exclusions must be path patterns")
    roots = [
        ("codex", "skill", ".codex/skills", "*/SKILL.md"),
        ("codex", "skill", ".agents/skills", "*/SKILL.md"),
        ("codex", "agent", ".codex/agents", "*.toml"),
        ("claude", "skill", ".claude/skills", "*/SKILL.md"),
        ("claude", "agent", ".claude/agents", "*.md"),
    ]
    found: dict[str, Definition] = {}
    catalog = Catalog()
    visited = 0
    for scope, base in (("user", home), ("project", repo)):
        for provider, kind, suffix, glob in roots:
            root = base / suffix
            parents = [root]
            cursor = root.parent
            while cursor != base:
                parents.append(cursor)
                cursor = cursor.parent
            if any(item.is_symlink() for item in parents):
                catalog.errors.append(f"Skipped symlink inventory root: {scope}/{suffix}")
                continue
            if not root.is_dir():
                continue
            for path in sorted(itertools.islice(root.glob(glob), MAX_DEFINITIONS + 1)):
                visited += 1
                if visited > MAX_DEFINITIONS:
                    catalog.errors.append("Inventory limit reached; remaining definitions were not inspected")
                    catalog.entries = sorted(found.values(), key=lambda item: item.id)
                    return catalog
                relative = path.relative_to(base).as_posix()
                if any(fnmatch.fnmatchcase(relative, pattern) for pattern in patterns):
                    continue
                try:
                    content = _safe_file(path, root)
                    name, description = _metadata(path, content)
                    entry = Definition(provider, kind, name, description, scope, str(path),
                                       hashlib.sha256(content).hexdigest(), str(root))
                    previous = found.get(entry.id)
                    if previous and previous.scope == scope:
                        catalog.errors.append(f"Duplicate {scope} definition: {entry.id}; last supported root wins")
                    found[entry.id] = entry
                except (OSError, ValueError, UnicodeError) as error:
                    catalog.errors.append(f"Skipped {scope}/{relative}: {type(error).__name__}")
    catalog.entries = sorted(found.values(), key=lambda item: item.id)
    return catalog
