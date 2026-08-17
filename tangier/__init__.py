"""tangier — a content-addressed CI/deploy pipeline toolkit.

Three concerns, one config (`pipeline.toml`):

  changemap  which parts of the repo does this diff touch?
  image      what is this bucket's content hash, and how do I build it?
  deploy     render and apply k8s manifests for those image tags.

Pure stdlib. See `docs/specs/changemap.md`.
"""
