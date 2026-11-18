"""Small, explicit context; no recursive source dumps or silent instruction truncation."""
from pathlib import Path
import subprocess


def build_context(repo: Path, files: list[str], limit: int) -> str:
    root = repo.resolve()
    instructions = []
    candidates = {root / "AGENTS.md", root / "CLAUDE.md"}
    for name in files:
        path = root / name
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_relative_to(root):
                candidates.update((parent / "AGENTS.md", parent / "CLAUDE.md"))
    for path in sorted(candidates):
        if path.is_file():
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Repository instructions escaped the selected repository")
            instructions.append(f"--- {path.relative_to(root)} ---\n{path.read_text(encoding='utf-8')}")
    mandatory = "\n".join(instructions)
    if len(mandatory) > limit - 1000:
        raise ValueError("Mandatory repository instructions exceed context cap; raise cap instead of dropping them")
    reports = []
    for args in (("status", "--short"), ("diff", "--stat")):
        result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, timeout=15)
        reports.append(result.stdout[:2000])
    return ("Selected repository instructions (mandatory):\n" + mandatory +
            "\nAssigned file ownership: " + ", ".join(files) +
            "\nGit summary (bounded metadata; inspect relevant files yourself):\n" + "\n".join(reports))
