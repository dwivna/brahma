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
    r"|\bsk_(?:live|test)_[A-Za-z0-9]{16,}|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{30,}|\bglpat-[A-Za-z0-9_-]{20,}|\bnpm_[A-Za-z0-9]{30,}|\bhf_[A-Za-z0-9]{30,}"
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


# A JSON field that names a credential: its value is hidden whatever it looks like (`{"password": "hunter2"}` has no
# recognisable shape once the name and value are separate fields). Matched on whole name segments, so `tokenizer`,
# `input_tokens` (a usage counter) and file paths used as keys (`src/passwords.py` -> fingerprint) are not credentials.
_CREDENTIAL_WORDS = frozenset({"password", "passwd", "passphrase", "pwd", "secret", "secrets", "token", "apikey",
                               "authorization", "auth", "credential", "credentials", "cookie", "cookies", "bearer"})
_CREDENTIAL_PAIRS = frozenset({("api", "key"), ("private", "key"), ("access", "key"), ("secret", "key"),
                               ("client", "secret"), ("session", "key"), ("access", "token"), ("refresh", "token")})


_CREDENTIAL_SUBSTRING = re.compile(r"(?i)api[_-]?key|apikey|secret|token|passw|authorization|credential|private[_-]?key|cookie")
_FILE_NAME = re.compile(r"^[\w.-]*\.[A-Za-z0-9]{1,5}$")


def _words(name: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)]


def credential_field(name: str) -> bool:
    """A field name that marks its value as a credential. Broad on purpose (substring or whole-word match); only keys
    that are file paths or file names (`src/tokenizer.py`, `passwords.py` -> fingerprint) are exempt."""
    words = _words(name)
    if "/" in name or (_FILE_NAME.match(name) and words and words[-1] not in _CREDENTIAL_WORDS):
        return False
    return (bool(_CREDENTIAL_SUBSTRING.search(name)) or any(w in _CREDENTIAL_WORDS for w in words)
            or any(pair in _CREDENTIAL_PAIRS for pair in zip(words, words[1:])))


def usage_counters(value) -> dict:
    """Only provider usage counters survive: non-negative numbers named `...tokens` (input_tokens,
    cache_read_input_tokens, ...) or `num_turns`. Anything else, such as a numeric `access_token`, is dropped."""
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, count in value.items():
        key = str(key)
        if isinstance(count, dict) and not credential_field(key):
            nested = usage_counters(count)                 # per-invocation maps: {"task_1_..._builder@run.1": {...}}
            if nested:
                out[key] = nested
        elif ((_usage_counter(key, count) or key == "num_turns") and isinstance(count, (int, float))
              and not isinstance(count, bool) and count >= 0):
            out[key] = count
    return out


def _usage_counter(name: str, value) -> bool:
    """Provider usage counters (`input_tokens`, `cache_read_input_tokens`, ...): numbers whose name ends in "tokens"."""
    words = _words(name)
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and bool(words) and words[-1] == "tokens"
            and "token" not in words)


def _hide_all(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, dict):
        return {k: _hide_all(item) for k, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hide_all(item) for item in value]
    return REDACTED if value != "" else value


# Structured fields that map repository paths to content fingerprints (ownership baselines, ready snapshots, trees).
PATH_MAPS = frozenset({"snapshot", "baseline", "changes", "uncommitted"})
# The shapes gittools._fingerprint produces: "<mode>:<sha256>", "absent", "unreadable", "special:<mode>",
# "changing:<ns>", or "symlink:<target>" (whose target text is still redacted).
_FINGERPRINT = re.compile(r"\d{1,7}:[0-9a-f]{64}|absent|unreadable|special:\d{1,7}|changing:\d{1,25}")


def _fingerprint_value(value):
    if isinstance(value, str):
        if _FINGERPRINT.fullmatch(value):
            return value
        if value.startswith("symlink:"):
            return redact(value)
    return REDACTED


def redact_deep(value):
    """redact() applied to every string inside JSON-like data. Dict keys are names and are kept; a value under a key that
    names a credential is hidden entirely, at any depth."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if str(key) in PATH_MAPS and isinstance(item, dict):
                # A file-fingerprint map: its keys are repository paths, never field names, so they are kept as they
                # are (a file called `tokenizer` keeps its fingerprint). Only values shaped like the fingerprints Brahma
                # computes are kept; anything else under such a name (worker-supplied JSON) is hidden.
                out[key] = {path: _fingerprint_value(fingerprint) for path, fingerprint in item.items()}
            elif credential_field(str(key)) and not _usage_counter(str(key), item):
                out[key] = _hide_all(item)
            else:
                out[key] = redact_deep(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_deep(item) for item in value]
    return value


def read_regular(path, limit: int, *, follow: bool = True) -> bytes:
    """Read at most `limit` bytes of a regular file without ever blocking. The file type is checked on the opened
    descriptor (O_NONBLOCK), so a FIFO or device placed at the path, even after an earlier check, is refused instead of
    hanging the coordinator. Raises OSError for anything else and ValueError when the file is larger than `limit`."""
    import os
    import stat
    flags = os.O_RDONLY | os.O_NONBLOCK | (0 if follow else getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"Not a regular file: {path}")
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"File exceeds its {limit}-byte limit: {path}")
    return data


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
    readonly = role in ("planner", "reviewer", "reply")
    if provider == "codex":
        confinement = "none (read-only sandbox)" if readonly else "Codex workspace-write sandbox (not a verified path boundary; changes are verified after the run)"
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
