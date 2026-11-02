"""Glob engine tests. SHA parity depends on these semantics exactly."""

import unittest

from tangier.globs import globs_to_ls_tree_paths, matches


class TestGlobToRegex(unittest.TestCase):
    def test_double_star_crosses_directories(self) -> None:
        self.assertTrue(matches("a/b/c.py", ["a/**"]))
        self.assertTrue(matches("a/b.py", ["a/**"]))

    def test_double_star_matches_empty(self) -> None:
        # `**/README.md` must exclude a ROOT-level README.md as well as nested
        # ones. `**` -> `.*` matches the empty string, and the separator after
        # `**` is swallowed. This is the case the default [sha] exclude and
        # askastro's `[markdown]` tag both depend on.
        self.assertTrue(matches("README.md", ["**/README.md"]))
        self.assertTrue(matches("docs/README.md", ["**/README.md"]))
        self.assertTrue(matches("a/b/c/README.md", ["**/README.md"]))

    def test_single_star_does_not_cross_directories(self) -> None:
        self.assertTrue(matches("a/b.py", ["a/*.py"]))
        self.assertFalse(matches("a/b/c.py", ["a/*.py"]))

    def test_question_mark_matches_one_non_slash_char(self) -> None:
        self.assertTrue(matches("ab.py", ["a?.py"]))
        self.assertFalse(matches("a/b.py", ["a?b.py"]))

    def test_anchored_at_both_ends(self) -> None:
        self.assertFalse(matches("x/a/b.py", ["a/**"]))
        self.assertFalse(matches("a/b.pyc", ["a/*.py"]))

    def test_regex_metacharacters_are_literal(self) -> None:
        self.assertTrue(matches("a.b+c(d).py", ["a.b+c(d).py"]))
        # A literal `.` must not act as "any character".
        self.assertFalse(matches("axb.py", ["a.b.py"]))

    def test_matches_returns_false_for_empty_globs(self) -> None:
        self.assertFalse(matches("anything", []))

    def test_suffix_glob_matches_any_depth(self) -> None:
        self.assertTrue(matches("service/x/y_test.py", ["service/**/*_test.py"]))
        self.assertTrue(matches("service/y_test.py", ["service/**/*_test.py"]))


class TestGlobsToLsTreePaths(unittest.TestCase):
    # This reduction is what keeps bucket SHAs byte-identical to the legacy
    # `git ls-tree -r HEAD <dir>` pipeline.
    def test_trailing_double_star_becomes_directory(self) -> None:
        self.assertEqual(globs_to_ls_tree_paths(["dir/**"]), ["dir"])

    def test_other_globs_pass_through_verbatim(self) -> None:
        self.assertEqual(
            globs_to_ls_tree_paths(["a/b.py", "src/*.py", "**/*.md"]),
            ["a/b.py", "src/*.py", "**/*.md"],
        )

    def test_only_trailing_double_star_is_stripped(self) -> None:
        self.assertEqual(globs_to_ls_tree_paths(["a/**/b"]), ["a/**/b"])


if __name__ == "__main__":
    _ = unittest.main()
