from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# Mirrors gittools.DEFAULT_IGNORED_ALLOWLIST (kept here to avoid an import cycle; a test asserts equality).
_DEFAULT_IGNORED = (".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                    "node_modules", "dist", "*.egg-info")


@dataclass
class Settings:
    planner: str = "codex"
    codex_model: str = "gpt-6.2-sol"
    codex_critical_model: str = "gpt-6.2-astra"
    claude_model: str = "sonnet"
    claude_critical_model: str = "opus"
    task_timeout: int = 3600
    repair_cycles: int = 3
    context_chars: int = 24000
    auto_commit: bool = True
    publish: bool = False
    checks: list[list[str]] = field(default_factory=list)
    ignored_allowlist: list[str] = field(default_factory=lambda: list(_DEFAULT_IGNORED))
    # Refuse to start a worker whose provider CLI cannot enforce protected reads (fail closed). Off by default
    # because neither supported CLI can currently enforce them; the limit is then reported on every launch.
    strict_enforcement: bool = False

    def model_for(self, provider: str, complexity: str = "routine") -> str:
        if provider not in ("codex", "claude"):
            raise ValueError("Unknown provider")
        return getattr(self, provider + ("_critical_model" if complexity == "critical" else "_model"))

    @classmethod
    def load(cls, repo: Path) -> Settings:
        path = repo / "brahma.toml"
        if not path.exists():
            return cls()
        from .safety import read_regular
        try:
            data = tomllib.loads(read_regular(path, 262144).decode("utf-8")).get("brahma", {})
        except OSError as error:
            raise ValueError("brahma.toml is not a readable regular file") from error
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise ValueError("Unknown Brahma configuration keys")
        result = cls(**data)
        if result.planner not in ("codex", "claude"):
            raise ValueError("Planner must be codex or claude")
        if type(result.task_timeout) is not int or not 1 <= result.task_timeout <= 3600:
            raise ValueError("Task timeout must be 1–3600 seconds")
        if type(result.repair_cycles) is not int or not 0 <= result.repair_cycles <= 3:
            raise ValueError("Repair cycles must be 0–3")
        if type(result.context_chars) is not int or not 2000 <= result.context_chars <= 100000:
            raise ValueError("Context limit must be 2000–100000 characters")
        if (type(result.auto_commit) is not bool or type(result.publish) is not bool
                or type(result.strict_enforcement) is not bool):
            raise ValueError("auto_commit, publish and strict_enforcement must be booleans")
        for field in ("codex_model", "codex_critical_model", "claude_model", "claude_critical_model"):
            if (not isinstance(getattr(result, field), str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}", getattr(result, field))):
                raise ValueError("Model names must be 1-100 characters of letters, digits and . _ : / [ ] - (no leading dash)")
        if not isinstance(result.checks, list) or any(not isinstance(cmd, list) or not cmd or
                any(not isinstance(arg, str) or not arg for arg in cmd) for cmd in result.checks):
            raise ValueError("Checks must be arrays of command arguments, never shell strings")
        allow = result.ignored_allowlist
        if (not isinstance(allow, list) or len(allow) > 32 or any(
                not isinstance(item, str) or not item.strip() or len(item) > 100 or "/" in item
                or "\\" in item or "\x00" in item or item in (".", "..")
                or not re.search(r"[A-Za-z0-9_]", item) for item in allow)):
            raise ValueError("ignored_allowlist must be a short list of directory/file name patterns without slashes")
        return result


def state_home() -> Path:
    return Path(os.environ.get("BRAHMA_STATE_DIR", str(Path.home() / ".local/state/brahma"))).expanduser()


def run_settings_line(models: dict) -> str:
    """The locked run settings as they will actually apply, defaults included (auto-commit is on by default)."""
    default = Settings()
    value = {name: models.get(name, getattr(default, name)) for name in ("publish", "auto_commit", "strict_enforcement")}
    parts = ["local commit on the brahma/ branch when ready" if value["auto_commit"] else "no automatic commit",
             "draft PR publication when ready" if value["publish"] else "no publication",
             "strict read enforcement" if value["strict_enforcement"] else "reads not enforced (see README)"]
    allow = models.get("ignored_allowlist")
    if allow is not None and list(allow) != list(default.ignored_allowlist):
        parts.append("ignored build caches: " + ", ".join(map(str, allow)))
    return "Run settings: " + "; ".join(parts)
