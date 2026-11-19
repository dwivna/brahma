"""Gitignore-style file ownership patterns (Round 13).

Rules, applied identically to approval, overlap checks and post-work verification:

* ``*`` and ``?`` and ``[...]`` never cross a ``/``.
* ``**`` as a whole path segment crosses directories: in the middle it matches zero or more
  directories (``a/**/b`` matches ``a/b``); as the last segment it matches one or more
  entries (``src/**`` matches everything under ``src``).
* A trailing ``/`` or a pattern with no wildcard owns that path and its whole subtree,
  so ``src``, ``src/`` and ``src/**`` all own ``src/a/b.py``.
* In a class, ``!`` or ``^`` negates and a ``]`` right after ``[`` / ``[!`` is literal; a ``[``
  with no closing ``]`` in its segment is an ordinary character.
* Matching is case-sensitive and anchored to the repository root.

Patterns come from untrusted planner output and are matched against worker-controlled
file names, so matching uses no regular expressions: each segment is matched with a
linear-pointer glob and segments with memoised dynamic programming, bounded by the
limits in :func:`validate`.
"""
from __future__ import annotations

import functools

MAX_PATTERN_CHARS = 200
MAX_PATTERN_SEGMENTS = 16

# Token kinds for one path segment.
_LIT, _ANY, _ONE, _SET = range(4)


@functools.lru_cache(maxsize=2048)
def _tokens(segment: str) -> tuple:
    tokens, i = [], 0
    while i < len(segment):
        char = segment[i]
        if char == "*":
            if not tokens or tokens[-1][0] != _ANY:  # `**` inside a segment is just `*`
                tokens.append((_ANY,))
            i += 1
        elif char == "?":
            tokens.append((_ONE,))
            i += 1
        elif char == "[":
            j = i + 1
            negate = j < len(segment) and segment[j] in "!^"
            if negate:
                j += 1
            start = j
            if j < len(segment) and segment[j] == "]":
                j += 1  # a first `]` is a literal member
            end = segment.find("]", j)
            if end == -1:
                tokens.append((_LIT, "["))
                i += 1
            else:
                body = segment[start:end]
                members, k = [], 0
                while k < len(body):
                    if k + 2 < len(body) and body[k + 1] == "-":
                        members.append((body[k], body[k + 2]))
                        k += 3
                    else:
                        members.append((body[k], body[k]))
                        k += 1
                tokens.append((_SET, negate, tuple(members)))
                i = end + 1
        else:
            tokens.append((_LIT, char))
            i += 1
    return tuple(tokens)


def _is_literal_segment(segment: str) -> bool:
    return all(token[0] == _LIT for token in _tokens(segment))


def _token_matches(token: tuple, char: str) -> bool:
    if token[0] == _LIT:
        return token[1] == char
    if token[0] == _ONE:
        return True
    inside = any(low <= char <= high for low, high in token[2])
    return inside != token[1]


@functools.lru_cache(maxsize=8192)
def _segment_matches(segment: str, text: str) -> bool:
    """Glob match of one path segment (two-pointer with star backtracking)."""
    tokens = _tokens(segment)
    t = s = 0
    star = mark = -1
    while s < len(text):
        if t < len(tokens) and tokens[t][0] == _ANY:
            star, mark = t, s
            t += 1
        elif t < len(tokens) and _token_matches(tokens[t], text[s]):
            t += 1
            s += 1
        elif star >= 0:
            mark += 1
            t, s = star + 1, mark
        else:
            return False
    while t < len(tokens) and tokens[t][0] == _ANY:
        t += 1
    return t == len(tokens)


def _segments(pattern: str) -> list[str]:
    return pattern.rstrip("/").split("/")


def is_subtree(pattern: str) -> bool:
    """A trailing slash, or a pattern with no wildcard at all, owns the whole subtree."""
    return pattern.endswith("/") or all(_is_literal_segment(part) for part in _segments(pattern))


def validate(pattern: str) -> None:
    """Reject patterns that cannot be a repository-relative ownership entry."""
    if (not isinstance(pattern, str) or not pattern.strip() or pattern.startswith(("/", "~"))
            or "\\" in pattern or "\x00" in pattern or ":" in pattern or pattern.strip("/") == ""
            or "//" in pattern):
        raise ValueError("File ownership must use repository-relative paths")
    if len(pattern) > MAX_PATTERN_CHARS or len(_segments(pattern)) > MAX_PATTERN_SEGMENTS:
        raise ValueError("File ownership pattern is too long or too deep")
    segments = _segments(pattern)
    if any(part in ("", ".", "..") for part in segments) or segments[0] == ".git":
        raise ValueError("File ownership must use repository-relative paths")


def owns(pattern: str, path: str) -> bool:
    pattern_segments, path_segments = _segments(pattern), path.split("/")
    if not path or any(part == "" for part in path_segments):
        return False
    subtree = is_subtree(pattern)
    last = len(pattern_segments) - 1
    memo: dict[tuple[int, int], bool] = {}

    def match(i: int, j: int) -> bool:
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(pattern_segments):
            result = j == len(path_segments) or (subtree and j < len(path_segments))
        elif pattern_segments[i] == "**":
            if i == last:
                result = j < len(path_segments)  # trailing `**` needs at least one more entry
            else:
                result = any(match(i + 1, k) for k in range(j, len(path_segments) + 1))
        elif j == len(path_segments):
            result = False
        else:
            result = _segment_matches(pattern_segments[i], path_segments[j]) and match(i + 1, j + 1)
        memo[key] = result
        return result

    return match(0, 0)


def _segments_can_coincide(left: str, right: str) -> bool | None:
    """Whether two single segments can name the same entry; None when both contain wildcards."""
    left_literal, right_literal = _is_literal_segment(left), _is_literal_segment(right)
    if left_literal and right_literal:
        return left == right
    if not left_literal and not right_literal:
        return None
    wild, literal = (right, left) if left_literal else (left, right)
    return _segment_matches(wild, literal)


def overlaps(left: str, right: str) -> bool:
    """Conservative intersection test: True unless the two patterns are provably disjoint."""
    if left == right:
        return True
    left_segments, right_segments = _segments(left), _segments(right)
    left_tree, right_tree = is_subtree(left), is_subtree(right)
    for a, b in zip(left_segments, right_segments):
        if a == "**" or b == "**":
            return True
        if _segments_can_coincide(a, b) is False:
            return False
    shared = min(len(left_segments), len(right_segments))
    left_plain = all(_is_literal_segment(part) for part in left_segments)
    right_plain = all(_is_literal_segment(part) for part in right_segments)
    if left_tree and left_plain:
        # A directory owns everything below it: any longer or equal-depth match inside overlaps.
        return len(right_segments) >= len(left_segments) or right_tree and shared == len(right_segments)
    if right_tree and right_plain:
        return len(left_segments) >= len(right_segments) or left_tree and shared == len(left_segments)
    has_deep = "**" in left_segments or "**" in right_segments
    if not has_deep and len(left_segments) != len(right_segments) and not (left_tree or right_tree):
        return False
    return True
