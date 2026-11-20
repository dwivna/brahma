"""Small, explicit context; no recursive source dumps or silent instruction truncation."""
from pathlib import Path
import subprocess


def build_context(repo: Path, files: list[str], limit: int, approved: dict[str, str] | None = None,
                  owned: tuple[str, ...] = ()) -> str:
    """Instructions and a bounded Git summary for a worker. With `approved` (path -> SHA-256 from the locked contract), an
    instruction file is sent only if it is the approved content; a file the approved plan owns may have been edited by
    this plan's own workers and is sent marked as such. Recognisable secrets are always redacted."""
    import hashlib
    from . import ownership
    from .safety import redact
    root = repo.resolve()
    instructions = []
    candidates = {root / "AGENTS.md", root / "CLAUDE.md"}
    for name in files:
        path = root / name
        if path.is_dir():                                # a bare directory owns its subtree: its own instructions apply
            candidates.update((path / "AGENTS.md", path / "CLAUDE.md"))
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_relative_to(root):
                candidates.update((parent / "AGENTS.md", parent / "CLAUDE.md"))
    def plan_owned(relative: str) -> bool:
        return any(ownership.owns(pattern, relative) for pattern in owned)   # the same matcher ownership checks use

    if approved is not None:
        # Every approved instruction file must be present, not only content-checked when found: one missing from this
        # checkout (untracked in the source, so absent from the isolated worktree) would silently drop its rules.
        for relative in approved:
            path = root / relative
            if not path.is_file():
                if plan_owned(relative):
                    instructions.append(f"--- {relative} (deleted within this approved plan) ---")
                    continue
                raise ValueError(f"Approved repository instructions are missing here: {relative}; commit them in the "
                                 "repository (the run uses an isolated checkout of committed files) or approve a new plan")
            candidates.add(path)
    for path in sorted(candidates):
        if path.is_file():
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Repository instructions escaped the selected repository")
            relative = path.relative_to(root).as_posix()
            content = path.read_bytes()
            label = relative
            if approved is not None and approved.get(relative) != hashlib.sha256(content).hexdigest():
                if plan_owned(relative):
                    label += " (edited within this approved plan)"
                else:
                    raise ValueError(f"Repository instructions changed since approval: {relative}; approve a new plan")
            instructions.append(f"--- {label} ---\n{redact(content.decode('utf-8'))}")
    mandatory = "\n".join(instructions)
    if len(mandatory) > limit - 1000:
        raise ValueError("Mandatory repository instructions exceed context cap; raise cap instead of dropping them")
    reports = []
    for args in (("status", "--short"), ("diff", "--stat")):
        from .gitsafe import run_git
        result = run_git(root, *args, timeout=15)
        reports.append(result.stdout[:2000])
    return ("Selected repository instructions (mandatory):\n" + mandatory +
            "\nAssigned file ownership: " + ", ".join(files) +
            "\nGit summary (bounded metadata; inspect relevant files yourself):\n" + "\n".join(reports))
