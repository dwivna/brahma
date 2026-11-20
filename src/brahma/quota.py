"""Provider-reported capacity observations; never inferred from request token totals."""
from __future__ import annotations

from . import __version__

import asyncio
import json
import math
import re
import shutil
import time
from typing import Any

FRESH_SECONDS = 300
MAX_RPC_LINE = 256 * 1024
MAX_RPC_BYTES = 1024 * 1024
MAX_WINDOWS = 64


def _number(value: Any, field: str, maximum: float, *, minimum: float = 0) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"Invalid quota {field}")
    return float(value)


def _observed(value: float | None) -> float:
    return _number(time.time() if value is None else value, "observation time", 253402300799)  # type: ignore[return-value]


def unknown(provider: str, source: str, error: str | None = None,
            observed_at: float | None = None) -> dict:
    if provider not in ("codex", "claude"):
        raise ValueError("Unknown quota provider")
    if not isinstance(source, str) or not source or len(source) > 100:
        raise ValueError("Invalid quota source")
    return {"provider": provider, "source": source, "observed_at": _observed(observed_at),
            "windows": [], "blocked": None, "reset_at": None,
            "error": error[:500] if isinstance(error, str) else None}


def _window(name: str, value: dict, *, duration: float | None = None,
            percent: Any = None, reset: Any = None, blocked: bool | None = None) -> dict:
    used = _number(percent, "used percentage", 100)
    reset_at = _number(reset, "reset time", 253402300799)
    seconds = _number(duration, "window duration", 366 * 86400, minimum=1)
    if blocked is None and used is not None:
        blocked = used >= 100
    return {"name": name, "duration_seconds": seconds, "used_percent": used,
            "reset_at": reset_at, "blocked": blocked}


def _finish(snapshot: dict) -> dict:
    windows = snapshot["windows"]
    if any(window["blocked"] is True for window in windows):
        snapshot["blocked"] = True
    elif snapshot["blocked"] is None and windows and all(window["blocked"] is False for window in windows):
        snapshot["blocked"] = False
    resets = [window["reset_at"] for window in windows
              if window["blocked"] is True and window["reset_at"] is not None]
    snapshot["reset_at"] = max(resets) if resets else None
    return snapshot


def normalize_codex(raw: dict, observed_at: float | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Invalid Codex quota response")
    snapshot = unknown("codex", "codex.app_server", observed_at=observed_at)
    buckets = raw.get("rateLimitsByLimitId")
    if buckets is not None and not isinstance(buckets, dict):
        raise ValueError("Invalid Codex quota buckets")
    if buckets:
        rows = list(buckets.items())
    else:
        single = raw.get("rateLimits", raw.get("rate_limits"))
        rows = [("codex", single)] if single is not None else []
    if len(rows) * 2 > MAX_WINDOWS:
        raise ValueError("Too many quota windows")
    for bucket, limits in rows:
        if not isinstance(bucket, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,100}", bucket) or not isinstance(limits, dict):
            raise ValueError("Invalid Codex quota bucket")
        reached = limits.get("rateLimitReachedType")
        if reached is not None:
            if not isinstance(reached, str) or not reached or len(reached) > 100:
                raise ValueError("Invalid Codex limit state")
            snapshot["blocked"] = True
        for name in ("primary", "secondary"):
            value = limits.get(name)
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ValueError("Invalid Codex quota window")
            minutes = _number(value.get("windowDurationMins"), "window minutes", 366 * 1440, minimum=1)
            window = _window(f"{bucket}:{name}", value,
                duration=minutes * 60 if minutes is not None else None,
                percent=value.get("usedPercent"), reset=value.get("resetsAt"),
                blocked=True if reached in (name, "both") else None)
            snapshot["windows"].append(window)
    return _finish(snapshot)


def normalize_claude(info: dict, observed_at: float | None = None) -> dict:
    if not isinstance(info, dict):
        raise ValueError("Invalid Claude quota event")
    snapshot = unknown("claude", "claude.stream", observed_at=observed_at)
    status = info.get("status")
    if status is not None and status not in ("allowed", "allowed_warning", "rejected"):
        raise ValueError("Invalid Claude quota status")
    snapshot["blocked"] = True if status == "rejected" else False if status else None
    snapshot["status"] = status
    # The SDK documents utilization but not its units: preserve, do not invent a percentage.
    utilization = _number(info.get("utilization"), "utilization", 100)
    snapshot["utilization"] = utilization
    overage = info.get("overageEnabled")
    if overage is not None and type(overage) is not bool:
        raise ValueError("Invalid Claude overage state")
    snapshot["overage_enabled"] = overage
    if info.get("errorCode") is not None:
        if info["errorCode"] != "credits_required":
            raise ValueError("Unknown Claude quota error code")
        snapshot["error"] = "credits_required"
        snapshot["blocked"] = True
    # Also accept the documented status-line fields when supplied by a future local bridge.
    if "five_hour" in info or "seven_day" in info:
        snapshot["source"] = "claude.statusline"
        for name, duration in (("five_hour", 5 * 3600), ("seven_day", 7 * 86400)):
            value = info.get(name)
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ValueError("Invalid Claude quota window")
            snapshot["windows"].append(_window(name, value, duration=duration,
                percent=value.get("used_percentage"), reset=value.get("resets_at")))
    else:
        snapshot["windows"].append(_window("subscription", info,
            percent=info.get("used_percentage", info.get("used_percent")),
            reset=info.get("resetsAt"), blocked=snapshot["blocked"]))
    return _finish(snapshot)


def sticky(previous: dict | None, current: dict) -> dict:
    """A later allowed event cannot erase a rejection in the same worker invocation."""
    if not previous or previous.get("blocked") is not True:
        return current
    if current.get("blocked") is not True:
        return previous
    result = dict(current)
    # Keep independently exhausted windows; the longest known wait dominates.
    windows = {json.dumps(window, sort_keys=True): window for window in
               previous.get("windows", []) + current.get("windows", [])}
    result["windows"] = list(windows.values())[:MAX_WINDOWS]
    if len(windows) > MAX_WINDOWS:
        return previous
    return _finish(result)


def eligibility(snapshot: dict | None, now: float | None = None) -> dict:
    """Return scheduling eligibility. Passing a reset always requires a fresh observation."""
    current = _observed(now)
    def decision(state, reason, next_at=None):
        return {"state": state, "next_at": next_at, "reason": reason}
    if not isinstance(snapshot, dict):
        return decision("unknown", "No provider quota observation")
    try:
        if snapshot.get("provider") not in ("codex", "claude") or not isinstance(snapshot.get("source"), str):
            return decision("unknown", "Invalid quota provenance")
        observed = _number(snapshot.get("observed_at"), "observation time", 253402300799)
        if observed is None or observed > current + 5:
            return decision("unknown", "Invalid or future quota observation")
        windows = snapshot.get("windows")
        if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
            return decision("unknown", "Invalid quota windows")
        blocked = snapshot.get("blocked")
        if blocked is not None and type(blocked) is not bool:
            return decision("unknown", "Invalid blocked state")
        exhausted, unknown_reset, passed_reset = [], False, False
        for window in windows:
            if not isinstance(window, dict):
                return decision("unknown", "Invalid quota window")
            used = _number(window.get("used_percent"), "used percentage", 100)
            reset = _number(window.get("reset_at"), "reset time", 253402300799)
            _number(window.get("duration_seconds"), "window duration", 366 * 86400, minimum=1)
            state = window.get("blocked")
            if state is not None and type(state) is not bool:
                return decision("unknown", "Invalid window state")
            is_blocked = state is True or used is not None and used >= 100
            if is_blocked:
                if reset is None:
                    unknown_reset = True
                elif reset > current:
                    exhausted.append(reset)
                else:
                    passed_reset = True
            elif reset is not None and reset <= current:
                passed_reset = True
        # Keep known future blocks even when the observation grows old. Rechecking is
        # permitted; treating a stale block as available would hammer exhausted quotas.
        if exhausted:
            return decision("waiting", "Provider-reported exhausted quota window", max(exhausted))
        if unknown_reset:
            return {**decision("unknown", "Provider reported a limit without a reset time"), "limit_without_reset": True}
        if passed_reset:
            return decision("unknown", "Reported quota window reset; recheck capacity", current)
        if current - observed > FRESH_SECONDS:
            return decision("unknown", "Quota observation is stale; recheck capacity", current)
        if blocked is True:
            return {**decision("unknown", "Provider reported a limit; reset is unknown"), "limit_without_reset": True}
        if blocked is False and windows and not snapshot.get("error"):
            return decision("available", "Recent provider observation permits work")
        return decision("unknown", "Provider did not expose remaining quota")
    except (TypeError, ValueError):
        return decision("unknown", "Invalid quota observation")


async def probe_codex(cancel: asyncio.Event, timeout: float = 8) -> dict:
    """Read native account quota through stdio RPC, without starting a thread or turn."""
    from .providers import _stop, child_env, doctor
    source = "codex.app_server"
    if cancel.is_set():
        return unknown("codex", source, "Quota probe cancelled")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        return unknown("codex", source, "Quota probe timed out")
    deadline = time.monotonic() + min(timeout, 30)
    process = None
    tasks: list[asyncio.Task] = []

    async def auth_and_exchange():
        nonlocal process
        auth = await doctor("codex")
        if not auth.get("available"):
            return unknown("codex", source, "Subscription sign-in unavailable")
        executable = shutil.which("codex")
        if not executable:
            return unknown("codex", source, "Codex CLI is not installed")
        process = await asyncio.create_subprocess_exec(executable, "app-server", "--listen", "stdio://",
            "-c", 'model_provider="openai"', "-c", 'forced_login_method="chatgpt"',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=child_env(), start_new_session=True, limit=MAX_RPC_LINE)
        stderr_task = asyncio.create_task(drain_stderr())
        tasks.append(stderr_task)
        pending = bytearray()
        consumed = 0
        messages = 0

        async def send(value):
            process.stdin.write((json.dumps(value) + "\n").encode())
            await process.stdin.drain()

        async def response(request_id):
            nonlocal consumed, messages
            while True:
                if stderr_task.done():
                    await stderr_task
                end = pending.find(b"\n")
                if end >= 0:
                    if end > MAX_RPC_LINE:
                        raise ValueError("Quota RPC line limit")
                    line = bytes(pending[:end])
                    del pending[:end+1]
                    value = json.loads(line)
                    messages += 1
                    if messages > 128 or not isinstance(value, dict):
                        raise ValueError("Quota RPC message limit")
                    if type(value.get("id")) is int and value["id"] == request_id:
                        if "error" in value or "result" not in value:
                            raise ValueError("Quota RPC rejected")
                        return value["result"]
                    continue
                chunk = await process.stdout.read(4096)
                if not chunk:
                    raise ValueError("Quota RPC ended without a response")
                consumed += len(chunk)
                pending.extend(chunk)
                if consumed > MAX_RPC_BYTES or len(pending) > MAX_RPC_LINE:
                    raise ValueError("Quota RPC output limit")

        await send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "brahma", "version": __version__},
            "capabilities": {"experimentalApi": False}}})
        await response(1)
        await send({"method": "initialized"})
        await send({"id": 2, "method": "account/rateLimits/read"})
        value = await response(2)
        return normalize_codex(value)

    async def drain_stderr():
        consumed = 0
        while chunk := await process.stderr.read(4096):
            consumed += len(chunk)
            if consumed > MAX_RPC_BYTES:
                raise ValueError("Quota RPC diagnostic limit")

    work = asyncio.create_task(auth_and_exchange())
    stopped = asyncio.create_task(cancel.wait())
    tasks.extend((work, stopped))
    try:
        done, _ = await asyncio.wait((work, stopped), timeout=max(0, deadline-time.monotonic()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if stopped in done and stopped.result():
            return unknown("codex", source, "Quota probe cancelled")
        if work not in done:
            return unknown("codex", source, "Quota probe timed out")
        return await work
    except (OSError, ValueError, TypeError, asyncio.IncompleteReadError):
        return unknown("codex", source, "Native quota probe unavailable or unsupported")
    finally:
        if process is not None:
            if process.stdin:
                process.stdin.close()
            await _stop(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
