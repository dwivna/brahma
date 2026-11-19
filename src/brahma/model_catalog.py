"""Native model discovery without prompts, and selection within configured profiles.

A catalog advertises models; it does not measure quota, prove an included billing
entitlement, or establish benchmark superiority. Account metadata is discarded.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import shutil
import tempfile
import time

from . import providers

MAX_LINE_BYTES = 128 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_MESSAGES = 128
MAX_MODELS = 256
MAX_PAGES = 8
PROFILE_FIELDS = ("codex_model", "codex_critical_model", "claude_model", "claude_critical_model")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]{0,199}\Z")


def _identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _text(value, limit=1000):
    return providers.clean_text(value)[:limit] if isinstance(value, str) else ""


def _model(row: dict, provider: str) -> dict | None:
    if not isinstance(row, dict):
        raise ValueError("Invalid model row")
    if provider == "codex":
        if row.get("hidden") is True:
            return None
        name = _identifier(row.get("model")) or _identifier(row.get("id"))
        resolved = name
        efforts = row.get("supportedReasoningEfforts", [])
        if not isinstance(efforts, list):
            raise ValueError("Invalid model reasoning efforts")
        efforts = [value.get("reasoningEffort") for value in efforts if isinstance(value, dict)]
        default_effort = _identifier(row.get("defaultReasoningEffort"))
    else:
        name = _identifier(row.get("value"))
        resolved = _identifier(row.get("resolvedModel")) or name
        efforts = row.get("supportedEffortLevels", [])
        if not isinstance(efforts, list):
            raise ValueError("Invalid model reasoning efforts")
        default_effort = None
    if not name or not resolved:
        raise ValueError("Invalid model identifier")
    return {"id": resolved, "model": name,
            "display_name": _text(row.get("displayName"), 200),
            "description": _text(row.get("description")),
            "is_default": row.get("isDefault") is True or (provider == "claude" and name == "default"),
            "reasoning_efforts": list(dict.fromkeys(value for value in efforts[:32] if _identifier(value))),
            "default_reasoning_effort": default_effort}


async def discover_models(provider: str, cancel=None, timeout=8) -> dict:
    """Read the installed subscription CLI's catalog; never send a user turn."""
    source = "codex.app_server.model_list" if provider == "codex" else "claude.control.initialize"
    observed_at = time.time()

    def report(models=None, error=None):
        return {"provider": provider, "source": source, "observed_at": observed_at,
                "models": models or [], "error": error}

    if provider not in ("codex", "claude"):
        return report(error="Unsupported provider")
    cancel = cancel if cancel is not None else asyncio.Event()
    if cancel.is_set():
        return report(error="Model discovery cancelled")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        return report(error="Model discovery timed out")
    process = None
    diagnostic = None
    tasks = []

    async def drain_diagnostics():
        count = 0
        while chunk := await process.stderr.read(4096):
            count += len(chunk)
            if count > MAX_OUTPUT_BYTES:
                raise ValueError("Model discovery diagnostic limit")

    async def exchange():
        nonlocal process, diagnostic
        auth = await providers.doctor(provider)
        if not isinstance(auth, dict) or auth.get("available") is not True:
            return report(error="Subscription sign-in unavailable; run brahma doctor")
        executable = shutil.which(provider)
        if not executable:
            return report(error=f"{provider} CLI is not installed")
        if provider == "codex":
            args = [executable, "app-server", "--listen", "stdio://", "-c", 'model_provider="openai"',
                    "-c", 'forced_login_method="chatgpt"']
        else:
            args = [executable, "--print", "--restricted", "--safe-mode", "--strict-mcp-config",
                    "--mcp-config", '{"mcpServers":{}}', "--tools", "", "--permission-mode", "dontAsk",
                    "--no-session-persistence", "--input-format", "stream-json",
                    "--output-format", "stream-json", "--verbose"]
        process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=tempfile.gettempdir(), env=providers.child_env(), start_new_session=True,
            limit=MAX_LINE_BYTES)
        diagnostic = asyncio.create_task(drain_diagnostics())
        tasks.append(diagnostic)
        pending = bytearray()
        consumed = messages = 0

        async def send(value):
            process.stdin.write((json.dumps(value) + "\n").encode())
            await process.stdin.drain()

        async def receive(request_id):
            nonlocal consumed, messages
            while True:
                if diagnostic.done():
                    await diagnostic
                end = pending.find(b"\n")
                if end >= 0:
                    line = bytes(pending[:end])
                    del pending[:end + 1]
                    if end > MAX_LINE_BYTES:
                        raise ValueError("Model discovery line limit")
                    value = json.loads(line)
                    messages += 1
                    if messages > MAX_MESSAGES or not isinstance(value, dict):
                        raise ValueError("Model discovery message limit")
                    if provider == "codex":
                        if type(value.get("id")) is int and value["id"] == request_id:
                            if "error" in value or not isinstance(value.get("result"), dict):
                                raise ValueError("Model discovery rejected")
                            return value["result"]
                    elif value.get("type") == "control_response":
                        response = value.get("response", {})
                        if isinstance(response, dict) and response.get("request_id") == request_id:
                            if response.get("subtype") != "success" or not isinstance(response.get("response"), dict):
                                raise ValueError("Model discovery rejected")
                            return response["response"]
                    continue
                chunk = await process.stdout.read(4096)
                if not chunk:
                    raise ValueError("Model discovery ended without a response")
                consumed += len(chunk)
                pending.extend(chunk)
                if consumed > MAX_OUTPUT_BYTES or len(pending) > MAX_LINE_BYTES:
                    raise ValueError("Model discovery output limit")

        models = []
        if provider == "claude":
            request_id = "brahma-model-discovery"
            await send({"type": "control_request", "request_id": request_id,
                        "request": {"subtype": "initialize", "hooks": None}})
            value = await receive(request_id)
            rows = value.get("models")
            if not isinstance(rows, list) or len(rows) > MAX_MODELS:
                raise ValueError("Invalid model catalog")
            models = [_model(row, provider) for row in rows]
        else:
            await send({"id": 1, "method": "initialize", "params": {
                "clientInfo": {"name": "brahma", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False}}})
            await receive(1)
            await send({"method": "initialized"})
            cursor = None
            seen = set()
            for page in range(MAX_PAGES):
                params = {"limit": 100, "includeHidden": False}
                if cursor is not None:
                    params["cursor"] = cursor
                await send({"id": page + 2, "method": "model/list", "params": params})
                value = await receive(page + 2)
                rows = value.get("data")
                if not isinstance(rows, list) or len(models) + len(rows) > MAX_MODELS:
                    raise ValueError("Invalid model catalog")
                models.extend(_model(row, provider) for row in rows)
                cursor = value.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or len(cursor) > 2000 or cursor in seen:
                    raise ValueError("Invalid model catalog cursor")
                seen.add(cursor)
            else:
                raise ValueError("Model catalog page limit")
        if diagnostic.done():
            await diagnostic
        unique = {}
        for model in models:
            if model:
                unique.setdefault(model["model"], model)
        return report(list(unique.values()))

    work = asyncio.create_task(exchange())
    stopped = asyncio.create_task(cancel.wait())
    tasks.extend((work, stopped))
    try:
        done, _ = await asyncio.wait((work, stopped), timeout=min(timeout, 30),
                                    return_when=asyncio.FIRST_COMPLETED)
        if stopped in done:
            return report(error="Model discovery cancelled")
        if work not in done:
            return report(error="Model discovery timed out")
        return await work
    except (OSError, ValueError, TypeError, asyncio.IncompleteReadError):
        return report(error="Native model catalog unavailable or unsupported")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None:
            if process.stdin:
                process.stdin.close()
            await providers._stop(process)


def _safe(provider, name):
    if provider == "claude":
        return name in ("sonnet", "opus", "haiku") or bool(
            isinstance(name, str) and re.fullmatch(r"claude-(?:sonnet|opus|haiku)-[0-9][0-9.-]*", name))
    return bool(isinstance(name, str) and re.fullmatch(r"gpt-[0-9]+(?:\.[0-9]+)*-(?:sol|astra)", name))


def _family(provider, name):
    if not _safe(provider, name):
        return None
    if provider == "codex":
        return name.rsplit("-", 1)[1]
    return name.split("-", 2)[1] if name.startswith("claude-") else name


def _version(name):
    return tuple(int(part) for part in re.findall(r"\d+", name))


def select_profiles(settings, catalogs) -> dict:
    """Prefer configured models, then current models from the same safe family.

    A successful catalog with no permitted match produces None, requiring the
    caller to block. Missing/failed catalogs preserve a safe configured preference
    as unverified. Critical work never falls back to the routine profile. Selection
    happens before approval; the returned concrete IDs are locked by the caller.
    """
    if isinstance(catalogs, list):
        catalogs = {value.get("provider"): value for value in catalogs if isinstance(value, dict)}
    catalogs = catalogs if isinstance(catalogs, dict) else {}
    configured = {field: getattr(settings, field) for field in PROFILE_FIELDS}
    selected, profiles = {}, {}
    now = time.time()
    for field, preferred in configured.items():
        provider = field.split("_", 1)[0]
        catalog = catalogs.get(provider)
        other = provider + ("_model" if "_critical_" in field else "_critical_model")
        row = {"configured": preferred, "selected": preferred, "verified": False,
               "resolved_model": None, "available": None,
               "reason": "Model catalog unavailable; configured preference is unverified",
               "source": catalog.get("source", "unknown") if isinstance(catalog, dict) else "unknown"}
        if not _safe(provider, preferred):
            row.update(selected=None, available=False,
                       reason="Configured model is unsupported by subscription-safe selection; update the plan's model profiles")
            selected[field] = row["selected"]
            profiles[field] = row
            continue
        stamp = catalog.get("observed_at") if isinstance(catalog, dict) else None
        fresh = (type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= now - stamp <= 300)
        if (isinstance(catalog, dict) and catalog.get("provider") == provider and not catalog.get("error")
                and isinstance(catalog.get("models"), list) and fresh):
            models = catalog["models"]

            def candidate(name):
                family = _family(provider, name)
                eligible = [model for model in models if isinstance(model, dict) and family is not None
                            and _family(provider, model.get("model")) == family
                            and _family(provider, model.get("id")) == family]
                exact = next((model for model in eligible if name in (model["model"], model["id"])), None)
                if exact:
                    return exact, False
                # Catalog default takes precedence only inside this requested family.
                # Numeric version ordering is deterministic, not benchmark evidence.
                eligible.sort(key=lambda model: model["model"])
                chosen = max(eligible, key=lambda model: (model.get("is_default") is True,
                             _version(model["id"])), default=None)
                return chosen, True

            match, family_choice = candidate(preferred)
            if match:
                row.update(selected=match["id"], resolved_model=match["id"], verified=True, available=True,
                           reason=("Configured model not advertised; selected the native default or newest numeric version in the same task family (not a benchmark ranking)"
                                   if family_choice else "Configured task profile is advertised by the installed CLI"))
            elif "_critical_" in field:
                row.update(selected=None, available=False,
                           reason="Configured critical model is unavailable; critical work cannot use the routine model")
            else:
                alternative = configured[other]
                fallback, _ = candidate(alternative)
                if fallback:
                    row.update(selected=fallback["id"], resolved_model=fallback["id"], verified=True, available=True,
                               reason="Configured routine family unavailable; using the configured critical subscription family")
                else:
                    row.update(selected=None, available=False,
                               reason="No configured subscription-safe model is advertised; update the plan's model profiles")
        selected[field] = row["selected"]
        profiles[field] = row
    return {"configured": configured, "selected": selected, "profiles": profiles}
