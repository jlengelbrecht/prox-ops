"""Owner browser authentication and anti-CSRF boundary fixtures."""

import asyncio
import sys
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from owner_auth import OwnerAuthConfig, OwnerAuthenticator, OwnerAuthError, CsrfLedger  # noqa: E402


def request(cookie="session=fixture", headers=None):
    pairs = [(b"cookie", cookie.encode())] if cookie else []
    pairs += [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/owner/pending", "headers": pairs})


class OwnerAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen = []

        def handler(call):
            self.seen.append(call)
            cookies = SimpleCookie()
            cookies.load(call.headers.get("cookie", ""))
            if not cookies.get("session") or cookies["session"].value != "fixture":
                return httpx.Response(401)
            return httpx.Response(200, headers={"X-authentik-uid": "owner"})

        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.config = OwnerAuthConfig(
            check_url="http://authentik.svc.cluster.local:9000/outpost.goauthentik.io/auth/nginx",
            origin="https://obsidian-approval.example.test", owner_ids=frozenset({"owner"}),
            vault_ids=frozenset({"iam", "homelab"}),
        )
        self.auth = OwnerAuthenticator(self.config, self.client)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_independent_cookie_check_ignores_forged_identity_and_bearer(self):
        identity = await self.auth.verify(request(headers={
            "Authorization": "Bearer agent-fixture", "X-authentik-uid": "attacker",
            "X-Original-URL": "http://attacker.invalid/", "X-Forwarded-Host": "attacker.invalid",
        }))
        self.assertEqual(identity.owner_id, "owner")
        self.assertEqual(identity.vault_ids, frozenset({"iam", "homelab"}))
        call = self.seen[-1]
        self.assertEqual(str(call.url), self.config.check_url)
        self.assertEqual(call.headers["x-original-url"], self.config.origin + "/owner/")
        self.assertNotIn("authorization", call.headers)
        self.assertNotIn("attacker.invalid", str(call.headers))

    async def test_session_check_preserves_browser_cookies(self):
        identity = await self.auth.verify(request(cookie="session=fixture; __Host-obsidian_csrf=nonce"))
        self.assertEqual(identity.owner_id, "owner")
        self.assertEqual(self.seen[-1].headers["cookie"],
                         "session=fixture; __Host-obsidian_csrf=nonce")

    async def test_missing_cookie_denial_identity_and_timeout_fail_closed(self):
        for candidate in (request(cookie=""), request(cookie="wrong=fixture")):
            with self.assertRaises(OwnerAuthError):
                await self.auth.verify(candidate)
        self.assertEqual(len(self.seen), 1)
        denied = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda call: httpx.Response(200, headers={"X-authentik-uid": "attacker"})))
        try:
            with self.assertRaises(OwnerAuthError):
                await OwnerAuthenticator(self.config, denied).verify(request())
        finally:
            await denied.aclose()

        async def timeout(call):
            await asyncio.sleep(0)
            raise httpx.ReadTimeout("fixture")

        failing = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
        try:
            with self.assertRaises(OwnerAuthError):
                await OwnerAuthenticator(self.config, failing).verify(request())
        finally:
            await failing.aclose()

    async def test_streamed_check_uses_headers_without_reading_body_and_closes_it(self):
        streams = []

        class EndlessBody(httpx.AsyncByteStream):
            def __init__(self):
                self.closed = False
                self.read = False

            async def __aiter__(self):
                self.read = True
                while True:
                    yield b"x"
                    await asyncio.sleep(0.01)

            async def aclose(self):
                self.closed = True

        def handler(call):
            stream = EndlessBody()
            streams.append(stream)
            return httpx.Response(200, headers={"X-authentik-uid": "owner"}, stream=stream)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with patch("owner_auth._OWNER_CHECK_TIMEOUT", 0.1):
                identity = await asyncio.wait_for(
                    OwnerAuthenticator(self.config, client).verify(request()), 0.3)
            self.assertEqual(identity.owner_id, "owner")
            self.assertTrue(streams[0].closed)
            self.assertFalse(streams[0].read)
        finally:
            await client.aclose()

    async def test_delayed_headers_have_absolute_deadline(self):
        async def delayed(call):
            await asyncio.sleep(0.2)
            return httpx.Response(200, headers={"X-authentik-uid": "owner"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(delayed))
        try:
            with patch("owner_auth._OWNER_CHECK_TIMEOUT", 0.05):
                with self.assertRaisesRegex(OwnerAuthError, "owner_session_required"):
                    await asyncio.wait_for(
                        OwnerAuthenticator(self.config, client).verify(request()), 0.15)
        finally:
            await client.aclose()

    async def test_denied_and_bad_identity_close_streams(self):
        closed = []

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise AssertionError("body must not be read")
                yield b"unreachable"

            async def aclose(self):
                closed.append(True)

        for status, uid in ((401, "owner"), (200, "attacker")):
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda call: httpx.Response(status, headers={"X-authentik-uid": uid}, stream=Body())))
            try:
                with self.assertRaisesRegex(OwnerAuthError, "owner_session_required"):
                    await OwnerAuthenticator(self.config, client).verify(request())
            finally:
                await client.aclose()
        self.assertEqual(len(closed), 2)

    def test_csrf_owner_pending_digest_origin_and_single_use(self):
        ledger = CsrfLedger(ttl=120)
        nonce = ledger.issue("owner", "pending", "a" * 64)
        self.assertFalse(ledger.consume("owner", "pending", "a" * 64, nonce, nonce,
                                        "https://wrong.example", self.config.origin))
        self.assertFalse(ledger.consume("attacker", "pending", "a" * 64, nonce, nonce,
                                        self.config.origin, self.config.origin))
        self.assertTrue(ledger.consume("owner", "pending", "a" * 64, nonce, nonce,
                                       self.config.origin, self.config.origin))
        self.assertFalse(ledger.consume("owner", "pending", "a" * 64, nonce, nonce,
                                        self.config.origin, self.config.origin))


if __name__ == "__main__":
    unittest.main()
