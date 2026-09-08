"""The Git correction writer, against a stub GitHub API.

``StubGitHub`` implements ``record.Transport``, so every request the writer would put on
the wire is recorded here instead — method, path, headers and decoded body — and the
assertions below are about request bodies, not just outcomes. No socket is opened: the
base URL is the real host because the writer refuses every other, but nothing here holds
a transport that could reach it. The proposal under test is the first live one the pilot
produced, copied verbatim from ``outputs/e2e-proposal.json`` into the fixture.
"""

import contextlib
import json
import pathlib
import threading
import unittest
import urllib.parse
from unittest import mock

from flagger_recovery import gitwriter, policy
from flagger_recovery.decide import reconcile
from flagger_recovery.gitwriter import Correction, GitWriter, corrector
from flagger_recovery.policy import ALLOWED_TARGETS, Refuse
from flagger_recovery.proposal import KIND_PROPOSAL
from flagger_recovery.record import (ApiWriteError, Document, InMemoryStore, canary_label,
                                     label_value, make_key_parts)
from flagger_recovery.server import CORRECTION_QUEUED, Receiver, queued_corrector
from tests.test_server import TOKEN, FakeCandidates

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
LIVE = json.loads((FIXTURES / "live-proposal.json").read_text(encoding="utf-8"))

TARGET = ALLOWED_TARGETS[0]
FAILED_SHA = LIVE["failed_source_sha"]
PROMOTED_SHA = LIVE["last_promoted_source_sha"]
TEMPLATE_HASH = LIVE["template_hash"]
PROMOTED_HASH = LIVE["promoted_identity"]["template_hash"]
AHEAD_SHA, FORK_SHA = "9" * 40, "8" * 40
BLOB_FAILED, BLOB_PROMOTED = "1" * 40, "2" * 40
NEW_TREE, NEW_COMMIT, HEAD_TREE = "3" * 40, "4" * 40, "5" * 40
BASE_URL = gitwriter.DEFAULT_BASE_URL  # the only host the writer accepts; the transport is a stub
TOKEN_VALUE = "ghs-not-a-real-token"

def proposal(**changes):
    return {**LIVE, **changes}

class FakeLive:
    """The two ``decide.LiveState`` methods the policy consults. ``serving`` is the spec
    the ``-primary`` Deployment is running, ``None`` while it is mid-rollout."""
    def __init__(self, applied=TEMPLATE_HASH, promoted=PROMOTED_HASH, serving=...):
        self.status = {"lastAppliedSpec": applied, "lastPromotedSpec": promoted}
        self.serving = promoted if serving is ... else serving

    def canary_status(self):
        return self.status

    def primary_template_hash(self):
        return self.serving

def views(*live):
    """A ``live_for`` over a script of views: one per ``evaluate`` pass, the last
    repeating. A real view memoises its reads, so the two passes must not share one."""
    calls = []
    def _next():
        calls.append(None)
        return live[min(len(calls) - 1, len(live) - 1)]
    return _next

def _json(payload):
    return 200, json.dumps(payload).encode("utf-8")

class StubGitHub:
    """Routes the seven endpoints the writer can reach and records every call."""
    def __init__(self, *, head=FAILED_SHA, blobs=None, compares=None, patch_status=200, contents=None):
        self.head = head
        self.blobs = {FAILED_SHA: BLOB_FAILED, PROMOTED_SHA: BLOB_PROMOTED, **(blobs or {})}
        # The promoted revision is on the branch: the failed one descends from it.
        self.compares = {(PROMOTED_SHA, FAILED_SHA): ("ahead", (TARGET,)), **(compares or {})}
        self.contents = contents  # a whole /contents payload for the restore revision
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
            if self.contents is not None and ref == PROMOTED_SHA:
                return _json(self.contents)
            blob = self.blobs.get(ref)
            return _json({"type": "file", "sha": blob}) if blob else (404, b"{}")
        if method == "GET" and path.startswith("/compare/"):
            base, head = path[len("/compare/"):].split("...")
            entry = self.compares.get((base, head), ("ahead", ()))
            if entry is None:  # GitHub cannot compare these two at all
                return 404, b'{"message": "Not Found"}'
            # As documented: the changed files come back on this one response, capped at
            # 300 for the whole comparison, with a commit total and no file total. Paging
            # would page the commits; a 700-file comparison simply answers 300 of them.
            return _json({"status": entry[0], "total_commits": 1, "files": [
                {"filename": name} for name in entry[1][:gitwriter.COMPARE_FILE_CAP]]})
        if method == "POST" and path in ("/git/trees", "/git/commits"):
            return _json({"sha": NEW_TREE if path.endswith("trees") else NEW_COMMIT})
        if method == "PATCH" and path == "/git/refs/heads/flagger-pilot":
            return self.patch_status, b'{"message": "Update is not a fast forward"}'
        raise AssertionError(f"unrouted {method} {path}")

class _FlakyTransport:
    """Wraps another transport, except its very first call raises ``exc`` before
    reaching it at all — the shape of a connect that never got an answer."""
    def __init__(self, stub, exc):
        self._stub, self._exc, self.calls = stub, exc, 0

    def request(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise self._exc
        return self._stub.request(*args, **kwargs)

class _AnyAuthStubGitHub(StubGitHub):
    """``StubGitHub`` that records whether each call carried an ``Authorization`` header
    instead of asserting a fixed one, for tests where the credential changes between
    calls on the same writer (missing, then present on a retry)."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.authorized: list[bool] = []

    def request(self, method, url, *, headers, body, ca_file, timeout):
        assert url.startswith(BASE_URL + "/repos/") and headers["User-Agent"] and timeout > 0
        self.authorized.append("Authorization" in headers)
        path = url.split("/repos/", 1)[1].split("/", 2)[2]
        self.calls.append((method, "/" + path, json.loads(body) if body else None))
        return self._route(method, "/" + path)

def writer(stub, *, dry_run=False, repository=policy.ALLOWED_REPOSITORY):
    return GitWriter(lambda: TOKEN_VALUE, repository=repository, base_url=BASE_URL,
                     dry_run=dry_run, transport=stub)

def run(stub=None, *, live=None, dry_run=False, **changes):
    stub = stub or StubGitHub()
    live_for = live if callable(live) else views(live or FakeLive())
    return writer(stub, dry_run=dry_run).correct(proposal(**changes), live_for), stub

class GitWriterTests(unittest.TestCase):
    def test_defaults_are_refusal_first(self):
        self.assertTrue(gitwriter.DEFAULT_BASE_URL.startswith("https://api.github.com"))
        self.assertFalse(gitwriter.CORRECTIONS_ENABLED)
        self.assertTrue(GitWriter(lambda: "").dry_run)
        # The first of these starts with the right characters, resolves to
        # attacker.example, and is where the bearer token would then have gone.
        for base_url in ("https://api.github.com@attacker.example", "https://github.invalid/api",
                         "http://api.github.com", "https://api.github.com:8443",
                         "https://api.github.com?x=1", "https://api.github.com/extra"):
            with self.subTest(base_url=base_url), self.assertRaises(ValueError):
                GitWriter(lambda: "", base_url=base_url)
        # The repository reaches every request path, and the read helpers are public.
        for repository in ("", "owner", "owner/name/extra", "owner/../../x", "owner/na me"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                GitWriter(lambda: "", repository=repository)

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

    def test_no_credential_at_write_time_refuses_and_touches_nothing_mutating(self):
        # enabled (dry_run=False) but the token callable answers empty: reads still
        # happen (anonymously), the write does not, and nothing is claimed.
        stub = _AnyAuthStubGitHub()
        gw = GitWriter(lambda: "", base_url=BASE_URL, dry_run=False, transport=stub)
        with self.assertLogs("flagger_recovery.gitwriter", "WARNING") as logged:
            result = gw.correct(proposal(), views(FakeLive()))
        self.assertEqual(result.verdict.reason, policy.CREDENTIAL_UNAVAILABLE)
        self.assertEqual(result.commit_sha, "")
        self.assertEqual(stub.mutating, [])
        self.assertNotIn(True, stub.authorized)
        self.assertIn("no credential is readable", logged.output[0])

    def test_non_fast_forward_patch_refuses_branch_moved_and_stops(self):
        result, stub = run(StubGitHub(patch_status=422))
        self.assertEqual(result.verdict.reason, policy.BRANCH_MOVED)
        self.assertEqual(result.commit_sha, "")
        # The ref update is the last call made: nothing is retried, forced or cleaned up.
        self.assertEqual(stub.calls[-1][0], "PATCH")
        self.assertEqual(len(stub.mutating), 3)

    def test_a_later_commit_outside_the_prefix_is_still_correctable(self):
        stub = StubGitHub(head=AHEAD_SHA, blobs={AHEAD_SHA: BLOB_FAILED},
                          compares={(FAILED_SHA, AHEAD_SHA): ("ahead", (".github/workflows/ci.yaml",))})
        result, _ = run(stub)
        self.assertTrue(result.verdict, result.verdict.reason)
        self.assertEqual(result.commit["parents"], [AHEAD_SHA])
        # 299 changed files is under GitHub's cap, so the list is whole and the prefix
        # bound is proven over all of them. It costs one request, not one per hundred.
        under_cap = StubGitHub(head=AHEAD_SHA, blobs={AHEAD_SHA: BLOB_FAILED}, compares={
            (FAILED_SHA, AHEAD_SHA): ("ahead", tuple(f"docs/{n}.md" for n in range(299)))})
        self.assertTrue(run(under_cap)[0].verdict)
        self.assertEqual([call[1] for call in under_cap.calls if call[1].startswith("/compare/")],
                         [f"/compare/{FAILED_SHA}...{AHEAD_SHA}", f"/compare/{PROMOTED_SHA}...{AHEAD_SHA}"])

    def test_revert_commit_is_accepted_only_at_single_file_scope(self):
        revert = {"correction": f"revert-commit {FAILED_SHA}"}
        result, stub = run(**revert)
        self.assertEqual(result.verdict.target_path, TARGET)
        self.assertEqual(result.verdict.restore_sha, PROMOTED_SHA)
        # The head being the failed revision makes the on-branch and the undone
        # comparison one request, and it is issued once.
        self.assertEqual([call[1] for call in stub.calls if call[1].startswith("/compare/")],
                         [f"/compare/{PROMOTED_SHA}...{FAILED_SHA}"])
        mixed = StubGitHub(compares={(PROMOTED_SHA, FAILED_SHA): ("ahead", (TARGET, "kustomization.yaml"))})
        self.assertEqual(run(mixed, **revert)[0].verdict.reason, policy.MIXED_SCOPE)

    def test_bounds_refuse_before_any_request(self):
        cases = {
            policy.WRONG_REF: {"branch": "main"},
            policy.WRONG_REPOSITORY: {"repository": "someone/else"},
            policy.PATH_OUTSIDE_PREFIX: {"correction": f"restore-file kubernetes/apps/x.yaml to {PROMOTED_SHA}"},
            policy.NOT_AN_ALLOWED_TARGET: {
                "correction": f"restore-file kubernetes/pilot/flagger-pilot/canary.yaml to {PROMOTED_SHA}"},
            policy.REQUIRES_DECISION: {"requires_decision": True, "correction": None},
            policy.UNUSABLE_SHA: {"correction": "restore-file " + TARGET + " to deadbeef"},
            policy.UNPARSABLE: {"correction": "delete-file " + TARGET},
            policy.SUPERSEDED: {"template_hash": "0000000000"},
        }
        # ``$`` would have accepted every trailing newline in the last four as an end.
        extra = ((policy.UNUSABLE_SHA, {"last_promoted_source_sha": PROMOTED_SHA + "\n"}),
                 (policy.UNUSABLE_SHA, {"correction": "revert-commit HEAD~1"}),
                 (policy.UNPARSABLE, {"correction": f"restore-file {TARGET} to {PROMOTED_SHA}\n"}),
                 (policy.UNPARSABLE, {"correction": f"revert-commit {FAILED_SHA}\n"}),
                 (policy.UNPARSABLE, {"correction": f"revert-commit {AHEAD_SHA}"}))
        for reason, changes in tuple(cases.items()) + extra:
            with self.subTest(reason=reason, changes=changes):
                result, stub = run(**changes)
                self.assertEqual(result.verdict.reason, reason)
                self.assertEqual(stub.calls, [])
        self.assertEqual(run(live=FakeLive(promoted=TEMPLATE_HASH))[0].verdict.reason, policy.ALREADY_RESTORED)
        built_for_another = writer(StubGitHub(), repository="someone/else")
        self.assertEqual(built_for_another.correct(proposal(), views(FakeLive())).verdict.reason,
                         policy.WRONG_REPOSITORY)

    def _refuses(self, cases):
        for reason, github, changes, live in cases:
            with self.subTest(reason=reason, github=github, live=live):
                result, stub = run(StubGitHub(**github), live=live, **changes)
                self.assertEqual(result.verdict.reason, reason)
                self.assertEqual(stub.mutating, [])

    def test_a_restore_revision_off_the_branch_refuses_before_any_write(self):
        """The bound on what bytes get committed. A fork's PR head lives in this public
        repository's object store and ``/contents`` serves it, so only ``compare`` can
        say whether the revision is this branch's own history — and the revert form has
        to ask the same of the diff it undoes, which means keeping that status."""
        fork = {"last_promoted_source_sha": FORK_SHA, "correction": f"restore-file {TARGET} to {FORK_SHA}"}
        off_branch = policy.RESTORE_NOT_ON_BRANCH
        self._refuses((
            (off_branch, {"blobs": {FORK_SHA: "6" * 40},
                          "compares": {(FORK_SHA, FAILED_SHA): ("diverged", (TARGET,))}}, fork, None),
            (off_branch, {"compares": {(PROMOTED_SHA, FAILED_SHA): ("behind", ())}}, {}, None),  # pre-rebase
            (off_branch, {"compares": {(PROMOTED_SHA, FAILED_SHA): None}}, {}, None),  # 404: uncomparable
            (off_branch, {"compares": {(PROMOTED_SHA, FAILED_SHA): ("diverged", (TARGET,))}},
             {"correction": f"revert-commit {FAILED_SHA}"}, None),
        ))

    def test_the_other_write_time_bounds_refuse_before_any_write(self):
        """300 files is GitHub's cap for a whole comparison, so the change that matters may
        be past it — here the 401st of 700 is under the pilot prefix and the answer never
        mentions it — and the prefix bound cannot be proven. A symlink, a submodule or a
        directory (a JSON array) is not a blob to install: restating 100644 is not a cast."""
        ahead = {"head": AHEAD_SHA, "blobs": {AHEAD_SHA: BLOB_FAILED}}
        symlink = {"type": "symlink", "sha": "7" * 40}
        past_cap = tuple(policy.ALLOWED_PATH_PREFIX + "canary.yaml" if n == 400 else f"docs/{n}.md"
                         for n in range(700))
        self._refuses((
            (policy.BRANCH_MOVED, {**ahead, "compares": {(FAILED_SHA, AHEAD_SHA): ("ahead", past_cap)}},
             {}, None),
            (policy.TARGET_CHANGED, {"head": AHEAD_SHA, "blobs": {AHEAD_SHA: "7" * 40}}, {}, None),
            (policy.BRANCH_MOVED, {**ahead, "compares": {(FAILED_SHA, AHEAD_SHA): ("ahead", (TARGET,))}}, {}, None),
            (policy.BRANCH_MOVED, {**ahead, "compares": {(FAILED_SHA, AHEAD_SHA): ("diverged", ())}}, {}, None),
            (policy.NO_CHANGE, {"blobs": {PROMOTED_SHA: BLOB_FAILED}}, {}, None),
            (policy.UNUSABLE_SHA, {"contents": symlink}, {}, None),
            (policy.UNUSABLE_SHA, {"contents": {**symlink, "type": "submodule"}}, {}, None),
            (policy.UNUSABLE_SHA, {"contents": [{"type": "file", "sha": "7" * 40}]}, {}, None),
        ))

    def test_the_live_view_is_read_again_after_the_git_round_trips(self):
        """The second pass reads the cluster afresh — an operator rolling back mid-write
        is what F8 exists for, and a memoised view cannot notice it. ``serving`` is the
        design's third re-read: the spec the primary is actually running."""
        self._refuses((
            (policy.SUPERSEDED, {}, {}, views(FakeLive(), FakeLive(applied="0" * 10))),
            (policy.ALREADY_RESTORED, {}, {}, views(FakeLive(), FakeLive(promoted=TEMPLATE_HASH))),
            (policy.SUPERSEDED, {}, {}, FakeLive(serving=None)),
            (policy.SUPERSEDED, {}, {}, FakeLive(serving="0" * 10)),
        ))

    def test_an_unusable_response_says_what_github_answered(self):
        for path, answer, expected in (("/git/trees", _json({}), "/git/trees"),  # a 2xx create with no sha
                                       ("/git/ref/", (500, b'{"message": "Server Error"}'), "Server Error")):
            with self.subTest(path=path):
                stub = StubGitHub()
                stub._route = lambda method, route, inner=stub._route: (
                    answer if route.startswith(path) else inner(method, route))
                with self.assertRaises(ApiWriteError) as raised:
                    run(stub)
                self.assertIn(expected, str(raised.exception))

    def test_every_reason_reached_here_is_in_the_closed_set(self):
        for reason in (policy.BRANCH_MOVED, policy.TARGET_CHANGED, policy.NO_CHANGE, policy.MIXED_SCOPE,
                       policy.WRONG_REPOSITORY, policy.ALREADY_RESTORED, policy.ALREADY_CORRECTED,
                       policy.DISABLED, policy.RESTORE_NOT_ON_BRANCH):
            self.assertIn(reason, policy.REFUSAL_REASONS)
        self.assertEqual(len(set(policy.REFUSAL_REASONS)), len(policy.REFUSAL_REASONS))

class CorrectorTests(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.stub = StubGitHub()
        self.writer = writer(self.stub)
        self.clock = lambda: "2026-09-07T21:12:42Z"
        # The module switch bounds everything below, so a test that needs a correction
        # to run has to turn it on — exactly as FRP-007b's deployment will.
        switch = mock.patch.object(gitwriter, "CORRECTIONS_ENABLED", True)
        switch.start()
        self.addCleanup(switch.stop)

    def correct(self, *, enabled=None, lock=contextlib.nullcontext):
        run_one = corrector(self.store, self.writer, lambda _ns, _name: FakeLive(),
                            clock=self.clock, lock=lock, enabled=enabled)
        return run_one(proposal())

    def test_the_switch_narrows_and_neither_a_caller_nor_a_flag_can_widen_it(self):
        with mock.patch.object(gitwriter, "CORRECTIONS_ENABLED", False):
            for enabled in (None, True):
                with self.subTest(enabled=enabled):
                    self.assertEqual(self.correct(enabled=enabled).verdict.reason, policy.DISABLED)
        self.assertEqual((self.store.writes, self.stub.calls), (0, []))
        self.assertEqual(self.correct(enabled=False).verdict.reason, policy.DISABLED)

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
        self.correct(lock=lambda: _recording(held))
        self.assertEqual(held, ["entered", "exited"])
        marker = self.store.get_document(policy.KIND_CORRECTION, _key(policy.KIND_CORRECTION))
        self.assertEqual(marker.payload["status"], policy.STATUS_IN_PROGRESS)
        linked = self.store.get_document(policy.KIND_CORRECTION_RESULT, _key(policy.KIND_CORRECTION_RESULT))
        self.assertEqual(linked.payload["old_source_sha"], FAILED_SHA)
        self.assertEqual(linked.payload["new_source_sha"], NEW_COMMIT)
        self.assertEqual(linked.payload["restore_sha"], PROMOTED_SHA)

    def test_a_refused_proposal_leaves_no_marker_behind(self):
        run_one = corrector(self.store, self.writer, lambda _ns, _name: FakeLive(),
                            clock=self.clock, lock=contextlib.nullcontext)
        self.assertEqual(run_one(proposal(branch="main")).verdict.reason, policy.WRONG_REF)
        self.assertEqual((self.store.writes, self.stub.calls), (0, []))

    def test_a_refusal_after_the_reads_leaves_nothing_and_the_next_attempt_retries(self):
        # Storage is create-only, so a marker written before a transient refusal — a
        # concurrent push, a file that moved — would suppress that proposal for good.
        self.stub.head = AHEAD_SHA
        self.stub.blobs[AHEAD_SHA] = "7" * 40  # target-changed
        self.assertEqual(self.correct().verdict.reason, policy.TARGET_CHANGED)
        self.assertEqual(self.store.writes, 0)
        self.stub.blobs[AHEAD_SHA] = BLOB_FAILED
        self.stub.compares[(FAILED_SHA, AHEAD_SHA)] = ("ahead", ("README.md",))
        self.assertEqual(self.correct().commit_sha, NEW_COMMIT)
        # Losing the claim to a concurrent writer costs no tree, commit or ref update.
        lost = self.writer.correct(proposal(), views(FakeLive()), claim=lambda _verdict: False)
        self.assertEqual(lost.verdict.reason, policy.ALREADY_CORRECTED)

    def test_a_live_view_that_is_not_rebuilt_refuses_before_any_write(self):
        """R2's fix rests on a fresh view per ``evaluate`` pass: a call site closing over one
        ``LiveCanary`` would hand the second pass the first's memoised reads."""
        one_view = FakeLive()
        run_one = corrector(self.store, self.writer, lambda _ns, _name: one_view,
                            clock=self.clock, lock=contextlib.nullcontext)
        with self.assertLogs("flagger_recovery.gitwriter", level="ERROR"):
            self.assertEqual(run_one(proposal()).verdict.reason, policy.SUPERSEDED)
        self.assertEqual((self.store.writes, self.stub.mutating), (0, []))

    def test_a_correction_the_queue_dropped_is_re_offered_by_the_next_pass(self):
        """A full queue drops the in-band offer, leaving a proposal with no claim beside it.
        The pass re-offers exactly those, and leaves a claimed or finished one alone."""
        offers = []
        self.store.put_document(Document(
            kind=KIND_PROPOSAL, key=_key(KIND_PROPOSAL),
            labels={"flagger-recovery/canary": canary_label(LIVE["namespace"], LIVE["canary_name"]),
                    "flagger-recovery/template-hash": label_value(TEMPLATE_HASH)},
            payload=proposal()))
        report = reconcile(self.store, FakeLive(), corrector=offers.append)
        self.assertEqual((report.corrections_reoffered, report.proposals_open), (1, 1))
        self.assertEqual(offers[0]["correction"], LIVE["correction"])
        self.correct()  # now claimed and finished
        self.assertEqual(reconcile(self.store, FakeLive(), corrector=offers.append).corrections_reoffered, 0)
        self.assertEqual(len(offers), 1)

    def test_a_crashed_writer_is_stale_and_a_finished_one_is_not(self):
        marker = {"status": policy.STATUS_IN_PROGRESS, "started_at": "2026-09-07T21:00:00Z"}
        self.assertFalse(policy.is_stale(marker, "2026-09-07T21:05:00Z"))
        self.assertTrue(policy.is_stale(marker, "2026-09-07T21:15:00Z"))
        self.assertTrue(policy.is_stale({"status": policy.STATUS_IN_PROGRESS}, "2026-09-07T21:00:00Z"))
        # A correction that finished writes a result beside its marker and is not a
        # crash; the marker itself is never updated, storage being create-only.
        self.correct()
        with mock.patch("flagger_recovery.decide.now", lambda: "2026-09-07T21:30:00Z"):
            self.assertEqual(reconcile(self.store, FakeLive()).corrections_stale, 0)
            self.store.put_document(Document(  # the crash the signal is for: no result
                kind=policy.KIND_CORRECTION, key=make_key_parts("iot", "other", "abc", policy.KIND_CORRECTION),
                labels={"flagger-recovery/canary": "iot.other", "flagger-recovery/template-hash": "abc"},
                payload=marker))
            self.assertEqual(reconcile(self.store, FakeLive()).corrections_stale, 1)

    def test_the_receiver_seam_forwards_the_payload_and_never_raises(self):
        seen = []
        receiver = Receiver(token=TOKEN, store=InMemoryStore(), candidates=FakeCandidates(),
                            corrector=lambda payload: seen.append(payload) or Correction(Refuse("x")))
        self.assertEqual(receiver._correct(_StubProposal()), "")
        self.assertEqual(seen, [{"template_hash": TEMPLATE_HASH}])
        with self.assertLogs("flagger_recovery.server", level="ERROR"):
            Receiver(token=TOKEN, store=InMemoryStore(), candidates=FakeCandidates(),
                     corrector=_boom)._correct(_StubProposal())

    def test_the_hook_answers_before_the_queued_correction_runs(self):
        started, release, ran = threading.Event(), threading.Event(), []

        def _work(payload):
            if not payload:
                raise RuntimeError("the writer fell over")
            started.set()
            release.wait(5)
            ran.append(payload)

        enqueue = queued_corrector(_work)
        receiver = Receiver(token=TOKEN, store=InMemoryStore(), candidates=FakeCandidates(), corrector=enqueue)
        with self.assertLogs("flagger_recovery.server", level="ERROR"):
            enqueue({})  # one worker, in order: a fault on it is logged and survived
            self.assertEqual(receiver._correct(_StubProposal()), CORRECTION_QUEUED)
            self.assertTrue(started.wait(5))
            self.assertEqual(ran, [])  # the hook answered with the writer still in flight
            release.set()

    def test_a_transport_failure_on_the_queue_is_transport_unavailable_and_counted(self):
        """F28: a stub whose first GET times out — the shape of the live pilot's
        ``socket.create_connection`` failures. Neither ``corrector()`` nor ``evaluate()``
        ever sees it; only the queued worker's own except clause does, and it must count
        it rather than only trace it. The next attempt, over the same writer, succeeds.

        One worker processes the queue strictly in order, so a marker payload enqueued
        right after the failing one is only picked up once the failure was fully handled
        (logged and counted) — a reliable wait with no sleep and no race on ``done``."""
        flaky = _FlakyTransport(StubGitHub(), TimeoutError("timed out"))
        run_one = corrector(self.store, writer(flaky), lambda _ns, _name: FakeLive(),
                            clock=self.clock, lock=contextlib.nullcontext)
        processed = threading.Event()

        def _run_one(payload):
            if payload.get("marker"):
                processed.set()
                return None
            return run_one(payload)

        enqueue = queued_corrector(_run_one)
        with self.assertLogs("flagger_recovery.server", level="WARNING") as logged:
            enqueue(proposal())
            enqueue({"marker": True})
            self.assertTrue(processed.wait(5))
        self.assertIn(policy.TRANSPORT_UNAVAILABLE, logged.output[0])
        self.assertIn("TimeoutError", logged.output[0])
        self.assertNotIn(gitwriter.API_HOST, logged.output[0])  # no URL, query or header
        self.assertEqual(enqueue.drain_failures(), 1)
        self.assertEqual(enqueue.drain_failures(), 0, "a drain must not double-count")
        self.assertIsNone(self.store.get_document(policy.KIND_CORRECTION, _key(policy.KIND_CORRECTION)))

        processed.clear()
        enqueue(proposal())
        enqueue({"marker": True})
        self.assertTrue(processed.wait(5))
        self.assertEqual(enqueue.drain_failures(), 0)
        linked = self.store.get_document(policy.KIND_CORRECTION_RESULT, _key(policy.KIND_CORRECTION_RESULT))
        self.assertEqual(linked.payload["new_source_sha"], NEW_COMMIT)

@contextlib.contextmanager
def _recording(log):
    log.append("entered")
    yield
    log.append("exited")

class _StubProposal:
    key = "proposal-key"

    def to_payload(self):
        return {"template_hash": TEMPLATE_HASH}

def _boom(_payload):
    raise RuntimeError("the writer fell over")

def _key(kind):
    return make_key_parts(LIVE["namespace"], LIVE["canary_name"], TEMPLATE_HASH, kind)

if __name__ == "__main__":
    unittest.main()
