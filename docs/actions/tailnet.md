# `tailnet` action

The `tailnet` action connects a GitHub Actions runner to the tailnet as one
environment's deploy identity, then points `kubectl` at the Tailscale Kubernetes
operator. Use it in any job that talks to the cluster.

It replaces a six-line pinned Tailscale block plus a kubeconfig step, and it
removes the tailnet tag literal each repo used to spell out.

## Usage

```yaml
jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: uat          # load-bearing — see "Never drop this line" below
    permissions:
      id-token: write         # required for the OIDC exchange
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: sminnee/tangier/.github/actions/tailnet@v0
        with:
          client-id: ${{ vars.TS_DEPLOY_CLIENT_ID }}
          audience: ${{ vars.TS_DEPLOY_AUDIENCE }}
          env: uat
      - run: kubectl get pods -n astronort-uat
```

The action reads the tailnet tag for `uat` from `pipeline.toml`:

```toml
[tailnet]
operator = "tailscale-operator"   # optional; this is the default

[tailnet.uat]
tag = "tag:astronort-uat-deploy"
```

Each environment states its tag explicitly. The tag is never derived from the
environment name, because the two are independent: `askastrowww` deploys to a
GitHub environment called `production` using a tag containing `prod`, and
`k8s-cluster` uses `tag:k8s-cluster-deploy` with no environment segment at all.

## Inputs

| Input | Required | Default | Purpose |
|---|---|---|---|
| `client-id` | yes | — | OAuth client ID for this environment's deploy identity |
| `audience` | yes | — | OIDC audience for this environment |
| `env` | no | `""` | Environment name, used to look up `[tailnet.<env>] tag` |
| `tag` | no | `""` | Tailnet tag, overriding config. For repos with no `pipeline.toml` |
| `operator` | no | `""` | Operator hostname, overriding `[tailnet] operator` |
| `version` | no | `1.78.1` | Tailscale release to install |

The action outputs `tag`: the tailnet tag the runner authenticated as.

### Why `client-id` and `audience` are inputs

A composite action cannot read the caller's `vars` context. The whole context is
absent, and any expression referring to it fails with
`Unrecognized named-value: 'vars'`
([actions/runner#2551](https://github.com/actions/runner/issues/2551)). So the
two values must be forwarded at the call site.

Passing them as declared inputs, rather than through an `env:` block, means
`action-validator` can check them and a typo fails cleanly. A missing `env:`
entry would instead yield an empty client ID and a confusing tailnet rejection.

### A repo with no `pipeline.toml`

Pass `tag` directly:

```yaml
- uses: sminnee/tangier/.github/actions/tailnet@v0
  with:
    client-id: ${{ vars.TS_DEPLOY_CLIENT_ID }}
    audience: ${{ vars.TS_DEPLOY_AUDIENCE }}
    tag: tag:k8s-cluster-deploy
```

`k8s-cluster` runs helm and kubectl and builds no images. A `pipeline.toml`
there would describe no tags, no buckets and no images — it would trip
tangier's `_warn_tags_without_input` warning and misrepresent the repo. An
explicit `tag` input is the supported path, not a workaround.

## Never drop the `environment:` line

**This is the most important thing on this page.** The separation between uat
and prod is enforced at three layers, and every layer keys off the
`environment:` line on the calling job:

1. The OIDC subject claim carries the environment name.
2. Tailscale's trust policy accepts exactly one subject.
3. The tailnet tag maps to a Kubernetes group with its own RBAC.

`TS_DEPLOY_CLIENT_ID` and `TS_DEPLOY_AUDIENCE` are deliberately the **same two
variable names** in both environments, holding **different values**. GitHub
resolves them per environment. Without `environment: uat` on the job, GitHub
resolves them at repository scope instead, and the job authenticates as the
wrong identity or not at all.

Two rules follow:

- **Never remove `environment:` from a job that calls this action.** The action
  cannot detect its absence — it receives whatever values GitHub resolved.
- **Never move `TS_DEPLOY_*` to repository scope** so that "both workflows can
  see it". Both workflows already see them. They see *different values*, which
  is the entire mechanism.

Before this action existed, each workflow spelled the Tailscale block out, and a
reader could see that two scopes were in play. Now a uat call site and a prod
call site look identical. The `environment:` line is the only remaining
difference, so it carries all the weight.

`k8s-cluster/DEPLOY-IDENTITIES.md` owns the Tailscale trust policy and the RBAC
halves of this arrangement. Read it before changing any identity.

## Diagnosing a failure

Run the preflight from a laptop on the same tailnet:

```
tangier tailnet check uat
```

It checks, in order: `tailscale` on PATH, the tailnet connection, `kubectl` on
PATH, a selected kubeconfig context, that the context is the operator's, that
the API server answers, and that this node carries the environment's tag. Each
failure names the command that fixes it.

The last check is the one worth having. Without it, a missing tag surfaces as a
`Forbidden` from the API server, which names nothing you can act on. With it,
the message names the tag.
