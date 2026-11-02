"""Image tests: rendered command lines and the skip decision.

`build_argv` is pure, so the buildx invocation is asserted as a command line and
never executed. No test touches a registry.
"""

import argparse
import contextlib
import io
import os
import tempfile
import unittest
import unittest.mock

from tangier.commands import image_cmds
from tangier.config import ImageSpec
from tangier.image import (
    ImageError,
    build_argv,
    parse_image_tag,
    render_compose,
    resolve_secrets,
)
from tangier.runner import Result
from tangier.tests.support import RecordingRunner, make_config

REF = "registry.example/ns/svc"


def _spec(**kw) -> ImageSpec:
    return ImageSpec(dockerfile=kw.pop("dockerfile", "svc/Dockerfile"), **kw)


class TestBuildArgv(unittest.TestCase):
    def test_push_reproduces_the_build_github_command_line(self) -> None:
        # Argument order, the full cache-to string, the :latest tag and the
        # unconditional PACKAGE_VERSION all reproduce bin/build-github exactly.
        argv = build_argv(_spec(), REF, "abc1234567", push=True)
        self.assertEqual(
            argv,
            [
                "docker",
                "buildx",
                "build",
                "--cache-from",
                f"type=registry,ref={REF}:buildcache",
                "--cache-to",
                f"type=registry,ref={REF}:buildcache,mode=max,image-manifest=true,oci-mediatypes=true",
                "--file",
                "svc/Dockerfile",
                "--tag",
                f"{REF}:abc1234567",
                "--tag",
                f"{REF}:latest",
                "--output",
                "type=registry",
                "--build-arg",
                "PACKAGE_VERSION=abc1234567",
                ".",
            ],
        )

    def test_cache_to_flags_are_verbatim(self) -> None:
        # image-manifest/oci-mediatypes are REQUIRED for a plain `distribution`
        # registry to accept the cache manifest. Dropping them fails the export.
        argv = build_argv(_spec(), REF, "t", push=True)
        cache_to = argv[argv.index("--cache-to") + 1]
        self.assertIn("mode=max", cache_to)
        self.assertIn("image-manifest=true", cache_to)
        self.assertIn("oci-mediatypes=true", cache_to)

    def test_no_push_uses_the_image_output_form_and_no_cache(self) -> None:
        argv = build_argv(_spec(), REF, "abc1234567")
        self.assertIn("--output", argv)
        self.assertEqual(argv[argv.index("--output") + 1], f"type=image,name={REF}")
        self.assertNotIn("--cache-from", argv)
        # No :latest tag when not pushing.
        self.assertEqual(argv.count("--tag"), 1)

    def test_push_of_latest_does_not_double_tag(self) -> None:
        argv = build_argv(_spec(), REF, "latest", push=True)
        self.assertEqual(argv.count("--tag"), 1)

    def test_cache_disabled_by_config(self) -> None:
        argv = build_argv(_spec(cache=False), REF, "t", push=True)
        self.assertNotIn("--cache-from", argv)
        self.assertEqual(argv[argv.index("--output") + 1], "type=registry")

    def test_load_builds_for_the_local_daemon(self) -> None:
        # The bin/build-local shape: cross-build and load, no --output.
        argv = build_argv(_spec(platform="linux/amd64"), REF, "t", load=True)
        self.assertIn("--load", argv)
        self.assertEqual(argv[argv.index("--platform") + 1], "linux/amd64")
        self.assertNotIn("--output", argv)

    def test_secrets_render_as_id_env_pairs(self) -> None:
        secrets = resolve_secrets(["sentry_auth_token"], env={"SENTRY_AUTH_TOKEN": "x"})
        argv = build_argv(_spec(), REF, "t", push=True, secrets=secrets)
        self.assertIn("--secret", argv)
        self.assertEqual(argv[argv.index("--secret") + 1], "id=sentry_auth_token,env=SENTRY_AUTH_TOKEN")

    def test_extra_build_args_are_sorted_after_package_version(self) -> None:
        argv = build_argv(_spec(args={"B": "2", "A": "1"}), REF, "t")
        tail = argv[argv.index("--build-arg") :]
        self.assertEqual(tail[:2], ["--build-arg", "PACKAGE_VERSION=t"])
        self.assertIn("A=1", tail)
        self.assertIn("B=2", tail)
        self.assertLess(tail.index("A=1"), tail.index("B=2"))

    def test_context_is_the_last_argument(self) -> None:
        self.assertEqual(build_argv(_spec(), REF, "t")[-1], ".")


class TestResolveSecrets(unittest.TestCase):
    def test_secret_absent_from_environment_is_omitted(self) -> None:
        # Preserves build-github's guard: an unset variable means no --secret,
        # rather than passing an empty one.
        self.assertEqual(resolve_secrets(["sentry_auth_token"], env={}), [])

    def test_empty_value_counts_as_absent(self) -> None:
        self.assertEqual(resolve_secrets(["sentry_auth_token"], env={"SENTRY_AUTH_TOKEN": ""}), [])

    def test_config_and_cli_secrets_union_without_duplicates(self) -> None:
        got = resolve_secrets(["a"], ["a", "b"], env={"A": "1", "B": "2"})
        self.assertEqual([s.id for s in got], ["a", "b"])

    def test_only_the_variable_name_is_used(self) -> None:
        # The value must never enter the rendered command line.
        secrets = resolve_secrets(["tok"], env={"TOK": "super-secret-value"})
        argv = build_argv(_spec(), REF, "t", secrets=secrets)
        self.assertNotIn("super-secret-value", " ".join(argv))


class TestParseImageTag(unittest.TestCase):
    def test_plain_reference(self) -> None:
        self.assertEqual(parse_image_tag("registry.example/ns/svc:abc123"), "abc123")

    def test_registry_with_a_port(self) -> None:
        # `awk -F: '{print $NF}'` returned the port here, after which a deploy
        # deleted a nonsense Job and rolled back against garbage.
        self.assertEqual(parse_image_tag("registry:5000/ns/svc:abc123"), "abc123")

    def test_untagged_reference_with_a_port(self) -> None:
        self.assertEqual(parse_image_tag("registry:5000/ns/svc"), "")

    def test_digest_is_stripped(self) -> None:
        self.assertEqual(parse_image_tag("registry.example/ns/svc:abc123@sha256:deadbeef"), "abc123")

    def test_untagged(self) -> None:
        self.assertEqual(parse_image_tag("registry.example/ns/svc"), "")


class TestExistsAndSkip(unittest.TestCase):
    def _cfg(self):
        return make_config(
            **{
                "registry": {"url": "registry.example/ns"},
                "image.svc": {"dockerfile": "svc/Dockerfile"},
                "svc": {"paths": ["svc/**"], "sha": True},
            }
        )

    def _args(self, runner, **kw) -> argparse.Namespace:
        base = dict(
            bucket="svc",
            tag="abc1234567",
            head="HEAD",
            runner=runner,
            push=False,
            load=False,
            force=False,
            secret=[],
            print=False,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def test_exists_prints_exists_and_exits_0(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = image_cmds.cmd_exists(self._cfg(), self._args(runner))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "exists")

    def test_missing_prints_missing_and_exits_1(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = image_cmds.cmd_exists(self._cfg(), self._args(runner))
        self.assertEqual(rc, 1)
        self.assertEqual(buf.getvalue().strip(), "missing")

    def test_missing_regctl_is_an_error_not_a_missing_report(self) -> None:
        # The old script printed `missing` when regctl was absent, causing a
        # rebuild-and-push of an already-published tag.
        runner = RecordingRunner()
        runner.missing.add("regctl")
        with self.assertRaises(ImageError):
            _ = image_cmds.cmd_exists(self._cfg(), self._args(runner))

    def test_probe_targets_the_computed_tag(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        with contextlib.redirect_stdout(io.StringIO()):
            _ = image_cmds.cmd_exists(self._cfg(), self._args(runner))
        self.assertEqual(
            runner.calls[0],
            ["regctl", "manifest", "get", "--platform", "linux/amd64", "registry.example/ns/svc:abc1234567"],
        )

    def test_build_skips_when_already_published(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = image_cmds.cmd_build(self._cfg(), self._args(runner, push=True))
        self.assertEqual(rc, 0)
        self.assertIn("already published", buf.getvalue())
        self.assertEqual(runner.commands(binary="docker"), [])

    def test_build_runs_when_missing(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        with contextlib.redirect_stdout(io.StringIO()):
            rc = image_cmds.cmd_build(self._cfg(), self._args(runner, push=True))
        self.assertEqual(rc, 0)
        self.assertEqual(len(runner.commands(binary="docker")), 1)

    def test_force_builds_even_when_published(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        with contextlib.redirect_stdout(io.StringIO()):
            _ = image_cmds.cmd_build(self._cfg(), self._args(runner, push=True, force=True))
        self.assertEqual(len(runner.commands(binary="docker")), 1)

    def test_build_without_push_does_not_probe_the_registry(self) -> None:
        runner = RecordingRunner()
        with contextlib.redirect_stdout(io.StringIO()):
            _ = image_cmds.cmd_build(self._cfg(), self._args(runner))
        self.assertEqual(runner.commands(binary="regctl"), [])

    def test_unbuildable_bucket_raises(self) -> None:
        cfg = make_config(**{"registry": {"url": "registry.example/ns"}, "svc": {"paths": ["svc/**"], "sha": True}})
        with self.assertRaises(ImageError) as ctx:
            _ = image_cmds.cmd_build(cfg, self._args(RecordingRunner()))
        self.assertIn("not buildable", str(ctx.exception))

    def test_unknown_bucket_tag_raises(self) -> None:
        from tangier.image import tag_for

        with self.assertRaises(ImageError):
            _ = tag_for(self._cfg(), "nope")


class TestBuildOutputs(unittest.TestCase):
    """`tag` and `built`, which the build action reads from $GITHUB_OUTPUT."""

    def _cfg(self):
        return make_config(
            **{
                "registry": {"url": "registry.example/ns"},
                "image.svc": {"dockerfile": "svc/Dockerfile"},
                "svc": {"paths": ["svc/**"], "sha": True},
            }
        )

    def _args(self, runner, **kw) -> argparse.Namespace:
        base = dict(
            bucket="svc",
            tag="abc1234567",
            head="HEAD",
            runner=runner,
            push=True,
            load=False,
            force=False,
            secret=[],
            print=False,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def _run(self, runner, **kw) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._rc = image_cmds.cmd_build(self._cfg(), self._args(runner, **kw))
        return buf.getvalue()

    def test_successful_build_emits_the_tag_and_built_true(self) -> None:
        out = self._run(RecordingRunner({("regctl",): Result(1)}))
        self.assertEqual(self._rc, 0)
        self.assertIn("tag=abc1234567", out)
        self.assertIn("built=true", out)

    def test_skipped_build_emits_the_tag_and_built_false(self) -> None:
        # The tag is still the answer to "what should I deploy?" — a skip means
        # it is already there, not that there is nothing to name.
        out = self._run(RecordingRunner({("regctl",): Result(0)}))
        self.assertEqual(self._rc, 0)
        self.assertIn("tag=abc1234567", out)
        self.assertIn("built=false", out)

    def test_failed_build_emits_built_false_and_keeps_the_returncode(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1), ("docker",): Result(7)})
        out = self._run(runner)
        self.assertEqual(self._rc, 7)
        self.assertIn("built=false", out)

    def test_print_emits_nothing(self) -> None:
        # --print returns before any registry probe, so it knows nothing about
        # whether a build would happen.
        out = self._run(RecordingRunner(), print=True)
        self.assertNotIn("built=", out)
        self.assertNotIn("tag=", out)

    def test_outputs_reach_the_github_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            with unittest.mock.patch.dict(os.environ, {"GITHUB_OUTPUT": path}):
                _ = self._run(RecordingRunner({("regctl",): Result(1)}))
            with open(path) as fh:
                self.assertEqual(fh.read().splitlines(), ["tag=abc1234567", "built=true"])


class TestCompose(unittest.TestCase):
    def test_replaces_placeholders_with_fully_qualified_refs(self) -> None:
        cfg = make_config(
            **{
                "registry": {"url": "registry.example/ns"},
                "a": {"paths": ["a/**"], "sha": True},
                "b": {"paths": ["b/**"], "sha": True},
            }
        )
        import unittest.mock

        from tangier import image as image_mod

        with unittest.mock.patch.object(image_mod, "sha_for_bucket", return_value="v1"):
            out = render_compose(cfg, "x: [[a]]\ny: [[b]]\nz: [[unknown]]\nw: ${VAR}\n")
        self.assertIn("x: registry.example/ns/a:v1", out)
        self.assertIn("y: registry.example/ns/b:v1", out)
        # Unmatched placeholders are left as-is; ${VAR} is left for docker-compose.
        self.assertIn("z: [[unknown]]", out)
        self.assertIn("w: ${VAR}", out)

    def test_bucket_without_a_placeholder_is_a_noop(self) -> None:
        cfg = make_config(**{"registry": {"url": "r/ns"}, "a": {"paths": ["a/**"], "sha": True}})
        import unittest.mock

        from tangier import image as image_mod

        with unittest.mock.patch.object(image_mod, "sha_for_bucket", return_value="v1"):
            self.assertEqual(render_compose(cfg, "nothing here\n"), "nothing here\n")

    def test_missing_registry_raises(self) -> None:
        cfg = make_config(a={"paths": ["a/**"], "sha": True})
        with self.assertRaises(ImageError):
            _ = render_compose(cfg, "x: [[a]]")


if __name__ == "__main__":
    _ = unittest.main()


class TestStdoutContract(unittest.TestCase):
    """`image exists` stdout is captured and string-compared by CI.

    The real runner echoes each command before running it; that trace must go to
    stderr, or the captured value stops matching `exists`/`missing` and the
    build step silently never runs.
    """

    def test_subprocess_echo_goes_to_stderr_not_stdout(self) -> None:
        from tangier.runner import Subprocess

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            _ = Subprocess(echo=True).run(["true"])
        self.assertEqual(out.getvalue(), "")
        self.assertIn("true", err.getvalue())

    def test_dry_run_trace_goes_to_stderr_not_stdout(self) -> None:
        from tangier.runner import DryRun

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            _ = DryRun().run(["docker", "buildx"])
            _ = DryRun().pipe([["a"], ["b"]])
        self.assertEqual(out.getvalue(), "")
        self.assertIn("would run", err.getvalue())


class TestPrintIsADryRun(unittest.TestCase):
    def _cfg(self):
        return make_config(
            **{
                "registry": {"url": "registry.example/ns"},
                "image.svc": {"dockerfile": "svc/Dockerfile"},
                "svc": {"paths": ["svc/**"], "sha": True},
            }
        )

    def test_print_does_not_need_regctl(self) -> None:
        # --print is the dry run, so it must work on the machine least likely to
        # have a registry client.
        runner = RecordingRunner()
        runner.missing.add("regctl")
        args = argparse.Namespace(
            bucket="svc",
            tag="abc1234567",
            head="HEAD",
            runner=runner,
            push=True,
            load=False,
            force=False,
            secret=[],
            print=True,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = image_cmds.cmd_build(self._cfg(), args)
        self.assertEqual(rc, 0)
        self.assertIn("docker buildx build", buf.getvalue())
        self.assertEqual(runner.calls, [])
