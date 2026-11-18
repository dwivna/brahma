from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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

    def model_for(self, provider: str, complexity: str = "routine") -> str:
        if provider not in ("codex", "claude"):
            raise ValueError("Unknown provider")
        return getattr(self, provider + ("_critical_model" if complexity == "critical" else "_model"))

    @classmethod
    def load(cls, repo: Path) -> Settings:
        path = repo / "brahma.toml"
        if not path.exists():
            return cls()
        with path.open("rb") as stream:
            data = tomllib.load(stream).get("brahma", {})
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
        if type(result.auto_commit) is not bool or type(result.publish) is not bool:
            raise ValueError("auto_commit and publish must be booleans")
        for field in ("codex_model", "codex_critical_model", "claude_model", "claude_critical_model"):
            if not isinstance(getattr(result, field), str) or not getattr(result, field).strip():
                raise ValueError("Model names must be nonempty strings")
        if not isinstance(result.checks, list) or any(not isinstance(cmd, list) or not cmd or
                any(not isinstance(arg, str) or not arg for arg in cmd) for cmd in result.checks):
            raise ValueError("Checks must be arrays of command arguments, never shell strings")
        return result


def state_home() -> Path:
    return Path(os.environ.get("BRAHMA_STATE_DIR", str(Path.home() / ".local/state/brahma"))).expanduser()
