"""Guard the effective Groschen Renovate policy and paired Sandbox images."""

import fnmatch
import json
import pathlib
import re
import subprocess
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = "ghcr.io/jlengelbrecht/groschen"
APP_FILE = "kubernetes/apps/groschen-test/groschen/app/deployment.yaml"
ADMIN_FILE = "kubernetes/apps/groschen-test/groschen/admin/job.yaml"
DIGEST = "ef71d858c9a1c3b2ab1b412d352bbfd5b1a74eea7dc2be87a9c885e2d5e07fee"


def load_json5(path):
    """Parse trusted repository JSON5 with the installed Node runtime, without packages."""
    script = (
        "const fs=require('fs'),vm=require('vm');"
        "const source=fs.readFileSync(process.argv[1],'utf8');"
        "process.stdout.write(JSON.stringify(vm.runInNewContext('('+source+')',"
        "Object.create(null),{timeout:1000})));"
    )
    return json.loads(subprocess.check_output(
        ["node", "-e", script, str(ROOT / path)], text=True))


def pattern_matches(pattern, value):
    if pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], value) is not None
    return fnmatch.fnmatchcase(value, pattern)


def rule_matches(rule, package, file_name, update_type):
    values = {
        "matchDatasources": "docker",
        "matchPackageNames": package,
        "matchFileNames": file_name,
        "matchUpdateTypes": update_type,
        "matchManagers": "kubernetes",
        "matchCurrentValue": "main",
        "matchCurrentVersion": "main",
    }
    for key, patterns in rule.items():
        if not key.startswith("match"):
            continue
        if key not in values:
            return False  # Do not guess the outcome of unfamiliar matchers.
        if isinstance(patterns, str):
            patterns = [patterns]
        if not any(pattern_matches(pattern, values[key]) for pattern in patterns):
            return False
    return True


def effective_policy(rules, package, file_name, update_type):
    result = {}
    for rule in rules:
        if rule_matches(rule, package, file_name, update_type):
            result.update({key: value for key, value in rule.items()
                           if key not in ("description",) and not key.startswith("match")})
    return result


class GroschenRenovateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_config = load_json5(".github/renovate.json5")
        inherited = load_json5(".github/renovate/autoMerge.json5")
        cls.inherited_rules = inherited["packageRules"]
        cls.rules = cls.inherited_rules + cls.root_config["packageRules"]

    def policy(self, package=PACKAGE, file_name=APP_FILE, update_type="digest"):
        return effective_policy(self.rules, package, file_name, update_type)

    def test_sandbox_only_tracks_main_digest_and_waits_for_ci(self):
        for update_type in ("digest", "pinDigest"):
            with self.subTest(update_type=update_type):
                policy = self.policy(update_type=update_type)
                self.assertTrue(policy["enabled"])
                self.assertTrue(policy["automerge"])
                self.assertEqual(policy["automergeType"], "pr")
                self.assertFalse(policy["ignoreTests"])
                self.assertTrue(policy["platformAutomerge"])
                self.assertEqual(policy["minimumReleaseAge"], "0")
                self.assertTrue(policy["pinDigests"])
                self.assertEqual(policy["allowedVersions"], "/^main$/")
                self.assertEqual(policy["groupName"], "Groschen Sandbox image")
                self.assertEqual(policy["groupSlug"], "groschen-sandbox-image")

    def test_sandbox_tag_and_replacement_updates_are_disabled(self):
        for update_type in ("major", "minor", "patch", "pin", "rollback", "bump", "replacement"):
            with self.subTest(update_type=update_type):
                policy = self.policy(update_type=update_type)
                self.assertFalse(policy["enabled"])
                self.assertFalse(policy["automerge"])
                self.assertEqual(policy["allowedVersions"], "/^main$/")

    def test_admin_is_disabled_and_other_groschen_files_stay_manual(self):
        for update_type in ("digest", "pinDigest", "patch", "minor"):
            with self.subTest(update_type=update_type):
                self.assertFalse(self.policy(file_name=ADMIN_FILE, update_type=update_type)["enabled"])
                self.assertFalse(self.policy(file_name="kubernetes/apps/groschen/app/deployment.yaml",
                                             update_type=update_type)["automerge"])

    def test_busybox_policy_is_unaffected(self):
        for file_name in (APP_FILE, ADMIN_FILE):
            for update_type in ("digest", "patch"):
                with self.subTest(file_name=file_name, update_type=update_type):
                    original = effective_policy(
                        self.inherited_rules + self.root_config["packageRules"][:-4],
                        "busybox", file_name, update_type)
                    self.assertEqual(self.policy("busybox", file_name, update_type), original)

    def test_deployments_share_the_requested_main_digest(self):
        documents = list(yaml.safe_load_all((ROOT / APP_FILE).read_text()))
        images = {}
        for deployment in documents:
            if deployment.get("kind") == "Deployment":
                name = deployment["metadata"]["name"]
                images[name] = [container["image"] for container in
                                deployment["spec"]["template"]["spec"]["containers"]]
        self.assertEqual(images, {
            "groschen-api": [f"{PACKAGE}:main@sha256:{DIGEST}"],
            "groschen-worker": [f"{PACKAGE}:main@sha256:{DIGEST}"],
        })


if __name__ == "__main__":
    unittest.main()
