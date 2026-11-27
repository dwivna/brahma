"""Read-only subscription status, without a planner or a model request."""
from __future__ import annotations

import asyncio
import math
import re
import time
from typing import Any

from . import providers as native, quota

PROVIDERS = ("codex", "claude")
_SOURCES = {"codex": {"codex.app_server", "worker.limit"},
            "claude": {"claude.stream", "claude.statusline", "worker.limit"}}
_STATUS_WORDS = frozenset("""
    codex claude both available availability usage limit limits yes i want current
    and remaining capacity for the tell me please show check report what is are
    my our how much many quota left on subscription subscriptions allowance
    allowances reset resets times time when next five seven hour hours day days
    week weeks weekly used percent percentage can could you of in now have do we
    get give display a an status their with to all last reported account accounts
""".split())


def usage_request(text: str) -> tuple[str, ...] | None:
    """Recognise status questions conservatively; feature requests still need planning."""
    if not isinstance(text, str) or len(text) > 512 or "\n" in text or "`" in text:
        return None
    value = " ".join(text.lower().strip().split())
    command = re.fullmatch(r"/usage(?: (codex|claude))?", value)
    if command:
        return (command[1],) if command[1] else PROVIDERS
    if value.startswith("/"):
        return None
    # A direct status path must consume the whole request. A small vocabulary
    # avoids guessing about a second instruction after "and", commas, or code.
    # Provider lists and "used and remaining" remain ordinary status phrases.
    value = re.sub(r"\b(codex|claude)'s\b", r"\1", value)
    value = value.replace("what's", "what is").rstrip("?.!")
    if re.search(r"[^a-z0-9\s%-]", value):
        return None
    if any(word not in _STATUS_WORDS for word in re.findall(r"[a-z]+", value)):
        return None
    if re.search(r"\bhow\b", value) and not re.search(r"\bhow (much|many)\b", value):
        return None
    # Do not silently consume implementation, design or multi-action requests.
    if re.search(r"\b(implement|add|build|create|fix|change|design|plan|integrate|wire|"
                 r"routing|route|handle|handling|manage|managing|support|dashboard|widget|"
                 r"feature|command|architecture|automate)\b", value):
        return None
    if re.search(r"\b(in|to|on|inside)\b.*\b(ui|app|header|panel|pane|view|screen)\b", value):
        return None
    selected = tuple(name for name in PROVIDERS if re.search(rf"\b{name}\b", value))
    capacity = re.search(r"\b(usage|quota|capacity|limits?|allowance)\b", value)
    read = re.search(r"\b(show|tell|check|get|give|display|current|remaining|available|left|what|how|yes|report)\b", value)
    if capacity and (read or value in ("usage", "quota", "capacity")):
        return selected or PROVIDERS
    if selected and re.search(r"\b(are|is|check|show)\b", value) and re.search(r"\bavailable\b", value):
        return selected
    return None


def _number(value: Any, maximum: float, minimum: float = 0) -> float | None:
    if value is None:
        return None
    if type(value) not in (float, int) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Invalid quota number")
    return float(value)


def _snapshot(value: Any, provider: str) -> dict | None:
    """Validate and whitelist observations; raw native/auth data never enters reports."""
    if not isinstance(value, dict) or value.get("provider") != provider or value.get("source") not in _SOURCES[provider]:
        return None
    try:
        observed = _number(value.get("observed_at"), 253402300799)
        if observed is None:
            return None
        windows = value.get("windows")
        if not isinstance(windows, list) or len(windows) > quota.MAX_WINDOWS:
            return None
        result = {"provider": provider, "source": value["source"], "observed_at": observed,
                  "windows": [], "blocked": value.get("blocked"),
                  "reset_at": _number(value.get("reset_at"), 253402300799),
                  "error": "Provider quota unavailable" if value.get("error") else None}
        if result["blocked"] is not None and type(result["blocked"]) is not bool:
            return None
        for index, window in enumerate(windows):
            if not isinstance(window, dict):
                return None
            blocked = window.get("blocked")
            if blocked is not None and type(blocked) is not bool:
                return None
            # Window IDs are not needed for a human capacity report.
            result["windows"].append({"name": f"window_{index + 1}",
                "duration_seconds": _number(window.get("duration_seconds"), 366 * 86400, 1),
                "used_percent": _number(window.get("used_percent"), 100),
                "reset_at": _number(window.get("reset_at"), 253402300799), "blocked": blocked})
        if provider == "claude":
            result["utilization"] = _number(value.get("utilization"), 100)
        return result
    except (TypeError, ValueError):
        return None


def _freshness(snapshot: dict | None, now: float) -> tuple[str, float | None]:
    if snapshot is None:
        return "unknown", None
    age = now - snapshot["observed_at"]
    if age < -5:
        return "future", None
    resets = [window["reset_at"] for window in snapshot["windows"] if window["reset_at"] is not None]
    if snapshot["reset_at"] is not None:
        resets.append(snapshot["reset_at"])
    if any(reset <= now for reset in resets):
        return "reset_passed", max(0, age)
    return ("stale" if age > quota.FRESH_SECONDS else "fresh"), max(0, age)


def _readiness(provider: str, result: Any = None) -> dict:
    result = result if isinstance(result, dict) else {}
    state = result.get("state")
    if state not in {"missing", "signed_out", "wrong_auth", "unverified", "ready"}:
        state = "unverified"
    login = "codex login" if provider == "codex" else "claude auth login"
    actions = {"missing": f"Install the {provider} CLI, then run /doctor.",
               "signed_out": f"Run `{login}` with subscription sign-in, then /doctor.",
               "wrong_auth": f"Run `{login}` and select subscription sign-in, then /doctor.",
               "unverified": "Run /doctor to verify subscription sign-in.", "ready": ""}
    return {"state": state,
            "installed": result.get("installed") if type(result.get("installed")) is bool else None,
            "signed_in": result.get("signed_in") if type(result.get("signed_in")) is bool else None,
            "subscription": result.get("subscription") if type(result.get("subscription")) is bool else None,
            "action": actions[state]}


class _CheckCancelled(Exception):
    pass


async def _check(awaitable, cancel: asyncio.Event):
    """Cancel and await native cleanup when the UI stops a read-only check."""
    task = asyncio.create_task(awaitable)
    stopped = asyncio.create_task(cancel.wait())
    try:
        if cancel.is_set():
            raise _CheckCancelled
        done, _ = await asyncio.wait((task, stopped), timeout=native.AUTH_TIMEOUT + 4,
                                     return_when=asyncio.FIRST_COMPLETED)
        if stopped in done or cancel.is_set():
            raise _CheckCancelled
        if task not in done:
            raise TimeoutError("Native status check timed out")
        return await task
    finally:
        for pending in (task, stopped):
            if not pending.done():
                pending.cancel()
        await asyncio.gather(task, stopped, return_exceptions=True)


async def read_usage(store, providers=PROVIDERS, cancel=None, refresh=True) -> dict:
    """Read auth readiness and actual quota observations without touching active work.

    Codex exposes a read-only quota RPC. Claude's cached stream/status-line signals
    are all that is available here; asking a model cannot discover account quota.
    """
    selected = tuple(providers)
    if not selected or len(set(selected)) != len(selected) or any(p not in PROVIDERS for p in selected):
        raise ValueError("Select codex, claude, or both")
    cancel = cancel if cancel is not None else asyncio.Event()
    report = {"observed_at": time.time(), "cancelled": False, "providers": []}
    for provider in selected:
        try:
            cached = _snapshot(store.quota(provider), provider)
        except (TypeError, ValueError):
            cached = None
        row = {"provider": provider, "readiness": _readiness(provider), "quota": cached,
               "origin": "cached" if cached else "unavailable", "refresh_error": None,
               "cancelled": False}
        report["providers"].append(row)
    for row in report["providers"]:
        provider = row["provider"]
        if cancel.is_set():
            report["cancelled"] = row["cancelled"] = True
            continue
        try:
            row["readiness"] = _readiness(provider, await _check(native.doctor(provider), cancel))
            auth = row["readiness"]
            if provider == "codex" and refresh and all(auth[k] is True for k in ("installed", "signed_in", "subscription")):
                fresh = _snapshot(await _check(quota.probe_codex(cancel), cancel), provider)
                meaningful = fresh is not None and (fresh["blocked"] is True or any(
                    w["used_percent"] is not None or w["blocked"] is True for w in fresh["windows"]))
                if meaningful and not fresh["error"] and _freshness(fresh, time.time())[0] == "fresh":
                    store.observe(fresh)
                    row.update(quota=fresh, origin="refreshed")
                else:
                    row["refresh_error"] = "Codex quota refresh unavailable; existing observations retained."
        except _CheckCancelled:
            report["cancelled"] = row["cancelled"] = True
        except Exception:
            # Exception text may contain native output. Keep it out of reports.
            row["refresh_error"] = "Native status check unavailable; existing observations retained."
    report["observed_at"] = time.time()
    for row in report["providers"]:
        row["freshness"], row["age_seconds"] = _freshness(row["quota"], report["observed_at"])
        row["eligibility"] = quota.eligibility(row["quota"], report["observed_at"])
    return report


def _local(value: float) -> str:
    """Observation and reset times in the system timezone; stored observations keep their UTC epochs."""
    from .chat import format_reset_time
    return format_reset_time(value)


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "quota window (duration unknown)"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds % size == 0:
            return f"{seconds / size:g}{unit} window"
    return f"{seconds:g}s window"


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "age unknown"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    return f"{seconds / 3600:.1f}h ago"


def format_usage(report: dict) -> str:
    """Format measured quota separately from CLI readiness and token counters."""
    lines = [f"Subscription usage checked {_local(report['observed_at'])}."]
    for row in report["providers"]:
        auth, snapshot = row["readiness"], row["quota"]
        installed = {True: "installed", False: "not installed", None: "installation unverified"}[auth["installed"]]
        signed = {True: "signed in", False: "signed out", None: "sign-in unverified"}[auth["signed_in"]]
        if auth["signed_in"] is True and auth["subscription"] is not True:
            signed += "; subscription sign-in unverified"
        lines.append(f"{row['provider'].capitalize()}: {installed}; {signed}.")
        if auth["action"]:
            lines.append(f"  {auth['action']}")
        if row["cancelled"]:
            lines.append("  Check cancelled; current remaining capacity unknown.")
        if snapshot is None:
            lines.append("  Remaining capacity unknown; no provider quota observation.")
        else:
            current = row["origin"] == "refreshed" and row["freshness"] == "fresh" and not row["cancelled"]
            prefix = "Provider reported" if current else "Last reported"
            lines.append(f"  Source: {snapshot['source']}; {_age(row['age_seconds'])} ({_local(snapshot['observed_at'])}).")
            for window in snapshot["windows"]:
                used = window["used_percent"]
                amounts = f"{used:g}% used, {100 - used:g}% remaining" if used is not None else "used/remaining percentage unknown"
                reset = f"; reset {_local(window['reset_at'])}" if window["reset_at"] is not None else "; reset unknown"
                lines.append(f"  {prefix} {_duration(window['duration_seconds'])}: {amounts}{reset}.")
            if not snapshot["windows"]:
                lines.append("  Used/remaining percentage unknown; no measured quota window.")
            if snapshot.get("utilization") is not None:
                lines.append("  Provider utilization has unspecified units; no percentage inferred.")
            if snapshot["blocked"] is True:
                lines.append(f"  {prefix}: provider limit reached.")
            if not current or not any(w["used_percent"] is not None for w in snapshot["windows"]):
                reason = {"stale": "observation is stale", "reset_passed": "reported reset has passed",
                          "future": "observation time is invalid"}.get(row["freshness"], "cached or unexposed quota")
                lines.append(f"  Current remaining capacity unknown ({reason}).")
        if row["refresh_error"]:
            lines.append(f"  {row['refresh_error']}")
        if row["provider"] == "claude":
            lines.append("  Claude quota uses saved provider observations; no independent live quota poll.")
    lines.append("Request token counters are not remaining subscription allowance.")
    return "\n".join(lines)
