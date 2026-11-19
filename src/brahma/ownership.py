"""Gitignore-style file ownership patterns (Round 13).

Rules, applied identically to approval, overlap checks and post-work verification:

* ``*`` and ``?`` and ``[...]`` never cross a ``/``.
* ``**`` as a whole path segment crosses directories (``**/x`` also matches ``x``).
* A trailing ``/`` or a pattern with no wildcard owns that path and its whole subtree,
  so ``src``, ``src/`` and ``src/**`` all own ``src/a/b.py``.
* Matching is case-sensitive and anchored to the repository root.
"""
from __future__ import annotations

import functools
import re

WILDCARD = re.compile(r"[*?\[]")


def _double_star(pattern: str, i: int) -> bool:
    """``**`` is special only as an entire segment."""
    if not pattern.startswith("**", i):
        return False
    before = i == 0 or pattern[i - 1] == "/"
    after = i + 2 == len(pattern) or pattern[i + 2] == "/"
    return before and after


def _translate(pattern: str) -> str:
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if _double_star(pattern, i):
            if pattern.startswith("**/", i):
                out.append("(?:[^/]+/)*")
                i += 3
            else:
                out.append(".*")
                i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        elif char == "[":
            end = pattern.find("]", i + 2)
            if end == -1:
                out.append(re.escape(char))
                i += 1
            else:
                body = pattern[i + 1:end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("(?!/)[" + body.replace("\\", "\\\\") + "]")
                i = end + 1
        else:
            out.append(re.escape(char))
            i += 1
    return "".join(out)


def is_subtree(pattern: str) -> bool:
    return pattern.endswith("/") or not WILDCARD.search(pattern)


@functools.lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern:
    body = _translate(pattern.rstrip("/"))
    if is_subtree(pattern):
        body += "(?:/.*)?"
    return re.compile(body + r"\Z", re.S)


def validate(pattern: str) -> None:
    """Reject patterns that cannot be a repository-relative ownership entry."""
    if (not isinstance(pattern, str) or not pattern.strip() or pattern.startswith(("/", "~"))
            or "\\" in pattern or "\x00" in pattern or ":" in pattern or pattern.strip("/") == ""
            or "//" in pattern):
        raise ValueError("File ownership must use repository-relative paths")
    segments = pattern.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in segments) or segments[0] == ".git":
        raise ValueError("File ownership must use repository-relative paths")


def owns(pattern: str, path: str) -> bool:
    return bool(_compile(pattern).match(path))


def _segment_match(left: str, right: str) -> bool | None:
    """Whether two single segments can name the same entry; None if unknown (both wild)."""
    left_wild, right_wild = bool(WILDCARD.search(left)), bool(WILDCARD.search(right))
    if not left_wild and not right_wild:
        return left == right
    if left_wild and right_wild:
        return None
    literal, wild = (right, left) if left_wild else (left, right)
    return bool(re.fullmatch(_translate(wild), literal))


def overlaps(left: str, right: str) -> bool:
    """Conservative intersection test: True unless the two patterns are provably disjoint."""
    if left == right:
        return True
    left_segments, right_segments = left.rstrip("/").split("/"), right.rstrip("/").split("/")
    left_tree, right_tree = is_subtree(left), is_subtree(right)
    for a, b in zip(left_segments, right_segments):
        if a == "**" or b == "**":
            return True
        if _segment_match(a, b) is False:
            return False
    shared = min(len(left_segments), len(right_segments))
    if left_tree and not WILDCARD.search(left):
        # A directory owns everything below it: any longer or equal-depth match inside overlaps.
        return len(right_segments) >= len(left_segments) or right_tree and shared == len(right_segments)
    if right_tree and not WILDCARD.search(right):
        return len(left_segments) >= len(right_segments) or left_tree and shared == len(left_segments)
    has_deep = "**" in left_segments or "**" in right_segments
    if not has_deep and len(left_segments) != len(right_segments) and not (left_tree or right_tree):
        return False
    return True
