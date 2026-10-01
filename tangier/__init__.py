"""tangier — a content-addressed CI/deploy pipeline toolkit.

Five concerns, one config (`pipeline.toml`):

  changemap  which parts of the repo does this diff touch?
  image      what is this bucket's content hash, and how do I build it?
  deploy     render and apply k8s manifests for those image tags.
  tailnet    can this machine reach the cluster, and as what identity?
  gate       has this content already passed its gate?

Pure stdlib. See `docs/specs/changemap.md` and `docs/specs/gate.md`.
"""

# Kept equal to `pyproject.toml` by a test. Gate records carry it.
__version__ = "0.1.0"
