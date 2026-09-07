"""The Git correction writer, against a stub GitHub API.

``StubGitHub`` implements ``record.Transport``, so every request the writer would
put on the wire is recorded here instead — method, path, headers and decoded body
— and the assertions below are about request bodies, not just outcomes. No socket
is opened and no test names the real API host; the writer's own default is
asserted once, in ``test_defaults_are_refusal_first``.

The proposal under test is the first live one the pilot ever produced, copied
verbatim from ``outputs/e2e-proposal.json`` (cycle 2) into
``fixtures/live-proposal.json``.
"""

import json
import pathlib
import unittest
import urllib.parse

from flagger_recovery import gitwriter, policy
from flagger_recovery.decide import reconcile
from flagger_recovery.gitwriter import Correction, GitWriter, corrector
from flagger_recovery.policy import ALLOWED_TARGETS, Refuse
from flagger_recovery.record import InMemoryStore
from flagger_recovery.server import Receiver
from tests.test_server import TOKEN, FakeCandidates

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
LIVE = json.loads((FIXTURES / "live-proposal.json").read_text(encoding="utf-8"))

TARGET = ALLOWED_TARGETS[0]
FAILED_SHA = LIVE["failed_source_sha"]
PROMOTED_SHA = LIVE["last_promoted_source_sha"]
TEMPLATE_HASH = LIVE["template_hash"]
PROMOTED_HASH = LIVE["promoted_identity"]["template_hash"]
AHEAD_SHA = "9" * 40
BLOB_FAILED, BLOB_PROMOTED = "1" * 40, "2" * 40
NEW_TREE, NEW_COMMIT, HEAD_TREE = "3" * 40, "4" * 40, "5" * 40
BASE_URL = "https://github.invalid/api"  # never the real host: nothing here leaves the process
TOKEN_VALUE = "ghs-not-a-real-token"

def proposal(**changes):
    return {**LIVE, **changes}

class FakeLive:
    """``decide.LiveState``'s one method the policy consults."""

    def __init__(self, applied=TEMPLATE_HASH, promoted=PROMOTED_HASH):
        self.status = {"lastAppliedSpec": applied, "lastPromotedSpec": promoted}

    def canary_status(self):
        return self.status

def _json(payload):
    return 200, json.dumps(payload).encode("utf-8")

class StubGitHub:
    """Routes the seven endpoints the writer can reach and records every call."""

    def __init__(self, *, head=FAILED_SHA, blobs=None, compares=None, patch_status=200):
        self.head = head
        self.blobs = {FAILED_SHA: BLOB_FAILED, PROMOTED_SHA: BLOB_PROMOTED, **(blobs or {})}
        self.compares = {(PROMOTED_SHA, FAILED_SHA): ("ahead", (TARGET,)), **(compares or {})}
        self.patch_status = patch_status
        self.calls = []

    @property
    def mutating(self):
        return [call for call in self.calls if call[0] != "GET"]

    def request(self, method, url, *, headers, body, ca_file, timeout):
        assert url.startswith(BASE_URL + "/repos/"), url
        assert headers["User-Agent"] and headers["Authorization"] == f"Bearer {TOKEN_VALUE}"
        assert timeout > 0
        path = url.split("/repos/", 1)[1].split("/", 2)[2]
        self.calls.append((method, "/" + path, json.loads(body) if body else None))
        return self._route(method, "/" + path)

    def _route(self, method, path):
        if method == "GET" and path.startswith("/git/ref/"):
            return _json({"object": {"sha": self.head}})
        if method == "GET" and path.startswith("/git/commits/"):
            return _json({"tree": {"sha": HEAD_TREE}})
        if method == "GET" and path.startswith("/contents/"):
            ref = urllib.parse.parse_qs(path.split("?", 1)[1])["ref"][0]
            blob = self.blobs.get(ref)
            return _json({"type": "file", "sha": blob}) if blob else (404, b"{}")
        if method == "GET" and path.startswith("/compare/"):
            base, head = path[len("/compare/"):].split("...")
            state, files = self.compares.get((base, head), ("ahead", ()))
            return _json({"status": state, "files": [{"filename": name} for name in files]})
        if method == "POST" and path in ("/git/trees", "/git/commits"):
            return _json({"sha": NEW_TREE if path.endswith("trees") else NEW_COMMIT})
        if method == "PATCH" and path == "/git/refs/heads/flagger-pilot":
            return self.patch_status, b'{"message": "Update is not a fast forward"}'
        raise AssertionError(f"unrouted {method} {path}")

def writer(stub, *, dry_run=False, repository=policy.ALLOWED_REPOSITORY):
    return GitWriter(lambda: TOKEN_VALUE, repository=repository, base_url=BASE_URL,
                     dry_run=dry_run, transport=stub)

def run(stub=None, *, live=None, dry_run=False, **changes):
    stub = stub or StubGitHub()
    return writer(stub, dry_run=dry_run).correct(proposal(**changes), live or FakeLive()), stub

class GitWriterTests(unittest.TestCase):
    def test_defaults_are_refusal_first(self):
        self.assertTrue(gitwriter.DEFAULT_BASE_URL.startswith("https://api.github.com"))
        self.assertFalse(gitwriter.CORRECTIONS_ENABLED)
        self.assertTrue(GitWriter(lambda: "").dry_run)
        with self.assertRaises(ValueError):
            GitWriter(lambda: "", base_url="http://github.invalid")

    def test_happy_path_writes_one_commit_over_the_live_proposal(self):
        result, stub = run()
        self.assertTrue(result.verdict, result.verdict.reason)
        self.assertEqual(result.commit_sha, NEW_COMMIT)
        self.assertEqual(result.tree, {"base_tree": HEAD_TREE, "tree": [
            {"path": TARGET, "mode": "100644", "type": "blob", "sha": BLOB_PROMOTED}]})
        # Exactly one path, inside the prefix, and a blob that differs from the head's.
        self.assertEqual([entry["path"] for entry in result.tree["tree"]], [TARGET])
        self.assertNotEqual(result.tree["tree"][0]["sha"], BLOB_FAILED)
        self.assertEqual(result.commit, {
            "message": f"fix(flagger-pilot): restore helmrelease.yaml to {PROMOTED_SHA[:8]} "
                       f"after failed rollout {TEMPLATE_HASH}",
            "tree": NEW_TREE, "parents": [FAILED_SHA],
            "author": gitwriter.AUTHOR, "committer": gitwriter.AUTHOR})
        self.assertEqual(
            [(method, path, body) for method, path, body in stub.mutating],
            [("POST", "/git/trees", result.tree), ("POST", "/git/commits", result.commit),
             ("PATCH", "/git/refs/heads/flagger-pilot", {"sha": NEW_COMMIT, "force": False})])

    def test_dry_run_builds_the_payload_and_calls_nothing_mutating(self):
        result, stub = run(dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.commit_sha, "")
        self.assertEqual(stub.mutating, [])
        self.assertEqual(result.commit["parents"], [FAILED_SHA])
        self.assertTrue(result.commit["message"].startswith("fix(flagger-pilot): restore helmrelease.yaml"))
        self.assertEqual(result.commit["tree"], "")  # the one field a dry run cannot fill

    def test_non_fast_forward_patch_refuses_branch_moved_and_stops(self):
        result, stub = run(StubGitHub(patch_status=422))
        self.assertEqual(result.verdict.reason, policy.BRANCH_MOVED)
        self.assertEqual(result.commit_sha, "")
        # The ref update is the last call made: nothing is retried, forced or cleaned up.
        self.assertEqual(stub.calls[-1][0], "PATCH")
        self.assertEqual(len(stub.mutating), 3)

    def test_blob_mismatch_refuses_target_changed(self):
        stub = StubGitHub(head=AHEAD_SHA, blobs={AHEAD_SHA: "7" * 40},
                          compares={(FAILED_SHA, AHEAD_SHA): ("ahead", ("README.md",))})
        result, stub = run(stub)
        self.assertEqual(result.verdict.reason, policy.TARGET_CHANGED)
        self.assertEqual(stub.mutating, [])

    def test_a_later_commit_inside_the_prefix_refuses_branch_moved(self):
        stub = StubGitHub(head=AHEAD_SHA, blobs={AHEAD_SHA: BLOB_FAILED},
                          compares={(FAILED_SHA, AHEAD_SHA): ("ahead", (TARGET,))})
        self.assertEqual(run(stub)[0].verdict.reason, policy.BRANCH_MOVED)
        diverged = StubGitHub(head=AHEAD_SHA, blobs={AHEAD_SHA: BLOB_FAILED},
                              compares={(FAILED_SHA, AHEAD_SHA): ("diverged", ())})
        self.assertEqual(run(diverged)[0].verdict.reason, policy.BRANCH_MOVED)

    def test_a_later_commit_outside_the_prefix_is_still_correctable(self):
        stub = StubGitHub(head=AHEAD_SHA, blobs={AHEAD_SHA: BLOB_FAILED},
                          compares={(FAILED_SHA, AHEAD_SHA): ("ahead", (".github/workflows/ci.yaml",))})
        result, _ = run(stub)
        self.assertTrue(result.verdict, result.verdict.reason)
        self.assertEqual(result.commit["parents"], [AHEAD_SHA])

    def test_an_unchanged_file_refuses_rather_than_committing_nothing(self):
        result, stub = run(StubGitHub(blobs={PROMOTED_SHA: BLOB_FAILED}))
        self.assertEqual(result.verdict.reason, policy.NO_CHANGE)
        self.assertEqual(stub.mutating, [])

    def test_revert_commit_is_accepted_only_at_single_file_scope(self):
        revert = {"correction": f"revert-commit {FAILED_SHA}"}
        result, _ = run(**revert)
        self.assertEqual(result.verdict.target_path, TARGET)
        self.assertEqual(result.verdict.restore_sha, PROMOTED_SHA)
        mixed = StubGitHub(compares={(PROMOTED_SHA, FAILED_SHA): ("ahead", (TARGET, "kustomization.yaml"))})
        self.assertEqual(run(mixed, **revert)[0].verdict.reason, policy.MIXED_SCOPE)

    def test_bounds_refuse_before_any_request(self):
        cases = {
            policy.WRONG_REF: {"branch": "main"},
            policy.PATH_OUTSIDE_PREFIX: {"correction": f"restore-file kubernetes/apps/x.yaml to {PROMOTED_SHA}"},
            policy.NOT_AN_ALLOWED_TARGET: {
                "correction": f"restore-file kubernetes/pilot/flagger-pilot/canary.yaml to {PROMOTED_SHA}"},
            policy.REQUIRES_DECISION: {"requires_decision": True, "correction": None},
            policy.UNUSABLE_SHA: {"correction": "restore-file " + TARGET + " to deadbeef"},
            policy.UNPARSABLE: {"correction": "delete-file " + TARGET},
            policy.SUPERSEDED: {"template_hash": "0000000000"},
        }
        for reason, changes in cases.items():
            with self.subTest(reason=reason):
                result, stub = run(**changes)
                self.assertEqual(result.verdict.reason, reason)
                self.assertEqual(stub.calls, [])
        self.assertEqual(run(live=FakeLive(promoted=TEMPLATE_HASH))[0].verdict.reason, policy.ALREADY_RESTORED)

    def test_a_writer_pointed_at_another_repository_refuses(self):
        stub = StubGitHub()
        result = writer(stub, repository="someone/else").correct(proposal(), FakeLive())
        self.assertEqual(result.verdict.reason, policy.WRONG_REPOSITORY)
        self.assertEqual(stub.calls, [])

    def test_every_reason_reached_here_is_in_the_closed_set(self):
        for reason in (policy.BRANCH_MOVED, policy.TARGET_CHANGED, policy.NO_CHANGE, policy.MIXED_SCOPE,
                       policy.WRONG_REPOSITORY, policy.ALREADY_RESTORED, policy.ALREADY_CORRECTED,
                       policy.DISABLED):
            self.assertIn(reason, policy.REFUSAL_REASONS)
        self.assertEqual(len(set(policy.REFUSAL_REASONS)), len(policy.REFUSAL_REASONS))

class CorrectorTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.stub = StubGitHub()
        self.writer = writer(self.stub)
        self.clock = lambda: "2026-09-07T21:12:42Z"

    def correct(self, *, enabled=True, lock=None):
        run_one = corrector(self.store, self.writer, lambda _ns, _name: FakeLive(),
                            clock=self.clock, lock=lock, enabled=enabled)
        return run_one(proposal())

    def test_the_switch_is_off_by_default_and_nothing_happens(self):
        result = corrector(self.store, self.writer, lambda _ns, _name: FakeLive(),
                           clock=self.clock)(proposal())
        self.assertEqual(result.verdict.reason, policy.DISABLED)
        self.assertEqual((self.store.writes, self.stub.calls), (0, []))

    def test_a_second_identical_proposal_is_suppressed_with_zero_git_calls(self):
        first = self.correct()
        self.assertEqual(first.commit_sha, NEW_COMMIT)
        calls_after_first = len(self.stub.calls)
        second = self.correct()
        self.assertEqual(second.verdict.reason, policy.ALREADY_CORRECTED)
        self.assertEqual(len(self.stub.calls), calls_after_first)

    def test_a_dry_run_leaves_no_marker_to_suppress_the_real_correction(self):
        self.writer.dry_run = True
        self.assertTrue(self.correct().dry_run)
        self.assertEqual(self.store.writes, 0)
        self.writer.dry_run = False
        self.assertEqual(self.correct().commit_sha, NEW_COMMIT)

    def test_a_completed_write_links_the_old_and_new_revisions(self):
        held = []
        self.correct(lock=lambda: _Recording(held))
        self.assertEqual(held, ["entered", "exited"])
        marker = self.store.get_document(policy.KIND_CORRECTION, _key(policy.KIND_CORRECTION))
        self.assertEqual(marker.payload["status"], policy.STATUS_IN_PROGRESS)
        linked = self.store.get_document(policy.KIND_CORRECTION_RESULT, _key(policy.KIND_CORRECTION_RESULT))
        self.assertEqual(linked.payload["old_source_sha"], FAILED_SHA)
        self.assertEqual(linked.payload["new_source_sha"], NEW_COMMIT)
        self.assertEqual(linked.payload["restore_sha"], PROMOTED_SHA)

    def test_a_refused_proposal_leaves_no_marker_behind(self):
        run_one = corrector(self.store, self.writer, lambda _ns, _name: FakeLive(),
                            clock=self.clock, enabled=True)
        self.assertEqual(run_one(proposal(branch="main")).verdict.reason, policy.WRONG_REF)
        self.assertEqual((self.store.writes, self.stub.calls), (0, []))

    def test_a_crashed_writer_leaves_a_marker_the_reconcile_pass_calls_stale(self):
        marker = {"status": policy.STATUS_IN_PROGRESS, "started_at": "2026-09-07T21:00:00Z"}
        self.assertFalse(policy.is_stale(marker, "2026-09-07T21:05:00Z"))
        self.assertTrue(policy.is_stale(marker, "2026-09-07T21:15:00Z"))
        self.assertTrue(policy.is_stale({"status": policy.STATUS_IN_PROGRESS}, "2026-09-07T21:00:00Z"))
        # The fixed clock puts this marker well past the window, so the pass sees it.
        self.correct()
        self.assertEqual(reconcile(self.store, FakeLive()).corrections_stale, 1)

    def test_the_receiver_seam_forwards_the_payload_and_never_raises(self):
        seen = []
        receiver = Receiver(token=TOKEN, store=InMemoryStore(), candidates=FakeCandidates(),
                            corrector=lambda payload: seen.append(payload) or Correction(Refuse("x")))
        receiver._correct(_StubProposal())
        self.assertEqual(seen, [{"template_hash": TEMPLATE_HASH}])
        with self.assertLogs("flagger_recovery.server", level="ERROR"):
            Receiver(token=TOKEN, store=InMemoryStore(), candidates=FakeCandidates(),
                     corrector=_boom)._correct(_StubProposal())

class _Recording:
    def __init__(self, log):
        self._log = log

    def __enter__(self):
        self._log.append("entered")

    def __exit__(self, *_exc):
        self._log.append("exited")
        return False

class _StubProposal:
    key = "proposal-key"

    def to_payload(self):
        return {"template_hash": TEMPLATE_HASH}

def _boom(_payload):
    raise RuntimeError("the writer fell over")

def _key(kind):
    from flagger_recovery.record import make_key_parts
    return make_key_parts(LIVE["namespace"], LIVE["canary_name"], TEMPLATE_HASH, kind)

if __name__ == "__main__":
    unittest.main()
