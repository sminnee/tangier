"""Deploy tests: the kubectl call sequence against a recording runner.

No test touches a cluster, and none imports `time` or `subprocess`. The poll
loop's clock advances off `runner.sleep`, so a 600s timeout costs microseconds.
"""

import argparse
import contextlib
import io
import json
import unittest
import unittest.mock

from tangier import deploy as deploy_mod
from tangier.commands import deploy_cmds
from tangier.deploy import (
    DeployError,
    deployment_targets,
    envsubst_allowlist,
    find_crashing_pods,
    migration_job_name,
    resolve_env,
)
from tangier.runner import Result
from tangier.tests.support import RecordingRunner, make_config

CONFIG_TABLES = {
    "registry": {"url": "registry.example/ns"},
    "deploy.uat": {
        "namespace": "ns-uat",
        "overlay": "k8s/overlays/uat",
        "migration_timeout": 600,
        "migration_job": "core-migrate-${CORE_VERSION}",
        "migration_version_bucket": "core",
    },
    "deploy.prod": {
        "namespace": "ns-prod",
        "overlay": "k8s/overlays/prod",
        "migration_timeout": 3600,
        "migration_job": "core-migrate-${CORE_VERSION}",
        "migration_version_bucket": "core",
    },
    "deploy.rollout": {"max_wait": 600, "poll_interval": 10, "crash_threshold": 3},
    "k8s.core": {"deployments": ["core", "core-worker"], "container": "server"},
    "k8s.scanner": {"deployments": ["scanner"], "container": "worker"},
    "core": {"paths": ["service/core/**"], "sha": True},
    "scanner": {"paths": ["service/scanner/**"], "sha": True},
}


def _cfg(**overrides):
    tables = {**CONFIG_TABLES, **overrides}
    return make_config(**tables)


def _args(runner, env="uat", **kw) -> argparse.Namespace:
    base = dict(env=env, head="HEAD", runner=runner, render=None, versions=None, compare_env=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _pods(*specs) -> str:
    """Build a `kubectl get pods -o json` payload."""
    items = []
    for pod, containers in specs:
        items.append(
            {
                "metadata": {"name": pod},
                "status": {
                    "containerStatuses": [
                        {"name": n, "state": {"waiting": {"reason": r}} if r else {}, "restartCount": c}
                        for n, r, c in containers
                    ]
                },
            }
        )
    return json.dumps({"items": items})


def _run(cfg, runner, **kw) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), unittest.mock.patch.object(deploy_mod, "sha_for_bucket", return_value="v2"):
        rc = deploy_cmds.cmd_deploy(cfg, _args(runner, **kw))
    return rc, buf.getvalue()


# A non-empty render: `_render` rejects empty output, because a failing
# `kubectl kustomize` still exits 0 through `envsubst` and empty manifests
# would otherwise be applied and reported as a successful deploy.
MANIFESTS = "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: core\n"

# Canned responses for a fully successful deploy.
OK_RESPONSES = {
    ("envsubst",): Result(0, MANIFESTS),
    ("kubectl", "wait"): Result(0),
    ("kubectl", "rollout", "status"): Result(0),
    ("kubectl", "apply"): Result(0),
}


class TestResolveEnv(unittest.TestCase):
    def test_unknown_environment_is_an_error(self) -> None:
        # The bash treated any argument but `prod` as uat, so a one-character
        # workflow typo silently retargeted an environment AND succeeded.
        with self.assertRaises(DeployError) as ctx:
            _ = resolve_env(_cfg(), "porduction")
        self.assertIn("porduction", str(ctx.exception))
        self.assertIn("uat", str(ctx.exception))

    def test_known_environments_resolve(self) -> None:
        self.assertEqual(resolve_env(_cfg(), "prod").namespace, "ns-prod")
        self.assertEqual(resolve_env(_cfg(), "uat").migration_timeout, 600)


class TestDerivation(unittest.TestCase):
    def test_allowlist_is_derived_and_sorted(self) -> None:
        # Derived from config rather than hand-maintained in two places.
        self.assertEqual(envsubst_allowlist(_cfg()), "$CORE_VERSION $SCANNER_VERSION")

    def test_deployment_targets_expand_one_to_many(self) -> None:
        self.assertEqual(
            deployment_targets(_cfg()),
            ["deployment/core", "deployment/core-worker", "deployment/scanner"],
        )

    def test_migration_job_name_embeds_the_version(self) -> None:
        env = resolve_env(_cfg(), "uat")
        self.assertEqual(migration_job_name(env, {"CORE_VERSION": "v2"}), "core-migrate-v2")

    def test_env_without_a_migration_job_returns_none(self) -> None:
        cfg = _cfg(**{"deploy.uat": {"namespace": "ns", "overlay": "o"}})
        self.assertIsNone(migration_job_name(resolve_env(cfg, "uat"), {}))


class TestCrashDetection(unittest.TestCase):
    def test_flags_a_crashlooping_container_past_the_threshold(self) -> None:
        found = find_crashing_pods(_pods(("pod-a", [("server", "CrashLoopBackOff", 3)])), 3)
        self.assertEqual([(p.pod, p.restarts) for p in found], [("pod-a", 3)])

    def test_ignores_restarts_below_the_threshold(self) -> None:
        self.assertEqual(find_crashing_pods(_pods(("pod-a", [("server", "CrashLoopBackOff", 2)])), 3), [])

    def test_reason_and_count_must_be_the_same_container(self) -> None:
        # The jsonpath|grep|awk pipeline scanned fields by offset, so a healthy
        # container's high restart count could satisfy another's crash reason.
        payload = _pods(("pod-a", [("server", "CrashLoopBackOff", 1), ("sidecar", "", 9)]))
        self.assertEqual(find_crashing_pods(payload, 3), [])

    def test_empty_waiting_reason_does_not_shift_parsing(self) -> None:
        payload = _pods(("pod-a", [("init", "", 0), ("server", "OOMKilled", 5)]))
        found = find_crashing_pods(payload, 3)
        self.assertEqual([p.container for p in found], ["server"])

    def test_scans_init_containers_too(self) -> None:
        payload = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "pod-a"},
                        "status": {
                            "initContainerStatuses": [
                                {
                                    "name": "migrate",
                                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                                    "restartCount": 4,
                                }
                            ]
                        },
                    }
                ]
            }
        )
        self.assertEqual([p.container for p in find_crashing_pods(payload, 3)], ["migrate"])

    def test_healthy_and_malformed_payloads_are_quiet(self) -> None:
        self.assertEqual(find_crashing_pods(_pods(("pod-a", [("server", "", 0)])), 3), [])
        self.assertEqual(find_crashing_pods("", 3), [])
        self.assertEqual(find_crashing_pods("not json", 3), [])


class TestDeploySequence(unittest.TestCase):
    def test_two_pass_apply_from_one_render(self) -> None:
        runner = RecordingRunner(OK_RESPONSES)
        rc, _out = _run(_cfg(), runner)
        self.assertEqual(rc, 0)

        # Exactly one kustomize render — that is what makes pass 2 byte-identical
        # to pass 1, so the Job re-apply is a genuine no-op.
        kustomize = [c for c in runner.calls if c[:2] == ["kubectl", "kustomize"]]
        self.assertEqual(len(kustomize), 1)

        applies = [c for c in runner.calls if c[:2] == ["kubectl", "apply"]]
        self.assertEqual(len(applies), 2)
        # Pass 1 is label-scoped to the migration Job; pass 2 is everything.
        self.assertIn("migrate-step=pre", applies[0])
        self.assertNotIn("-l", applies[1])
        # Both passes are fed the same manifests, via stdin.
        applied_stdin = [s for s in runner.stdins if s is not None]
        self.assertEqual(applied_stdin[0], applied_stdin[1])

    def test_ordering_prior_read_then_apply_then_wait_then_rollout(self) -> None:
        runner = RecordingRunner(OK_RESPONSES)
        _rc, _out = _run(_cfg(), runner)
        seq = [" ".join(c[:3]) for c in runner.calls]
        get_prior = seq.index("kubectl get deployment")
        first_apply = seq.index("kubectl apply -f")
        wait = seq.index("kubectl wait --for=condition=complete")
        rollout = seq.index("kubectl rollout status")
        # The prior tag must be read BEFORE anything is applied.
        self.assertLess(get_prior, first_apply)
        self.assertLess(first_apply, wait)
        self.assertLess(wait, rollout)

    def test_migration_failure_does_not_roll_back(self) -> None:
        # Nothing has touched the Deployments yet — old pods still serve.
        runner = RecordingRunner({**OK_RESPONSES, ("kubectl", "wait"): Result(1)})
        rc, out = _run(_cfg(), runner)
        self.assertEqual(rc, 1)
        self.assertIn("Migration failed", out)
        self.assertEqual([c for c in runner.calls if c[:3] == ["kubectl", "rollout", "undo"]], [])
        # Only pass 1 ran.
        self.assertEqual(len([c for c in runner.calls if c[:2] == ["kubectl", "apply"]]), 1)
        # Logs are printed to aid diagnosis.
        self.assertTrue(any(c[:2] == ["kubectl", "logs"] and "--tail=200" in c for c in runner.calls))

    def test_apply_failure_is_logged_and_ignored(self) -> None:
        # No implicit abort: the bash had no `set -e`, so a partial apply
        # proceeds and is caught at rollout-status.
        runner = RecordingRunner({**OK_RESPONSES, ("kubectl", "apply"): Result(1)})
        with contextlib.redirect_stderr(io.StringIO()):
            rc, _out = _run(_cfg(), runner)
        self.assertEqual(rc, 0)
        self.assertEqual(len([c for c in runner.calls if c[:2] == ["kubectl", "apply"]]), 2)

    def test_missing_kubectl_exits_before_touching_anything(self) -> None:
        runner = RecordingRunner(OK_RESPONSES)
        runner.missing.add("kubectl")
        with self.assertRaises(DeployError):
            _ = _run(_cfg(), runner)
        self.assertEqual(runner.calls, [])


class TestRolloutAndRollback(unittest.TestCase):
    def test_rollout_retries_then_succeeds(self) -> None:
        runner = RecordingRunner({**OK_RESPONSES, ("kubectl", "rollout", "status"): [Result(1), Result(1), Result(0)]})
        rc, out = _run(_cfg(), runner)
        self.assertEqual(rc, 0)
        self.assertIn("rolled out successfully", out)
        self.assertEqual(runner.slept, [10, 10])

    def test_timeout_sleeps_the_full_budget_without_real_waiting(self) -> None:
        runner = RecordingRunner({**OK_RESPONSES, ("kubectl", "rollout", "status"): Result(1)})
        rc, out = _run(_cfg(), runner)
        self.assertEqual(rc, 1)
        self.assertIn("timed out after 600s", out)
        self.assertEqual(sum(runner.slept), 600)

    def test_crash_detection_breaks_immediately_and_rolls_back(self) -> None:
        runner = RecordingRunner(
            {
                **OK_RESPONSES,
                ("kubectl", "rollout", "status"): Result(1),
                ("kubectl", "get", "pods"): Result(0, _pods(("pod-a", [("server", "CrashLoopBackOff", 3)]))),
            }
        )
        rc, out = _run(_cfg(), runner)
        self.assertEqual(rc, 1)
        self.assertIn("Crash-looping pods detected", out)
        # Breaks out at once rather than burning the full timeout.
        self.assertEqual(runner.slept, [])
        self.assertIn("Rolling back", out)

    def test_rollback_undoes_each_deployment_in_order(self) -> None:
        runner = RecordingRunner({**OK_RESPONSES, ("kubectl", "rollout", "status"): Result(1)})
        _rc, _out = _run(_cfg(), runner)
        undos = [c for c in runner.calls if c[:3] == ["kubectl", "rollout", "undo"]]
        self.assertEqual(
            [c[3] for c in undos],
            ["deployment/core", "deployment/core-worker", "deployment/scanner"],
        )

    def test_prior_tag_migration_runs_when_prior_differs(self) -> None:
        runner = RecordingRunner(
            {
                **OK_RESPONSES,
                ("kubectl", "rollout", "status"): Result(1),
                ("kubectl", "get", "deployment"): Result(0, "registry.example/ns/core:v1"),
            }
        )
        _rc, out = _run(_cfg(), runner)
        self.assertIn("core-migrate-v1", out)
        self.assertTrue(any(c[:3] == ["kubectl", "delete", "job"] and "core-migrate-v1" in c for c in runner.calls))
        # A second render happens for the prior-tag manifests.
        self.assertEqual(len([c for c in runner.calls if c[:2] == ["kubectl", "kustomize"]]), 2)

    def test_prior_tag_migration_skipped_when_unchanged(self) -> None:
        runner = RecordingRunner(
            {
                **OK_RESPONSES,
                ("kubectl", "rollout", "status"): Result(1),
                # Prior tag equals the tag being deployed.
                ("kubectl", "get", "deployment"): Result(0, "registry.example/ns/core:v2"),
            }
        )
        _rc, _out = _run(_cfg(), runner)
        self.assertEqual([c for c in runner.calls if c[:3] == ["kubectl", "delete", "job"]], [])

    def test_prior_tag_migration_skipped_on_first_ever_deploy(self) -> None:
        runner = RecordingRunner(
            {
                **OK_RESPONSES,
                ("kubectl", "rollout", "status"): Result(1),
                ("kubectl", "get", "deployment"): Result(1, ""),
            }
        )
        _rc, _out = _run(_cfg(), runner)
        self.assertEqual([c for c in runner.calls if c[:3] == ["kubectl", "delete", "job"]], [])

    def test_prior_version_parsing_survives_a_registry_port(self) -> None:
        runner = RecordingRunner(
            {
                **OK_RESPONSES,
                ("kubectl", "rollout", "status"): Result(1),
                ("kubectl", "get", "deployment"): Result(0, "registry:5000/ns/core:v1"),
            }
        )
        _rc, out = _run(_cfg(), runner)
        # Not `5000`, which is what the old awk returned.
        self.assertIn("core-migrate-v1", out)

    def test_rollback_does_not_mutate_the_outer_versions(self) -> None:
        # The prior version must not leak into the manifests applied for any
        # other bucket — the bash isolated this with a subshell.
        runner = RecordingRunner(
            {
                **OK_RESPONSES,
                ("kubectl", "rollout", "status"): Result(1),
                ("kubectl", "get", "deployment"): Result(0, "registry.example/ns/core:v1"),
            }
        )
        with unittest.mock.patch.object(deploy_mod, "sha_for_bucket", return_value="v2"):
            vs = deploy_mod.versions(_cfg())
            before = dict(vs)
            _rc, _out = _run(_cfg(), runner)
            self.assertEqual(vs, before)

    def test_rollback_migration_timeout_comes_from_config(self) -> None:
        cfg = _cfg(
            **{
                "deploy.rollout": {
                    "max_wait": 600,
                    "poll_interval": 10,
                    "crash_threshold": 3,
                    "rollback_migration_timeout": 1800,
                }
            }
        )
        runner = RecordingRunner(
            {
                **OK_RESPONSES,
                ("kubectl", "rollout", "status"): Result(1),
                ("kubectl", "get", "deployment"): Result(0, "registry.example/ns/core:v1"),
            }
        )
        _rc, _out = _run(cfg, runner)
        waits = [" ".join(c) for c in runner.calls if c[:2] == ["kubectl", "wait"]]
        self.assertTrue(any("--timeout=1800s" in w for w in waits))


class TestReadOnlyModes(unittest.TestCase):
    def test_render_prints_manifests_and_runs_no_apply(self) -> None:
        # `pipe` reports the LAST stage's result, shell-style, so the response
        # is keyed on envsubst rather than kustomize.
        runner = RecordingRunner({("envsubst",): Result(0, "kind: Deployment\n")})
        buf = io.StringIO()
        with (
            contextlib.redirect_stdout(buf),
            unittest.mock.patch.object(deploy_mod, "sha_for_bucket", return_value="v2"),
        ):
            rc = deploy_cmds._dispatch(_cfg(), _args(runner, env=None, render="uat"))
        self.assertEqual(rc, 0)
        self.assertIn("kind: Deployment", buf.getvalue())
        self.assertEqual([c for c in runner.calls if c[:2] == ["kubectl", "apply"]], [])

    def test_versions_prints_the_env_var_lines(self) -> None:
        buf = io.StringIO()
        with (
            contextlib.redirect_stdout(buf),
            unittest.mock.patch.object(deploy_mod, "sha_for_bucket", return_value="v2"),
        ):
            rc = deploy_cmds._dispatch(_cfg(), _args(RecordingRunner(), env=None, versions="uat"))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().splitlines(), ["CORE_VERSION=v2", "SCANNER_VERSION=v2"])

    def test_compare_env_covers_every_configured_bucket(self) -> None:
        # Driven by [k8s.*], so no bucket is silently absent from the table.
        payload = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "core"},
                        "spec": {"template": {"spec": {"containers": [{"image": "r/ns/core:v1"}]}}},
                    },
                    {
                        "metadata": {"name": "scanner"},
                        "spec": {"template": {"spec": {"containers": [{"image": "r/ns/scanner:v2"}]}}},
                    },
                ]
            }
        )
        runner = RecordingRunner({("kubectl", "get", "deployment"): Result(0, payload)})
        buf = io.StringIO()
        with (
            contextlib.redirect_stdout(buf),
            unittest.mock.patch.object(deploy_mod, "sha_for_bucket", return_value="v2"),
            unittest.mock.patch("tangier.changemap.sha_for_bucket", return_value="v2"),
        ):
            rc = deploy_cmds._dispatch(_cfg(), _args(runner, env=None, compare_env="uat"))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # core changed v1 -> v2; scanner is unchanged so renders bare.
        self.assertIn("| core | v1 -> **v2** |", out)
        self.assertIn("| scanner | v2 |", out)

    def test_no_env_and_no_mode_exits_2(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            rc = deploy_cmds._dispatch(_cfg(), _args(RecordingRunner(), env=None))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    _ = unittest.main()


class TestRenderGuards(unittest.TestCase):
    def test_empty_render_is_an_error_not_a_successful_deploy(self) -> None:
        # `pipe` reports the LAST stage's returncode, so a failing
        # `kubectl kustomize` still exits 0 through `envsubst`. Without this
        # guard, empty manifests get applied, the returncode is ignored per the
        # no-`set -e` rule, rollout-status passes against unchanged deployments,
        # and a broken overlay reports success.
        runner = RecordingRunner({**OK_RESPONSES, ("envsubst",): Result(0, "")})
        with self.assertRaises(DeployError) as ctx:
            _ = _run(_cfg(), runner)
        self.assertIn("no manifests", str(ctx.exception))
        self.assertEqual([c for c in runner.calls if c[:2] == ["kubectl", "apply"]], [])

    def test_whitespace_only_render_is_also_rejected(self) -> None:
        runner = RecordingRunner({**OK_RESPONSES, ("envsubst",): Result(0, "\n  \n")})
        with self.assertRaises(DeployError):
            _ = _run(_cfg(), runner)

    def test_config_without_sha_buckets_is_an_error(self) -> None:
        # An empty envsubst allowlist substitutes NOTHING, so every ${VAR} would
        # reach kubectl literally.
        cfg = make_config(
            **{
                "deploy.uat": {"namespace": "ns", "overlay": "o"},
                "a": {"paths": ["a/**"], "touched": True},
            }
        )
        with self.assertRaises(DeployError) as ctx:
            _ = _run(cfg, RecordingRunner(OK_RESPONSES))
        self.assertIn("nothing to substitute", str(ctx.exception))


class TestMigrationJobValidation(unittest.TestCase):
    def test_unknown_variable_in_migration_job_raises(self) -> None:
        # Left alone this yields a job name no cluster can have, and the deploy
        # waits out the whole migration timeout before failing misleadingly.
        cfg = _cfg(
            **{
                "deploy.uat": {
                    "namespace": "ns",
                    "overlay": "o",
                    "migration_job": "core-migrate-${CORE_VERISON}",
                    "migration_version_bucket": "core",
                }
            }
        )
        with self.assertRaises(DeployError) as ctx:
            _ = migration_job_name(resolve_env(cfg, "uat"), {"CORE_VERSION": "v2"})
        self.assertIn("CORE_VERISON", str(ctx.exception))
        self.assertIn("CORE_VERSION", str(ctx.exception))


class TestDeployedVersionsContainerSelection(unittest.TestCase):
    def test_picks_the_configured_container_not_the_first(self) -> None:
        # `[k8s.*] container` exists precisely because the image-carrying
        # container is not always first; reading containers[0] reports a
        # sidecar's tag and the build table shows a spurious or missing change.
        payload = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "scanner"},
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {"name": "istio-proxy", "image": "r/ns/proxy:sidecar1"},
                                        {"name": "worker", "image": "r/ns/scanner:real9"},
                                    ]
                                }
                            }
                        },
                    }
                ]
            }
        )
        runner = RecordingRunner({("kubectl", "get", "deployment"): Result(0, payload)})
        got = deploy_mod.deployed_versions(runner, _cfg(), "ns-uat")
        self.assertEqual(got["scanner"], "real9")

    def test_falls_back_to_the_first_container_when_the_name_is_absent(self) -> None:
        payload = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "scanner"},
                        "spec": {"template": {"spec": {"containers": [{"name": "other", "image": "r/ns/s:v7"}]}}},
                    }
                ]
            }
        )
        runner = RecordingRunner({("kubectl", "get", "deployment"): Result(0, payload)})
        self.assertEqual(deploy_mod.deployed_versions(runner, _cfg(), "ns-uat")["scanner"], "v7")


class TestCrashDetectionDegradation(unittest.TestCase):
    def test_failed_pod_listing_warns(self) -> None:
        # Detection is off for that tick; silence would burn the full timeout on
        # a rollout that is already crash-looping.
        runner = RecordingRunner(
            {
                **OK_RESPONSES,
                ("kubectl", "rollout", "status"): Result(1),
                ("kubectl", "get", "pods"): Result(1, ""),
            }
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, _out = _run(_cfg(), runner)
        self.assertEqual(rc, 1)
        self.assertIn("crash detection degraded", err.getvalue())
