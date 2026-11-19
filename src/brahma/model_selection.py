"""Human-facing, no-generation inspection of task model preferences."""
from __future__ import annotations

import asyncio

from .config import Settings
from .model_catalog import discover_models, select_profiles


async def read_models(settings: Settings, names=("codex", "claude"), cancel=None) -> dict:
    catalogs = await asyncio.gather(*(discover_models(name, cancel) for name in names))
    return {"catalogs": catalogs, "selection": select_profiles(settings, catalogs)}


def format_models(report: dict) -> str:
    lines = ["Task model profiles — native catalogs checked without a model request."]
    for catalog in report["catalogs"]:
        provider = catalog["provider"]
        if catalog.get("error"):
            lines.append(f"{provider.capitalize()}: {catalog['error']}")
        else:
            lines.append(f"{provider.capitalize()} advertised models: " +
                         (", ".join(row["model"] for row in catalog["models"]) or "none"))
        for suffix, purpose in (("_model", "routine work"), ("_critical_model", "complex or critical work")):
            row = report["selection"]["profiles"][provider + suffix]
            status = "advertised" if row["verified"] else "unavailable" if row["available"] is False else "unverified"
            lines.append(f"  {purpose}: {row['selected'] or 'no permitted model'} ({status}). {row['reason']}.")
    lines.extend(("These are task profiles, not measured benchmark rankings; no performance data yet.",
                  "Model listings do not establish remaining quota or included billing entitlement. Use /usage for quota.",
                  "Planning locks these choices for approval. Inspection changes no active plan."))
    return "\n".join(lines)
