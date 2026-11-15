"""Read a rendered kustomize overlay: what pass 1 applies, and which inputs it needs.

Pure: no runner, no subprocess. tangier is stdlib-only, so it cannot parse
YAML. The deploy instead pipes the render through `inspect_argv()`, a
`kubectl annotate --local` call that needs no cluster and prints each document
as JSON in input order. This module parses that JSON into dicts, and splits the
original YAML into documents for pass 1 to apply.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

PASS_ONE_LABEL = ("migrate-step", "pre")
PASS_ONE_SELECTOR = "=".join(PASS_ONE_LABEL)
INPUT_KINDS = ("ConfigMap", "Secret")

# Workload kinds whose pod template sits at `spec.template.spec`.
_TEMPLATED = frozenset({"Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"})


def inspect_argv() -> list[str]:
    """Convert the YAML on stdin to JSON objects, without a cluster.

    `--local` makes no API call, and removing an annotation that is absent is a
    no-op, so the output is the input plus an empty `annotations` map.
    """
    return ["kubectl", "annotate", "--local", "-f", "-", "-o", "json", "tangier-noop-"]


def parse_objects(json_stream: str) -> list[dict]:
    """The objects in a stream of concatenated JSON values, in order."""
    decoder = json.JSONDecoder()
    out: list[dict] = []
    pos = 0
    while True:
        while pos < len(json_stream) and json_stream[pos].isspace():
            pos += 1
        if pos >= len(json_stream):
            return out
        obj, pos = decoder.raw_decode(json_stream, pos)
        if isinstance(obj, dict):
            out.append(obj)


def split_documents(render: str) -> list[str]:
    """The render's YAML documents, split on lines that are exactly `---`.

    A `---` inside a block scalar is indented, so it does not split. Documents
    holding only blank lines and comments are dropped, as kubectl drops them.
    """
    docs: list[str] = []
    current: list[str] = []
    for line in render.splitlines(keepends=True):
        if line.rstrip("\r\n") == "---":
            docs.append("".join(current))
            current = []
        else:
            current.append(line)
    docs.append("".join(current))
    return [d if d.endswith("\n") else d + "\n" for d in docs if _has_content(d)]


def _has_content(doc: str) -> bool:
    return any(line.strip() and not line.lstrip().startswith("#") for line in doc.splitlines())


@dataclass(frozen=True)
class Ref:
    """A ConfigMap or Secret that a pod template reads."""

    kind: str
    namespace: str
    name: str


def pod_spec(obj: dict) -> dict | None:
    """The pod spec inside a workload, or None if `obj` has none."""
    kind = obj.get("kind")
    spec = obj.get("spec") or {}
    if kind in _TEMPLATED:
        found = (spec.get("template") or {}).get("spec")
    elif kind == "CronJob":
        found = (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    elif kind == "Pod":
        found = spec
    else:
        found = None
    return found if isinstance(found, dict) else None


def _namespace(obj: dict, default_ns: str) -> str:
    return (obj.get("metadata") or {}).get("namespace") or default_ns


def pod_refs(obj: dict, default_ns: str) -> list[Ref]:
    """Every ConfigMap and Secret the pod template in `obj` reads, in spec order."""
    spec = pod_spec(obj)
    if spec is None:
        return []
    ns = _namespace(obj, default_ns)
    out: list[Ref] = []

    def add(kind: str, source: dict | None, key: str = "name") -> None:
        if isinstance(source, dict) and source.get(key):
            out.append(Ref(kind, ns, source[key]))

    for container in [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]:
        for source in container.get("envFrom") or []:
            add("ConfigMap", source.get("configMapRef"))
            add("Secret", source.get("secretRef"))
        for var in container.get("env") or []:
            value_from = var.get("valueFrom") or {}
            add("ConfigMap", value_from.get("configMapKeyRef"))
            add("Secret", value_from.get("secretKeyRef"))
    for volume in spec.get("volumes") or []:
        add("ConfigMap", volume.get("configMap"))
        add("Secret", volume.get("secret"), key="secretName")
        for source in (volume.get("projected") or {}).get("sources") or []:
            add("ConfigMap", source.get("configMap"))
            add("Secret", source.get("secret"))
    for pull in spec.get("imagePullSecrets") or []:
        add("Secret", pull)
    return out


def _key(obj: dict, default_ns: str) -> tuple[str, str, str]:
    return (_namespace(obj, default_ns), obj.get("kind", ""), (obj.get("metadata") or {}).get("name", ""))


def _labelled(obj: dict) -> bool:
    key, value = PASS_ONE_LABEL
    return ((obj.get("metadata") or {}).get("labels") or {}).get(key) == value


def pass_one(docs: list[str], objects: list[dict], default_ns: str) -> str:
    """The YAML pass 1 applies: the labelled objects' inputs from the render, then the labelled objects.

    Inputs come first so they exist before the Job's pod is scheduled. Each
    document is the original render text, so pass 2 re-applies it unchanged.
    Empty when nothing carries the label. `docs` and `objects` must be
    index-aligned: the same render, one entry per document.
    """
    labelled = [i for i, obj in enumerate(objects) if _labelled(obj)]
    if not labelled:
        return ""
    wanted = {(r.namespace, r.kind, r.name) for i in labelled for r in pod_refs(objects[i], default_ns)}
    inputs = [
        i
        for i, obj in enumerate(objects)
        if i not in labelled and obj.get("kind") in INPUT_KINDS and _key(obj, default_ns) in wanted
    ]
    return "---\n".join(docs[i] for i in [*inputs, *labelled])
