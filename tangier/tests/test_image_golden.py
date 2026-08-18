"""Golden test over a six-bucket monorepo shaped like askastro's.

Buckets, dockerfile layout, registry and compose template all mirror the real
consumer, so the rendered command lines and compose output can be eyeballed
against what its current scripts produce.
"""

import os
import unittest
import unittest.mock

from tangier import image as image_mod
from tangier.image import build_argv, image_ref, render_compose, resolve_secrets
from tangier.tests.support import make_config

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

REGISTRY = "registry.sminn.ee/tangerine"

# The six buckets, with the dockerfile paths that today live scattered across
# six workflow jobs as `code-path` + `dockerfile` inputs.
BUCKETS = {
    "smartypants": "service/smartypants/Dockerfile",
    "astronort-typesetter": "service/typesetter/Dockerfile",
    "astronort-lector": "service/lector/Dockerfile",
    "astronort-scheduler": "service/scheduler/Dockerfile",
    "astrochat": "frontend/Dockerfile",
    "astronort-fetcher-mcp": "service/fetcher-mcp/Dockerfile",
}


def _config():
    tables = {"registry": {"url": REGISTRY}}
    for bucket, dockerfile in BUCKETS.items():
        tables[bucket] = {"paths": [f"{os.path.dirname(dockerfile)}/**"], "sha": True}
        spec = {"dockerfile": dockerfile}
        if bucket == "astrochat":
            spec["secrets"] = ["sentry_auth_token"]
        tables[f"image.{bucket}"] = spec
    return make_config(**tables)


class TestGoldenBuildCommands(unittest.TestCase):
    def test_every_bucket_renders_the_expected_push_command(self) -> None:
        cfg = _config()
        for bucket, dockerfile in BUCKETS.items():
            ref = image_ref(cfg, bucket)
            argv = build_argv(cfg.images[bucket], ref, "abc1234567", push=True)
            self.assertEqual(
                " ".join(argv),
                "docker buildx build "
                f"--cache-from type=registry,ref={ref}:buildcache "
                f"--cache-to type=registry,ref={ref}:buildcache,mode=max,image-manifest=true,oci-mediatypes=true "
                f"--file {dockerfile} "
                f"--tag {ref}:abc1234567 --tag {ref}:latest "
                "--output type=registry "
                "--build-arg PACKAGE_VERSION=abc1234567 .",
            )

    def test_frontend_carries_the_sentry_secret_when_the_token_is_set(self) -> None:
        cfg = _config()
        spec = cfg.images["astrochat"]
        secrets = resolve_secrets(spec.secrets, env={"SENTRY_AUTH_TOKEN": "tok"})
        argv = build_argv(spec, image_ref(cfg, "astrochat"), "t", push=True, secrets=secrets)
        self.assertIn("id=sentry_auth_token,env=SENTRY_AUTH_TOKEN", argv)

    def test_frontend_omits_the_secret_when_the_token_is_unset(self) -> None:
        cfg = _config()
        spec = cfg.images["astrochat"]
        argv = build_argv(
            spec, image_ref(cfg, "astrochat"), "t", push=True, secrets=resolve_secrets(spec.secrets, env={})
        )
        self.assertNotIn("--secret", argv)

    def test_build_context_is_the_repo_root_for_every_bucket(self) -> None:
        # `code-path` was a dockerfile PREFIX, not a context. Modelling it as a
        # context would break every build.
        cfg = _config()
        for bucket in BUCKETS:
            self.assertEqual(build_argv(cfg.images[bucket], "r", "t")[-1], ".")


class TestGoldenCompose(unittest.TestCase):
    def test_renders_the_real_template_shape(self) -> None:
        cfg = _config()
        with open(os.path.join(FIXTURES, "compose.tmpl.yml")) as fh:
            template = fh.read()
        with unittest.mock.patch.object(image_mod, "sha_for_bucket", side_effect=lambda c, b, h="HEAD": f"sha-{b}"):
            out = render_compose(cfg, template)

        # Two services share one bucket's image; both are substituted.
        self.assertEqual(out.count(f"{REGISTRY}/smartypants:sha-smartypants"), 2)
        for bucket in ("astronort-typesetter", "astronort-lector", "astronort-fetcher-mcp", "astrochat"):
            self.assertIn(f"{REGISTRY}/{bucket}:sha-{bucket}", out)
        # No placeholder survives, and docker-compose's own ${VAR} syntax is untouched.
        self.assertNotIn("[[", out)
        self.assertIn("${ANTHROPIC_API_KEY}", out)
        self.assertIn("${LLM_MODEL_OVERRIDE:-}", out)

    def test_bucket_without_a_placeholder_is_harmless(self) -> None:
        # astronort-scheduler is a real bucket with no compose service.
        cfg = _config()
        with open(os.path.join(FIXTURES, "compose.tmpl.yml")) as fh:
            template = fh.read()
        with unittest.mock.patch.object(image_mod, "sha_for_bucket", side_effect=lambda c, b, h="HEAD": f"sha-{b}"):
            out = render_compose(cfg, template)
        # It appears only in the fixture's prose; no image ref is emitted for it.
        self.assertNotIn(f"{REGISTRY}/astronort-scheduler", out)


if __name__ == "__main__":
    _ = unittest.main()
