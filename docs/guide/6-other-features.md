# 6. Other features

tangier also deploys the images it builds, and checks the network path to the cluster. Both read
the same `pipeline.toml`. Keys are in the
[`pipeline.toml` reference](../../skills/tangier/reference/pipeline-toml.md#deploy).

## `tangier deploy`

`tangier deploy <env>` puts this commit's images on a Kubernetes environment:

1. Render the kustomize overlay **once**, substituting each `<BUCKET>_VERSION`.
2. Apply the migration Job, the objects labelled `migrate-step=pre`, and wait for it.
3. Apply everything, from the same rendered bytes. The Job re-apply is then a no-op.
4. Wait for every deployment to roll out.

A migration failure exits 1 with no rollback: nothing has touched the deployments yet, so the old
pods still serve. A rollout that times out or crash-loops re-runs the prior version's migration,
rolls each deployment back, and exits 1.

```toml
[deploy.uat]
namespace     = "astronort-uat"
overlay       = "k8s/overlays/uat"
migration_job = "smartypants-migrate-${SMARTYPANTS_VERSION}"
migration_version_bucket = "smartypants"

[k8s.smartypants]
deployments = ["smartypants", "smartypants-worker"]
```

Read-only modes show what a deploy would do:

```sh
tangier deploy --render uat        # the manifests
tangier deploy --versions uat      # the version variables
tangier deploy --compare-env uat   # computed tags against what is live
```

`--summary` writes the comparison table to `$GITHUB_STEP_SUMMARY`, or stdout, before it applies
anything. The first apply overwrites the tags the table reads.

`[deploy] after` runs a command once a deploy has fully rolled out, never after a rollback:

```toml
[deploy]
after = "bin/sentry-release ${ENV}"
```

The command runs without a shell. A failed hook does not fail the deploy unless the table form sets
`fatal = true`.

## `tangier tailnet`

CI reaches the cluster over a Tailscale tailnet. Each environment names the tailnet ACL tag that
its deploys authenticate as:

```toml
[tailnet.uat]
tag = "tag:astronort-uat-deploy"
```

`tangier tailnet check uat` walks the path from this machine to the cluster: Tailscale up, `kubectl`
pointed at the operator, the API server answering, and this node carrying the environment's tag.
It stops at the first broken link and names the command that fixes it.

In CI, the `tailnet` action connects the runner and configures `kubectl`, and the `deploy` action
adds `tangier deploy <env> --summary`. Their inputs are in the
[actions reference](../../skills/tangier/reference/github-actions.md#shipped-actions). Read the
[tailnet action guide](../actions/tailnet.md) before changing a workflow that deploys: the
`environment:` line on the calling job is what keeps a uat job out of prod.
