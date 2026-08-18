"""Answer-set and SHA-bucket tests.

Ported from askastro's `bin/changemap_test.py`. The `_real_config()` cases now
read `fixtures/synthetic.toml` instead of another repo's live config.
"""

import contextlib
import os
import unittest
import unittest.mock
from typing import Any

from tangier import changemap, git
from tangier.config import read_config
from tangier.tests.support import make_config, make_git_repo

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def synthetic_config() -> Any:
    import io

    with contextlib.redirect_stderr(io.StringIO()):
        return read_config(os.path.join(FIXTURES, "synthetic.toml"))


class TestExpandWithDependents(unittest.TestCase):
    # SPEC: changemap#reverse-transitive-expansion
    # SPEC: changemap#global-as-dependency
    def test_reverse_transitive_closure(self) -> None:
        # Transitive chain + diamond in one. `global` is just a leaf depended on.
        depends = {
            "orgs": ["auth", "global"],
            "agent": ["orgs"],
            "threads": ["agent"],
            "dashboards": ["agent"],
        }
        # leaf input -> just the leaf
        self.assertEqual(changemap.expand_with_dependents({"threads"}, depends), {"threads"})
        # middle input -> forward chain
        self.assertEqual(changemap.expand_with_dependents({"agent"}, depends), {"agent", "threads", "dashboards"})
        # bottom input -> everything
        self.assertEqual(
            changemap.expand_with_dependents({"global"}, depends),
            {"global", "orgs", "agent", "threads", "dashboards"},
        )


class TestShaBucketWalk(unittest.TestCase):
    # SPEC: changemap#sha-bucket-walk
    def test_bucket_walk_unions_members_and_transitive_deps(self) -> None:
        cfg = make_config(
            shared={"paths": ["lib/**"], "unittest_items": ["lib"]},
            core={"paths": ["c/**"], "sha": "svc", "depends": ["shared"]},
            extras={"paths": ["e/**"], "sha": "svc"},
        )
        # The bucket has both members; walking from core picks up shared via deps.
        self.assertEqual(set(changemap.buckets(cfg)["svc"]), {"core", "extras"})
        self.assertEqual(changemap.transitive_deps("core", cfg.depends), {"shared"})


class TestShaBucketExclude(unittest.TestCase):
    """SHA hashing over a throwaway git repo, exercising the real `git ls-tree` walk.

    This is the test that protects the hashing itself — everything else fakes
    the diff, but these two run git.
    """

    def _sha(self, cfg: Any, bucket: str, root: str) -> str:
        # sha_for_bucket shells out to `git ls-tree`, which resolves against CWD.
        with contextlib.chdir(root):
            return changemap.sha_for_bucket(cfg, bucket, "HEAD")

    # SPEC: changemap#exclude-applies-to-sha
    def test_exclude_applies_to_bucket_contents(self) -> None:
        cfg = make_config(svc={"paths": ["f/**"], "exclude": ["f/gen.ts"], "sha": True})
        before = self._sha(cfg, "svc", make_git_repo(self, {"f/hand.ts": "a", "f/gen.ts": "one"}))
        after = self._sha(cfg, "svc", make_git_repo(self, {"f/hand.ts": "a", "f/gen.ts": "TWO"}))
        # A change confined to the excluded path leaves the bucket SHA untouched.
        self.assertEqual(before, after)

    # SPEC: changemap#exclude-applies-to-sha
    def test_bucket_keeps_path_another_member_claims(self) -> None:
        # Exclusion is per-tag, so a path a sibling member still claims stays in
        # the hash — otherwise excluding it anywhere would stop the image rebuilding.
        # `image` is a reserved section name, so the tag is declared nested.
        cfg = make_config(
            shared={"paths": ["f/**"], "exclude": ["f/gen.ts"], "sha": "svc"},
            **{"tags.image": {"paths": ["f/**"], "sha": "svc"}},
        )
        before = self._sha(cfg, "svc", make_git_repo(self, {"f/gen.ts": "one"}))
        after = self._sha(cfg, "svc", make_git_repo(self, {"f/gen.ts": "TWO"}))
        self.assertNotEqual(before, after)

    # SPEC: changemap#sha-exclude
    def test_readme_change_does_not_move_the_sha(self) -> None:
        cfg = make_config(svc={"paths": ["f/**"], "sha": True})
        before = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "f/README.md": "one"}))
        after = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "f/README.md": "TWO"}))
        self.assertEqual(before, after)

    # SPEC: changemap#sha-exclude
    def test_root_readme_is_excluded_too(self) -> None:
        # `**/README.md` matches the empty prefix, so a root-level README is
        # excluded as well as nested ones.
        cfg = make_config(svc={"paths": ["**"], "sha": True})
        before = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "README.md": "one"}))
        after = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "README.md": "TWO"}))
        self.assertEqual(before, after)

    # SPEC: changemap#sha-exclude
    def test_empty_exclude_lets_readme_changes_move_the_sha(self) -> None:
        cfg = make_config(
            **{"sha": {"exclude": []}},
            svc={"paths": ["f/**"], "sha": True},
        )
        before = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "f/README.md": "one"}))
        after = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "f/README.md": "TWO"}))
        self.assertNotEqual(before, after)

    def test_custom_exclude_is_honoured(self) -> None:
        cfg = make_config(
            **{"sha": {"exclude": ["**/*.md"]}},
            svc={"paths": ["f/**"], "sha": True},
        )
        before = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "f/notes.md": "one"}))
        after = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "f/notes.md": "TWO"}))
        self.assertEqual(before, after)

    def test_sha_exclude_and_tag_exclude_compose(self) -> None:
        # Both filters apply at once: the README via [sha] exclude, the
        # generated file via the tag's own exclude.
        cfg = make_config(svc={"paths": ["f/**"], "exclude": ["f/gen.ts"], "sha": True})
        before = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "f/gen.ts": "1", "f/README.md": "1"}))
        after = self._sha(cfg, "svc", make_git_repo(self, {"f/a.ts": "a", "f/gen.ts": "2", "f/README.md": "2"}))
        self.assertEqual(before, after)


class TestAnswerSet(unittest.TestCase):
    def _answers(self, cfg: Any, files: list[str], expand: bool = True) -> Any:
        with unittest.mock.patch.object(git, "changed_files", return_value=files):
            return changemap.compute_answer_set(cfg, "base", "head", expand=expand, compute_shas=False)

    # SPEC: changemap#unmapped-paths-silently-ignored
    # SPEC: changemap#items-field-projection
    # SPEC: changemap#touched-opt-in
    def test_end_to_end(self) -> None:
        cfg = make_config(
            auth={"paths": ["a/**"], "unittest_items": "py/auth", "e2e_items": "e2e/auth", "touched": True},
            orgs={"paths": ["o/**"], "depends": ["auth"], "unittest_items": "py/orgs", "touched": True},
            untouched={"paths": ["u/**"], "touched": True},
            **{"unittest-files": {"files": True, "paths": ["a/**/*_test.py"]}},
        )
        answers = self._answers(cfg, ["a/x.py", "a/foo_test.py", "random/y.py"])
        self.assertEqual(answers.matched, {"auth"})
        self.assertEqual(answers.expanded, {"auth", "orgs"})  # orgs pulled in via depends
        self.assertEqual(answers.ignored, ["random/y.py"])
        self.assertEqual(answers.items["unittest"], ["py/auth", "py/orgs"])
        self.assertEqual(answers.items["e2e"], ["e2e/auth"])  # orgs has no e2e_items
        self.assertEqual(answers.touched, {"auth": True, "orgs": True, "untouched": False})
        # File-set tables are projection-only: never matched/expanded/touched/ignored.
        self.assertNotIn("unittest-files", answers.matched)
        self.assertNotIn("unittest-files", answers.expanded)
        self.assertNotIn("unittest-files", answers.touched)
        self.assertNotIn("a/foo_test.py", answers.ignored)
        self.assertEqual(answers.file_sets["unittest-files"], ["a/foo_test.py"])

    # SPEC: changemap#exclude-subtracts-from-paths
    def test_exclude_removes_path_from_tag(self) -> None:
        cfg = make_config(shared={"paths": ["f/**"], "exclude": ["f/gen.ts"], "e2e_items": "e2e/shared"})
        answers = self._answers(cfg, ["f/gen.ts"])
        self.assertEqual(answers.matched, set())
        self.assertEqual(answers.items["e2e"], [])
        # Excluded from every tag -> falls through to ignore-by-default.
        self.assertEqual(answers.ignored, ["f/gen.ts"])

    # SPEC: changemap#exclude-is-per-tag
    def test_exclude_is_per_tag(self) -> None:
        # The property that keeps the frontend image building: one tag excludes
        # the generated file, the tag that ships it still matches.
        cfg = make_config(
            shared={"paths": ["f/**"], "exclude": ["f/gen.ts"], "unittest_items": "x"},
            **{"tags.image": {"paths": ["f/**"], "sha": True, "touched": True}},
        )
        answers = self._answers(cfg, ["f/gen.ts"])
        self.assertEqual(answers.matched, {"image"})
        self.assertEqual(answers.touched, {"image": True})

    def test_excluded_path_still_matches_other_globs_of_same_tag(self) -> None:
        # Exclusion is per-file, not per-tag-wide: a hand-written sibling in the
        # same diff still matches.
        cfg = make_config(shared={"paths": ["f/**"], "exclude": ["f/gen.ts"], "unittest_items": "x"})
        answers = self._answers(cfg, ["f/gen.ts", "f/hand.ts"])
        self.assertEqual(answers.matched, {"shared"})

    # SPEC: changemap#no-expand-debug-flag
    def test_no_expand_returns_raw_matched(self) -> None:
        cfg = make_config(
            auth={"paths": ["a/**"], "unittest_items": "x"},
            orgs={"paths": ["o/**"], "depends": ["auth"], "unittest_items": "y"},
        )
        answers = self._answers(cfg, ["a/x.py"], expand=False)
        self.assertEqual(answers.expanded, {"auth"})

    def test_eval_items_projection_for_changed_eval_subsystem(self) -> None:
        cfg = make_config(
            agent={"paths": ["agent/**"], "eval_items": "py/agent"},
            notes={"paths": ["notes/**"], "unittest_items": "py/notes"},
        )
        answers = self._answers(cfg, ["agent/foo.py"])
        self.assertEqual(answers.items.get("eval", []), ["py/agent"])

    def test_eval_items_empty_when_only_non_eval_subsystem_changes(self) -> None:
        cfg = make_config(
            agent={"paths": ["agent/**"], "eval_items": "py/agent"},
            notes={"paths": ["notes/**"], "unittest_items": "py/notes"},
        )
        answers = self._answers(cfg, ["notes/foo.py"])
        self.assertEqual(answers.items.get("eval", []), [])

    def test_framework_change_expands_to_every_eval_bearing_subsystem(self) -> None:
        # A change to a leaf that every eval-bearing subsystem depends on pulls
        # in all of their eval_items via reverse-transitive expansion.
        cfg = make_config(
            framework={"paths": ["framework/**"], "eval_items": "py/framework_apps"},
            agent={"paths": ["agent/**"], "depends": ["framework"], "eval_items": "py/agent"},
            playbooks={"paths": ["playbooks/**"], "depends": ["framework"], "eval_items": "py/playbooks"},
        )
        answers = self._answers(cfg, ["framework/x.py"])
        self.assertEqual(
            set(answers.items.get("eval", [])),
            {"py/framework_apps", "py/agent", "py/playbooks"},
        )

    def test_eval_files_collected_from_diff(self) -> None:
        cfg = make_config(
            agent={"paths": ["agent/**"], "eval_items": "py/agent"},
            **{
                "unittest-files": {"files": True, "paths": ["agent/**/*_test.py"]},
                "eval-files": {"files": True, "paths": ["agent/**/*_eval.py"]},
            },
        )
        answers = self._answers(cfg, ["agent/foo.py", "agent/bar_eval.py", "agent/baz_test.py"])
        self.assertEqual(answers.file_sets["eval-files"], ["agent/bar_eval.py"])
        self.assertEqual(answers.file_sets["unittest-files"], ["agent/baz_test.py"])

    def test_test_and_eval_files_outside_globs_are_dropped(self) -> None:
        # File-set selection is glob-scoped: test/eval files outside the table's
        # globs silently drop out — the same ignore-by-default contract as
        # unmapped non-test paths.
        cfg = make_config(
            agent={"paths": ["agent/**"], "unittest_items": "py/agent"},
            **{"unittest-files": {"files": True, "paths": ["agent/**/*_test.py"]}},
        )
        answers = self._answers(cfg, ["agent/foo_test.py", "bin/tool_test.py", "scripts/x_eval.py"])
        self.assertEqual(answers.file_sets["unittest-files"], ["agent/foo_test.py"])
        self.assertEqual(answers.file_sets.get("eval-files", []), [])

    # SPEC: changemap#file-set-projection
    def test_file_set_table_not_in_matched_expanded_touched_ignored(self) -> None:
        cfg = make_config(
            agent={"paths": ["agent/**"], "unittest_items": "py/agent", "touched": True},
            **{"unittest-files": {"files": True, "paths": ["agent/**/*_test.py"]}},
        )
        answers = self._answers(cfg, ["agent/foo_test.py"])
        self.assertNotIn("unittest-files", answers.matched)
        self.assertNotIn("unittest-files", answers.expanded)
        self.assertNotIn("unittest-files", answers.touched)
        self.assertEqual(answers.ignored, [])

    def test_answer_set_has_no_test_files_alias(self) -> None:
        # `test_files`/`eval_files` were aliases of `file_sets[...]` read only by
        # tests. Callers use `file_sets` directly.
        cfg = make_config(agent={"paths": ["a/**"], "unittest_items": "x"})
        answers = self._answers(cfg, ["a/x.py"])
        self.assertFalse(hasattr(answers, "test_files"))
        self.assertFalse(hasattr(answers, "eval_files"))


class TestSyntheticConfig(unittest.TestCase):
    """The four regressions a real monorepo's config records as having broken CI.

    Shapes preserved, names generic — see fixtures/synthetic.toml.
    """

    def _answers(self, files: list[str]) -> Any:
        with unittest.mock.patch.object(git, "changed_files", return_value=files):
            return changemap.compute_answer_set(synthetic_config(), "base", "head", compute_shas=False)

    # SPEC: changemap#exclude-is-per-tag
    def test_generated_api_client_alone_runs_no_e2e(self) -> None:
        # The generated API client is emitted from backend routes. Counting it as
        # cross-cutting frontend code expanded to EVERY `-ui` suite — a full e2e
        # run with no added signal. It must still build the webapp image.
        answers = self._answers(["frontend/src/api/api.generated.ts"])
        self.assertEqual(answers.items["e2e"], [])
        self.assertNotIn("frontend-shared", answers.expanded)
        self.assertIn("webapp", answers.expanded)
        self.assertTrue(answers.touched["webapp"])

    # SPEC: changemap#exclude-subtracts-from-paths
    def test_handwritten_shared_frontend_still_runs_e2e(self) -> None:
        # The exclusion is scoped to the one generated file: hand-written
        # cross-cutting frontend code still expands to the e2e suites.
        answers = self._answers(["frontend/src/app/routes.tsx"])
        self.assertIn("frontend-shared", answers.matched)
        self.assertNotEqual(answers.items["e2e"], [])

    # SPEC: changemap#file-set-projection
    def test_dedicated_job_test_file_excluded_from_unittest_files(self) -> None:
        # A renderer test must NOT land in `unittest-files` — it runs in the
        # dedicated renderer job, not the shared backend one.
        answers = self._answers(["service/renderer/renderer/tasks_test.py"])
        self.assertEqual(answers.file_sets["unittest-files"], [])

    # SPEC: changemap#file-set-projection
    def test_service_test_file_included_in_unittest_files(self) -> None:
        answers = self._answers(["service/scanner/scanner/ocr_test.py"])
        self.assertEqual(answers.file_sets["unittest-files"], ["service/scanner/scanner/ocr_test.py"])

    # SPEC: changemap#file-set-projection
    def test_shared_lib_test_file_included_in_unittest_files(self) -> None:
        answers = self._answers(["lib/shared/shared/dates_test.py"])
        self.assertEqual(answers.file_sets["unittest-files"], ["lib/shared/shared/dates_test.py"])

    # SPEC: changemap#file-set-projection
    def test_eval_files_only_from_eval_bearing_trees(self) -> None:
        answers = self._answers(
            [
                "service/core/core/agent/x_eval.py",
                "lib/shared/shared/y_eval.py",
                "service/scanner/scanner/z_eval.py",
                "service/renderer/renderer/w_eval.py",
            ]
        )
        self.assertEqual(
            answers.file_sets["eval-files"],
            ["service/core/core/agent/x_eval.py", "lib/shared/shared/y_eval.py"],
        )

    # SPEC: changemap#file-set-projection
    def test_service_source_change_projects_unittest_items(self) -> None:
        # A source change routes the service's suite into the shared job via
        # `--dirs` (unittest_items), independent of file-set projection.
        answers = self._answers(["service/scanner/scanner/ocr.py"])
        self.assertIn("service/scanner", answers.items["unittest"])


if __name__ == "__main__":
    _ = unittest.main()
