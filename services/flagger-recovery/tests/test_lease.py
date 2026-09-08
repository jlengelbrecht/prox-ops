"""The writer's Lease, the correction switch, and the credential that is usually absent.

No socket: the Lease's transport is a recorder answering a script, and the GitHub side
reuses ``tests.test_gitwriter``'s stub. A 409 stands in for the API server's conflict
detection, the only race outcome this code is allowed to have."""

import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from flagger_recovery import gitwriter, policy, server
from flagger_recovery.gitwriter import GitWriter, corrector
from flagger_recovery.lease import DURATION_SECONDS, LEASE_NAME, Lease, _stamp
from flagger_recovery.policy import LockUnavailable
from flagger_recovery.record import ApiWriteError, InMemoryStore
from tests.test_gitwriter import BASE_URL, FakeLive, StubGitHub, proposal, writer as git_writer

BASE = "https://kubernetes.default.svc:443"
URL = f"{BASE}/apis/coordination.k8s.io/v1/namespaces/flagger-system/leases/{LEASE_NAME}"
NOW = datetime(2026, 9, 7, 21, 12, 42, tzinfo=timezone.utc)
US, THEM = "recovery-receiver-1", "recovery-receiver-0"

class FakeApi:  # one scripted (status, payload) per method, and every request recorded
    def __init__(self, get, put=(200, {"metadata": {"resourceVersion": "8"}}),
                 post=(201, {"metadata": {"resourceVersion": "9"}})):
        self.answers, self.calls = {"GET": get, "PUT": put, "POST": post}, []

    def request(self, method, url, *, headers, body, ca_file, timeout):
        assert headers["Authorization"] == "Bearer sa-token" and timeout > 0
        self.calls.append((method, url, json.loads(body) if body else None))
        status, payload = self.answers[method]
        return status, json.dumps(payload).encode("utf-8")

def held(holder, *, renewed=NOW, version="7", acquired=NOW):
    return 200, {"metadata": {"resourceVersion": version},
                 "spec": {"holderIdentity": holder, "leaseDurationSeconds": DURATION_SECONDS,
                          "acquireTime": _stamp(acquired), "renewTime": _stamp(renewed)}}

def lease(api, holder=US):
    return Lease(BASE, namespace="flagger-system", holder=holder, token="sa-token",
                 transport=api, clock=lambda: NOW)

class LeaseTests(unittest.TestCase):
    def test_an_absent_lease_is_created_held_and_released_free(self):
        api = FakeApi(get=(404, {}))
        with lease(api)() as version:
            self.assertEqual(version, "9")
        self.assertEqual([call[0] for call in api.calls], ["GET", "POST", "PUT"])
        created, released = api.calls[1][2]["spec"], api.calls[2][2]
        # The renewTime is MicroTime: six fractional digits, or the API server rejects it.
        self.assertEqual((created["holderIdentity"], created["leaseDurationSeconds"],
                          created["renewTime"]), (US, DURATION_SECONDS, "2026-09-07T21:12:42.000000Z"))
        self.assertEqual((api.calls[2][1], released["spec"]["holderIdentity"],
                          released["metadata"]["resourceVersion"]), (URL, "", "9"))

    def test_nothing_is_stolen_and_no_race_or_fault_acquires_silently(self):
        # A live holder, an unreadable one, a lost race, a broken API server: refuse,
        # refuse, refuse, be loud — and never come back holding it.
        stale = held(THEM, renewed=NOW - timedelta(hours=1))
        unreadable = (200, {"metadata": {}, "spec": {"holderIdentity": THEM, "renewTime": "soon"}})
        fresh = held(THEM, renewed=NOW - timedelta(seconds=DURATION_SECONDS - 1))
        for label, api, expected in (
                ("renewed now", FakeApi(get=held(THEM)), LockUnavailable),
                ("a second short of expiry", FakeApi(get=fresh), LockUnavailable),
                ("an unreadable renewTime fails closed", FakeApi(get=unreadable), LockUnavailable),
                ("created under us", FakeApi(get=(404, {}), post=(409, {})), LockUnavailable),
                ("another won the expired lease", FakeApi(get=stale, put=(409, {})), LockUnavailable),
                ("create 404", FakeApi(get=(404, {}), post=(404, {})), LockUnavailable),  # not held
                ("it vanished mid-takeover", FakeApi(get=stale, put=(404, {})), LockUnavailable),
                ("the read failed", FakeApi(get=(500, {})), ApiWriteError),
                ("the takeover failed", FakeApi(get=stale, put=(500, {})), ApiWriteError)):
            with self.subTest(label), self.assertRaises(expected):
                lease(api).acquire()

    def test_an_expired_lease_is_taken_over_on_the_version_just_read(self):
        api = FakeApi(get=held(THEM, renewed=NOW - timedelta(seconds=DURATION_SECONDS), version="12"),
                      put=(200, {"metadata": {"resourceVersion": "13"}}))
        self.assertEqual(lease(api).acquire(), "13")
        update = api.calls[1][2]
        self.assertEqual((update["metadata"]["resourceVersion"], update["spec"]["holderIdentity"],
                          update["spec"]["acquireTime"]), ("12", US, _stamp(NOW)))

    def test_a_failed_release_is_logged_and_a_holderless_lock_is_refused(self):
        with self.assertLogs("flagger_recovery.lease", "WARNING") as logged, \
                lease(FakeApi(get=(404, {}), put=(409, {})))():
            pass
        self.assertIn("not released", logged.output[0])
        # A lock with no holder excludes nothing, so it cannot be built at all.
        self.assertRaises(ValueError, lambda: Lease(BASE, namespace="flagger-system", holder=""))

class SwitchTests(unittest.TestCase):
    def test_a_held_lease_refuses_the_correction_and_costs_nothing(self):
        store, stub = InMemoryStore(), StubGitHub()
        def taken():
            raise LockUnavailable(f"lease {LEASE_NAME} is held by {THEM!r}")
        with mock.patch.object(gitwriter, "CORRECTIONS_ENABLED", True), \
                self.assertLogs("flagger_recovery.gitwriter", "WARNING"):
            result = corrector(store, git_writer(stub), lambda _ns, _name: FakeLive(),
                               clock=lambda: "2026-09-07T21:12:42Z", lock=taken)(proposal())
        self.assertEqual(result.verdict.reason, policy.LEASE_HELD)
        self.assertIn(policy.LEASE_HELD, policy.REFUSAL_REASONS)
        self.assertEqual((store.writes, stub.calls), (0, []))

    def test_the_mode_comes_from_the_environment_and_a_typo_is_off(self):
        for environ, expected in (({}, server.WRITE_DRY_RUN),
                                  ({"RECOVERY_GIT_WRITE": ""}, server.WRITE_DRY_RUN),
                                  ({"RECOVERY_GIT_WRITE": " ENABLED "}, server.WRITE_ENABLED),
                                  ({"RECOVERY_GIT_WRITE": "off"}, server.WRITE_OFF)):
            with self.subTest(environ):
                self.assertEqual(server.git_write_mode(environ), expected)
        with self.assertLogs("flagger_recovery.server", "WARNING") as logged:
            self.assertEqual(server.git_write_mode({"RECOVERY_GIT_WRITE": "yes"}), server.WRITE_OFF)
        self.assertIn("git corrections are off", logged.output[0])
        self.assertEqual(server._git_startup_note(server.WRITE_DRY_RUN, False),  # the post-merge check
                         "git corrections: dry-run, no credential mounted")

    def test_the_credential_is_read_at_call_time_and_may_be_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "token"
            read = server.git_token_reader(str(path))
            self.assertEqual(read(), "")  # between windows: no Secret, so no file
            path.write_text("ghp-not-a-real-token\n", encoding="utf-8")
            self.assertEqual(read(), "ghp-not-a-real-token")
            path.unlink()  # and the window can close under a running pod
            self.assertEqual((read(), server.git_token_reader("")()), ("", ""))

    def test_no_credential_means_an_anonymous_read_rather_than_a_401(self):
        seen = []
        class Recorder:
            def request(self, method, url, *, headers, body, ca_file, timeout):
                seen.append(headers)
                return 200, b'{"object": {"sha": "%s"}}' % (b"a" * 40)
        for token in ("", "ghp-not-a-real-token"):
            GitWriter(lambda: token, base_url=BASE_URL, transport=Recorder()).read_ref()
        self.assertEqual([("Authorization" in headers) for headers in seen], [False, True])
