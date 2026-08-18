"""Config parser tests.

The `TestTomlReader` / `TestConfigWarnings` cases are ported from askastro's
`bin/changemap_test.py` unchanged in substance — they pin the tag grammar that
SHA parity depends on. The reserved-section cases are new.
"""

import unittest

from tangier.config import DEFAULT_SHA_EXCLUDE, ConfigError, read_config
from tangier.tests.support import parse_toml, parse_toml_stderr


class TestTomlReader(unittest.TestCase):
    # SPEC: changemap#toml-table-form
    # SPEC: changemap#items-field-projection
    # SPEC: changemap#sha-bucket-walk
    # SPEC: changemap#touched-opt-in
    # SPEC: changemap#subsystem-depends-key
    # SPEC: changemap#str-or-list-shorthand
    def test_parses_all_fields(self) -> None:
        cfg = parse_toml(
            "\n".join(
                [
                    "[a]",
                    'paths = "a/**"',
                    "sha = true",
                    "touched = true",
                    "[b]",
                    'paths = ["b/**"]',
                    'depends = "a"',
                    'sha = "a"',
                    'unittest_items = "py/b"',
                    'e2e_items = ["e/b1", "e/b2"]',
                ]
            )
        )
        self.assertEqual(cfg.paths, {"a": ["a/**"], "b": ["b/**"]})
        self.assertEqual(cfg.sha_bucket, {"a": "a", "b": "a"})
        self.assertEqual(cfg.touched, {"a"})
        self.assertEqual(cfg.depends, {"b": ["a"]})
        self.assertEqual(cfg.items, {"unittest": {"b": ["py/b"]}, "e2e": {"b": ["e/b1", "e/b2"]}})

    # SPEC: changemap#exclude-subtracts-from-paths
    # SPEC: changemap#str-or-list-shorthand
    def test_parses_exclude_field(self) -> None:
        cfg = parse_toml(
            "\n".join(
                [
                    "[a]",
                    'paths = "a/**"',
                    "sha = true",
                    'exclude = "a/gen.ts"',
                    "[b]",
                    'paths = "b/**"',
                    "sha = true",
                    'exclude = ["b/one.ts", "b/two.ts"]',
                ]
            )
        )
        # Bare string and list both normalise to a list; tags without the field are absent.
        self.assertEqual(cfg.exclude, {"a": ["a/gen.ts"], "b": ["b/one.ts", "b/two.ts"]})

    # SPEC: changemap#file-set-field
    def test_exclude_on_files_table_raises(self) -> None:
        # A file-set table's globs are already an explicit allowlist — nothing to subtract from.
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[unittest-files]\nfiles = true\npaths = ["a/**"]\nexclude = "a/gen.py"\n')
        self.assertIn("exclude", str(ctx.exception))

    # SPEC: changemap#unknown-field-raises
    def test_unknown_field_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[a]\npaths = "a/**"\nfoo = "bar"\n')
        self.assertIn("foo", str(ctx.exception))

    # SPEC: changemap#unknown-dependency-raises
    def test_unknown_dependency_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[a]\npaths = "a/**"\ndepends = "nonexistent"\n')
        self.assertIn("nonexistent", str(ctx.exception))

    # SPEC: changemap#cycle-raises
    def test_cycle_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[a]\npaths = "a/**"\ndepends = "b"\n[b]\npaths = "b/**"\ndepends = "a"\n')
        self.assertIn("cycle", str(ctx.exception).lower())

    def test_paths_optional_for_aggregator_tags(self) -> None:
        cfg = parse_toml('[a]\npaths = "a/**"\n[agg]\ndepends = "a"\ntouched = true\n')
        self.assertEqual(cfg.paths["agg"], [])
        self.assertEqual(cfg.depends["agg"], ["a"])

    # SPEC: changemap#file-set-field
    def test_files_table_registered_as_projection_only(self) -> None:
        cfg = parse_toml(
            "\n".join(
                [
                    "[a]",
                    'paths = "a/**"',
                    "sha = true",
                    "[unittest-files]",
                    "files = true",
                    'paths = ["a/**/*_test.py"]',
                ]
            )
        )
        self.assertEqual(cfg.file_sets, {"unittest-files": ["a/**/*_test.py"]})
        # Projection-only: never enters the tag-match map.
        self.assertNotIn("unittest-files", cfg.paths)

    # SPEC: changemap#file-set-field
    def test_files_table_must_be_true(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[x]\nfiles = "yes"\npaths = ["a/**"]\n')
        self.assertIn("files", str(ctx.exception))

    def test_non_table_top_level_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('a = "not a table"\n')
        self.assertIn("must be a table", str(ctx.exception))


class TestConfigWarnings(unittest.TestCase):
    # SPEC: changemap#warn-tag-without-input
    def test_warns_when_tag_has_no_paths_and_no_depends(self) -> None:
        stderr = parse_toml_stderr("[empty]\ntouched = true\n")
        self.assertIn("empty", stderr)
        self.assertIn("neither", stderr)

    # SPEC: changemap#warn-tag-without-output
    def test_warns_when_tag_has_no_outputs(self) -> None:
        stderr = parse_toml_stderr('[lonely]\npaths = "x/**"\n')
        self.assertIn("lonely", stderr)
        self.assertIn("no sha", stderr)


class TestNestedTags(unittest.TestCase):
    # SPEC: changemap#tags-table-nesting
    def test_tags_table_declares_tags(self) -> None:
        cfg = parse_toml('[tags.a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.paths, {"a": ["a/**"]})
        self.assertEqual(cfg.sha_bucket, {"a": "a"})

    def test_bare_and_nested_tags_mix(self) -> None:
        cfg = parse_toml('[a]\npaths = "a/**"\nsha = true\n[tags.b]\npaths = "b/**"\nsha = true\n')
        self.assertEqual(set(cfg.paths), {"a", "b"})

    def test_depends_resolves_across_the_bare_nested_boundary(self) -> None:
        # Dependency validation is post-parse, so a nested tag may depend on a
        # bare one declared later in the file (and vice versa).
        cfg = parse_toml('[tags.b]\npaths = "b/**"\ndepends = "a"\nsha = true\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.depends["b"], ["a"])

    def test_duplicate_tag_in_both_forms_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[a]\npaths = "a/**"\n[tags.a]\npaths = "other/**"\n')
        self.assertIn("both", str(ctx.exception))

    def test_reserved_name_usable_as_a_tag_when_nested(self) -> None:
        # `[sha]` is the reserved settings section; a tag genuinely called `sha`
        # is written `[tags.sha]` and the two coexist.
        cfg = parse_toml('[sha]\nexclude = []\n[tags.sha]\npaths = "s/**"\nsha = true\n')
        self.assertEqual(cfg.paths["sha"], ["s/**"])
        self.assertEqual(cfg.sha.exclude, [])

    def test_malformed_reserved_section_error_names_the_nesting_escape(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('sha = "not a table"\n')
        self.assertIn("[tags.sha]", str(ctx.exception))


class TestShaSettings(unittest.TestCase):
    # SPEC: changemap#sha-exclude
    def test_default_excludes_readmes(self) -> None:
        cfg = parse_toml('[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.sha.exclude, DEFAULT_SHA_EXCLUDE)

    def test_explicit_empty_exclude_disables_filtering(self) -> None:
        # An explicit `exclude = []` must stay distinguishable from an absent
        # section, which gets the default.
        cfg = parse_toml('[sha]\nexclude = []\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.sha.exclude, [])

    def test_custom_exclude_replaces_the_default(self) -> None:
        cfg = parse_toml('[sha]\nexclude = ["**/*.md", "docs/**"]\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.sha.exclude, ["**/*.md", "docs/**"])

    def test_string_shorthand_normalises_to_list(self) -> None:
        cfg = parse_toml('[sha]\nexclude = "**/*.md"\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.sha.exclude, ["**/*.md"])

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[sha]\nnope = 1\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIn("nope", str(ctx.exception))


class TestRunners(unittest.TestCase):
    # SPEC: changemap#runners-section
    def test_parses_runners(self) -> None:
        cfg = parse_toml(
            "\n".join(
                [
                    "[runners]",
                    'unittest = { cmd = "bin/test", files = "unittest-files" }',
                    'e2e = { cmd = "bin/e2e-test" }',
                    "[unittest-files]",
                    "files = true",
                    'paths = ["a/**/*_test.py"]',
                    "[a]",
                    'paths = "a/**"',
                    "sha = true",
                ]
            )
        )
        self.assertEqual(cfg.runners["unittest"].cmd, "bin/test")
        self.assertEqual(cfg.runners["unittest"].files, "unittest-files")
        # A runner with no `files` never gets a --files suffix.
        self.assertIsNone(cfg.runners["e2e"].files)

    def test_runner_requires_cmd(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[runners]\nunittest = { files = "x" }\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIn("cmd", str(ctx.exception))

    def test_runner_rejects_unknown_key(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[runners]\nunittest = { cmd = "x", nope = "y" }\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIn("nope", str(ctx.exception))


class TestRegistryAndImage(unittest.TestCase):
    def test_parses_registry_url_and_strips_trailing_slash(self) -> None:
        cfg = parse_toml('[registry]\nurl = "reg.example/ns/"\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.registry, "reg.example/ns")

    def test_parses_image_defaults(self) -> None:
        cfg = parse_toml('[image.a]\ndockerfile = "a/Dockerfile"\n[a]\npaths = "a/**"\nsha = true\n')
        spec = cfg.images["a"]
        self.assertEqual(spec.dockerfile, "a/Dockerfile")
        self.assertEqual(spec.context, ".")
        self.assertTrue(spec.cache)
        self.assertEqual(spec.secrets, [])

    def test_image_requires_dockerfile(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[image.a]\ncontext = "."\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIn("dockerfile", str(ctx.exception))

    def test_image_for_unknown_bucket_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[image.nope]\ndockerfile = "d"\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIn("nope", str(ctx.exception))


class TestK8sAndDeploy(unittest.TestCase):
    def test_deployments_default_to_the_bucket_name(self) -> None:
        cfg = parse_toml('[k8s.a]\ncontainer = "worker"\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.k8s["a"].deployments, ["a"])
        self.assertEqual(cfg.k8s["a"].container, "worker")

    def test_version_var_is_derived_from_the_bucket_name(self) -> None:
        cfg = parse_toml('[k8s.my-svc]\ndeployments = ["x"]\n[my-svc]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.k8s["my-svc"].version_var, "MY_SVC_VERSION")

    def test_one_bucket_can_drive_several_deployments(self) -> None:
        cfg = parse_toml('[k8s.a]\ndeployments = ["a", "a-worker"]\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.k8s["a"].deployments, ["a", "a-worker"])

    def test_k8s_for_unknown_bucket_raises(self) -> None:
        # A typo here would otherwise be silent deploy breakage: the version
        # variable it derives would never be substituted into any manifest.
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[k8s.nope]\ndeployments = ["x"]\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIn("nope", str(ctx.exception))

    def test_parses_deploy_envs_and_rollout(self) -> None:
        cfg = parse_toml(
            "\n".join(
                [
                    "[deploy.uat]",
                    'namespace = "ns-uat"',
                    'overlay = "k8s/overlays/uat"',
                    "migration_timeout = 600",
                    'migration_job = "mig-${A_VERSION}"',
                    'migration_version_bucket = "a"',
                    "[deploy.rollout]",
                    "max_wait = 900",
                    "crash_threshold = 5",
                    "[a]",
                    'paths = "a/**"',
                    "sha = true",
                ]
            )
        )
        env = cfg.deploy_envs["uat"]
        self.assertEqual(env.namespace, "ns-uat")
        self.assertEqual(env.migration_version_bucket, "a")
        self.assertEqual(cfg.rollout.max_wait, 900)
        self.assertEqual(cfg.rollout.crash_threshold, 5)
        # Unset rollout keys keep their defaults.
        self.assertEqual(cfg.rollout.poll_interval, 10)
        # `rollout` is not an environment.
        self.assertNotIn("rollout", cfg.deploy_envs)

    def test_deploy_env_requires_namespace_and_overlay(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[deploy.uat]\nnamespace = "ns"\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIn("overlay", str(ctx.exception))


class TestReadConfigErrors(unittest.TestCase):
    def test_missing_file_raises_filenotfound(self) -> None:
        with self.assertRaises(FileNotFoundError):
            _ = read_config("/nonexistent/pipeline.toml")


if __name__ == "__main__":
    _ = unittest.main()


class TestRunnerFilesValidation(unittest.TestCase):
    # A typo here is otherwise silent: explain drops the --files suffix and CI
    # goes green having tested the wrong thing.
    def test_runner_files_naming_no_file_set_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml(
                "\n".join(
                    [
                        "[runners]",
                        'unittest = { cmd = "bin/test", files = "unittest-fils" }',
                        "[unittest-files]",
                        "files = true",
                        'paths = ["a/**/*_test.py"]',
                        "[a]",
                        'paths = "a/**"',
                        "sha = true",
                    ]
                )
            )
        self.assertIn("unittest-fils", str(ctx.exception))

    def test_runner_without_files_is_fine(self) -> None:
        cfg = parse_toml('[runners]\ne2e = { cmd = "bin/e2e-test" }\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIsNone(cfg.runners["e2e"].files)


class TestShaGlobWarning(unittest.TestCase):
    # `git ls-tree` takes literal prefixes, not globs, so a starred path
    # contributes nothing to the hash and the image stops rebuilding.
    def test_warns_when_a_sha_bucket_tag_uses_a_star_glob(self) -> None:
        stderr = parse_toml_stderr('[a]\npaths = "src/*.py"\nsha = true\n')
        self.assertIn("src/*.py", stderr)
        self.assertIn("contributes nothing", stderr)

    def test_no_warning_for_literal_or_dir_star_star_paths(self) -> None:
        stderr = parse_toml_stderr('[a]\npaths = ["src/**", "Makefile"]\nsha = true\n')
        self.assertNotIn("contributes nothing", stderr)

    def test_warns_for_a_dependency_that_feeds_a_bucket(self) -> None:
        stderr = parse_toml_stderr('[lib]\npaths = "lib/*.py"\n[a]\npaths = "a/**"\ndepends = "lib"\nsha = true\n')
        self.assertIn("lib/*.py", stderr)

    def test_no_warning_for_a_tag_outside_every_bucket(self) -> None:
        # Tag matching uses the full glob engine, so a starred path is only a
        # problem when it feeds a content hash.
        stderr = parse_toml_stderr('[docs]\npaths = "**/*.md"\ntouched = true\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertNotIn("contributes nothing", stderr)


class TestAfterHook(unittest.TestCase):
    def test_string_shorthand_splits_and_defaults_to_non_fatal(self) -> None:
        cfg = parse_toml('[deploy]\nafter = "bin/sentry-release ${ENV}"\n')
        self.assertEqual(cfg.after.argv, ["bin/sentry-release", "${ENV}"])
        self.assertFalse(cfg.after.fatal)

    def test_explicit_table_form(self) -> None:
        cfg = parse_toml('[deploy.after]\ncmd = "bin/x"\nfatal = true\n')
        self.assertEqual(cfg.after.argv, ["bin/x"])
        self.assertTrue(cfg.after.fatal)

    def test_absent_means_no_hook(self) -> None:
        self.assertIsNone(parse_toml('[a]\npaths = "a/**"\n').after)

    def test_quoted_argument_survives_splitting(self) -> None:
        cfg = parse_toml("[deploy]\nafter = \"bin/notify 'a b'\"\n")
        self.assertEqual(cfg.after.argv, ["bin/notify", "a b"])

    def test_the_string_form_is_not_reported_as_a_malformed_table(self) -> None:
        # `_require_table` would say "`[deploy.after]` ... must be a table, got
        # str", which is both wrong and unactionable. Parsing `after` first is
        # what prevents that.
        cfg = parse_toml('[deploy]\nafter = "bin/x"\n')
        self.assertEqual(cfg.after.argv, ["bin/x"])

    def test_shell_operator_is_rejected_at_parse_time(self) -> None:
        for op in ("&&", "||", "|", ";", ">", "&"):
            with self.subTest(op=op):
                with self.assertRaises(ConfigError) as ctx:
                    _ = parse_toml(f'[deploy]\nafter = "bin/x {op} bin/y"\n')
                message = str(ctx.exception)
                self.assertIn("without a shell", message)
                self.assertIn("script", message)

    def test_command_substitution_is_rejected(self) -> None:
        # Neither form can ever be substituted, so both mean "run a shell".
        for marker in ("$(whoami)", "`whoami`"):
            with self.subTest(marker=marker), self.assertRaises(ConfigError):
                _ = parse_toml(f'[deploy]\nafter = "bin/x {marker}"\n')

    def test_an_operator_inside_an_argument_is_not_a_shell_operator(self) -> None:
        # `&` here is a character in a value, not a backgrounding operator. No
        # shell ever sees it, so rejecting it would refuse a legitimate hook.
        cfg = parse_toml('[deploy]\nafter = "bin/notify --to=a&b"\n')
        self.assertEqual(cfg.after.argv, ["bin/notify", "--to=a&b"])

    def test_the_substitution_syntax_is_not_an_operator(self) -> None:
        cfg = parse_toml('[deploy]\nafter = "bin/x ${ENV}"\n')
        self.assertEqual(cfg.after.argv, ["bin/x", "${ENV}"])

    def test_empty_command_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            _ = parse_toml('[deploy]\nafter = ""\n')

    def test_table_without_cmd_is_rejected(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml("[deploy.after]\nfatal = true\n")
        self.assertIn("requires `cmd`", str(ctx.exception))

    def test_after_does_not_become_a_deploy_environment(self) -> None:
        cfg = parse_toml('[deploy]\nafter = "bin/x"\n[deploy.uat]\nnamespace = "ns"\noverlay = "k8s/uat"\n')
        self.assertEqual(sorted(cfg.deploy_envs), ["uat"])


class TestTailnet(unittest.TestCase):
    def test_env_tables_and_the_default_operator(self) -> None:
        cfg = parse_toml('[tailnet.uat]\ntag = "tag:astronort-uat-deploy"\n')
        self.assertEqual(cfg.tailnet.operator, "tailscale-operator")
        self.assertEqual(cfg.tailnet.envs["uat"].tag, "tag:astronort-uat-deploy")
        self.assertEqual(cfg.tailnet.envs["uat"].name, "uat")

    def test_operator_is_overridable(self) -> None:
        cfg = parse_toml('[tailnet]\noperator = "ts-op"\n\n[tailnet.uat]\ntag = "tag:x"\n')
        self.assertEqual(cfg.tailnet.operator, "ts-op")

    def test_absent_section_gives_the_default(self) -> None:
        cfg = parse_toml('[a]\npaths = "a/**"\nsha = true\n')
        self.assertEqual(cfg.tailnet.operator, "tailscale-operator")
        self.assertEqual(cfg.tailnet.envs, {})

    def test_env_without_a_tag_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml("[tailnet.uat]\n")
        self.assertIn("`[tailnet.uat]` requires `tag`", str(ctx.exception))

    def test_unknown_scalar_field_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[tailnet]\noperater = "typo"\n')
        self.assertIn("operater", str(ctx.exception))

    def test_unknown_field_on_an_env_raises(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[tailnet.uat]\ntag = "tag:x"\naudience = "y"\n')
        self.assertIn("audience", str(ctx.exception))

    def test_tailnet_is_reserved_so_a_bare_tag_of_that_name_must_nest(self) -> None:
        # The escape hatch, which is what makes reserving the name non-breaking.
        cfg = parse_toml('[tags.tailnet]\npaths = "net/**"\nsha = true\n')
        self.assertIn("tailnet", cfg.paths)
        self.assertEqual(cfg.tailnet.envs, {})

    def test_warns_when_an_env_names_no_deploy_environment(self) -> None:
        stderr = parse_toml_stderr(
            '[a]\npaths = "a/**"\nsha = true\n'
            '[deploy.uat]\nnamespace = "ns"\noverlay = "k8s/uat"\n'
            '[tailnet.staging]\ntag = "tag:x"\n'
        )
        self.assertIn("`[tailnet.staging]` names no deploy environment", stderr)

    def test_no_warning_when_the_repo_declares_no_deploy_environments(self) -> None:
        # k8s-cluster: helm and kubectl, no images, no deploy envs, tailnet only.
        stderr = parse_toml_stderr('[tailnet.prod]\ntag = "tag:k8s-cluster-deploy"\n')
        self.assertNotIn("names no deploy environment", stderr)


class TestErrorMessageQuality(unittest.TestCase):
    def test_nested_reserved_section_error_omits_the_tags_advice(self) -> None:
        # `[tags.deploy.uat]` is not a valid tag declaration, so suggesting it
        # would send the author somewhere worse.
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[deploy]\nuat = 1\n[a]\npaths = "a/**"\nsha = true\n')
        self.assertIn("deploy.uat", str(ctx.exception))
        self.assertNotIn("tags.deploy.uat", str(ctx.exception))

    def test_field_directly_under_tags_names_the_real_problem(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            _ = parse_toml('[tags]\npaths = "a/**"\n')
        self.assertIn("[tags.paths]", str(ctx.exception))
        self.assertIn("tag names", str(ctx.exception))
