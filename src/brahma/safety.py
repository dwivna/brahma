"""Central sanitisation, untrusted-text handling and enforcement reporting (Round 7; version 0.6.0).

Everything a worker, repository file, issue, web page or tool prints is data, never an instruction. This module
is the single place that redacts secrets before text reaches logs, events or packets, flags embedded
instructions for the user in redacted form, and states honestly which restrictions the installed provider CLIs
can and cannot enforce. It imports nothing from the store, engine or providers so any layer can use it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

REDACTED = "[REDACTED]"
EXCERPT = 160

_SECRET = re.compile(
    # name containing a credential word (compound names, quoted keys/values, : = or space separators)
    r"(?i)[\w.-]*(?:api[_-]?key|apikey|secret|token|passwd|password|authorization|credential|private[_-]?key)[\w.-]*"
    r"[\"']?\s*(?:[:=]\s*[\"']?|\s+(?=[^\s\"',;]*\d))(?:(?:bearer|basic)\s+)?(?P<value>[^\s\"',;]{6,})"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|sk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bAKIA[0-9A-Z]{16}|\bxox[baprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")
# `NAME=value` lines from environment files, where the name suggests a credential.
_ENV_LINE = re.compile(r"(?im)^(\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASS|PASSWORD|CREDENTIAL|DSN)[A-Z0-9_]*\s*=\s*)\S.*$")

_FLAG_PATTERNS = (
    ("override", re.compile(r"(?i)\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your)\b[^.\n]{0,30}"
                            r"\b(instructions?|rules?|prompts?|guidelines?)\b")),
    ("secret_access", re.compile(r"(?i)(\b(read|cat|print|show|send|upload|exfiltrate|curl|echo|type)\b[^.\n]{0,60}"
                                 r"(\.env\b|\bcredentials?\b|id_rsa|\.ssh\b|auth\.json|\.npmrc|\.netrc|api[_ -]?keys?|secrets?\b|sign-?in material))"
                                 r"|(\.env\b[^.\n]{0,40}\b(contents?|file)\b)")),
    ("publication", re.compile(r"(?i)\bgit\s+push\b|\bpush\b[^.\n]{0,40}\b(main|master|develop|default branch|origin|production)\b"
                               r"|\bforce[- ]push\b|\bgh\s+pr\s+(create|merge)\b|\bgh\s+release\b|\bnpm\s+publish\b|\btwine\s+upload\b")),
    ("merge_release", re.compile(r"(?i)\b(merge|release|deploy|publish|tag)\b[^.\n]{0,30}\b(this|the)\b[^.\n]{0,30}"
                                 r"\b(pr|pull request|branch|version|build|release)\b|\bgit\s+(merge|tag|rebase|reset)\b")),
    ("remote_exec", re.compile(r"(?i)\b(curl|wget)\b[^|\n]*\|\s*(ba|z)?sh\b|\bbash\s+-c\b|\beval\s*\(|\brm\s+-rf\s+[/~]")),
)


def _hide(match: "re.Match[str]") -> str:
    """Keep the name so a reader sees what was hidden; drop the value."""
    value = match.group("value")
    return match.group(0).replace(value, REDACTED) if value else REDACTED


def redact(text: str) -> str:
    """Remove secret-looking values; keeps names so a reader can still see what was hidden."""
    text = _ENV_LINE.sub(lambda m: m.group(1) + REDACTED, text)
    return _SECRET.sub(_hide, text)


def looks_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


def argv_looks_secret(argv) -> bool:
    """Scan a command both word by word and joined, so `--password hunter22` and `TOKEN=x` are both seen."""
    words = [str(word) for word in argv]
    return any(looks_secret(word) for word in words) or looks_secret(" ".join(words))


def sanitize(text) -> str:
    """The one function applied before text is written to an event, log, packet or screen."""
    from .providers import clean_text  # terminal escapes; local import keeps this module dependency-free at load
    return redact(clean_text(text))


@dataclass(frozen=True)
class Flag:
    kind: str       # override | secret_access | publication | merge_release | remote_exec
    excerpt: str    # redacted, bounded; shown to the user, never acted on


def flag_embedded_instructions(text: str) -> list[Flag]:
    """Detect text that tries to direct Brahma or a worker. Flags are for the user; nothing here acts."""
    flags, seen = [], set()
    for kind, pattern in _FLAG_PATTERNS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 40)
            excerpt = sanitize(text[start:match.end() + 40]).replace("\n", " ").strip()[:EXCERPT]
            if (kind, excerpt) not in seen:
                seen.add((kind, excerpt))
                flags.append(Flag(kind, excerpt))
    return flags


def inspect_untrusted(text: str) -> tuple[str, list[Flag]]:
    """Process repository, issue, PR, web or worker text as data: returns redacted text and flagged excerpts."""
    return sanitize(text), flag_embedded_instructions(text)


# ---- enforcement reporting --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Enforcement:
    provider: str
    role: str
    write_confinement: str      # what stops a worker writing outside its worktree before the fact
    ownership: str              # how approved ownership is enforced
    protected_reads: bool       # True only if the CLI can be told to refuse reading secrets/outside paths
    limits: tuple[str, ...]     # what Brahma cannot prevent, stated plainly

    def describe(self) -> str:
        return (f"{self.provider} {self.role}: writes {self.write_confinement}; ownership {self.ownership}; "
                f"protected reads {'enforced' if self.protected_reads else 'NOT enforced'}. Limits: " + "; ".join(self.limits))


def enforcement(provider: str, role: str) -> Enforcement:
    """What the installed launch settings can enforce. Only capabilities Brahma has verified are claimed.

    Neither provider CLI is launched with a flag that denies reads of secret files or of paths outside the
    worktree, so protected reads are reported as not enforced rather than assumed.
    """
    if provider not in ("codex", "claude"):
        raise ValueError("Unknown provider")
    readonly = role in ("planner", "reviewer")
    if provider == "codex":
        confinement = "none (read-only sandbox)" if readonly else "confined to the worktree by the workspace-write sandbox"
        limits = ("the sandbox does not restrict which files the worker may read, including files in your home directory",
                  "writes inside the worktree are limited to approved ownership only after the fact (revert and block)")
    else:
        confinement = "none (read-only tools)" if readonly else "limited to the Edit and Write tools, which have no shell"
        limits = ("the Read, Glob and Grep tools are not path-restricted and can read outside the worktree",
                  "writes inside the worktree are limited to approved ownership only after the fact (revert and block)")
    return Enforcement(provider, role, confinement, "verified after the run against the prelaunch snapshot", False, limits)


def check_launch(provider: str, role: str, strict: bool) -> Enforcement:
    """Prelaunch gate. With `strict_enforcement` on, a worker whose provider cannot enforce protected reads is not
    started (fail closed); otherwise the limitation is reported and recorded by the caller."""
    report = enforcement(provider, role)
    if strict and not report.protected_reads:
        raise ValueError("Launch refused (strict_enforcement): " + report.describe())
    return report
