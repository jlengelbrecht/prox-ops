"""Offline contract check for the pinned CNPG chart and Flux post-render patch."""

import pathlib
import os
import shutil
import subprocess
import tempfile
import unittest
import copy

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART_CACHE = ROOT / "_bmad-output/implementation-artifacts/cluster-0.5.0.tgz"
RELEASE = ROOT / "kubernetes/apps/database/cluster/app/helmrelease.yaml"
NATIVE_KUSTOMIZE = pathlib.Path.home() / ".local/share/mise/installs/aqua-kubernetes-sigs-kustomize/5.7.1/kustomize"


class PinnedChartRenderTests(unittest.TestCase):
    def test_cluster_snapshot_and_future_storage_contract(self):
        chart = pathlib.Path(os.environ.get("HINDSIGHT_CNPG_CHART") or CHART_CACHE)
        self.assertTrue(chart.is_file(),
                        f"Pinned CNPG chart missing at {chart}. Set HINDSIGHT_CNPG_CHART "
                        "to a local cluster-0.5.0.tgz, or fetch it to the documented "
                        "cache path; see docs/operations/hindsight-maintenance.md")
        release = yaml.safe_load(RELEASE.read_text())
        self.assertEqual(release["spec"]["chart"]["spec"]["chart"], "cluster")
        self.assertEqual(release["spec"]["chart"]["spec"]["version"], "0.5.0")
        values = release["spec"]["values"]
        self.assertEqual(values["cluster"]["storage"]["storageClass"], "ceph-block-retain")
        self.assertIs(values["backups"]["enabled"], False)
        patches = release["spec"]["postRenderers"][0]["kustomize"]["patches"]
        chart_metadata = subprocess.run(
            ["helm", "show", "chart", str(chart)],
            check=True, capture_output=True, text=True,
        )
        metadata = yaml.safe_load(chart_metadata.stdout)
        self.assertEqual(metadata["name"], "cluster", "CNPG chart name must be cluster")
        self.assertEqual(metadata["version"], "0.5.0", "CNPG chart version must be 0.5.0")

        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)
            values_file = directory / "values.yaml"
            values_file.write_text(yaml.safe_dump(values))
            helm = subprocess.run(
                ["helm", "template", "postgres-cluster", str(chart),
                 "--namespace", "database", "--values", str(values_file)],
                check=True, capture_output=True, text=True,
            )
            rendered = list(yaml.safe_load_all(helm.stdout))
            clusters = [item for item in rendered if item and item.get("kind") == "Cluster"]
            self.assertEqual(len(clusters), 1)
            self.assertEqual(clusters[0]["metadata"]["name"], "postgres")
            self.assertNotIn("backup", clusters[0]["spec"])
            self.assertEqual(clusters[0]["spec"]["storage"]["storageClass"], "ceph-block-retain")

            (directory / "rendered.yaml").write_text(helm.stdout)
            (directory / "kustomization.yaml").write_text(yaml.safe_dump({
                "apiVersion": "kustomize.config.k8s.io/v1beta1",
                "kind": "Kustomization",
                "resources": ["rendered.yaml"],
                "patches": patches,
            }))
            kustomize = str(NATIVE_KUSTOMIZE) if NATIVE_KUSTOMIZE.is_file() else shutil.which("kustomize")
            self.assertIsNotNone(kustomize, "native kustomize is required")
            built = subprocess.run(
                [kustomize, "build", str(directory)],
                check=True, capture_output=True, text=True,
            )
            patched = [item for item in yaml.safe_load_all(built.stdout)
                       if item and item.get("kind") == "Cluster"]
            self.assertEqual(len(patched), 1)
            spec = patched[0]["spec"]
            self.assertEqual(spec["storage"]["storageClass"], "ceph-block-retain")
            self.assertEqual(spec["backup"], {"volumeSnapshot": {
                "className": "ceph-block-snapshot-retain",
                "snapshotOwnerReference": "none",
                "online": False,
            }})
            without_backup = copy.deepcopy(patched[0])
            del without_backup["spec"]["backup"]
            self.assertEqual(without_backup, clusters[0])


if __name__ == "__main__":
    unittest.main()
