"""Image tests: rendered command lines and the skip decision.

`build_argv` is pure, so the buildx invocation is asserted as a command line and
never executed. No test touches a registry.
"""

import argparse
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
import unittest.mock

from tangier import git
from tangier.commands import image_cmds
from tangier.config import ImageSpec
from tangier.image import (
    ImageError,
    build_argv,
    parse_image_tag,
    plan_build,
    render_compose,
    resolve_secrets,
)
from tangier.runner import Result
from tangier.tests.support import RecordingRunner, make_config, make_git_repo, write_config

REF = "registry.example/ns/svc"

# The real one, for `TestUncommittedFiles`: `setUpModule` patches it away.
_uncommitted_files = git.uncommitted_files


def setUpModule() -> None:
    # `image build` checks the working tree for uncommitted changes. These
    # tests run in tangier's own checkout, so they see a clean one; a test of
    # the dirty rule patches its own file list.
    patcher = unittest.mock.patch.object(git, "uncommitted_files", return_value=[])
    _ = patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def _spec(**kw) -> ImageSpec:
    return ImageSpec(dockerfile=kw.pop("dockerfile", "svc/Dockerfile"), **kw)


def _cfg(mirrors: tuple[str, ...] = ()):
    registry: dict[str, object] = {"url": "registry.example/ns"}
    if mirrors:
        registry["mirrors"] = list(mirrors)
    return make_config(
        **{
            "registry": registry,
            "image.svc": {"dockerfile": "svc/Dockerfile"},
            "svc": {"paths": ["svc/**"], "sha": True},
        }
    )


def _args(runner, **kw) -> argparse.Namespace:
    """`image build`/`image exists` arguments, as the parser would hand them over."""
    base = dict(
        bucket="svc",
        buckets=["svc"],
        tag="abc1234567",
        head="HEAD",
        runner=runner,
        push=False,
        pull=False,
        jobs=1,
        force=False,
        no_mirrors=False,
        secret=[],
        print=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


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

    def test_local_build_loads_and_reads_the_cache_without_writing_it(self) -> None:
        # A local build must never write the cache: a branch's RUN layers would
        # poison what published builds reuse.
        argv = build_argv(_spec(), REF, "abc1234567")
        self.assertEqual(
            argv,
            [
                "docker",
                "buildx",
                "build",
                "--load",
                "--cache-from",
                f"type=registry,ref={REF}:buildcache",
                "--file",
                "svc/Dockerfile",
                "--tag",
                f"{REF}:abc1234567",
                "--build-arg",
                "PACKAGE_VERSION=abc1234567",
                ".",
            ],
        )

    def test_local_build_with_the_cache_disabled_has_no_cache_flags(self) -> None:
        argv = build_argv(_spec(cache=False), REF, "t")
        self.assertNotIn("--cache-from", argv)
        self.assertNotIn("--cache-to", argv)

    def test_push_of_latest_does_not_double_tag(self) -> None:
        argv = build_argv(_spec(), REF, "latest", push=True)
        self.assertEqual(argv.count("--tag"), 1)

    def test_cache_disabled_by_config(self) -> None:
        argv = build_argv(_spec(cache=False), REF, "t", push=True)
        self.assertNotIn("--cache-from", argv)
        self.assertEqual(argv[argv.index("--output") + 1], "type=registry")

    def test_local_build_cross_builds_for_the_spec_platform(self) -> None:
        # The bin/build-local shape: cross-build and load, no --output.
        argv = build_argv(_spec(platform="linux/amd64"), REF, "t")
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
    def test_exists_prints_exists_and_exits_0(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = image_cmds.cmd_exists(_cfg(), _args(runner))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "exists")

    def test_missing_prints_missing_and_exits_1(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = image_cmds.cmd_exists(_cfg(), _args(runner))
        self.assertEqual(rc, 1)
        self.assertEqual(buf.getvalue().strip(), "missing")

    def test_missing_regctl_is_an_error_not_a_missing_report(self) -> None:
        # The old script printed `missing` when regctl was absent, causing a
        # rebuild-and-push of an already-published tag.
        runner = RecordingRunner()
        runner.missing.add("regctl")
        with self.assertRaises(ImageError):
            _ = image_cmds.cmd_exists(_cfg(), _args(runner))

    def test_probe_targets_the_computed_tag(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        with contextlib.redirect_stdout(io.StringIO()):
            _ = image_cmds.cmd_exists(_cfg(), _args(runner))
        self.assertEqual(
            runner.calls[0],
            ["regctl", "manifest", "get", "--platform", "linux/amd64", "registry.example/ns/svc:abc1234567"],
        )

    def test_build_skips_when_already_published(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = image_cmds.cmd_build(_cfg(), _args(runner, push=True))
        self.assertEqual(rc, 0)
        self.assertIn("already published", buf.getvalue())
        self.assertEqual(runner.commands(binary="docker"), [])

    def test_build_runs_when_missing(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        with contextlib.redirect_stdout(io.StringIO()):
            rc = image_cmds.cmd_build(_cfg(), _args(runner, push=True))
        self.assertEqual(rc, 0)
        self.assertEqual(len(runner.commands(binary="docker")), 1)

    def test_force_builds_even_when_published(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        with contextlib.redirect_stdout(io.StringIO()):
            _ = image_cmds.cmd_build(_cfg(), _args(runner, push=True, force=True))
        self.assertEqual(len(runner.commands(binary="docker")), 1)

    def test_unbuildable_bucket_raises(self) -> None:
        cfg = make_config(**{"registry": {"url": "registry.example/ns"}, "svc": {"paths": ["svc/**"], "sha": True}})
        with self.assertRaises(ImageError) as ctx:
            _ = image_cmds.cmd_build(cfg, _args(RecordingRunner(), push=True, force=True))
        self.assertIn("not buildable", str(ctx.exception))

    def test_unknown_bucket_tag_raises(self) -> None:
        from tangier.image import tag_for

        with self.assertRaises(ImageError):
            _ = tag_for(_cfg(), "nope")


class TestBuildOutputs(unittest.TestCase):
    """`tag` and `built`, which the build action reads from $GITHUB_OUTPUT."""

    def _run(self, runner, **kw) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self._rc = image_cmds.cmd_build(_cfg(), _args(runner, **{"push": True, **kw}))
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
    def test_print_does_not_need_regctl(self) -> None:
        # --print is the dry run, so it must work on the machine least likely to
        # have a registry client.
        runner = RecordingRunner()
        runner.missing.add("regctl")
        args = _args(runner, push=True, print=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = image_cmds.cmd_build(_cfg(), args)
        self.assertEqual(rc, 0)
        self.assertIn("docker buildx build", buf.getvalue())
        self.assertEqual(runner.calls, [])


PRIMARY = "registry.example/ns/svc"
MIRROR = "mirror.example/ns/svc"
MIRROR_2 = "mirror2.example/ns/svc"


def _probe(ref: str) -> tuple[str, ...]:
    return ("regctl", "manifest", "get", "--platform", "linux/amd64", f"{ref}:abc1234567")


def _copy(src: str, dst: str) -> list[str]:
    return ["regctl", "image", "copy", f"{src}:abc1234567", f"{dst}:abc1234567"]


class TestMirrors(unittest.TestCase):
    """`[registry] mirrors`: every push destination gets the tag; only `url` is read."""

    def _build(self, runner, mirrors=("mirror.example/ns",), **kw) -> int:
        self.out = io.StringIO()
        with contextlib.redirect_stdout(self.out):
            return image_cmds.cmd_build(_cfg(mirrors), _args(runner, **{"push": True, **kw}))

    def _copies(self, runner) -> list[list[str]]:
        return [c for c in runner.calls if c[:3] == ["regctl", "image", "copy"]]

    # --- build_argv ---------------------------------------------------------

    def test_push_tags_every_destination_and_caches_on_the_primary_only(self) -> None:
        argv = build_argv(_spec(), PRIMARY, "t", push=True, mirrors=[MIRROR])
        self.assertEqual(
            argv,
            [
                "docker",
                "buildx",
                "build",
                "--cache-from",
                f"type=registry,ref={PRIMARY}:buildcache",
                "--cache-to",
                f"type=registry,ref={PRIMARY}:buildcache,mode=max,image-manifest=true,oci-mediatypes=true",
                "--file",
                "svc/Dockerfile",
                "--tag",
                f"{PRIMARY}:t",
                "--tag",
                f"{PRIMARY}:latest",
                "--tag",
                f"{MIRROR}:t",
                "--tag",
                f"{MIRROR}:latest",
                "--output",
                "type=registry",
                "--build-arg",
                "PACKAGE_VERSION=t",
                ".",
            ],
        )

    # --- cmd_build ----------------------------------------------------------

    def test_all_published_skips_without_a_copy(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        self.assertEqual(self._build(runner), 0)
        self.assertEqual(runner.commands(binary="docker"), [])
        self.assertEqual(self._copies(runner), [])
        self.assertIn("built=false", self.out.getvalue())

    def test_a_mirror_missing_the_tag_is_copied_from_the_primary(self) -> None:
        runner = RecordingRunner({_probe(PRIMARY): Result(0), _probe(MIRROR): Result(1)})
        self.assertEqual(self._build(runner), 0)
        self.assertEqual(runner.commands(binary="docker"), [])
        self.assertEqual(self._copies(runner), [_copy(PRIMARY, MIRROR)])
        self.assertIn("built=false", self.out.getvalue())

    def test_a_primary_missing_the_tag_is_copied_from_the_mirror(self) -> None:
        runner = RecordingRunner({_probe(PRIMARY): Result(1), _probe(MIRROR): Result(0)})
        self.assertEqual(self._build(runner), 0)
        self.assertEqual(self._copies(runner), [_copy(MIRROR, PRIMARY)])

    def test_every_destination_missing_the_tag_gets_a_copy_from_the_first_that_has_it(self) -> None:
        runner = RecordingRunner({_probe(PRIMARY): Result(1), _probe(MIRROR): Result(0), _probe(MIRROR_2): Result(1)})
        self.assertEqual(self._build(runner, mirrors=("mirror.example/ns", "mirror2.example/ns")), 0)
        self.assertEqual(self._copies(runner), [_copy(MIRROR, PRIMARY), _copy(MIRROR, MIRROR_2)])

    def test_a_failed_copy_stops_and_exits_non_zero(self) -> None:
        runner = RecordingRunner(
            {
                _probe(PRIMARY): Result(0),
                _probe(MIRROR): Result(1),
                _probe(MIRROR_2): Result(1),
                ("regctl", "image", "copy"): Result(5),
            }
        )
        self.assertEqual(self._build(runner, mirrors=("mirror.example/ns", "mirror2.example/ns")), 5)
        self.assertEqual(self._copies(runner), [_copy(PRIMARY, MIRROR)])
        self.assertIn("built=false", self.out.getvalue())

    def test_none_published_builds_once_with_every_tag(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        self.assertEqual(self._build(runner), 0)
        [docker] = runner.commands(binary="docker")
        self.assertIn(f"{PRIMARY}:abc1234567", docker)
        self.assertIn(f"{MIRROR}:latest", docker)
        self.assertEqual(self._copies(runner), [])
        self.assertIn("built=true", self.out.getvalue())

    def test_no_mirrors_probes_and_tags_the_primary_only(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        _ = self._build(runner, no_mirrors=True)
        self.assertEqual(runner.commands(binary="regctl"), [" ".join(_probe(PRIMARY))])
        [docker] = runner.commands(binary="docker")
        self.assertNotIn(MIRROR, docker)

    def test_pull_ignores_mirrors(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        _ = self._build(runner, push=False, pull=True)
        self.assertEqual(runner.commands(binary="regctl"), [" ".join(_probe(PRIMARY))])
        [docker] = runner.commands(binary="docker")
        self.assertNotIn(MIRROR, docker)

    def test_the_no_mirrors_flag_reaches_the_build(self) -> None:
        # Through the real parser, so the flag and the attribute cmd_build reads agree.
        from tangier import cli

        path = write_config(
            self,
            '[registry]\nurl = "registry.example/ns"\nmirrors = ["mirror.example/ns"]\n'
            '[image.svc]\ndockerfile = "svc/Dockerfile"\n[svc]\npaths = ["svc/**"]\nsha = true\n',
        )

        def printed(*flags: str) -> str:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["--config", path, "image", "build", "svc", "--tag", "t", "--push", "--print", *flags])
            self.assertEqual(rc, 0)
            return buf.getvalue()

        self.assertIn(f"{MIRROR}:t", printed())
        self.assertNotIn(MIRROR, printed("--no-mirrors"))

    # --- image registries -----------------------------------------------------

    def _registries(self, cfg, **kw) -> list[str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(image_cmds.cmd_registries(cfg, argparse.Namespace(**{"no_mirrors": False, **kw})), 0)
        return buf.getvalue().splitlines()

    def test_registries_lists_url_then_each_mirror(self) -> None:
        cfg = _cfg(("mirror.example/ns", "mirror2.example/ns"))
        self.assertEqual(self._registries(cfg), ["registry.example/ns", "mirror.example/ns", "mirror2.example/ns"])

    def test_registries_without_mirrors_lists_url_only(self) -> None:
        self.assertEqual(self._registries(_cfg(("mirror.example/ns",)), no_mirrors=True), ["registry.example/ns"])

    def test_registries_without_a_registry_raises(self) -> None:
        cfg = make_config(**{"svc": {"paths": ["svc/**"], "sha": True}})
        with self.assertRaises(ImageError):
            _ = self._registries(cfg)

    # --- only `url` is read ---------------------------------------------------

    def test_exists_reads_the_primary_only(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        with contextlib.redirect_stdout(io.StringIO()):
            _ = image_cmds.cmd_exists(_cfg(("mirror.example/ns",)), _args(runner))
        self.assertEqual(runner.calls, [list(_probe(PRIMARY))])

    def test_compose_reads_the_primary_only(self) -> None:
        from tangier import image as image_mod

        with unittest.mock.patch.object(image_mod, "sha_for_bucket", return_value="v1"):
            self.assertEqual(render_compose(_cfg(("mirror.example/ns",)), "x: [[svc]]"), f"x: {PRIMARY}:v1")


def _multi_cfg(mirrors: tuple[str, ...] = (), api_platform: str | None = None):
    """Three buckets: `api` and `web` are buildable, `docs` has no `[image.docs]`."""
    registry: dict[str, object] = {"url": "registry.example/ns"}
    if mirrors:
        registry["mirrors"] = list(mirrors)
    api: dict[str, object] = {"dockerfile": "api/Dockerfile"}
    if api_platform:
        api["platform"] = api_platform
    return make_config(
        **{
            "registry": registry,
            "image.api": api,
            "image.web": {"dockerfile": "web/Dockerfile"},
            "api": {"paths": ["api/**"], "sha": True},
            "web": {"paths": ["web/**"], "sha": True},
            "docs": {"paths": ["docs/**"], "sha": True},
        }
    )


def _fake_tags(testcase: unittest.TestCase) -> None:
    """Tag each bucket `sha-<bucket>` without reading a git tree."""
    from tangier import image as image_mod

    patcher = unittest.mock.patch.object(image_mod, "sha_for_bucket", side_effect=lambda c, b, h="HEAD": f"sha-{b}")
    _ = patcher.start()
    testcase.addCleanup(patcher.stop)


class TestPlanBuild(unittest.TestCase):
    def setUp(self) -> None:
        _fake_tags(self)
        self.probed: list[tuple[str, str]] = []

    def _plan(self, names, published=(), **kw):
        def is_published(bucket: str, tag: str) -> bool:
            self.probed.append((bucket, tag))
            return bucket in published

        steps = plan_build(_multi_cfg(), names, is_published=is_published, **kw)
        return [(s.bucket, s.tag, s.action) for s in steps]

    def test_published_is_reused_and_unpublished_is_built(self) -> None:
        self.assertEqual(
            self._plan(["api", "web"], published={"api"}),
            [("api", "sha-api", "reuse"), ("web", "sha-web", "build")],
        )

    def test_force_builds_everything_without_probing(self) -> None:
        self.assertEqual(
            self._plan(["api", "web"], published={"api", "web"}, force=True),
            [("api", "sha-api", "build"), ("web", "sha-web", "build")],
        )
        self.assertEqual(self.probed, [])

    def test_a_dirty_bucket_builds_even_when_published(self) -> None:
        plan = self._plan(["api", "web"], published={"api", "web"}, dirty={"web"})
        self.assertEqual(plan, [("api", "sha-api", "reuse"), ("web", "sha-web", "build")])
        self.assertEqual(self.probed, [("api", "sha-api")])

    def test_a_published_bucket_without_an_image_spec_is_reused(self) -> None:
        self.assertEqual(self._plan(["docs"], published={"docs"}), [("docs", "sha-docs", "reuse")])

    def test_an_unknown_bucket_raises_before_any_probe(self) -> None:
        with self.assertRaises(ImageError) as ctx:
            _ = self._plan(["api", "nope"])
        self.assertIn("unknown bucket: nope", str(ctx.exception))
        self.assertEqual(self.probed, [])

    def test_a_bucket_to_build_without_an_image_spec_raises(self) -> None:
        with self.assertRaises(ImageError) as ctx:
            _ = self._plan(["api", "docs"], force=True)
        self.assertIn("[image.docs]", str(ctx.exception))

    def test_tag_overrides_the_computed_tag(self) -> None:
        self.assertEqual(self._plan(["api"], tag="t1"), [("api", "t1", "build")])


def _probe_of(bucket: str) -> tuple[str, ...]:
    return ("regctl", "manifest", "get", "--platform", "linux/amd64", f"registry.example/ns/{bucket}:sha-{bucket}")


def _local_build(bucket: str) -> str:
    """The whole `--pull` build command line for one of `_multi_cfg`'s buckets."""
    ref = f"registry.example/ns/{bucket}"
    return (
        f"docker buildx build --load --cache-from type=registry,ref={ref}:buildcache "
        f"--file {bucket}/Dockerfile --tag {ref}:sha-{bucket} --build-arg PACKAGE_VERSION=sha-{bucket} ."
    )


class TestBuildCommand(unittest.TestCase):
    """`image build` over several buckets, through `cmd_build`."""

    def setUp(self) -> None:
        _fake_tags(self)

    def _build(self, runner, buckets, cfg=None, **kw) -> int:
        self.out = io.StringIO()
        self.err = io.StringIO()
        args = _args(runner, buckets=buckets, tag=None, **kw)
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            return image_cmds.cmd_build(cfg or _multi_cfg(), args)

    def test_pull_pulls_the_published_and_builds_the_rest(self) -> None:
        runner = RecordingRunner({_probe_of("api"): Result(0), _probe_of("web"): Result(1)})
        self.assertEqual(self._build(runner, ["api", "web"], pull=True), 0)
        self.assertEqual(
            runner.commands(binary="docker"),
            ["docker pull registry.example/ns/api:sha-api", _local_build("web")],
        )
        self.assertEqual(self.out.getvalue().splitlines(), ["pulled api", "built web"])

    def test_pull_asks_for_the_spec_platform(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        _ = self._build(runner, ["api"], cfg=_multi_cfg(api_platform="linux/amd64"), pull=True)
        self.assertEqual(
            runner.commands(binary="docker"), ["docker pull --platform linux/amd64 registry.example/ns/api:sha-api"]
        )

    def test_pull_builds_a_dirty_bucket_and_names_the_files(self) -> None:
        runner = RecordingRunner({("regctl",): Result(0)})
        with unittest.mock.patch.object(git, "uncommitted_files", return_value=["web/app.py"]):
            self.assertEqual(self._build(runner, ["api", "web"], pull=True), 0)
        self.assertEqual(
            self.out.getvalue().splitlines(), ["pulled api", "built web (uncommitted changes: web/app.py)"]
        )

    def test_push_with_a_dirty_bucket_errors_before_any_call(self) -> None:
        runner = RecordingRunner()
        dirty = unittest.mock.patch.object(git, "uncommitted_files", return_value=["web/app.py", "other.txt"])
        with dirty, self.assertRaises(ImageError) as ctx:
            _ = self._build(runner, ["api", "web"], push=True)
        self.assertIn("web: web/app.py", str(ctx.exception))
        self.assertNotIn("other.txt", str(ctx.exception))
        self.assertEqual(runner.calls, [])

    def test_push_copies_one_bucket_and_builds_another(self) -> None:
        runner = RecordingRunner(
            {
                _probe_of("api"): Result(0),
                ("regctl", "manifest", "get", "--platform", "linux/amd64", "mirror.example/ns/api:sha-api"): Result(1),
                ("regctl", "manifest", "get", "--platform", "linux/amd64", "registry.example/ns/web:sha-web"): Result(
                    1
                ),
                ("regctl", "manifest", "get", "--platform", "linux/amd64", "mirror.example/ns/web:sha-web"): Result(1),
            }
        )
        self.assertEqual(self._build(runner, ["api", "web"], cfg=_multi_cfg(("mirror.example/ns",)), push=True), 0)
        copies = [c for c in runner.calls if c[:3] == ["regctl", "image", "copy"]]
        self.assertEqual(
            copies, [["regctl", "image", "copy", "registry.example/ns/api:sha-api", "mirror.example/ns/api:sha-api"]]
        )
        [build] = runner.commands(binary="docker")
        self.assertIn("--tag mirror.example/ns/web:sha-web", build)
        self.assertEqual(self.out.getvalue().splitlines()[-2:], ["copied api", "built web"])

    def test_a_failed_build_does_not_stop_the_other(self) -> None:
        api_build = tuple(_local_build("api").split())
        runner = RecordingRunner({("regctl",): Result(1), api_build: Result(3)})
        self.assertEqual(self._build(runner, ["api", "web"], pull=True, jobs=2), 3)
        self.assertEqual(len(runner.commands(binary="docker")), 2)
        self.assertEqual(self.out.getvalue().splitlines(), ["build failed api", "built web"])
        # Side by side, each build's output is printed whole under its own header.
        self.assertIn("== build api (exit 3) ==", self.err.getvalue())
        self.assertIn("== build web (exit 0) ==", self.err.getvalue())

    def test_a_failed_pull_reports_and_returns_its_code(self) -> None:
        runner = RecordingRunner(
            {
                _probe_of("api"): Result(0),
                _probe_of("web"): Result(1),
                ("docker", "pull"): Result(4),
                tuple(_local_build("web").split()): Result(3),
            }
        )
        self.assertEqual(self._build(runner, ["api", "web"], pull=True), 4)
        self.assertEqual(self.out.getvalue().splitlines(), ["pull failed api", "build failed web"])

    def test_missing_docker_errors_before_any_pull_or_build(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        runner.missing.add("docker")
        with self.assertRaises(ImageError):
            _ = self._build(runner, ["api"], pull=True)
        self.assertEqual(runner.commands(binary="docker"), [])

    def test_several_buckets_emit_no_outputs(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        _ = self._build(runner, ["api", "web"], push=True)
        self.assertNotIn("tag=", self.out.getvalue())

    def test_one_bucket_still_emits_tag_and_built(self) -> None:
        runner = RecordingRunner({("regctl",): Result(1)})
        _ = self._build(runner, ["api"], pull=True)
        self.assertEqual(self.out.getvalue().splitlines(), ["built api", "tag=sha-api", "built=true"])

    def test_print_shows_every_bucket_without_any_call(self) -> None:
        runner = RecordingRunner()
        self.assertEqual(self._build(runner, ["api", "web"], pull=True, print=True), 0)
        self.assertEqual(self.out.getvalue().splitlines(), [_local_build("api"), _local_build("web")])
        self.assertEqual(runner.calls, [])


class TestBuildParser(unittest.TestCase):
    """The `image build` flags, through the real parser."""

    def setUp(self) -> None:
        self.path = write_config(
            self,
            '[registry]\nurl = "registry.example/ns"\n'
            '[image.a]\ndockerfile = "a/Dockerfile"\n[a]\npaths = ["a/**"]\nsha = true\n'
            '[image.b]\ndockerfile = "b/Dockerfile"\n[b]\npaths = ["b/**"]\nsha = true\n',
        )

    def _main(self, *argv: str) -> tuple[int, str, str]:
        """Exit code, stdout and stderr of `image build <argv>`."""
        from tangier import cli

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", self.path, "image", "build", *argv])
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 1
        return rc, out.getvalue(), err.getvalue()

    def test_neither_push_nor_pull_is_an_error(self) -> None:
        rc, _, err = self._main("a", "--tag", "t", "--print")
        self.assertEqual(rc, 2)
        self.assertIn("one of the arguments --push --pull is required", err)

    def test_push_and_pull_together_is_an_error(self) -> None:
        rc, _, err = self._main("a", "--tag", "t", "--push", "--pull", "--print")
        self.assertEqual(rc, 2)
        self.assertIn("not allowed with argument", err)

    def test_load_is_pull(self) -> None:
        rc, out, _ = self._main("a", "--tag", "t", "--load", "--print")
        self.assertEqual(rc, 0)
        self.assertIn("--load", out)
        self.assertNotIn("--output", out)

    def test_tag_with_two_buckets_is_an_error(self) -> None:
        rc, _, err = self._main("a", "b", "--tag", "t", "--pull", "--print")
        self.assertEqual(rc, 2)
        self.assertIn("--tag names one image", err)

    def test_jobs_must_be_at_least_one(self) -> None:
        rc, _, err = self._main("a", "--tag", "t", "--pull", "--jobs", "0", "--print")
        self.assertEqual(rc, 2)
        self.assertIn("must be at least 1", err)


class TestUncommittedFiles(unittest.TestCase):
    """`git.uncommitted_files`: the files that can make a bucket dirty."""

    def setUp(self) -> None:
        self.root = make_git_repo(self, {"svc/main.py": "main = 1\n", ".gitignore": "*.log\n"})

    def _write(self, rel: str, content: str = "x\n") -> None:
        with open(os.path.join(self.root, rel), "w") as fh:
            _ = fh.write(content)

    def _files(self, cwd: str = "") -> list[str]:
        with contextlib.chdir(os.path.join(self.root, cwd)):
            return _uncommitted_files()

    def _git(self, *args: str) -> None:
        _ = subprocess.run(["git", "-C", self.root, *args], capture_output=True, check=True)

    def test_a_clean_tree_has_none(self) -> None:
        self.assertEqual(self._files(), [])

    def test_a_modified_tracked_file(self) -> None:
        self._write("svc/main.py", "main = 2\n")
        self.assertEqual(self._files(), ["svc/main.py"])

    def test_a_staged_change(self) -> None:
        self._write("svc/main.py", "main = 2\n")
        self._git("add", "svc/main.py")
        self.assertEqual(self._files(), ["svc/main.py"])

    def test_a_staged_rename_lists_both_paths(self) -> None:
        # The old path left the bucket, so it counts as much as the new one.
        self._git("mv", "svc/main.py", "svc/moved.py")
        self.assertEqual(sorted(self._files()), ["svc/main.py", "svc/moved.py"])

    def test_an_untracked_file(self) -> None:
        self._write("svc/new.py")
        self.assertEqual(self._files(), ["svc/new.py"])

    def test_an_ignored_file_is_left_out(self) -> None:
        self._write("svc/out.log")
        self.assertEqual(self._files(), [])

    def test_paths_are_repo_relative_from_a_subdirectory(self) -> None:
        self._write("svc/new.py")
        self.assertEqual(self._files("svc"), ["svc/new.py"])
