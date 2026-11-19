from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
