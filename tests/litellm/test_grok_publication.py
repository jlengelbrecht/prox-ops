"""Mocked release-state tests; no Docker or registry access."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "publication_state", ROOT / "services/litellm-grok/publication_state.py")
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)

IMAGE = "ghcr.io/jlengelbrecht/litellm-grok"
SHA = "b" * 40
SOURCE = "https://github.com/jlengelbrecht/prox-ops"
CANDIDATE = "sha256:" + "a" * 64
EXISTING = "sha256:" + "c" * 64


def evidence(digest, revision=SHA, source=SOURCE, passed=True):
    """Build independent inspection and qualification evidence for one digest."""
    ref = f"{IMAGE}@{digest}"
    inspection = [{"RepoDigests": [ref], "Config": {"Labels": {
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.source": source}}}]
    receipt = {"status": "pass" if passed else "fail",
               "reason": None if passed else "asgi_qualification_failed",
               "published_digest": ref, "child_exit_zero": passed,
               "nonalias_paired_equal": passed,
               "generic_denial_paired_equal": passed,
               "checks": {f"check_{number}": "pass" for number in range(26)}}
    return inspection, receipt


class PublicationStateTests(unittest.TestCase):
    def plan(self, tag_state, existing=None):
        """Ask publication_state to decide with a qualified candidate."""
        candidate_info, candidate_receipt = evidence(CANDIDATE)
        extra = {} if existing is None else {
            "existing_inspection": existing[0], "existing_receipt": existing[1]}
        return state.decide(tag_state, IMAGE, CANDIDATE, candidate_info,
                            candidate_receipt, SHA, SOURCE, **extra)

    def test_first_publication_uses_qualified_candidate(self):
        self.assertEqual(self.plan({"state": "absent", "digest": None}),
                         {"action": "create", "digest": CANDIDATE})

    def test_writer_creates_commit_tag_between_lookup_and_push(self):
        decision = self.plan({"state": "absent", "digest": None})
        # The pre-create lookup observes the other writer's tag. The workflow
        # must stop before docker tag/push, leaving that tag untouched.
        with self.assertRaises(state.Denied):
            state.create_ready(decision, {"state": "present", "digest": EXISTING},
                               CANDIDATE)
        state.create_ready(decision, {"state": "absent", "digest": None}, CANDIDATE)
        with self.assertRaises(state.Denied):
            state.create_ready(decision, {"state": "unknown", "digest": None}, CANDIDATE)

    def test_rerun_after_commit_tag_creation_reuses_independently_qualified_digest(self):
        # A rebuild can differ while the already published tag still matches source.
        self.assertEqual(self.plan({"state": "present", "digest": EXISTING},
                                   evidence(EXISTING)),
                         {"action": "reuse", "digest": EXISTING})

    def test_conflicting_tag_and_missing_provenance_fail_closed(self):
        """Exercise each provenance failure without another failing field."""
        for inspection, receipt in (
            evidence(EXISTING, revision="d" * 40),
            evidence(EXISTING, source="https://github.com/other/repo"),
        ):
            with self.assertRaises(state.Denied):
                self.plan({"state": "present", "digest": EXISTING},
                          (inspection, receipt))
        inspection, receipt = evidence(EXISTING)
        inspection[0]["RepoDigests"] = [f"{IMAGE}@{CANDIDATE}"]
        with self.assertRaises(state.Denied):
            self.plan({"state": "present", "digest": EXISTING},
                      (inspection, receipt))
        with self.assertRaises(state.Denied):
            self.plan({"state": "present", "digest": EXISTING})
        with self.assertRaises(state.Denied):
            self.plan({"state": "unknown", "digest": None})
        with self.assertRaises(state.Denied):
            self.plan({"state": "present", "digest": EXISTING, "extra": True},
                      evidence(EXISTING))

    def test_unverified_digest_cannot_promote(self):
        inspection, receipt = evidence(EXISTING, passed=False)
        with self.assertRaises(state.Denied):
            self.plan({"state": "present", "digest": EXISTING},
                      (inspection, receipt))
        inspection, receipt = evidence(CANDIDATE)
        receipt["published_digest"] = f"{IMAGE}@{EXISTING}"
        with self.assertRaises(state.Denied):
            state.decide({"state": "absent", "digest": None}, IMAGE, CANDIDATE,
                         inspection, receipt, SHA, SOURCE)

    def test_stale_run_skips_promotion_and_newest_source_progresses(self):
        self.assertFalse(state.source_fresh(SHA, lambda *a, **k: SimpleNamespace(returncode=1)))
        self.assertTrue(state.source_fresh(SHA, lambda *a, **k: SimpleNamespace(returncode=0)))
        with self.assertRaises(state.Denied):
            state.source_fresh(SHA, lambda *a, **k: SimpleNamespace(returncode=128))


if __name__ == "__main__":
    unittest.main()
