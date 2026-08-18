"""Tailnet preflight: is this machine actually able to reach the cluster?

Pure logic only — parsing `tailscale status --json` and comparing a kubeconfig
context against an operator name. The checks themselves live in
`tangier/commands/tailnet_cmds.py`, the same split as `image.build_argv` and
`deploy.find_crashing_pods`.

The point of the whole feature is check 7 in the command module: turning a
`Forbidden` from the API server — which names nothing you can act on — into a
sentence naming the tag the node is missing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


class TailnetError(RuntimeError):
    """A tailnet prerequisite is missing or misconfigured. Exits 2."""


@dataclass
class TailnetStatus:
    """The fields of `tailscale status --json` that a preflight cares about."""

    backend_state: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.backend_state == "Running"


def parse_status(stdout: str) -> TailnetStatus:
    """Parse `tailscale status --json`.

    Parsed with stdlib `json` rather than grepped, for the same reason
    `find_crashing_pods` is: a tag or a state that happens to appear as a
    substring of a hostname would otherwise satisfy a grep.

    A node with no tags reports `Self.Tags` absent rather than empty, so the
    default matters — an untagged node is the common failure this exists to
    diagnose, not a malformed status.
    """
    try:
        data = json.loads(stdout or "{}")
    except json.JSONDecodeError as e:
        raise TailnetError(f"could not parse `tailscale status --json`: {e}") from e
    if not isinstance(data, dict):
        raise TailnetError("`tailscale status --json` did not return an object")
    self_node = data.get("Self") or {}
    tags = self_node.get("Tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise TailnetError("`tailscale status --json`: Self.Tags is not a list of strings")
    state = data.get("BackendState") or ""
    return TailnetStatus(backend_state=state if isinstance(state, str) else "", tags=list(tags))


def context_matches(context: str, operator: str) -> bool:
    """Whether a kubeconfig context belongs to the Tailscale operator.

    Substring, not equality: `tailscale configure kubeconfig` writes a bare
    `tailscale-operator` on a fresh kubeconfig but `astronort-uat@tailscale-operator`
    where a context of that name already exists. Both are the operator.
    """
    return bool(operator) and operator in context
