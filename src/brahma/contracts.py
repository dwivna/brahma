"""Human-reviewable feature contracts bound to explicit authority and evidence."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable

from .discovery import Definition, fingerprint
from . import ownership, toolchain
from .models import Plan

MAX_CONTRACT_BYTES = 512 * 1024
# 2: gitignore-style ownership patterns, ignored-file detection, ignored_allowlist in models (0.2.0).
CONTRACT_VERSION = 3          # 3: the payload binds the toolchain that runs the checks


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _line(value: str) -> str:
    return " ".join(value.split())


def render_feature(plan: Plan) -> str:
    if plan.feature:
        lines = [line.strip() for line in plan.feature.splitlines()
                 if line.strip() and not line.lstrip().startswith(("#", "@"))]
        if not lines or not lines[0].startswith("Feature:") or not any(
                line.startswith(("Scenario:", "Scenario Outline:")) for line in lines):
            raise ValueError("Feature needs a Feature and at least one Scenario")
        return plan.feature.rstrip() + "\n"
    lines = ["# Acceptance-derived review specification; not an executable BDD test suite.",
             "Feature: " + _line(plan.summary), ""]
    for index, task in enumerate(plan.tasks, 1):
        if task.scenarios:
            for scenario_index, scenario in enumerate(task.scenarios, 1):
                content = scenario.strip().splitlines()
                if not content or not content[0].lstrip().startswith(("Scenario:", "Scenario Outline:")):
                    raise ValueError("Task scenarios must begin with Scenario or Scenario Outline")
                lines.extend((f"  @task_{index} @scenario_{index}_{scenario_index}",
                              *("  " + line for line in content), ""))
        else:
            for scenario_index, acceptance in enumerate(task.acceptance, 1):
                lines.extend((f"  @task_{index} @scenario_{index}_{scenario_index}",
                              "  Scenario: " + _line(task.title) + f" — acceptance {scenario_index}",
                              "    Given the approved task ownership and constraints",
                              "    When the implementation is checked using the approved commands",
                              "    Then " + _line(acceptance), ""))
    return "\n".join(lines).rstrip() + "\n"


def _overlap(left: str, right: str) -> bool:
    return ownership.overlaps(left, right)


def validate_ownership(plan: Plan) -> None:
    for index, task in enumerate(plan.tasks):
        for other in plan.tasks[:index]:
            for left in task.files:
                for right in other.files:
                    if _overlap(left, right):
                        raise ValueError(f"Ownership overlaps between {other.title!r} and {task.title!r}: {right} / {left}")


def _instructions(repo: Path, plan: Plan) -> dict[str, str]:
    directories = {repo}
    for task in plan.tasks:
        for owned in task.files:
            prefix = re.split(r"[*?\[]", owned, maxsplit=1)[0]
            candidate = repo / prefix
            if ownership.is_subtree(owned) and (repo / owned.rstrip("/")).is_dir():
                candidate = repo / owned.rstrip("/")  # a bare directory owns its subtree (AGENTS.md inside it applies)
            elif not prefix.endswith("/"):
                candidate = candidate.parent
            while candidate != repo:
                if not candidate.is_relative_to(repo):
                    raise ValueError("Instruction path escaped repository")
                directories.add(candidate)
                candidate = candidate.parent
    result = {}
    for directory in sorted(directories):
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = directory / name
            if not path.exists() and not path.is_symlink():
                continue
            for item in (path, *path.parents):
                if item.is_symlink():
                    raise ValueError("Instruction symlinks cannot enter an approval")
                if item == repo:
                    break
            with path.open("rb") as stream:
                content = stream.read(65537)
            if len(content) > 65536:
                raise ValueError("Instruction exceeds contract fingerprint limit")
            result[path.relative_to(repo).as_posix()] = hashlib.sha256(content).hexdigest()
    return result


def _revision(repo: Path) -> str:
    from .gitsafe import run_git
    process = run_git(repo, "rev-parse", "HEAD", timeout=8)
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, ["git", "rev-parse", "HEAD"], process.stdout, process.stderr)
    return process.stdout.strip()


def prepare_contract(plan: Plan, repo: Path, checks: list[list[str]] | None = None, *,
                     task_timeout: float = 3600, repair_cycles: int = 3,
                     definitions: Iterable[Definition] = (), model_profiles: dict | None = None,
                     base_revision: str | None = None, toolchain_directory: Path | None = None,
                     toolchain_basis: str = "repo") -> dict:
    # Revalidate objects built directly, not just planner JSON.
    plan = Plan.parse(json.dumps(plan.to_dict(), ensure_ascii=False))
    if plan.questions:
        raise ValueError("Unanswered questions cannot be locked")
    validate_ownership(plan)
    if isinstance(task_timeout, bool) or not isinstance(task_timeout, (int, float)) or not 0 < task_timeout <= 3600:
        raise ValueError("Active task budget must be at most sixty minutes")
    if type(repair_cycles) is not int or not 0 <= repair_cycles <= 3:
        raise ValueError("At most three repair cycles are permitted")
    selected_checks = checks if checks is not None else plan.checks
    from .safety import argv_looks_secret, looks_secret
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield str(key)
                yield from strings(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                yield from strings(item)
    # Every string the contract stores or forwards: the whole plan (feature, questions, ...) and the rendered feature.
    plan_text = [*strings(plan.to_dict()), render_feature(plan)]
    if any(looks_secret(value) for value in plan_text) or any(argv_looks_secret(command) for command in selected_checks or []):
        raise ValueError("The plan or its checks contain a credential-shaped value; remove it before approval "
                         "(it would be stored in the contract and sent to workers)")
    # Reuse the argv schema validator; no shell-string conversion is accepted.
    Plan.parse(json.dumps({**plan.to_dict(), "checks": selected_checks}))
    if checks is not None and plan.checks and selected_checks != plan.checks:
        raise ValueError("Plan and configured check commands disagree")
    repo = repo.resolve()
    available = {entry.id: entry for entry in definitions}
    selected = {}
    for task in plan.tasks:
        for kind, ids in (("skill", task.allowed_skills), ("agent", task.allowed_agents)):
            for identifier in ids:
                entry = available.get(identifier)
                if entry is None or entry.kind != kind:
                    raise ValueError(f"Selected {kind} is not in the current inventory: {identifier}")
                if task.provider == "auto" or entry.provider != task.provider:
                    raise ValueError("Provider-specific definitions require the task to pin their provider: " + identifier)
                current = fingerprint(entry)
                if current != entry.sha256:
                    raise ValueError(f"Definition changed since discovery: {identifier}")
                selected[identifier] = {"provider": entry.provider, "kind": entry.kind,
                                        "scope": entry.scope, "path": entry.path, "sha256": current,
                                        "state": "discovered", "enabled": False}
    payload = {"plan": plan.to_dict(), "feature": render_feature(plan),
               "checks": selected_checks, "repository": str(repo),
               "base_revision": base_revision if base_revision is not None else _revision(repo),
               "instructions": _instructions(repo, plan), "definitions": selected,
               "task_budget": {"active_seconds": task_timeout, "repair_cycles": repair_cycles,
                               "quota_wait_counts": False}, "models": model_profiles or {},
               "toolchain": {**toolchain.probe([["git"], *selected_checks], toolchain_directory or repo), "basis": toolchain_basis}}
    content = _canonical(payload)
    if len(content) + 512 > MAX_CONTRACT_BYTES:
        raise ValueError("Contract exceeds size limit")
    return {"version": CONTRACT_VERSION, "payload": payload, "digest": hashlib.sha256(content).hexdigest()}


def _validate(contract: dict) -> dict:
    if not isinstance(contract, dict) or set(contract) != {"version", "payload", "digest"} or type(contract["version"]) is not int:
        raise ValueError("Unsupported or malformed contract")
    if contract["version"] != CONTRACT_VERSION:
        # Never reinterpret an approval made under different rules (Round 11/13).
        raise ValueError(f"Contract format {contract['version']} is not supported (this Brahma reads "
                         f"{CONTRACT_VERSION}); its approval is not honoured. Review and approve a new plan")
    if not isinstance(contract["payload"], dict):
        raise ValueError("Contract payload must be an object")
    actual = hashlib.sha256(_canonical(contract["payload"])).hexdigest()
    if not isinstance(contract["digest"], str) or contract["digest"] != actual:
        raise ValueError("Contract digest mismatch; approval is invalid")
    return contract


def verify_contract(contract: dict, plan: Plan, repo: Path, checks: list[list[str]] | None = None,
                    **kwargs) -> None:
    _validate(contract)
    fresh = prepare_contract(plan, repo, checks, **kwargs)
    if fresh != contract:
        # Only blame the toolchain when everything else in the approval is unchanged.
        same = {k: v for k, v in contract["payload"].items() if k != "toolchain"} == {k: v for k, v in fresh["payload"].items() if k != "toolchain"}
        moved = toolchain.differences(contract["payload"].get("toolchain", {}), fresh["payload"]["toolchain"]) if same else []
        if moved:
            raise ValueError("Toolchain changed since approval, so the approval is not honoured: " + "; ".join(moved)
                             + ". Fix the environment or review and approve a new contract")
        raise ValueError("Contract changed; review and approve the new contract")


def _private_directory(directory: Path) -> Path:
    directory = directory.absolute()
    if _has_symlink(directory):
        raise ValueError("Contract output cannot use a symlink directory")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory.resolve()


def _has_symlink(path: Path) -> bool:
    # macOS standard system aliases are not user-controlled state redirection.
    aliases = {Path("/var"): Path("/private/var"), Path("/tmp"): Path("/private/tmp")}
    return any(item.is_symlink() and not (item in aliases and item.resolve() == aliases[item])
               for item in (path, *path.parents))


def _bounded_read(path: Path) -> bytes:
    if _has_symlink(path):
        raise ValueError("Contract symlinks cannot authorize execution")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        content = stream.read(MAX_CONTRACT_BYTES + 1)
    if len(content) > MAX_CONTRACT_BYTES:
        raise ValueError("Contract artifact exceeds size limit")
    return content


def _write_once(path: Path, content: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if _bounded_read(path) != content:
            raise ValueError("Existing immutable contract artifact differs")
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def write_contract(directory: Path, contract: dict) -> Path:
    _validate(contract)
    directory = _private_directory(directory)
    path = directory / (contract["digest"] + ".json")
    feature = contract["payload"].get("feature")
    if not isinstance(feature, str):
        raise ValueError("Contract has no feature artifact")
    _write_once(path.with_suffix(".feature"), feature.encode("utf-8"))
    _write_once(path, _canonical(contract) + b"\n")
    return path


def read_contract(path: Path) -> dict:
    content = _bounded_read(path)
    contract = _validate(json.loads(content))
    feature_path = path.with_suffix(".feature")
    if _bounded_read(feature_path) != contract["payload"].get("feature", "").encode("utf-8"):
        raise ValueError("Feature artifact changed; approval is invalid")
    return contract
