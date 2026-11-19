from __future__ import annotations

import itertools
import random
import time
import unittest

from brahma import ownership
from brahma.models import Plan


class Owns(unittest.TestCase):
    def test_single_star_does_not_cross_directories(self):
        # Round 13: `src/*.py` must not own nested files (it did with fnmatch).
        self.assertTrue(ownership.owns("src/*.py", "src/a.py"))
        self.assertFalse(ownership.owns("src/*.py", "src/a/b.py"))
        self.assertFalse(ownership.owns("*.py", "src/a.py"))
        self.assertFalse(ownership.owns("src/?.py", "src/a/b.py"))

    def test_double_star_crosses_directories_and_may_match_zero(self):
        self.assertTrue(ownership.owns("src/**/*.py", "src/a.py"))
        self.assertTrue(ownership.owns("src/**/*.py", "src/a/b/c.py"))
        self.assertTrue(ownership.owns("**/*.py", "x.py"))
        self.assertTrue(ownership.owns("**/*.py", "a/b/x.py"))
        self.assertFalse(ownership.owns("src/**/*.py", "tests/a.py"))
        self.assertTrue(ownership.owns("src/**", "src/a/b.py"))
        # `**` that is not a whole segment behaves like a single star.
        self.assertFalse(ownership.owns("a**b", "a/b"))

    def test_bare_directory_and_trailing_slash_own_the_subtree(self):
        for pattern in ("src", "src/", "src/**"):
            with self.subTest(pattern=pattern):
                self.assertTrue(ownership.owns(pattern, "src/a/b.py"))
                self.assertTrue(ownership.owns(pattern, "src/a.py"))
                self.assertFalse(ownership.owns(pattern, "srcx/a.py"))
                self.assertFalse(ownership.owns(pattern, "other/src/a.py"))

    def test_exact_file_is_only_that_file(self):
        self.assertTrue(ownership.owns("README.md", "README.md"))
        self.assertFalse(ownership.owns("README.md", "docs/README.md"))
        self.assertFalse(ownership.owns("README.md", "README.md.bak"))

    def test_character_classes_stay_inside_a_segment(self):
        self.assertTrue(ownership.owns("t[0-9].py", "t5.py"))
        self.assertFalse(ownership.owns("t[0-9].py", "ta.py"))
        self.assertFalse(ownership.owns("a[!x]b", "a/b"))
        self.assertTrue(ownership.owns("a[!x]b", "ayb"))

    def test_matching_is_case_sensitive(self):
        self.assertFalse(ownership.owns("src/A.py", "src/a.py"))


class Validate(unittest.TestCase):
    def test_rejects_unsafe_or_malformed_patterns(self):
        for bad in ("", " ", "/etc/x", "~/x", "../x", "a/../b", "a\\b", "a:b", "//", "/", ".git",
                    ".git/config", "a//b", "./a", "a/./b", "x\x00y"):
            with self.subTest(pattern=bad), self.assertRaises(ValueError):
                ownership.validate(bad)

    def test_accepts_directory_forms_including_trailing_slash(self):
        for good in ("src", "src/", "src/**", "src/*.py", "tests/test_*.py", "docs/a b.md", ".github/x.yml"):
            with self.subTest(pattern=good):
                ownership.validate(good)


class Overlaps(unittest.TestCase):
    def test_provably_disjoint(self):
        for left, right in (("src/a.py", "src/b.py"), ("tests/", "src/"), ("src/*.py", "src/sub/y.py"),
                            ("src/*.py", "src/readme.md"), ("a/b", "c/*")):
            with self.subTest(left=left, right=right):
                self.assertFalse(ownership.overlaps(left, right))
                self.assertFalse(ownership.overlaps(right, left))

    def test_overlapping_fails_closed(self):
        for left, right in (("src", "src/a/b.py"), ("src/", "src/x.py"), ("src/**", "src/a.py"),
                            ("src/*.py", "src/x.py"), ("a/b", "a/*"), ("src/a", "src/a/b"),
                            ("**/*.py", "src/x.py"), ("src/*.py", "src/*.md"), ("x.py", "x.py")):
            with self.subTest(left=left, right=right):
                self.assertTrue(ownership.overlaps(left, right))
                self.assertTrue(ownership.overlaps(right, left))

    def test_bare_directory_now_conflicts_with_its_children(self):
        # Previously `src` owned nothing; two tasks owning `src` and `src/a.py` must now conflict.
        from brahma.contracts import validate_ownership
        plan = Plan("g", [])
        from brahma.models import Task
        plan.tasks = [Task("One", "i", ["src"], ["ok"]), Task("Two", "i", ["src/a.py"], ["ok"])]
        with self.assertRaises(ValueError):
            validate_ownership(plan)


class Hostile(unittest.TestCase):
    """Planner patterns are untrusted and worker file names are attacker-shaped (independent review H1/H2)."""

    def test_pathological_patterns_are_bounded_in_time(self):
        cases = (("*a" * 30 + "b", "a" * 60), ("a*" * 50 + "b", "a" * 80), ("**/" * 30 + "x", "a/" * 40 + "y"),
                 ("*/**/" * 15 + "x", "a/" * 40), ("**/a/" * 12 + "z", "a/" * 40))
        started = time.monotonic()
        for pattern, path in cases:
            with self.subTest(pattern=pattern[:20]):
                ownership.owns(pattern, path.rstrip("/"))
                ownership.overlaps(pattern, path.rstrip("/"))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_validate_caps_length_and_depth(self):
        for bad in ("a" * 201, "/".join("a" * 17), "a/" * 20 + "b"):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                ownership.validate(bad)
        ownership.validate("a/" * 15 + "b")

    def test_malformed_brackets_never_raise_and_stay_in_their_segment(self):
        for pattern in ("[!]a]", "[]a]", "a[b", "[", "[]", "[!", "a[b/[^a]", "x[a-]", "[a-z", "[[]"):
            with self.subTest(pattern=pattern):
                ownership.validate(pattern) if pattern.strip("/") else None
                for path in ("a", "ab", "]", "a]", "a/b", "[", "x-"):
                    ownership.owns(pattern, path)
                    ownership.overlaps(pattern, path)
        self.assertTrue(ownership.owns("[!]a]", "b"))        # not `]` and not `a`
        self.assertFalse(ownership.owns("[!]a]", "a"))
        self.assertTrue(ownership.owns("[]a]", "]"))          # a leading `]` is a literal member
        self.assertTrue(ownership.owns("a[b", "a[b"))         # unterminated `[` is an ordinary character
        # A stray `[` must not pair with a `]` in a later segment and swallow the `/`.
        self.assertFalse(ownership.owns("a[b/[^a]", "ab"))
        self.assertTrue(ownership.owns("a[b/[^a]", "a[b/c"))
        self.assertFalse(ownership.overlaps("ab/", "a[b/[^a]"))  # provably disjoint now: `a[b` is literal

    def test_overlaps_is_sound_against_brute_force(self):
        """If any path is owned by both patterns, overlaps() must say True (seeded, deterministic)."""
        rng = random.Random(1307)
        atoms = ["a", "b", "*", "?", "**", "[ab]", "[!a]", "a*", "*b", "[", "]", "[!]a]"]
        names = ["a", "b", "ab", "ba", "[", "]", "a]", "c"]
        paths = [ "/".join(p) for depth in (1, 2, 3) for p in itertools.product(names, repeat=depth)]
        def pattern():
            parts = [rng.choice(atoms) for _ in range(rng.randint(1, 3))]
            text = "/".join(parts)
            return text + "/" if rng.random() < 0.15 else text
        unsound = []
        for _ in range(250):
            left, right = pattern(), pattern()
            try:
                ownership.validate(left), ownership.validate(right)
            except ValueError:
                continue
            both = any(ownership.owns(left, path) and ownership.owns(right, path) for path in paths)
            if both and not ownership.overlaps(left, right):
                unsound.append((left, right))
        self.assertEqual(unsound, [])


if __name__ == "__main__":
    unittest.main()
