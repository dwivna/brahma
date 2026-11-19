"""Subscription CLI adapters. No API clients, credential files, or shell execution.

Worker output is untrusted: only bounded, sanitised structured events are forwarded.
Auth probes and workers share the same scrubbed environment.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from .models import Emit, Event, Result
from . import quota

MAX_LINE_BYTES = 1024 * 1024
MAX_TEXT_CHARS = 256 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
AUTH_TIMEOUT = 8.0
TERMINATE_GRACE = 0.4

# CSI, OSC, DCS/APC/PM and remaining escape/control characters cannot reach a TUI.
_ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|"
                      r"\x1b[P_^].*?\x1b\\|\x1b\[[0-?]*[ -/]*[@-~]|"
                      r"\x1b[@-_]|[\x00-\x08\x0b-\x1f\x7f-\x9f]", re.S)


def clean_text(value: Any) -> str:
    return _ESCAPES.sub("", value if isinstance(value, str) else str(value))


def child_env() -> dict[str, str]:
    """Keep local sign-in paths, remove credentials and alternate provider routing."""
    result = {}
    for name, value in os.environ.items():
        key = name.upper()
        credential = (any(part in key for part in ("API_KEY", "APIKEY", "ACCESS_TOKEN", "AUTH_TOKEN", "BEARER_TOKEN"))
                      or key.endswith(("_TOKEN", "_SECRET", "_PASSWORD", "_SECRET_ACCESS_KEY"))
                      or key in {"TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"})
        override = (key.startswith(("OPENAI_", "ANTHROPIC_", "AZURE_OPENAI_", "CODEX_API_"))
                    or key.startswith("CLAUDE_CODE_USE_")
                    or key in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
                               "CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "CLAUDE_CODE_SIMPLE",
                               "CODEX_MODEL_PROVIDER", "CODEX_BASE_URL", "API_BASE_URL"}
                    or key.endswith(("BASE_URL", "API_BASE", "API_ENDPOINT")))
        if not credential and not override:
            result[name] = value
    result["NO_COLOR"] = "1"
    result["TERM"] = "dumb"
    return result


async def _stop(process: asyncio.subprocess.Process) -> None:
    """Kill the whole worker group, even when the group leader already exited."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    await asyncio.sleep(TERMINATE_GRACE)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await process.wait()


async def _auth_capture(args: list[str], merge_stderr: bool = False) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.DEVNULL,
        env=child_env(), start_new_session=True, limit=MAX_LINE_BYTES,
    )
    try:
        async def collect():
            data = bytearray()
            while chunk := await process.stdout.read(4096):
                data.extend(chunk)
                if len(data) > 65536:
                    raise ValueError("Authentication response too large")
            await process.wait()
            return process.returncode, clean_text(data.decode("utf-8", "replace"))
        return await asyncio.wait_for(collect(), AUTH_TIMEOUT)
    finally:
        # A subprocess may spawn a child holding stdout open, including on exit.
        await _stop(process)


async def doctor(provider: str) -> dict[str, Any]:
    result = {"provider": provider, "installed": False, "signed_in": False,
              "signedin": False, "subscription": False, "available": False, "error": None,
              "state": "unverified", "action": ""}
    if provider not in ("codex", "claude"):
        result["error"] = "Unsupported provider"
        return result
    executable = shutil.which(provider)
    if not executable:
        result["error"] = f"{provider} CLI is not installed"
        result["state"] = "missing"
        instructions = ("https://developers.openai.com/codex/cli" if provider == "codex"
                        else "https://code.claude.com/docs/en/setup")
        result["action"] = f"Please install {provider} using {instructions}, then run /doctor again."
        return result
    result["installed"] = True
    login = "codex login" if provider == "codex" else "claude auth login"
    result["action"] = f"Please run `{login}` in your terminal and choose subscription sign-in, then run /doctor again."
    try:
        args = [executable, "login", "status"] if provider == "codex" else [executable, "auth", "status", "--json"]
        code, output = await _auth_capture(args, merge_stderr=provider == "codex")
        if provider == "codex":
            signed_in = code == 0 and bool(re.search(r"^Logged in using .+$", output, re.I | re.M))
            subscription = code == 0 and bool(re.search(r"^Logged in using ChatGPT\s*$", output, re.I | re.M))
            signed_out = bool(re.search(r"^Not logged in\s*$", output, re.I | re.M))
        else:
            value = json.loads(output)
            if not isinstance(value, dict):
                raise ValueError("Unknown auth response")
            signed_in = value.get("loggedIn") is True
            signed_out = value.get("loggedIn") is False
            method = str(value.get("authMethod", "")).lower()
            # claude.ai OAuth is subscription auth; apiKey/console are not.
            subscription = code == 0 and signed_in and method in ("claude.ai", "claude_ai")
        result.update(signed_in=signed_in, signedin=signed_in,
                      subscription=subscription, available=subscription)
        if subscription:
            result.update(state="ready", action="")
        elif signed_out:
            result.update(state="signed_out", error=f"{provider} CLI is installed but not signed in")
        elif signed_in:
            result["state"] = "wrong_auth"
            result["error"] = "Subscription sign-in required; API or unknown authentication is refused"
        else:
            result["error"] = "Could not verify subscription sign-in"
    except (OSError, ValueError, asyncio.TimeoutError):
        result["error"] = "Could not verify subscription sign-in"
    if result["state"] == "unverified":
        result["action"] = (f"Please run `{provider} {'login status' if provider == 'codex' else 'auth status'}` "
                            "in your terminal to check access, then run /doctor again.")
    return result


def _usage(value: Any) -> dict[str, int | float]:
    # Provider-reported counts only. No inferred spend, savings or quota claims.
    if not isinstance(value, dict):
        return {}
    return {str(key): count for key, count in value.items()
            if isinstance(count, (int, float)) and not isinstance(count, bool)
            and count >= 0 and ("token" in str(key).lower() or key in ("num_turns",))}


def _native_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", value):
        raise ValueError("Invalid native session identifier")
    return value


# A provider capacity error (HTTP 529 / overloaded_error), not a model's prose that happens to say "overloaded".
_OVERLOADED = re.compile(r"overloaded_error|\b529\b|^\W*(?:api error:?\s*)?(?:\d{3}\s*)?overloaded\b", re.I)


class StreamParser:
    def __init__(self, provider: str, role: str, emit: Emit):
        self.provider, self.role, self.emit = provider, role, emit
        self.text = ""
        self.final_text = ""
        self.usage: dict[str, Any] = {}
        self.session_id: str | None = None
        self.completed = False
        self.failed = False
        self.limited = False
        self.overloaded = False
        self.truncated = False
        self.error = ""
        self._claude_assistant = ""
        self.quota_snapshot: dict | None = None

    def report_quota(self, snapshot: dict) -> None:
        self.quota_snapshot = quota.sticky(self.quota_snapshot, snapshot)
        self.limited = self.limited or self.quota_snapshot.get("blocked") is True
        self.emit(Event(self.provider, self.role, "quota", json.dumps(self.quota_snapshot)))

    def message(self, value: str) -> None:
        value = clean_text(value)
        remaining = MAX_TEXT_CHARS - len(self.text)
        if len(value) > remaining:
            self.truncated = True
        value = value[:max(0, remaining)]
        if value:
            self.text += ("\n" if self.text else "") + value
            self.text = self.text[:MAX_TEXT_CHARS]
            self.emit(Event(self.provider, self.role, "message", value))

    def consume(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError):
            raise ValueError("Worker emitted invalid JSONL") from None
        if not isinstance(value, dict):
            raise ValueError("Worker emitted an invalid event")
        kind = value.get("type")
        session = value.get("thread_id") if self.provider == "codex" else value.get("session_id")
        if isinstance(session, str) and session != self.session_id:
            self.session_id = _native_id(session)
            self.emit(Event(self.provider, self.role, "native_session", self.session_id))
        if self.provider == "codex" and (value.get("method") == "account/rateLimits/updated"
                or kind in ("rate_limits", "rate_limits.updated", "rate_limits_updated", "account.rate_limits.updated")):
            payload = value.get("params", value)
            self.report_quota(quota.normalize_codex(payload))
        if self.provider == "codex":
            if kind == "item.completed":
                item = value.get("item", {})
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    if isinstance(item.get("text"), str):
                        self.message(item["text"])
                        self.final_text = clean_text(item["text"])[:MAX_TEXT_CHARS]
            elif kind == "turn.completed":
                self.completed = True
                self.usage.update(_usage(value.get("usage")))
            elif kind in ("error", "turn.failed"):
                self.failed = True
                error = value.get("error", value)
                message = error.get("message", "Worker reported an error") if isinstance(error, dict) else error
                self.error = clean_text(message)[:2000]
        else:
            if kind == "assistant":
                message = value.get("message", {})
                if not isinstance(message, dict):
                    raise ValueError("Invalid assistant message")
                blocks = message.get("content", [])
                text = "\n".join(block["text"] for block in blocks
                                 if isinstance(block, dict) and block.get("type") == "text"
                                 and isinstance(block.get("text"), str)) if isinstance(blocks, list) else ""
                if text:
                    self._claude_assistant = clean_text(text)[:MAX_TEXT_CHARS]
                    self.message(text)
                    self.final_text = self._claude_assistant
                # Per-message usage is provisional; final result is authoritative.
                self.usage.update(_usage(message.get("usage")))
                if value.get("error"):
                    self.failed = True
                    self.error = clean_text(value["error"])[:2000]
            elif kind == "result":
                self.completed = True
                self.failed = self.failed or value.get("is_error") is True or value.get("subtype", "success") != "success"
                self.usage.update(_usage(value.get("usage")))
                final = value.get("result", "")
                if isinstance(final, str) and clean_text(final) != self._claude_assistant:
                    self.message(final)
                if isinstance(final, str) and final:
                    self.final_text = clean_text(final)[:MAX_TEXT_CHARS]
                if self.failed:
                    errors = value.get("errors", [])
                    self.error = clean_text(final or "; ".join(str(e) for e in errors))[:2000]
            elif kind in ("error", "rate_limit_event"):
                if kind == "error":
                    self.failed = True
                    self.error = clean_text(value.get("error", value.get("message", "Worker error")))[:2000]
                else:
                    info = value.get("rate_limit_info", {})
                    self.report_quota(quota.normalize_claude(info))
        lowered = self.error.lower()
        if self.error and any(word in lowered for word in
                              ("rate limit", "usage limit", "quota", "limit reached", "hit your limit")):
            self.limited = True
        elif self.error and _OVERLOADED.search(self.error):
            # A transient capacity error, not an account limit: never touches quota state (Round 13).
            self.overloaded = True


def _command(provider: str, executable: str, cwd: Path, role: str, settings,
             native_session_id: str | None = None) -> list[str]:
    model = settings.model_for(provider)
    readonly = role in ("planner", "reviewer")
    if native_session_id is not None:
        _native_id(native_session_id)
    if provider == "codex":
        command = [executable, "exec", "--json", "--color", "never", "--ignore-user-config",
                "--sandbox", "read-only" if readonly else "workspace-write", "-C", str(cwd),
                "-c", 'approval_policy="never"', "-c", 'model_provider="openai"',
                "-c", 'forced_login_method="chatgpt"', "-m", model]
        if native_session_id is not None:
            command += ["resume", native_session_id]
        return command + ["-"]
    tools = "Read,Glob,Grep" + ("" if readonly else ",Edit,Write")
    command = [executable, "-p", "--output-format", "stream-json", "--verbose",
            "--permission-mode", "plan" if readonly else "acceptEdits", "--permission-prompts", "none",
            "--restricted", "--safe-mode", "--strict-mcp-config", '--mcp-config', '{"mcpServers":{}}',
            "--tools", tools, "--allowedTools", tools, "--model", model]
    if native_session_id is not None:
        command += ["--resume", native_session_id]
    return command


# Waits between retries of a transiently overloaded provider (about 1, 5 and 10 minutes).
OVERLOAD_DELAYS = (60.0, 300.0, 600.0)
# A retry is only worth waiting for when at least this much budget (or a quarter of it) remains afterwards.
OVERLOAD_MIN_BUDGET = 60.0


async def run(provider: str, prompt: str, cwd: Path, role: str, settings,
              emit: Emit, cancel: asyncio.Event, timeout: float,
              native_session_id: str | None = None) -> Result:
    """Run one bounded worker, retrying a transient "overloaded" error inside the same attempt.

    Retries spend no repair cycle and never touch quota state; they stay inside the caller's
    time budget. If the provider is still overloaded the result status is "overloaded".
    """
    from . import safety
    try:
        report = safety.check_launch(provider, role, bool(getattr(settings, "strict_enforcement", False)))
    except ValueError as error:
        emit(Event(provider, role, "enforcement", str(error)))
        return Result(provider, "unavailable", str(error))  # fail closed: no worker starts, the limit is recorded
    emit(Event(provider, role, "enforcement", report.describe()))
    started = time.monotonic()
    result = await _run_once(provider, prompt, cwd, role, settings, emit, cancel, timeout, native_session_id)
    for delay in OVERLOAD_DELAYS:
        if result.status != "overloaded":
            break
        remaining = timeout - (time.monotonic() - started)
        # Waiting is only worth it if a useful slice of the budget is left afterwards.
        if remaining <= delay + max(OVERLOAD_MIN_BUDGET, 0.25 * timeout):
            break
        emit(Event(provider, role, "progress",
                   f"Provider overloaded; retrying in {int(delay)}s within this attempt (no repair cycle used)"))
        try:
            await asyncio.wait_for(cancel.wait(), delay)
            return Result(provider, "cancelled", seconds=time.monotonic() - started)
        except asyncio.TimeoutError:
            pass
        result = await _run_once(provider, prompt, cwd, role, settings, emit, cancel,
                                 timeout - (time.monotonic() - started), native_session_id)
    return result


async def _run_once(provider: str, prompt: str, cwd: Path, role: str, settings,
                    emit: Emit, cancel: asyncio.Event, timeout: float,
                    native_session_id: str | None = None) -> Result:
    """Run one bounded worker. A JSON completion marker and exit code zero are required."""
    started = time.monotonic()
    if cancel.is_set():
        return Result(provider, "cancelled")
    if timeout <= 0:
        return Result(provider, "timeout")
    deadline = started + timeout
    auth_task = asyncio.create_task(doctor(provider))
    auth_cancel = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait((auth_task, auth_cancel), timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if auth_cancel in done and auth_cancel.result():
            return Result(provider, "cancelled", seconds=time.monotonic() - started)
        if auth_task not in done:
            return Result(provider, "timeout", "Subscription check exceeded the time limit", seconds=time.monotonic() - started)
        auth = await auth_task
    finally:
        for pending in (auth_task, auth_cancel):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(auth_task, auth_cancel, return_exceptions=True)
    if not auth["available"]:
        return Result(provider, "unavailable", auth["error"] or "Subscription unavailable", seconds=time.monotonic() - started)
    if cancel.is_set():
        return Result(provider, "cancelled", seconds=time.monotonic() - started)
    executable = shutil.which(provider)
    parser = StreamParser(provider, role, emit)
    process = None
    watchers: list[asyncio.Task] = []
    status = "error"
    error = ""
    try:
        process = await asyncio.create_subprocess_exec(
            *_command(provider, executable, Path(cwd), role, settings, native_session_id), cwd=cwd, env=child_env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True, limit=MAX_LINE_BYTES,
        )
        emit(Event(provider, role, "progress", "Worker started"))

        async def feed():
            try:
                process.stdin.write(prompt.encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        async def stdout():
            pending = bytearray()
            total = 0
            while chunk := await process.stdout.read(8192):
                total += len(chunk)
                if total > MAX_STREAM_BYTES:
                    raise ValueError("Worker stream exceeded its byte limit")
                pending.extend(chunk)
                while True:
                    end = pending.find(b"\n")
                    if end < 0:
                        if len(pending) > MAX_LINE_BYTES:
                            raise ValueError("Worker event exceeded its line limit")
                        break
                    if end > MAX_LINE_BYTES:
                        raise ValueError("Worker event exceeded its line limit")
                    line = bytes(pending[:end])
                    del pending[:end + 1]
                    parser.consume(line)
            if pending:
                parser.consume(bytes(pending))

        async def stderr():
            # Drain without retaining or exposing raw diagnostics/auth details.
            total = 0
            while chunk := await process.stderr.read(8192):
                total += len(chunk)
                if total > MAX_STREAM_BYTES:
                    raise ValueError("Worker diagnostics exceeded their byte limit")

        async def complete():
            await asyncio.gather(feed(), stdout(), stderr(), process.wait())

        work = asyncio.create_task(complete())
        stopped = asyncio.create_task(cancel.wait())
        watchers = [work, stopped]
        done, _ = await asyncio.wait(watchers, timeout=max(0, deadline - time.monotonic()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if stopped in done and stopped.result():
            status = "cancelled"
        elif work not in done:
            status = "timeout"
        else:
            await work
            if parser.limited:
                status = "limited"
            elif parser.overloaded:
                status = "overloaded"
            elif process.returncode != 0 or parser.failed:
                status = "error"
                error = parser.error or "Worker exited unsuccessfully"
            elif not parser.completed:
                status = "error"
                error = "Worker exited without a structured completion event"
            elif parser.truncated:
                status = "error"
                error = "Worker response exceeded the retained text limit"
            else:
                status = "ok"
    except (OSError, ValueError) as exc:
        error = clean_text(str(exc))[:2000]
    finally:
        if process is not None:
            await _stop(process)
        for task in watchers:
            if not task.done():
                task.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
    if parser.usage:
        emit(Event(provider, role, "usage", usage=parser.usage))
    if status != "ok":
        detail = error or parser.error or {"timeout": "Worker time limit reached", "cancelled": "Worker cancelled",
                                          "limited": "Provider limit reached",
                                          "overloaded": "Provider overloaded"}.get(status, "Worker failed")
        emit(Event(provider, role, "error", clean_text(detail)))
    return Result(provider, status, parser.final_text or parser.text or error or parser.error, parser.usage,
                  parser.session_id, time.monotonic() - started)
