"""`tangier.manifests`: pure dict and string fixtures, plus one real-kubectl contract test."""

import json
import os
import shutil
import subprocess
import unittest

from tangier.manifests import (
    Ref,
    inspect_argv,
    parse_objects,
    pass_one,
    pod_refs,
    split_documents,
)
from tangier.tests.support import make_configmap, make_workload

NS = "app"


# One reference at every location tangier reads.
EVERY_REF_POD = {
    "initContainers": [{"name": "init", "envFrom": [{"configMapRef": {"name": "init-cm"}}]}],
    "containers": [
        {
            "name": "c",
            "envFrom": [{"configMapRef": {"name": "ef-cm"}}, {"secretRef": {"name": "ef-s"}}],
            "env": [
                {"name": "A", "valueFrom": {"configMapKeyRef": {"name": "kr-cm", "key": "a"}}},
                {"name": "B", "valueFrom": {"secretKeyRef": {"name": "kr-s", "key": "b"}}},
                {"name": "C", "value": "plain"},
                {"name": "D", "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}}},
            ],
        }
    ],
    "volumes": [
        {"name": "v1", "configMap": {"name": "vol-cm"}},
        {"name": "v2", "secret": {"secretName": "vol-s"}},
        {"name": "v3", "projected": {"sources": [{"configMap": {"name": "proj-cm"}}, {"secret": {"name": "proj-s"}}]}},
        {"name": "v4", "emptyDir": {}},
    ],
    "imagePullSecrets": [{"name": "pull-s"}],
}


class TestPodRefs(unittest.TestCase):
    def test_every_ref_location(self) -> None:
        refs = pod_refs(make_workload("Deployment", EVERY_REF_POD), NS)
        self.assertEqual(
            {(r.kind, r.name) for r in refs},
            {
                ("ConfigMap", "init-cm"),
                ("ConfigMap", "ef-cm"),
                ("Secret", "ef-s"),
                ("ConfigMap", "kr-cm"),
                ("Secret", "kr-s"),
                ("ConfigMap", "vol-cm"),
                ("Secret", "vol-s"),
                ("ConfigMap", "proj-cm"),
                ("Secret", "proj-s"),
                ("Secret", "pull-s"),
            },
        )

    def test_every_workload_shape(self) -> None:
        pod = {"containers": [{"name": "c", "envFrom": [{"configMapRef": {"name": "cm"}}]}]}
        for kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job", "CronJob", "Pod"):
            with self.subTest(kind=kind):
                self.assertEqual(pod_refs(make_workload(kind, pod), NS), [Ref("ConfigMap", NS, "cm")])

    def test_objects_without_a_pod_template_have_no_refs(self) -> None:
        self.assertEqual(pod_refs(make_configmap("x"), NS), [])
        self.assertEqual(pod_refs({"kind": "Service", "spec": {"template": {"spec": EVERY_REF_POD}}}, NS), [])

    def test_namespace_comes_from_the_workload_or_defaults(self) -> None:
        pod = {"containers": [{"name": "c", "envFrom": [{"configMapRef": {"name": "cm"}}]}]}
        self.assertEqual(pod_refs(make_workload("DaemonSet", pod), NS)[0].namespace, NS)
        self.assertEqual(
            pod_refs(make_workload("DaemonSet", pod, namespace="kube-system"), NS)[0].namespace, "kube-system"
        )


class TestSplitDocuments(unittest.TestCase):
    def test_indented_separator_in_a_block_scalar_does_not_split(self) -> None:
        render = "kind: ConfigMap\ndata:\n  x: |\n    ---\n    y\n---\nkind: Secret\n"
        self.assertEqual(
            split_documents(render), ["kind: ConfigMap\ndata:\n  x: |\n    ---\n    y\n", "kind: Secret\n"]
        )

    def test_empty_and_comment_only_documents_are_dropped(self) -> None:
        render = "---\nkind: A\n---\n\n---\n# just a comment\n---\nkind: B"
        self.assertEqual(split_documents(render), ["kind: A\n", "kind: B\n"])


class TestParseObjects(unittest.TestCase):
    def test_concatenated_pretty_printed_json(self) -> None:
        stream = (
            json.dumps({"kind": "A"}, indent=4) + "\n" + json.dumps({"kind": "B", "x": {"y": [1]}}, indent=4) + "\n"
        )
        self.assertEqual([o["kind"] for o in parse_objects(stream)], ["A", "B"])

    def test_empty_stream(self) -> None:
        self.assertEqual(parse_objects(" \n"), [])


class TestPassOne(unittest.TestCase):
    JOB_POD = {"containers": [{"name": "m", "envFrom": [{"configMapRef": {"name": "env-config"}}]}]}
    WEB_POD = {"containers": [{"name": "s", "envFrom": [{"configMapRef": {"name": "web-config"}}]}]}

    def _render(self):
        objects = [
            make_workload("Deployment", self.WEB_POD, "web"),
            make_workload("Job", self.JOB_POD, "migrate", labels={"migrate-step": "pre"}),
            make_configmap("web-config"),
            make_configmap("env-config"),
        ]
        docs = ["doc-web\n", "doc-job\n", "doc-web-config\n", "doc-env-config\n"]
        return docs, objects

    def test_inputs_first_then_labelled_and_only_the_jobs_inputs(self) -> None:
        docs, objects = self._render()
        self.assertEqual(pass_one(docs, objects, NS), "doc-env-config\n---\ndoc-job\n")

    def test_a_labelled_input_is_not_repeated(self) -> None:
        docs, objects = self._render()
        objects[3]["metadata"]["labels"] = {"migrate-step": "pre"}
        # Labelled objects keep render order.
        self.assertEqual(pass_one(docs, objects, NS), "doc-job\n---\ndoc-env-config\n")

    def test_inputs_match_on_namespace(self) -> None:
        # A labelled workload in kube-system reads kube-system's `env-config`,
        # not the deploy namespace's.
        docs, objects = self._render()
        objects[1]["metadata"]["namespace"] = "kube-system"
        objects.append(make_configmap("env-config", namespace="kube-system"))
        docs.append("doc-kube-system-env-config\n")
        self.assertEqual(pass_one(docs, objects, NS), "doc-kube-system-env-config\n---\ndoc-job\n")

    def test_nothing_labelled_is_empty(self) -> None:
        docs, objects = self._render()
        del objects[1]["metadata"]["labels"]
        self.assertEqual(pass_one(docs, objects, NS), "")


@unittest.skipUnless(shutil.which("kubectl"), "kubectl not on PATH")
class TestInspectContract(unittest.TestCase):
    """The one external behaviour the design rests on: kubectl turns YAML into
    one JSON object per document, in order, with no cluster."""

    def test_object_count_and_order_match_split_documents(self) -> None:
        render = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: a\ndata:\n  x: |\n    ---\n    y\n"
            "---\n"
            "apiVersion: example.com/v1\nkind: Widget\nmetadata:\n  name: w\n"
            "---\n"
            "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: j\n  labels:\n    migrate-step: pre\n"
            "spec:\n  template:\n    spec:\n      containers:\n      - name: m\n        image: x\n"
        )
        result = subprocess.run(
            inspect_argv(),
            input=render,
            capture_output=True,
            text=True,
            env={**os.environ, "KUBECONFIG": "/nonexistent"},
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        objects = parse_objects(result.stdout)
        docs = split_documents(render)
        self.assertEqual(len(objects), len(docs))
        self.assertEqual([o["kind"] for o in objects], ["ConfigMap", "Widget", "Job"])
        self.assertEqual(objects[2]["metadata"]["labels"], {"migrate-step": "pre"})


if __name__ == "__main__":
    unittest.main()
