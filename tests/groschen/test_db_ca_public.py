"""Guard the database-local, public-only PostgreSQL CA mirror."""

import collections
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
APPS = ROOT / "kubernetes/apps"
DATABASE = APPS / "database"
MIRROR = DATABASE / "groschen-ca/app"
SCHEMAS = pathlib.Path(os.environ.get(
    "GROSCHEN_CA_SCHEMA_DIR", ROOT / "_bmad-output/epics/groschen-smoke-test"))


def tool(name):
    override = f"GROSCHEN_{name.upper()}"
    found = shutil.which(os.environ.get(override) or name)
    if found is None:
        reason = f"{name} unavailable: set {override} or add {name} to PATH"
        if os.environ.get("GROSCHEN_CA_REQUIRE_SCHEMAS") != "1":
            raise unittest.SkipTest(reason)
        raise RuntimeError(reason)
    return found


def render(path, *options):
    output = subprocess.check_output([tool("kustomize"), "build", *options, str(path)], text=True)
    return [doc for doc in yaml.safe_load_all(output) if doc]


def one(docs, kind, name):
    matches = [doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {kind}/{name}; found {len(matches)}")
    return matches[0]


def identity(doc):
    return (doc["apiVersion"], doc["kind"], doc["metadata"].get("namespace", ""),
            doc["metadata"]["name"])


def render_cluster_apps_root():
    """Render every category in cluster-apps' Flux path as a single root."""
    categories = sorted(path.name for path in APPS.iterdir()
                        if path.is_dir() and not path.name.startswith(".")
                        and (path / "kustomization.yaml").is_file())
    with tempfile.TemporaryDirectory(dir=APPS, prefix=".groschen-root-render-") as directory:
        root = pathlib.Path(directory)
        (root / "kustomization.yaml").write_text(
            "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\nresources:\n"
            + "".join(f"  - ../{category}\n" for category in categories))
        return render(root, "--load-restrictor=LoadRestrictionsNone")


class DatabaseCAPublicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mirror = render(MIRROR)
        cls.database = render(DATABASE)
        cls.root = render_cluster_apps_root()

    def test_source_identity_cannot_enumerate_or_read_other_secrets(self):
        self.assertEqual(collections.Counter(doc["kind"] for doc in self.mirror), {
            "ServiceAccount": 1, "Role": 1, "RoleBinding": 1,
            "SecretStore": 1, "ExternalSecret": 1,
        })
        for doc in self.mirror:
            self.assertEqual(doc["metadata"]["namespace"], "database")
        sa = one(self.mirror, "ServiceAccount", "groschen-postgres-ca-source-reader")
        self.assertIs(sa["automountServiceAccountToken"], False)
        role = one(self.mirror, "Role", sa["metadata"]["name"])
        self.assertEqual(role["rules"], [{
            "apiGroups": [""], "resources": ["secrets"],
            "resourceNames": ["postgres-ca"], "verbs": ["get"],
        }])
        binding = one(self.mirror, "RoleBinding", sa["metadata"]["name"])
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": sa["metadata"]["name"],
            "namespace": "database",
        }])
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
            "name": role["metadata"]["name"],
        })

    def test_store_uses_local_trust_and_database_service_account(self):
        store = one(self.mirror, "SecretStore", "groschen-postgres-ca-source")
        self.assertEqual(store["apiVersion"], "external-secrets.io/v1")
        self.assertEqual(store["spec"], {"provider": {"kubernetes": {
            "remoteNamespace": "database",
            "server": {
                "url": "https://kubernetes.default.svc",
                "caProvider": {"type": "ConfigMap", "name": "kube-root-ca.crt", "key": "ca.crt"},
            },
            "auth": {"serviceAccount": {"name": "groschen-postgres-ca-source-reader"}},
        }}})

    def test_only_ca_property_can_reach_new_opaque_target(self):
        mirror = one(self.mirror, "ExternalSecret", "groschen-postgres-ca-public")
        self.assertEqual(mirror["apiVersion"], "external-secrets.io/v1")
        self.assertEqual(mirror["spec"], {
            "refreshPolicy": "Periodic", "refreshInterval": "15m",
            "secretStoreRef": {"kind": "SecretStore", "name": "groschen-postgres-ca-source"},
            "target": {
                "name": "groschen-postgres-ca-public", "creationPolicy": "Owner",
                "deletionPolicy": "Retain",
                "template": {"engineVersion": "v2", "mergePolicy": "Replace", "type": "Opaque",
                             "data": {"ca.crt": "{{ .publicCA }}"}},
            },
            "data": [{"secretKey": "publicCA", "remoteRef": {
                "key": "postgres-ca", "property": "ca.crt"}}],
        })
        self.assertNotEqual(mirror["spec"]["target"]["name"], "postgres-ca")

    def test_separate_flux_reconciliation_is_registered_once(self):
        ks = one(self.database, "Kustomization", "groschen-postgres-ca-public")
        self.assertEqual(ks["apiVersion"], "kustomize.toolkit.fluxcd.io/v1")
        self.assertEqual(ks["metadata"]["namespace"], "database")
        self.assertEqual(ks["spec"]["path"], "./kubernetes/apps/database/groschen-ca/app")
        self.assertEqual(ks["spec"]["targetNamespace"], "database")
        self.assertEqual(ks["spec"]["sourceRef"], {
            "kind": "GitRepository", "name": "flux-system", "namespace": "flux-system"})
        self.assertEqual(ks["spec"]["dependsOn"], [
            {"name": "external-secrets-operator", "namespace": "flux-system"},
        ])
        self.assertTrue(ks["spec"]["wait"])
        self.assertTrue(ks["spec"]["prune"])
        self.assertEqual(ks["spec"]["interval"], "15m")
        self.assertEqual(ks["spec"]["retryInterval"], "1m")
        cluster = one(self.database, "Kustomization", "postgres-cluster")
        self.assertNotEqual(cluster["spec"]["path"], ks["spec"]["path"])
        self.assertEqual(cluster["spec"]["path"], "./kubernetes/apps/database/cluster/app")
        self.assertNotIn("groschen-postgres-ca-public", {
            dep["name"] for dep in cluster["spec"]["dependsOn"]})

    def test_full_cluster_apps_root_has_unique_resource_identities(self):
        for docs in (self.mirror, self.database, self.root):
            ids = [identity(doc) for doc in docs]
            self.assertEqual(len(ids), len(set(ids)), [
                item for item, count in collections.Counter(ids).items() if count > 1])
        ks = one(self.root, "Kustomization", "groschen-postgres-ca-public")
        self.assertEqual(ks, one(self.database, "Kustomization", "groschen-postgres-ca-public"))
        self.assertEqual(len([doc for doc in self.root if doc["kind"] == "Kustomization"
                              and doc["metadata"]["name"] == "groschen-postgres-ca-public"]), 1)

    def test_eso_and_flux_resources_validate_against_saved_served_schemas(self):
        schemas = {
            "SecretStore": "secretstores.external-secrets.io.json",
            "ExternalSecret": "externalsecrets.external-secrets.io.json",
            "Kustomization": "kustomizations.kustomize.toolkit.fluxcd.io.schema.json",
        }
        missing = [filename for filename in schemas.values()
                   if not (SCHEMAS / filename).is_file()]
        if missing:
            reason = "served schema artifacts unavailable: " + ", ".join(missing)
            if os.environ.get("GROSCHEN_CA_REQUIRE_SCHEMAS") == "1":
                self.fail(reason)
            self.skipTest(reason)
        kubeconform = tool("kubeconform")
        with tempfile.TemporaryDirectory() as directory:
            for kind, filename in schemas.items():
                with self.subTest(kind=kind):
                    schema = json.loads((SCHEMAS / filename).read_text())
                    if kind != "Kustomization":
                        versions = schema["spec"]["versions"]
                        schema = next(version["schema"]["openAPIV3Schema"]
                                      for version in versions if version["name"] == "v1")
                    path = pathlib.Path(directory) / f"{kind}.json"
                    path.write_text(json.dumps(schema))
                    document = (one(self.database, kind, "groschen-postgres-ca-public")
                                if kind == "Kustomization" else
                                next(doc for doc in self.mirror if doc["kind"] == kind))
                    result = subprocess.run(
                        [kubeconform, "-strict", "-schema-location", path.as_uri(), "-"],
                        input=yaml.safe_dump(document), text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
