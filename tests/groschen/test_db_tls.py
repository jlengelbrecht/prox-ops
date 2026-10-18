"""Guard Groschen's second public-CA hop and strict client TLS."""

import collections
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

from test_db_ca_public import APPS, DATABASE, SCHEMAS, identity, one, render, render_cluster_apps_root, tool


GROSCHEN = APPS / "groschen"
READER = DATABASE / "groschen-ca-reader/app"


class GroschenDBTLSTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reader = render(READER)
        cls.database = render(DATABASE)
        cls.infra = render(GROSCHEN / "infra")
        cls.groschen = render(GROSCHEN)
        cls.app = render(GROSCHEN / "app")
        cls.root = render_cluster_apps_root()

    def test_database_permission_only_names_public_mirror(self):
        self.assertEqual(collections.Counter(d["kind"] for d in self.reader),
                         {"Role": 1, "RoleBinding": 1})
        role = one(self.reader, "Role", "groschen-postgres-ca-reader")
        binding = one(self.reader, "RoleBinding", "groschen-postgres-ca-reader")
        self.assertEqual(role["metadata"]["namespace"], "database")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["secrets"],
                                          "resourceNames": ["groschen-postgres-ca-public"],
                                          "verbs": ["get"]}])
        self.assertNotIn("postgres-ca", role["rules"][0]["resourceNames"])
        self.assertEqual(binding["subjects"], [{"kind": "ServiceAccount",
                                                 "name": "groschen-postgres-ca-reader",
                                                 "namespace": "groschen"}])
        self.assertEqual(binding["roleRef"], {"apiGroup": "rbac.authorization.k8s.io",
                                                "kind": "Role", "name": role["metadata"]["name"]})
        self.assertEqual(binding["metadata"]["namespace"], "database")

    def test_app_store_uses_only_named_reader_and_local_api_trust(self):
        sa = one(self.infra, "ServiceAccount", "groschen-postgres-ca-reader")
        self.assertEqual(sa["metadata"]["namespace"], "groschen")
        self.assertIs(sa["automountServiceAccountToken"], False)
        self.assertFalse(any(d["kind"] in {"Role", "RoleBinding", "ClusterRole",
                                              "ClusterRoleBinding"} for d in self.infra))
        store = one(self.infra, "SecretStore", "groschen-postgres-ca-public")
        self.assertEqual(store["metadata"]["namespace"], "groschen")
        self.assertEqual(store["spec"], {"provider": {"kubernetes": {
            "remoteNamespace": "database",
            "server": {"url": "https://kubernetes.default.svc",
                       "caProvider": {"type": "ConfigMap", "name": "kube-root-ca.crt",
                                      "key": "ca.crt"}},
            "auth": {"serviceAccount": {"name": sa["metadata"]["name"]}},
        }}})

    def test_second_hop_is_public_only_owned_and_periodic(self):
        mirror = one(self.infra, "ExternalSecret", "groschen-postgres-ca-public")
        self.assertEqual(mirror["metadata"]["namespace"], "groschen")
        self.assertEqual(mirror["spec"], {
            "refreshPolicy": "Periodic", "refreshInterval": "15m",
            "secretStoreRef": {"kind": "SecretStore", "name": "groschen-postgres-ca-public"},
            "target": {"name": "groschen-postgres-ca-public", "creationPolicy": "Owner",
                       "deletionPolicy": "Retain", "template": {
                           "engineVersion": "v2", "mergePolicy": "Replace", "type": "Opaque",
                           "data": {"ca.crt": "{{ .publicCA }}"}}},
            "data": [{"secretKey": "publicCA", "remoteRef": {
                "key": "groschen-postgres-ca-public", "property": "ca.crt"}}],
        })
        self.assertNotEqual(mirror["spec"]["data"][0]["remoteRef"]["key"], "postgres-ca")

    def test_flux_waits_for_central_mirror_reader_and_app_secret(self):
        reader = one(self.database, "Kustomization", "groschen-postgres-ca-reader")
        self.assertEqual(reader["spec"]["path"],
                         "./kubernetes/apps/database/groschen-ca-reader/app")
        self.assertEqual(reader["spec"]["targetNamespace"], "database")
        self.assertEqual(reader["spec"]["dependsOn"], [{
            "name": "groschen-postgres-ca-public", "namespace": "database"}])
        self.assertIs(reader["spec"]["wait"], True)
        infra = one(self.groschen, "Kustomization", "groschen-namespace")
        self.assertEqual(infra["spec"]["dependsOn"], [{
            "name": "groschen-postgres-ca-reader", "namespace": "database"}])
        self.assertIs(infra["spec"]["wait"], True)
        self.assertEqual(infra["spec"]["healthCheckExprs"], [
            {"apiVersion": "external-secrets.io/v1", "kind": kind,
             "current": "has(status.conditions) && "
                        "status.conditions.exists(c, c.type == 'Ready' && c.status == 'True')"}
            for kind in ("SecretStore", "ExternalSecret")])
        app = one(self.groschen, "Kustomization", "groschen-app")
        self.assertEqual(app["spec"]["dependsOn"], [{
            "name": "groschen-namespace", "namespace": "flux-system"}])
        self.assertIs(app["spec"]["wait"], True)
        for docs in (self.reader, self.database, self.infra, self.groschen, self.root):
            ids = [identity(d) for d in docs]
            self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(one(self.root, "Kustomization", reader["metadata"]["name"]),
                         reader)

    def test_full_root_resolves_groschen_flux_dependencies(self):
        owners = {
            (doc["metadata"].get("namespace"), doc["metadata"]["name"]): doc
            for doc in self.root
            if doc["apiVersion"].startswith("kustomize.toolkit.fluxcd.io/")
            and doc["kind"] == "Kustomization"
        }
        expected_edges = {
            ("database", "groschen-postgres-ca-public"):
                ("flux-system", "external-secrets-operator"),
            ("database", "groschen-postgres-ca-reader"):
                ("database", "groschen-postgres-ca-public"),
            ("flux-system", "groschen-namespace"):
                ("database", "groschen-postgres-ca-reader"),
            ("flux-system", "groschen-app"):
                ("flux-system", "groschen-namespace"),
        }
        for owner, dependency in expected_edges.items():
            with self.subTest(owner=owner):
                self.assertIn(owner, owners)
                self.assertEqual(
                    [(ref.get("namespace", owner[0]), ref["name"])
                     for ref in owners[owner]["spec"].get("dependsOn", [])],
                    [dependency],
                )
                self.assertIn(dependency, owners)

    def test_both_clients_fail_closed_and_reload_on_ca_rotation(self):
        deployments = [d for d in self.app if d["kind"] == "Deployment"]
        self.assertEqual({d["metadata"]["name"] for d in deployments},
                         {"groschen-api", "groschen-worker"})
        for deployment in deployments:
            with self.subTest(name=deployment["metadata"]["name"]):
                self.assertEqual(deployment["metadata"]["annotations"], {
                    "secret.reloader.stakater.com/reload":
                        "groschen-db-credentials,groschen-postgres-ca-public"})
                pod = deployment["spec"]["template"]["spec"]
                self.assertIs(pod["automountServiceAccountToken"], False)
                container = pod["containers"][0]
                env = {item["name"]: item["value"] for item in container["env"]}
                self.assertEqual(env["DATABASE_URL"],
                                 "host=postgres-rw.database.svc.cluster.local port=5432 "
                                 "user=groschen dbname=groschen sslmode=verify-full "
                                 "sslrootcert=/tls/ca.crt pool_max_conns=4")
                self.assertEqual(env["PGPASSFILE"], "/run/secrets/groschen_db_pgpass")
                self.assertEqual({"name": "postgres-ca", "mountPath": "/tls",
                                  "readOnly": True} in container["volumeMounts"], True)
                ca = one_volume(pod, "postgres-ca")
                self.assertEqual(ca["secret"], {
                    "secretName": "groschen-postgres-ca-public", "defaultMode": 0o444,
                    "items": [{"key": "ca.crt", "path": "ca.crt"}]})

    def test_changed_custom_resources_match_served_schemas(self):
        schemas = {
            "SecretStore": "secretstores.external-secrets.io.json",
            "ExternalSecret": "externalsecrets.external-secrets.io.json",
            "Kustomization": "kustomizations.kustomize.toolkit.fluxcd.io.schema.json",
        }
        missing = [name for name in schemas.values() if not (SCHEMAS / name).is_file()]
        if missing:
            reason = "served schema artifacts unavailable: " + ", ".join(missing)
            if os.environ.get("GROSCHEN_CA_REQUIRE_SCHEMAS") == "1":
                self.fail(reason)
            self.skipTest(reason)
        with tempfile.TemporaryDirectory() as directory:
            for kind, filename in schemas.items():
                with self.subTest(kind=kind):
                    schema = json.loads((SCHEMAS / filename).read_text())
                    if kind != "Kustomization":
                        schema = next(v["schema"]["openAPIV3Schema"] for v in
                                      schema["spec"]["versions"] if v["name"] == "v1")
                    path = pathlib.Path(directory) / (kind + ".json")
                    path.write_text(json.dumps(schema))
                    doc = one(self.database, kind, "groschen-postgres-ca-reader") if (
                        kind == "Kustomization") else one(
                            self.infra, kind, "groschen-postgres-ca-public")
                    result = subprocess.run([tool("kubeconform"), "-strict",
                                             "-schema-location", path.as_uri(), "-"],
                                            input=yaml.safe_dump(doc), text=True,
                                            capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def one_volume(pod, name):
    matches = [v for v in pod["volumes"] if v["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"Expected one volume {name}; found {len(matches)}")
    return matches[0]


if __name__ == "__main__":
    unittest.main()
