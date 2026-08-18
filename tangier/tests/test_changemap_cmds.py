"""CLI formatter tests.

Every output shape here is parsed by something downstream, so these pin
formats, not just values.
"""

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
import unittest.mock

from tangier import changemap, cli, git
from tangier.commands import changemap_cmds
from tangier.tests.support import Raw, make_config


def _capture(fn, *args) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _ = fn(*args)
    return buf.getvalue()


class TestBuildMatrix(unittest.TestCase):
    """The JSON array feeding `strategy.matrix`, plus its empty guard."""

    def _cfg(self):
        return make_config(
            **{
                "registry": {"url": "r/ns"},
                "image.astrochat": {"dockerfile": "chat/Dockerfile"},
                "image.smartypants": {"dockerfile": "sp/Dockerfile"},
                "astrochat": {"paths": ["chat/**"], "sha": True},
                "smartypants": {"paths": ["sp/**"], "sha": True},
                # Feeds smartypants' bucket without being a bucket itself —
                # many-to-one, which is why the set is deduped by bucket.
                "warehouse": {"paths": ["wh/**"], "sha": "smartypants"},
                # A bucket with no `[image.*]`: hashed, but not buildable.
                "infra": {"paths": ["infra/**"], "sha": True},
                # Rebuilt only via depends.
                "shared": {"paths": ["shared/**"]},
            }
        )

    def _run(self, changed: list[str], no_expand: bool = False) -> dict[str, str]:
        args = argparse.Namespace(base="base", head="head", no_expand=no_expand)
        with unittest.mock.patch.object(git, "changed_files", return_value=changed):
            out = _capture(changemap_cmds.cmd_build_matrix, self._cfg(), args)
        return dict(line.split("=", 1) for line in out.splitlines())

    def test_touched_buckets_render_as_a_json_array(self) -> None:
        result = self._run(["chat/app.py"])
        self.assertEqual(result["build-packages"], '["astrochat"]')
        self.assertEqual(result["build-packages-empty"], "false")

    def test_several_tags_feeding_one_bucket_build_it_once(self) -> None:
        # `sha_bucket` is many-to-one: a tag-level intersection would try to
        # build smartypants twice.
        result = self._run(["sp/a.py", "wh/b.py"])
        self.assertEqual(result["build-packages"], '["smartypants"]')

    def test_a_bucket_without_an_image_table_is_excluded(self) -> None:
        result = self._run(["infra/main.tf"])
        self.assertEqual(result["build-packages"], "[]")
        self.assertEqual(result["build-packages-empty"], "true")

    def test_no_matching_files_yields_the_empty_guard(self) -> None:
        # The hot path: any docs-only PR. An empty `strategy.matrix` is a hard
        # error in Actions, not a skip.
        result = self._run(["README.md"])
        self.assertEqual(result["build-packages"], "[]")
        self.assertEqual(result["build-packages-empty"], "true")

    def test_the_array_is_sorted_and_valid_json(self) -> None:
        result = self._run(["sp/a.py", "chat/b.py"])
        self.assertEqual(json.loads(result["build-packages"]), ["astrochat", "smartypants"])

    def test_a_bucket_reached_only_through_depends_still_builds(self) -> None:
        # Its content hash has moved, so the image must be rebuilt — the whole
        # premise of content-addressed tagging.
        cfg = make_config(
            **{
                "registry": {"url": "r/ns"},
                "image.astrochat": {"dockerfile": "chat/Dockerfile"},
                "astrochat": {"paths": ["chat/**"], "sha": True, "depends": "shared"},
                "shared": {"paths": ["shared/**"]},
            }
        )
        args = argparse.Namespace(base="base", head="head", no_expand=False)
        with unittest.mock.patch.object(git, "changed_files", return_value=["shared/lib.py"]):
            out = _capture(changemap_cmds.cmd_build_matrix, cfg, args)
        self.assertIn('build-packages=["astrochat"]', out)

    def test_outputs_reach_the_github_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            with unittest.mock.patch.dict(os.environ, {"GITHUB_OUTPUT": path}):
                _ = self._run(["chat/app.py"])
            with open(path) as fh:
                self.assertEqual(
                    fh.read().splitlines(),
                    ['build-packages=["astrochat"]', "build-packages-empty=false"],
                )


class TestGithubOutputs(unittest.TestCase):
    # SPEC: changemap#github-outputs-answer-set
    def test_emits_full_answer_set_to_stdout_and_github_output(self) -> None:
        cfg = make_config(
            warehouse={
                "paths": ["w/**"],
                "unittest_items": "py/wh",
                "e2e_items": "e2e/wh",
                "sha": "smartypants",
                "touched": True,
            },
            other={"paths": ["o/**"], "touched": True},
            **{
                "unittest-files": {"files": True, "paths": ["w/**/*_test.py"]},
                "eval-files": {"files": True, "paths": ["w/**/*_eval.py"]},
            },
        )
        with tempfile.NamedTemporaryFile("w", suffix=".out", delete=False) as fh:
            out_path = fh.name
        try:
            with (
                unittest.mock.patch.object(git, "changed_files", return_value=["w/x.py", "w/y_test.py"]),
                unittest.mock.patch.object(changemap, "sha_for_bucket", return_value="deadbeef00"),
                unittest.mock.patch.dict(os.environ, {"GITHUB_OUTPUT": out_path}),
            ):
                args = argparse.Namespace(base="base", head="head", no_expand=False)
                out = _capture(changemap_cmds.cmd_github_outputs, cfg, args)
            with open(out_path) as fh:
                file_out = fh.read()
        finally:
            os.unlink(out_path)

        # stdout and the GITHUB_OUTPUT file get the same lines.
        self.assertEqual(set(out.splitlines()), set(file_out.splitlines()))
        expected = {
            "smartypants-sha=deadbeef00",
            "unittest-items=py/wh",
            "e2e-items=e2e/wh",
            "warehouse-touched=true",
            "other-touched=false",
            "unittest-files=w/y_test.py",
            "eval-files=",
        }
        self.assertTrue(expected.issubset(set(out.splitlines())))

    def test_emits_eval_items_and_eval_files(self) -> None:
        cfg = make_config(
            agent={"paths": ["a/**"], "eval_items": "py/agent"},
            **{"eval-files": {"files": True, "paths": ["a/**/*_eval.py"]}},
        )
        with unittest.mock.patch.object(git, "changed_files", return_value=["a/x_eval.py"]):
            args = argparse.Namespace(base="base", head="head", no_expand=False)
            out = _capture(changemap_cmds.cmd_github_outputs, cfg, args)
        lines = set(out.splitlines())
        self.assertIn("eval-items=py/agent", lines)
        self.assertIn("eval-files=a/x_eval.py", lines)

    def test_file_set_group_name_is_the_output_name(self) -> None:
        # No suffix is added — the table name IS the output name, so a new
        # file-set table needs no code change here.
        cfg = make_config(
            a={"paths": ["a/**"], "unittest_items": "x"},
            **{"custom-group": {"files": True, "paths": ["a/**"]}},
        )
        with unittest.mock.patch.object(git, "changed_files", return_value=["a/x.py"]):
            args = argparse.Namespace(base="base", head="head", no_expand=False)
            out = _capture(changemap_cmds.cmd_github_outputs, cfg, args)
        self.assertIn("custom-group=a/x.py", out.splitlines())


class TestExplain(unittest.TestCase):
    # SPEC: changemap#explain-groups-by-tag
    def test_renders_modified_dependents_and_invocations(self) -> None:
        cfg = make_config(
            warehouse={"paths": ["w/**"], "unittest_items": "py/wh", "e2e_items": "e2e/wh"},
            dashboards={
                "paths": ["d/**"],
                "depends": ["warehouse"],
                "unittest_items": "py/db",
                "e2e_items": "e2e/db",
            },
        )
        cfg.runners = _runners()
        with unittest.mock.patch.object(git, "changed_files", return_value=["w/x.py"]):
            args = argparse.Namespace(
                base="base",
                head="head",
                no_expand=False,
                files_map={"unittest-files": "w/foo_test.py"},
            )
            out = _capture(changemap_cmds.cmd_explain, cfg, args)
        self.assertIn("warehouse (matched directly):", out)
        self.assertIn("- w/x.py", out)
        self.assertIn("dashboards (depends on: warehouse)", out)
        # CSV invocations project through the expanded set, sorted, --files forwarded.
        self.assertIn("bin/test --dirs py/db,py/wh --files w/foo_test.py", out)
        self.assertIn("bin/e2e-test --dirs e2e/db,e2e/wh", out)

    def test_renders_eval_invocation_with_files(self) -> None:
        cfg = make_config(agent={"paths": ["a/**"], "eval_items": "py/agent"})
        cfg.runners = _runners()
        with unittest.mock.patch.object(git, "changed_files", return_value=["a/x_eval.py"]):
            args = argparse.Namespace(
                base="base", head="head", no_expand=False, files_map={"eval-files": "a/x_eval.py"}
            )
            out = _capture(changemap_cmds.cmd_explain, cfg, args)
        self.assertIn("bin/evals --dirs py/agent --files a/x_eval.py", out)

    def test_empty_item_list_renders_the_quote_sentinel(self) -> None:
        # The literal two-character `""` — a shell-safe empty argument, not a blank.
        cfg = make_config(agent={"paths": ["a/**"], "e2e_items": "e2e/agent"})
        cfg.runners = _runners()
        with unittest.mock.patch.object(git, "changed_files", return_value=["unmatched/x.py"]):
            args = argparse.Namespace(base="base", head="head", no_expand=False, files_map={})
            out = _capture(changemap_cmds.cmd_explain, cfg, args)
        self.assertIn('bin/e2e-test --dirs ""', out)

    def test_items_without_a_runner_render_as_comment_lines(self) -> None:
        cfg = make_config(agent={"paths": ["a/**"], "frontend_items": "pkg/agent"})
        cfg.runners = _runners()
        with unittest.mock.patch.object(git, "changed_files", return_value=["a/x.py"]):
            args = argparse.Namespace(base="base", head="head", no_expand=False, files_map={})
            out = _capture(changemap_cmds.cmd_explain, cfg, args)
        self.assertIn("# frontend: pkg/agent", out)

    def test_runner_without_files_never_gets_a_files_suffix(self) -> None:
        # `[runners] e2e` declares no `files`, reproducing the behaviour where
        # e2e never receives a --files argument even when a group is supplied.
        cfg = make_config(agent={"paths": ["a/**"], "e2e_items": "e2e/agent"})
        cfg.runners = _runners()
        with unittest.mock.patch.object(git, "changed_files", return_value=["a/x.py"]):
            args = argparse.Namespace(
                base="base", head="head", no_expand=False, files_map={"unittest-files": "a/x_test.py"}
            )
            out = _capture(changemap_cmds.cmd_explain, cfg, args)
        self.assertIn("bin/e2e-test --dirs e2e/agent\n", out)

    def test_no_matches_renders_the_none_line(self) -> None:
        cfg = make_config(agent={"paths": ["a/**"], "unittest_items": "x"})
        cfg.runners = _runners()
        with unittest.mock.patch.object(git, "changed_files", return_value=["zzz/x.py"]):
            args = argparse.Namespace(base="base", head="head", no_expand=False, files_map={})
            out = _capture(changemap_cmds.cmd_explain, cfg, args)
        self.assertIn("(none — no changed file matched any tag's paths)", out)


# Built through the real parser, not hand-constructed: a hand-built config is a
# parallel reimplementation that drifts, and these tests would then keep passing
# if `_parse_runners` broke.
RUNNERS_TABLE = {
    "runners": {
        "unittest": Raw('{ cmd = "bin/test", files = "unittest-files" }'),
        "e2e": Raw('{ cmd = "bin/e2e-test" }'),
        "eval": Raw('{ cmd = "bin/evals", files = "eval-files" }'),
    },
    "unittest-files": {"files": True, "paths": ["zz/**/*_test.py"]},
    "eval-files": {"files": True, "paths": ["zz/**/*_eval.py"]},
}


def _runners():
    return make_config(**RUNNERS_TABLE, a={"paths": ["zz/**"], "unittest_items": "zz"}).runners


class TestParseFilesArgs(unittest.TestCase):
    def test_parses_repeated_pairs(self) -> None:
        self.assertEqual(
            changemap_cmds.parse_files_args(["unittest-files=a.py,b.py", "eval-files=c.py"]),
            {"unittest-files": "a.py,b.py", "eval-files": "c.py"},
        )

    def test_empty_csv_is_legal(self) -> None:
        self.assertEqual(changemap_cmds.parse_files_args(["unittest-files="]), {"unittest-files": ""})

    def test_missing_equals_raises(self) -> None:
        with self.assertRaises(ValueError):
            _ = changemap_cmds.parse_files_args(["unittest-files"])

    def test_none_is_empty(self) -> None:
        self.assertEqual(changemap_cmds.parse_files_args(None), {})


class TestSha(unittest.TestCase):
    def _cfg(self):
        return make_config(
            a={"paths": ["a/**"], "sha": True},
            b={"paths": ["b/**"], "sha": True},
        )

    def test_unknown_bucket_exits_2(self) -> None:
        args = argparse.Namespace(bucket="nope", all=False, github_notice=False, head="HEAD")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(changemap_cmds.cmd_sha(self._cfg(), args), 2)

    def test_all_emits_version_env_lines(self) -> None:
        # `export $(tangier changemap sha --all)` depends on this KEY=value shape,
        # and the derived names are what k8s manifests reference.
        args = argparse.Namespace(bucket=None, all=True, github_notice=False, head="HEAD")
        with unittest.mock.patch.object(changemap_cmds, "sha_for_bucket", return_value="abc1234567"):
            out = _capture(changemap_cmds.cmd_sha, self._cfg(), args)
        self.assertEqual(out.splitlines(), ["A_VERSION=abc1234567", "B_VERSION=abc1234567"])

    def test_all_beats_a_positional_bucket(self) -> None:
        args = argparse.Namespace(bucket="a", all=True, github_notice=False, head="HEAD")
        with unittest.mock.patch.object(changemap_cmds, "sha_for_bucket", return_value="abc1234567"):
            out = _capture(changemap_cmds.cmd_sha, self._cfg(), args)
        self.assertEqual(len(out.splitlines()), 2)

    def test_github_notice_renders_the_markdown_table(self) -> None:
        args = argparse.Namespace(bucket=None, all=True, github_notice=True, head="HEAD")
        with unittest.mock.patch.object(changemap_cmds, "sha_for_bucket", return_value="abc1234567"):
            out = _capture(changemap_cmds.cmd_sha, self._cfg(), args)
        self.assertEqual(
            out.splitlines(),
            [
                "## Package builds",
                "",
                "| Package | Build ID |",
                "|-----|-------|",
                "| a | abc1234567 |",
                "| b | abc1234567 |",
            ],
        )

    def test_github_notice_skips_base_buckets(self) -> None:
        cfg = make_config(
            a={"paths": ["a/**"], "sha": True},
            **{"tags.x-base": {"paths": ["x/**"], "sha": True}},
        )
        args = argparse.Namespace(bucket=None, all=True, github_notice=True, head="HEAD")
        with unittest.mock.patch.object(changemap_cmds, "sha_for_bucket", return_value="abc1234567"):
            out = _capture(changemap_cmds.cmd_sha, cfg, args)
        self.assertNotIn("x-base", out)


class TestItems(unittest.TestCase):
    def test_unknown_items_name_exits_0_silently(self) -> None:
        # A typo in a runner script yields "nothing changed", not a failure.
        cfg = make_config(a={"paths": ["a/**"], "unittest_items": "py/a"})
        args = argparse.Namespace(name="nope", base="b", head="h", no_expand=False)
        out = _capture(changemap_cmds.cmd_items, cfg, args)
        self.assertEqual(out, "")


class TestCli(unittest.TestCase):
    def _write_config(self, body: str) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            _ = fh.write(body)
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_malformed_files_pair_exits_2(self) -> None:
        path = self._write_config('[a]\npaths = "a/**"\nunittest_items = "x"\n')
        with contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--config", path, "changemap", "explain", "--files", "no-equals-sign"])
        self.assertEqual(rc, 2)

    def test_missing_config_exits_2(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--config", "/nonexistent/pipeline.toml", "changemap", "list"])
        self.assertEqual(rc, 2)

    def test_config_error_exits_2(self) -> None:
        path = self._write_config('[a]\npaths = "a/**"\nbogus = 1\n')
        with contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--config", path, "changemap", "list"])
        self.assertEqual(rc, 2)

    def test_list_prints_tags_and_globs(self) -> None:
        path = self._write_config('[b]\npaths = "b/**"\nsha = true\n[a]\npaths = "a/**"\nsha = true\n')
        with contextlib.redirect_stderr(io.StringIO()):
            out = _capture(cli.main, ["--config", path, "changemap", "list"])
        self.assertEqual(out.splitlines(), ["a", "  a/**", "b", "  b/**"])

    def test_list_graph_prints_depends(self) -> None:
        path = self._write_config('[a]\npaths = "a/**"\nsha = true\n[b]\npaths = "b/**"\ndepends = "a"\nsha = true\n')
        with contextlib.redirect_stderr(io.StringIO()):
            out = _capture(cli.main, ["--config", path, "changemap", "list", "--graph"])
        self.assertEqual(out.splitlines(), ["a", "b", "  a"])


if __name__ == "__main__":
    _ = unittest.main()
