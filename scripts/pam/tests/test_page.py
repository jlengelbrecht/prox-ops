"""The approval page in a real headless Chromium, signing in through OpenBao's OIDC method against
a stand-in identity provider, on one origin with the API as it will be served. The browser is
driven over the DevTools pipe, so this needs nothing beyond chromium itself."""
import base64
import hashlib
import http.server
import json
import os
import pathlib
import re
import select
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import lab

PAGE = pathlib.Path(lab.ROOT, "scripts/pam/page/index.html").read_text()
CHROMIUM = os.environ.get("CHROMIUM") or shutil.which("chromium") or shutil.which("chromium-browser")
BODY_CANARY = "S3cretBodyCanary"
REASON_CANARY = "S3cretReasonCanary"


def serve(handler):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def stop(server):
    server.shutdown()
    server.server_close()


class Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, body=b"", content_type="application/json", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


def identity_provider(keys, groups):
    """Enough of Authentik: discovery, authorize that signs in at once, token, and keys."""
    numbers = keys.key.public_key().public_numbers()
    jwk = {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": "lab",
           "n": lab.b64url(numbers.n.to_bytes(256, "big")), "e": lab.b64url(numbers.e.to_bytes(3, "big"))}
    nonces = {}

    class Handler(Quiet):
        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(url.query))
            if url.path == "/.well-known/openid-configuration":
                base = f"http://{self.headers['Host']}"
                self.reply(200, json.dumps({
                    "issuer": base, "authorization_endpoint": f"{base}/authorize", "token_endpoint": f"{base}/token",
                    "jwks_uri": f"{base}/jwks", "response_types_supported": ["code"], "subject_types_supported": ["public"],
                    "id_token_signing_alg_values_supported": ["RS256"],
                }).encode())
            elif url.path == "/jwks":
                self.reply(200, json.dumps({"keys": [jwk]}).encode())
            elif url.path == "/authorize":
                nonces["code"] = query.get("nonce")
                target = query["redirect_uri"] + "?" + urllib.parse.urlencode({"code": "code", "state": query["state"]})
                self.reply(302, headers=[("Location", target)])
            else:
                self.reply(404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            base = f"http://{self.headers['Host']}"
            token = keys.jwt("owner-sub", groups(), aud="openbao", issuer=base, nonce=nonces.get("code"))
            self.reply(200, json.dumps({"access_token": "x", "token_type": "Bearer", "id_token": token, "expires_in": 300}).encode())

    return serve(Handler)


def same_origin(addr):
    """Serves the page at /pam/ and /pam/callback and passes /v1/ to OpenBao, like the HTTPRoute."""

    class Handler(Quiet):
        def forward(self):
            if not self.path.startswith("/v1/"):
                if self.path.split("?")[0] in ("/pam/", "/pam/callback"):
                    return self.reply(200, PAGE.encode(), "text/html")
                if self.path == "/tampered/":
                    return self.reply(200, PAGE.replace('"use strict";', 'document.title = "ran";').encode(), "text/html")
                return self.reply(404)
            length = int(self.headers.get("Content-Length", 0))
            req = urllib.request.Request(
                addr + self.path, method=self.command, data=self.rfile.read(length) if length else None,
                headers={k: v for k, v in self.headers.items() if k.lower() in ("x-vault-token", "content-type")},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as f:
                    self.reply(f.status, f.read())
            except urllib.error.HTTPError as e:
                with e:
                    self.reply(e.code, e.read())

        do_GET = do_POST = forward

    return serve(Handler)


class Browser:
    """Chromium's DevTools protocol over --remote-debugging-pipe: fds 3 and 4, NUL-separated JSON."""

    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="pam-chromium-")
        to_child_read, self.to_child = os.pipe()
        self.from_child, from_child_write = os.pipe()

        def fds():
            os.dup2(to_child_read, 3)
            os.dup2(from_child_write, 4)

        self.proc = subprocess.Popen(
            [CHROMIUM, "--headless=new", "--remote-debugging-pipe", "--no-first-run", "--no-sandbox",
             "--disable-gpu", f"--user-data-dir={self.profile}", "about:blank"],
            preexec_fn=fds, close_fds=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(to_child_read)
        os.close(from_child_write)
        self.buffer = b""
        self.next_id = 0
        self.problems = []
        target = self.send("Target.createTarget", url="about:blank")["targetId"]
        self.session = self.send("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        for domain in ("Runtime", "Page", "Log"):
            self.command(f"{domain}.enable")

    def close(self):
        self.proc.kill()
        self.proc.wait(timeout=10)
        os.close(self.to_child)
        os.close(self.from_child)
        shutil.rmtree(self.profile, ignore_errors=True)

    def send(self, method, session=None, **params):
        self.next_id += 1
        message = {"id": self.next_id, "method": method, "params": params, **({"sessionId": session} if session else {})}
        os.write(self.to_child, json.dumps(message).encode() + b"\0")
        wanted = self.next_id
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            while b"\0" not in self.buffer:
                if time.monotonic() > deadline:
                    raise TimeoutError(method)
                ready, _, _ = select.select([self.from_child], [], [], 1)
                if ready:
                    chunk = os.read(self.from_child, 1 << 16)
                    if not chunk:
                        raise RuntimeError(f"Chromium exited during {method}")
                    self.buffer += chunk
            raw, self.buffer = self.buffer.split(b"\0", 1)
            reply = json.loads(raw)
            # Every token but a held request answers the page's scan with a 400, so network
            # errors are normal. Script errors and CSP violations aren't.
            entry = reply.get("params", {}).get("entry", {})
            if reply.get("method") == "Runtime.exceptionThrown" or (
                    reply.get("method") == "Log.entryAdded" and entry.get("source") != "network"):
                self.problems.append(reply["params"])
            if reply.get("id") == wanted:
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                return reply["result"]
        raise TimeoutError(method)

    def command(self, method, **params):
        return self.send(method, self.session, **params)

    def evaluate(self, expression):
        result = self.command("Runtime.evaluate", expression=expression, returnByValue=True)
        return result["result"].get("value")

    def wait_for(self, expression, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(expression):
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {expression}: {self.evaluate('document.body.innerText')!r}")

    def click(self, label):
        self.wait_for(f"[...document.querySelectorAll('button')].some(b => b.textContent === {json.dumps(label)})")
        self.evaluate(f"[...document.querySelectorAll('button')].find(b => b.textContent === {json.dumps(label)}).click()")


@unittest.skipUnless(CHROMIUM, "chromium isn't installed")
class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = lab.Lab()
        cls.addClassCleanup(cls.lab.close)
        cls.groups = [lab.CLAIM]
        idp, issuer = identity_provider(cls.lab.keys, lambda: cls.groups)
        cls.addClassCleanup(stop, idp)
        site, cls.origin = same_origin(cls.lab.addr)
        cls.addClassCleanup(stop, site)
        role = json.loads(lab.ROLE_FILE.read_text())
        cls.assertRole(role)
        role["allowed_redirect_uris"] = [f"{cls.origin}/pam/callback"]
        cls.lab.ok("POST", "sys/auth/pam-oidc", {"type": "oidc"})
        cls.lab.ok("POST", "auth/pam-oidc/config", {
            "oidc_discovery_url": issuer, "oidc_client_id": "openbao", "oidc_client_secret": "lab", "default_role": "pam-approver",
        })
        cls.lab.ok("POST", "auth/pam-oidc/role/pam-approver", role)
        group = cls.lab.ok("GET", f"identity/group/name/{lab.GROUP}")["data"]
        cls.lab.ok("DELETE", f"identity/group-alias/id/{group['alias']['id']}")
        accessor = cls.lab.ok("GET", "sys/auth")["data"]["pam-oidc/"]["accessor"]
        cls.lab.ok("POST", "identity/group-alias", {"name": lab.CLAIM, "mount_accessor": accessor, "canonical_id": group["id"]})
        cls.lab.seed("pam/live/demo", value="x")
        cls.browser = Browser()
        cls.addClassCleanup(cls.browser.close)

    @staticmethod
    def assertRole(role):
        assert role["allowed_redirect_uris"] == ["https://bao.homelab0.org/pam/callback"]
        assert role["token_policies"] == ["pam-approver"]
        assert role["token_no_default_policy"] is True

    def tearDown(self):
        self.__class__.groups = [lab.CLAIM]
        self.assertEqual(self.browser.problems, [])

    def sign_in(self, expect="document.querySelector('#toolbar button')?.textContent === 'Refresh'"):
        self.browser.command("Page.navigate", url=f"{self.origin}/pam/")
        self.browser.click("Sign in")
        self.browser.wait_for(expect)
        self.browser.wait_for("document.getElementById('status').textContent !== ''"
                              " || document.querySelectorAll('article').length > 0")

    def hold(self, method="GET", path="kv/data/pam/live/demo", body=None, query="", token=None):
        status, reply = self.lab.api(method, path + query, token=token or self.lab.agent(), body=body)
        self.assertEqual(status, 200)
        self.addCleanup(self.drop, reply["wrap_info"]["accessor"])
        return reply["wrap_info"]

    def drop(self, accessor):
        self.lab.api("POST", "auth/token/revoke-accessor", body={"accessor": accessor})

    def html(self):
        return self.browser.evaluate("document.documentElement.outerHTML")

    def test_signing_in_leaves_nothing_behind(self):
        self.sign_in()
        state = self.browser.evaluate("JSON.stringify([location.pathname, location.search, localStorage.length,"
                                      " sessionStorage.length, document.cookie])")
        self.assertEqual(json.loads(state), ["/pam/", "", 0, 0, ""])

    def count_callbacks(self):
        """Counts the page's calls to the OpenBao callback, from before its own script runs."""
        script = self.browser.command("Page.addScriptToEvaluateOnNewDocument", source="""
            window.callbacks = 0;
            const real = window.fetch;
            window.fetch = (url, init) => {
                if (String(url).includes("/callback?")) window.callbacks += 1;
                return real(url, init);
            };""")["identifier"]
        self.addCleanup(self.browser.command, "Page.removeScriptToEvaluateOnNewDocument", identifier=script)

    def test_a_callback_this_tab_didnt_start_is_refused(self):
        self.count_callbacks()
        self.browser.command("Page.navigate", url=f"{self.origin}/pam/callback?code=theirs&state=theirs")
        self.browser.wait_for("document.getElementById('status').textContent.includes('Sign-in failed')")
        self.assertEqual(self.browser.evaluate("window.callbacks"), 0)
        self.assertEqual(self.browser.evaluate("location.search"), "")

    def test_a_callback_for_another_state_is_refused(self):
        self.count_callbacks()
        self.browser.command("Page.navigate", url=f"{self.origin}/pam/")
        self.browser.wait_for("document.readyState === 'complete'")
        self.browser.evaluate("sessionStorage.setItem('pam-client-nonce', JSON.stringify({nonce: 'mine', state: 'mine'}))")
        self.browser.command("Page.navigate", url=f"{self.origin}/pam/callback?code=theirs&state=theirs")
        self.browser.wait_for("document.getElementById('status').textContent.includes('Sign-in failed')")
        self.assertEqual(self.browser.evaluate("window.callbacks"), 0)
        self.assertEqual(self.browser.evaluate("sessionStorage.length"), 0)

    def test_someone_outside_the_group_cant_sign_in(self):
        self.__class__.groups = ["openbao-readers"]
        self.browser.command("Page.navigate", url=f"{self.origin}/pam/")
        self.browser.click("Sign in")
        self.browser.wait_for("document.getElementById('status').textContent.includes('Sign-in failed')")
        self.browser.problems.clear()

    def test_the_page_token_isnt_an_admin_token(self):
        wrap = self.hold()
        self.sign_in()
        self.browser.click("Approve")
        self.browser.wait_for("document.querySelector('article')?.innerText.includes('approved by')")
        approver = self.lab.request(wrap["accessor"], self.lab.root)[1]["data"]["authorizations"][0]["entity_id"]
        entity = self.lab.ok("GET", f"identity/entity/id/{approver}")["data"]
        mount = self.lab.ok("GET", "sys/auth")["data"]["pam-oidc/"]["accessor"]
        self.assertEqual([a["mount_accessor"] for a in entity["aliases"]], [mount])
        groups = [self.lab.ok("GET", f"identity/group/id/{g}")["data"] for g in entity["group_ids"]]
        self.assertEqual([(g["name"], g["policies"]) for g in groups], [(lab.GROUP, ["pam-approver"])])

    def test_a_redirect_uri_outside_the_exact_one_is_refused(self):
        status, reply = self.lab.api("POST", "auth/pam-oidc/oidc/auth_url", token=None, body={
            "role": "pam-approver", "redirect_uri": f"{self.origin}/other"})
        self.assertFalse(status == 200 and reply["data"].get("auth_url"))

    def test_a_request_shows_openbaos_fields_first_then_the_claim(self):
        self.hold(query="?reason=rotate%20the%20dns%20token")
        self.sign_in()
        self.browser.wait_for("document.querySelectorAll('article').length === 1")
        text = self.browser.evaluate("document.querySelector('article').innerText")
        self.assertLess(text.index("agent-claude wants to read"), text.index("kv/data/pam/live/demo"))
        self.assertLess(text.index("kv/data/pam/live/demo"), text.index("agent says: rotate the dns token"))

    def test_the_card_shows_what_will_run(self):
        self.hold(query="?reason=x&delivery=" + urllib.parse.quote("run kubectl get pods"))
        self.sign_in()
        self.browser.wait_for("document.querySelectorAll('article').length === 1")
        self.assertIn("agent says it will run kubectl get pods", self.browser.evaluate("document.querySelector('article').innerText"))

    def test_a_flood_is_capped_and_counted(self):
        for _ in range(30):
            self.hold()
        self.sign_in()
        self.browser.wait_for("document.querySelectorAll('article').length === 25")
        self.assertEqual(self.browser.evaluate("document.getElementById('status').textContent"), "5 more waiting. Deal with these, then refresh.")

    def test_open_approvals_cant_push_new_requests_past_the_cap(self):
        self.sign_in()
        # 30 approved and 5 pending, the pending ones listed last, served in place of OpenBao.
        self.browser.evaluate("""(() => {
            const real = window.fetch;
            const keys = Array.from({length: 35}, (_, n) => "a" + n);
            const reply = (data) => Promise.resolve(new Response(JSON.stringify({data}), {status: 200}));
            window.fetch = (url, init) => {
                if (String(url).includes("auth/token/accessors")) return reply({keys});
                if (String(url).includes("sys/control-group/request")) {
                    const n = Number(JSON.parse(init.body).accessor.slice(1));
                    return reply({approved: n < 30, request_path: "kv/data/pam/live/x", request_operation: "read",
                                  request_entity: {name: "agent"}, request_data: {reason: "r" + n},
                                  authorizations: n < 30 ? [{entity_name: "owner"}] : []});
                }
                return real(url, init);
            };
        })()""")
        self.browser.click("Refresh")
        self.browser.wait_for("document.getElementById('status').textContent === '10 more waiting. Deal with these, then refresh.'")
        buttons = self.browser.evaluate(
            "[...document.querySelectorAll('article')].map(a => a.querySelector('button').textContent)")
        self.assertEqual(buttons, ["Approve"] * 5 + ["Revoke"] * 20)

    def test_a_gated_write_body_is_never_rendered(self):
        self.lab.seed("pam/live/body", value="x")
        self.hold("POST", "kv/data/pam/live/body", token=self.lab.writer(), body=
                  {"data": {"password": BODY_CANARY, "note": {"nested": BODY_CANARY}}, "reason": REASON_CANARY},
                  query=f"?reason={REASON_CANARY}")
        self.sign_in()
        self.browser.wait_for("document.querySelectorAll('article').length === 1")
        html = self.html()
        self.assertIn("agent-writer wants to update", html)
        self.assertIn("writes keys: note, password", html)
        self.assertNotIn(BODY_CANARY, html)
        self.assertNotIn(REASON_CANARY, html)

    def test_the_page_reads_write_bodies_only_inside_summarize(self):
        self.assertEqual(len(re.findall(r"request_data", PAGE)), 1)
        self.assertRegex(PAGE, r"function summarize[^}]*request_data")

    def test_agent_text_is_only_ever_text(self):
        attack = "<img src=x onerror=window.pwned=1><b>hi</b>"
        self.hold(query="?reason=" + urllib.parse.quote(attack))
        self.sign_in()
        self.browser.wait_for("document.querySelectorAll('article').length === 1")
        self.assertIn(attack, self.browser.evaluate("document.querySelector('article').innerText"))
        self.assertEqual(self.browser.evaluate("document.querySelectorAll('article img, article b').length"), 0)
        self.assertIsNone(self.browser.evaluate("window.pwned"))

    def test_approve_works_with_the_page_token(self):
        wrap = self.hold()
        self.sign_in()
        self.browser.click("Approve")
        self.browser.wait_for("document.querySelector('article')?.innerText.includes('approved by')")
        self.assertFalse(self.browser.evaluate("[...document.querySelectorAll('button')].some(b => b.textContent === 'Approve')"))
        status, reply = self.lab.unwrap(wrap["token"])
        self.assertEqual(status, 200)
        self.assertEqual(reply["data"]["data"], {"value": "x"})

    def test_deny_kills_the_request(self):
        wrap = self.hold()
        self.sign_in()
        self.browser.click("Deny")
        self.browser.wait_for("document.getElementById('status').textContent === 'Nothing is waiting.'")
        self.assertEqual(self.lab.unwrap(wrap["token"])[0], 400)

    def test_an_approved_request_can_still_be_revoked(self):
        wrap = self.hold()
        self.sign_in()
        self.browser.click("Approve")
        self.browser.click("Revoke")
        self.browser.wait_for("document.getElementById('status').textContent === 'Nothing is waiting.'")
        self.assertEqual(self.lab.unwrap(wrap["token"])[0], 400)

    def test_a_changed_script_doesnt_run(self):
        self.browser.command("Page.navigate", url=f"{self.origin}/tampered/")
        self.browser.wait_for("document.readyState === 'complete'")
        self.assertEqual(self.browser.evaluate("document.title"), "Agent PAM")
        self.assertTrue([p for p in self.browser.problems if "Content Security Policy" in p["entry"]["text"]])
        self.browser.problems.clear()

    def test_nothing_waiting(self):
        self.sign_in("document.getElementById('status').textContent === 'Nothing is waiting.'")

    def fail_requests(self, fragment, status, body="<html>busy</html>"):
        self.browser.evaluate(f"""(() => {{
            const real = window.fetch;
            window.fetch = (url, init) => String(url).includes({json.dumps(fragment)})
                ? Promise.resolve(new Response({json.dumps(body)}, {{status: {status}}}))
                : real(url, init);
        }})()""")

    def test_a_failed_action_stays_visible_after_the_refresh(self):
        self.hold()
        self.sign_in("document.querySelectorAll('article').length === 1")
        self.fail_requests("sys/control-group/authorize", 500)
        self.browser.evaluate("document.querySelector('article').dataset.before = 'yes'")
        self.browser.click("Approve")
        # The refresh redraws the cards; only then is the final status settled.
        self.browser.wait_for("document.querySelectorAll('article').length === 1 && !document.querySelector('article').dataset.before")
        status = self.browser.evaluate("document.getElementById('status').textContent")
        self.assertTrue(status.startswith("That didn't work (500)."), status)

    def test_a_failed_listing_is_reported_not_shown_as_empty(self):
        self.hold()
        self.sign_in("document.querySelectorAll('article').length === 1")
        self.fail_requests("auth/token/accessors", 500)
        self.browser.click("Refresh")
        self.browser.wait_for("document.getElementById('status').textContent.startsWith(\"Couldn't check\")")
        self.assertIn("(500)", self.browser.evaluate("document.getElementById('status').textContent"))
        self.assertEqual(self.browser.evaluate("document.querySelectorAll('article').length"), 0)

    def test_a_failed_lookup_is_reported_not_skipped(self):
        self.hold()
        self.sign_in("document.querySelectorAll('article').length === 1")
        self.fail_requests("sys/control-group/request", 429, '{"errors":["rate limited"]}')
        self.browser.click("Refresh")
        self.browser.wait_for("document.getElementById('status').textContent.startsWith(\"Couldn't check\")")
        self.assertIn("(429)", self.browser.evaluate("document.getElementById('status').textContent"))


class Source(unittest.TestCase):
    """What the browser tests can't show."""

    def csp_hash(self, tag):
        body = re.search(rf"<{tag}>(.*?)</{tag}>", PAGE, re.S).group(1)
        return base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()

    def test_csp_hashes_match(self):
        self.assertIn(f"script-src 'sha256-{self.csp_hash('script')}'", PAGE)
        self.assertIn(f"style-src 'sha256-{self.csp_hash('style')}'", PAGE)

    def test_csp_is_strict(self):
        csp = re.search(r'Content-Security-Policy" content="([^"]*)"', PAGE).group(1)
        for part in ("default-src 'none'", "connect-src 'self'", "base-uri 'none'", "form-action 'none'"):
            self.assertIn(part, csp)
        self.assertNotIn("unsafe", csp)

    def test_the_token_comes_only_from_oidc(self):
        for banned in ("<input", "<textarea", "<form", "prompt(", "localStorage", "document.cookie", "console."):
            self.assertNotIn(banned, PAGE)
        self.assertEqual(PAGE.count("token = "), 3)  # Declared, set from the OIDC callback, cleared.
        self.assertEqual(re.findall(r"sessionStorage\.\w+\((\w+)", PAGE), ["NONCE_KEY"] * 3)

    def test_no_raw_html_or_third_party_code(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "src="):
            self.assertNotIn(banned, PAGE)
        self.assertEqual(re.findall(r"https?://", PAGE), [])


if __name__ == "__main__":
    unittest.main()
