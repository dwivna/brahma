from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass
class Event:
    provider: str = "brahma"
    role: str = "manager"
    kind: str = "message"
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


Emit = Callable[[Event], None]


@dataclass
class Result:
    provider: str
    status: str
    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    seconds: float = 0.0


@dataclass
class Task:
    title: str
    instructions: str
    files: list[str]
    acceptance: list[str]
    provider: str = "auto"
    complexity: str = "routine"
    scenarios: list[str] = field(default_factory=list)
    allowed_skills: list[str] = field(default_factory=list)
    allowed_agents: list[str] = field(default_factory=list)


@dataclass
class Plan:
    summary: str
    tasks: list[Task]
    questions: list[str] = field(default_factory=list)
    checks: list[list[str]] = field(default_factory=list)
    feature: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def parse(cls, text: str) -> Plan:
        value = json.loads(extract_json(text))
        if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
            raise ValueError("Planner must return a JSON object with summary and tasks")
        questions = value.get("questions", [])
        if not isinstance(questions, list) or any(not isinstance(q, str) for q in questions):
            raise ValueError("Questions must be strings")
        checks = value.get("checks", [])
        if not isinstance(checks, list) or len(checks) > 32 or any(
                not isinstance(command, list) or not command or len(command) > 128 or
                any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command)
                for command in checks):
            raise ValueError("Checks must be explicit argv arrays")
        feature = value.get("feature", "")
        if not isinstance(feature, str) or len(feature) > 65536 or "\x00" in feature:
            raise ValueError("Feature must be bounded Gherkin text")
        rows = value.get("tasks", [])
        if not isinstance(rows, list) or len(rows) > 12:
            raise ValueError("Plan must contain at most twelve bounded tasks")
        tasks = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid task")
            for name in ("title", "instructions"):
                if not isinstance(row.get(name), str) or not row[name].strip():
                    raise ValueError(f"Task needs {name}")
            for name in ("files", "acceptance"):
                if not isinstance(row.get(name), list) or not row[name] or any(
                        not isinstance(x, str) or not x.strip() for x in row[name]):
                    raise ValueError(f"Task needs explicit {name}")
            for name in ("scenarios", "allowed_skills", "allowed_agents"):
                items = row.get(name, [])
                if not isinstance(items, list) or len(items) > 64 or any(
                        not isinstance(item, str) or not item.strip() or "\x00" in item for item in items):
                    raise ValueError(f"Task {name} must contain bounded strings")
                if len(set(items)) != len(items):
                    raise ValueError(f"Task {name} must not contain duplicates")
            if row.get("provider", "auto") not in ("auto", "codex", "claude"):
                raise ValueError("Unknown provider")
            if row.get("complexity", "routine") not in ("routine", "critical"):
                raise ValueError("Unknown complexity")
            if any(p.startswith(("/", "~")) or ".." in p.split("/") or p == ".git" or p.startswith(".git/")
                   or "\\" in p or "\x00" in p or ":" in p or p.endswith("/")
                   for p in row["files"]):
                raise ValueError("File ownership must use repository-relative paths")
            tasks.append(Task(**{k: row[k] for k in
                         ("title", "instructions", "files", "acceptance", "provider", "complexity",
                          "scenarios", "allowed_skills", "allowed_agents") if k in row}))
        if not tasks and not questions:
            raise ValueError("An empty plan cannot be approved")
        if len({task.title for task in tasks}) != len(tasks):
            raise ValueError("Task titles must be unique for resumable checkpoints")
        return cls(value["summary"], tasks, questions, checks, feature)


def extract_json(text: str) -> str:
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            clean = "\n".join(lines[1:-1])
    return clean
