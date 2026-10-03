"""Discovery of vaults whose bridge plugin registered itself in the runtime directory."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import BASE_CAPABILITIES, RESULT_KEYS, BridgeClient, VaultDirectory, _result  # noqa: E402


def register(runtime: Path, name: str, **overrides) -> Path:
    vault_id = name
    directory = runtime / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    document = {"version": 1, "vault_id": vault_id, "app_name": vault_id.title(),
                "app_root": "/vaults/" + vault_id, **overrides}
    endpoint = directory / "endpoint.json"
    endpoint.write_text(json.dumps(document))
    endpoint.chmod(0o600)
    credential = directory / "credential"
    credential.write_text("x" * 43)
    credential.chmod(0o600)
    return directory


class VaultDirectoryTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self._temporary.name) / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.runtime.chmod(0o700)

    def tearDown(self):
        self._temporary.cleanup()

    def scan(self):
        return VaultDirectory(self.runtime, ttl=0.01)._scan()

    def test_discovers_registered_vaults_with_their_names(self):
        register(self.runtime, "iam-team", app_name="IAM Team", app_root="/vaults/IAM Team")
        register(self.runtime, "homelab", app_name="Homelab", app_root="/vaults/Homelab")
        found = self.scan()
        self.assertEqual(sorted(found), ["homelab", "iam-team"])
        self.assertEqual(found["iam-team"].app_name, "IAM Team")
        self.assertEqual(found["iam-team"].socket_path, self.runtime / "iam-team" / "iam-team.sock")
        bridge = BridgeClient(VaultDirectory(self.runtime))
        self.assertEqual(bridge.vault_ids(), ["homelab", "iam-team"])
        self.assertEqual(bridge.vault_name("iam-team"), "IAM Team")
        self.assertIsNone(bridge.vault_name("missing"))

    def test_skips_unsafe_or_malformed_registrations(self):
        register(self.runtime, "good")
        register(self.runtime, "mismatch", vault_id="other")
        register(self.runtime, "badname", app_name="../escape")
        register(self.runtime, "relative", app_root="vaults/relative")
        register(self.runtime, "extra", extra=True)
        register(self.runtime, "version", version=2)
        loose = register(self.runtime, "loose")
        (loose / "endpoint.json").chmod(0o644)
        open_dir = register(self.runtime, "opendir")
        open_dir.chmod(0o755)
        (self.runtime / "Upper").mkdir(mode=0o700)
        (self.runtime / "notadir").write_text("x")
        self.assertEqual(sorted(self.scan()), ["good"])

    def test_parent_binding_requires_root_to_match_the_vault_name(self):
        register(self.runtime, "iam-team", app_name="IAM Team", app_root="/config/IAM Team")
        register(self.runtime, "spoof", app_name="Spoof", app_root="/config/Elsewhere")
        found = VaultDirectory(self.runtime, ttl=0.01, parent=Path("/config"))._scan()
        self.assertEqual(sorted(found), ["iam-team"])

    def test_ambiguous_roots_disable_discovery(self):
        register(self.runtime, "one", app_root="/vaults/same")
        register(self.runtime, "two", app_root="/vaults/same")
        self.assertEqual(self.scan(), {})

    def test_unsafe_runtime_directory_yields_nothing(self):
        register(self.runtime, "good")
        self.runtime.chmod(0o755)
        self.assertEqual(self.scan(), {})
        self.runtime.chmod(0o700)
        self.assertEqual(sorted(self.scan()), ["good"])

    def test_results_are_cached_for_the_ttl(self):
        directory = VaultDirectory(self.runtime, ttl=60)
        self.assertEqual(directory.endpoints(), {})
        register(self.runtime, "late")
        self.assertEqual(directory.endpoints(), {})
        self.assertEqual(sorted(VaultDirectory(self.runtime, ttl=60).endpoints()), ["late"])

    def test_rejects_relative_runtime(self):
        with self.assertRaises(ValueError):
            VaultDirectory(Path("relative"))


class HealthCapabilityTests(unittest.TestCase):
    def health(self, capabilities):
        return _result("health", {"protocol": 1, "vault": "iam", "app_name": "IAM", "app_root": "/v/iam",
                                  "capabilities": capabilities}, {})

    def test_base_plugins_and_vault_management_plugins_are_both_accepted(self):
        self.assertTrue(self.health(sorted(BASE_CAPABILITIES)))
        self.assertTrue(self.health(list(RESULT_KEYS)))

    def test_missing_unknown_or_duplicate_capabilities_are_rejected(self):
        self.assertFalse(self.health(sorted(BASE_CAPABILITIES - {"read"})))
        self.assertFalse(self.health(sorted(BASE_CAPABILITIES) + ["shell"]))
        self.assertFalse(self.health(sorted(BASE_CAPABILITIES) + ["read"]))
        self.assertFalse(self.health("health"))


if __name__ == "__main__":
    unittest.main()
