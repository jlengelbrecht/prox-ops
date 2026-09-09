"""Control-flow tests for voice-bridge with the conversational path disabled.

Runs the REAL module extracted from `voice-bridge/app/configmap.yaml` (see
extract_app.py). Third-party libraries are stubbed; the code under test is not.

With LLM_ENABLED=false the outbound endpoint hands Twilio `<Say/><Hangup/>`
instead of `<Connect><ConversationRelay/>`, so no session opens and the LLM and
TTS paths are unreachable. `*Disabled*` cases cover that mode; the rest guard
behaviour that must survive unchanged in enabled mode.

Run:  python3 -m unittest discover -s services/voice-bridge/tests -p 'test_*.py'
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import time
import types
import unittest

import yaml

from extract_app import BASE_ENV, CONFIGMAP, REPO_ROOT, load_module, run, run_env

AUDIO_DIR = "/tmp/voice-bridge-tests-audio"
DEFAULT_MESSAGE = (
    "The assistant is temporarily unavailable. Please try again later. Goodbye."
)

DISABLED_ENV = dict(BASE_ENV, LLM_ENABLED="false", AUDIO_DIR=AUDIO_DIR)
ENABLED_ENV = dict(BASE_ENV, LLM_ENABLED="true", LLM_API_KEY="k",
                    LLM_BASE_URL="http://litellm.ai.svc.cluster.local:4000/v1",
                    LLM_MODEL="qwen3-30b", AUDIO_DIR=AUDIO_DIR)


class FakeTwilio:
    """Records `client.calls.create(**kw)`; optionally raises."""

    def __init__(self, raises: Exception | None = None):
        self.created: list[dict] = []
        self.raises = raises
        self.ctor_args: list[tuple] = []

    def client_factory(self):
        outer = self

        class _Calls:
            def create(self, **kw):
                if outer.raises:
                    raise outer.raises
                outer.created.append(kw)
                return types.SimpleNamespace(sid="CA00000000000000000000000000000000")

        class _Client:
            def __init__(self, *a, **k):
                outer.ctor_args.append(a)
                self.calls = _Calls()

        return _Client


class FakeWS:
    """Minimal Twilio ConversationRelay socket."""

    def __init__(self, script, disconnect_exc):
        self.script = list(script)
        self.sent: list[str] = []
        self.accepted = False
        self.closed = None
        self._disconnect = disconnect_exc

    async def accept(self):
        self.accepted = True

    async def close(self, code=None, reason=None):
        self.closed = (code, reason)

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_text(self):
        if not self.script:
            raise self._disconnect()
        return self.script.pop(0)


def call_request(**over):
    base = dict(
        to="+15551234567",
        context="You are a helpful AI assistant on a phone call.",
        greeting="Hello, this is the assistant. How can I help you today?",
        voice=None,
        from_number=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def poison_llm_and_tts(mod, case):
    """Make every LLM/TTS entry point fail loudly if reached."""

    def boom(name):
        async def _boom(*a, **k):
            case.fail(f"disabled path invoked {name}()")
        return _boom

    for fn in ("generate_llm_response", "generate_tts_audio", "stream_llm_response",
               "stream_llm_and_respond", "_send_tts_play"):
        if hasattr(mod, fn):
            setattr(mod, fn, boom(fn))


def outbound(mod, twilio, req=None):
    mod.TwilioClient = twilio.client_factory()
    return run(mod.initiate_outbound_call(
        request=types.SimpleNamespace(client=None),
        call_request=req or call_request(),
        authenticated=True,
    ))


def ws_session(mod, call_id, messages):
    """Drive conversation_relay_handler with a scripted socket."""
    import json
    disconnect = mod._stubs["fastapi"].WebSocketDisconnect
    ws = FakeWS([json.dumps(m) for m in messages], disconnect)
    calls = []

    async def recorder(websocket, msgs, voice, cid):
        calls.append((cid, list(msgs)))
        return "recorded response"

    mod.stream_llm_and_respond = recorder
    mod.active_calls[call_id] = {
        "call_id": call_id, "to": "+15551234567", "from_number": "+15550000000",
        "context": "ctx", "greeting": "hi", "voice": "ryan", "status": "pending",
        "started_at": "2026-01-01T00:00:00", "created_at": time.time(),
        "twilio_sid": None, "error": None,
    }
    run(mod.conversation_relay_handler(ws, call_id))
    return ws, calls


def run_one_reaper_pass(mod):
    """Run exactly one iteration of the real cleanup_expired_calls loop.

    The loop is `while True: await asyncio.sleep(3600); <sweep>`. Swapping the
    module's asyncio reference lets the first sleep return immediately and the
    second one break out, so the sweep body itself is the code exercised.
    """
    class _Stop(Exception):
        pass

    seen = {"n": 0}

    async def sleep(_seconds):
        seen["n"] += 1
        if seen["n"] > 1:
            raise _Stop

    original, mod.asyncio = mod.asyncio, types.SimpleNamespace(sleep=sleep)
    try:
        run(mod.cleanup_expired_calls())
    except _Stop:
        pass
    finally:
        mod.asyncio = original


def run_lifespan(mod, inside=None):
    """Enter the app lifespan, return `inside(mod)` (awaited if needed), then exit."""

    async def go():
        async with mod.lifespan(mod.app):
            if inside is None:
                return None
            value = inside(mod)
            return await value if hasattr(value, "__await__") else value

    return run_env(mod, go)


class DisabledOutbound(unittest.TestCase):
    """Disabled mode terminates the call with vendor TwiML and opens no session."""

    def setUp(self):
        self.mod = load_module(DISABLED_ENV)
        poison_llm_and_tts(self.mod, self)
        self.twilio = FakeTwilio()

    def test_emits_say_then_hangup(self):
        outbound(self.mod, self.twilio)
        self.assertEqual(len(self.twilio.created), 1, "expected exactly one Twilio call")
        twiml = self.twilio.created[0]["twiml"]
        self.assertIn("<Say", twiml)
        self.assertIn("<Hangup", twiml)
        self.assertLess(twiml.index("<Say"), twiml.index("<Hangup"),
                        "Say must precede Hangup (TwiML executes top to bottom)")

    def test_no_conversationrelay_and_no_websocket_url(self):
        outbound(self.mod, self.twilio)
        twiml = self.twilio.created[0]["twiml"]
        for forbidden in ("ConversationRelay", "<Connect>", "wss://", "/ws/"):
            self.assertNotIn(forbidden, twiml,
                             f"disabled TwiML must not contain {forbidden!r}")

    def test_say_text_is_the_unavailable_message(self):
        outbound(self.mod, self.twilio)
        twiml = self.twilio.created[0]["twiml"]
        self.assertEqual(getattr(self.mod, "LLM_UNAVAILABLE_MESSAGE", None),
                         DEFAULT_MESSAGE)
        self.assertIn(self.mod.LLM_UNAVAILABLE_MESSAGE, twiml)
        self.assertNotIn(call_request().greeting, twiml,
                         "caller greeting must not precede the unavailable message")

    def test_say_text_is_escaped(self):
        self.mod.LLM_UNAVAILABLE_MESSAGE = 'Closed & <gone> "now"'
        outbound(self.mod, self.twilio)
        twiml = self.twilio.created[0]["twiml"]
        self.assertIn("Closed &amp; &lt;gone&gt;", twiml)
        self.assertNotIn("<gone>", twiml, "message must not inject TwiML markup")

    def test_no_drain_delay_and_no_hangup_delay_knob(self):
        t0 = time.monotonic()
        outbound(self.mod, self.twilio)
        self.assertLess(time.monotonic() - t0, 1.0,
                        "disabled path must not sleep; there is no drain race")
        self.assertFalse(hasattr(self.mod, "LLM_UNAVAILABLE_HANGUP_DELAY"),
                         "the arbitrary drain knob must not exist")

    def test_retired_backend_defaults_neutralized(self):
        """No retired gateway endpoint may survive as a module default."""
        for value in ((self.mod.LLM_BASE_URL or ""), (self.mod.LLM_MODEL or "")):
            for token in ("moltbot", "clawdbot", "openclaw", "18789"):
                self.assertNotIn(token, value.lower(),
                                 f"{token!r} still reachable as a disabled-mode default")


class DisabledStartup(unittest.TestCase):
    """A missing LLM credential must not crash a disabled deployment."""

    def test_starts_without_llm_api_key(self):
        mod = load_module(DISABLED_ENV)  # note: no LLM_API_KEY in env
        self.assertIsNone(mod._env.get("LLM_API_KEY"))

        body = run_lifespan(mod, lambda m: m.health())
        self.assertEqual(body["status"], "healthy")

    def test_llm_client_not_created_when_disabled(self):
        mod = load_module(DISABLED_ENV)

        llm, tts = run_lifespan(mod, lambda m: (m.llm_client, m.tts_client))
        self.assertIsNone(llm, "no LLM client may be constructed when disabled")
        self.assertIsNotNone(tts, "TTS client is unrelated and must survive")


class DisabledSession(unittest.TestCase):
    """A disabled build must not run a conversational session at all."""

    def test_websocket_session_refused(self):
        mod = load_module(DISABLED_ENV)
        ws, calls = ws_session(mod, "cid-disabled", [
            {"type": "setup", "callSid": "CA1", "from": "+1555", "to": "+1666"},
            {"type": "prompt", "voicePrompt": "hello"},
            {"type": "prompt", "voicePrompt": "are you there"},
        ])
        self.assertEqual(calls, [], "stream_llm_and_respond must never run when disabled")
        self.assertEqual(ws.sent, [], "nothing may be sent on a disabled session")
        self.assertIsNotNone(ws.closed, "a disabled build must refuse the session")

    def test_refused_session_releases_the_connection_slot(self):
        mod = load_module(DISABLED_ENV)
        ws, _ = ws_session(mod, "cid-slot", [{"type": "prompt", "voicePrompt": "hi"}])
        self.assertFalse(ws.accepted, "a refused session must not be accepted")
        self.assertEqual(mod.active_websocket_count, 0,
                         "the connection counter must not leak on refusal")
        self.assertEqual(mod.active_websockets, {})

    def test_refused_session_does_not_mutate_the_call_record(self):
        mod = load_module(DISABLED_ENV)
        ws, _ = ws_session(mod, "cid-record", [{"type": "prompt", "voicePrompt": "hi"}])
        record = mod.active_calls["cid-record"]
        self.assertEqual(record["status"], "pending", "refusal must not touch the record")
        self.assertNotIn("ended_at", record)


class DisabledCallRecord(unittest.TestCase):
    """No remote WebSocket does not mean no local call record."""

    def test_disabled_outbound_leaves_one_reapable_record(self):
        mod = load_module(DISABLED_ENV)
        poison_llm_and_tts(mod, self)
        result = outbound(mod, FakeTwilio())

        # The record is created exactly as in enabled mode and stays in_progress:
        # nothing marks it completed because the handler's finally block never
        # runs for a call that opens no socket. The TTL sweep is what retires it.
        self.assertEqual(list(mod.active_calls), [result.call_id])
        self.assertEqual(mod.active_calls[result.call_id]["status"], "in_progress")
        self.assertEqual(mod.active_websockets, {})
        self.assertEqual(mod.active_websocket_count, 0)

        mod.active_calls[result.call_id]["created_at"] = (
            time.time() - mod.CALL_TTL_SECONDS - 1)
        run_one_reaper_pass(mod)
        self.assertEqual(mod.active_calls, {}, "expired record must be swept")


class EnabledModeUnchanged(unittest.TestCase):
    EXPECTED = ('<Response><Connect><ConversationRelay url="wss://{host}/ws/{cid}" '
                'welcomeGreeting="{greet}" dtmfDetection="true" /></Connect></Response>')

    def test_enabled_twiml_is_byte_identical(self):
        mod = load_module(ENABLED_ENV)
        twilio = FakeTwilio()
        req = call_request()
        result = outbound(mod, twilio, req)
        twiml = twilio.created[0]["twiml"]
        self.assertEqual(twiml, self.EXPECTED.format(
            host=mod.VOICE_BRIDGE_HOST, cid=result.call_id, greet=req.greeting))

    def test_enabled_startup_requires_llm_api_key(self):
        mod = load_module(dict(BASE_ENV, LLM_ENABLED="true", LLM_BASE_URL="http://x",
                                LLM_MODEL="m", AUDIO_DIR=AUDIO_DIR))

        with self.assertRaises(RuntimeError) as ctx:
            run_lifespan(mod)
        message = str(ctx.exception)
        self.assertIn("LLM_API_KEY", message)
        # LLM_API_KEY must be the ONLY thing missing - proves the env fixture is
        # real and the failure is not "everything is unset".
        self.assertNotIn("TWILIO_ACCOUNT_SID", message)
        self.assertNotIn("LITELLM_API_KEY", message)

    def test_enabled_startup_creates_both_clients(self):
        mod = load_module(ENABLED_ENV)

        llm, tts = run_lifespan(mod, lambda m: (m.llm_client, m.tts_client))
        self.assertIsNotNone(llm)
        self.assertIsNotNone(tts)

    def test_enabled_session_still_answers_every_prompt(self):
        mod = load_module(ENABLED_ENV)
        ws, calls = ws_session(mod, "cid-enabled", [
            {"type": "setup", "callSid": "CA1", "from": "+1555", "to": "+1666"},
            {"type": "prompt", "voicePrompt": "hello"},
            {"type": "prompt", "voicePrompt": "again"},
        ])
        self.assertEqual(len(calls), 2, "enabled mode must still answer both prompts")
        self.assertIsNone(ws.closed, "enabled session ends by disconnect, not refusal")


class LLMEnabledConfigValidation(unittest.TestCase):
    def test_aliases_invalid_value_and_absent_default(self):
        for value in ("True", " true ", "1", "yes", "ON"):
            self.assertTrue(load_module(dict(ENABLED_ENV, LLM_ENABLED=value)).LLM_ENABLED,
                            f"{value!r} must parse as enabled")
        for value in ("False", " false ", "0", "no", "OFF"):
            self.assertFalse(load_module(dict(DISABLED_ENV, LLM_ENABLED=value)).LLM_ENABLED,
                             f"{value!r} must parse as disabled")
        with self.assertRaises(RuntimeError) as ctx:
            load_module(dict(DISABLED_ENV, LLM_ENABLED="not-a-boolean-xyz"))
        message = str(ctx.exception)
        self.assertIn("LLM_ENABLED", message)
        self.assertNotIn("not-a-boolean-xyz", message, "the raw operator value must not be echoed")
        env = dict(BASE_ENV, LLM_API_KEY="k", LLM_BASE_URL="http://x", LLM_MODEL="m",
                    AUDIO_DIR=AUDIO_DIR)
        self.assertTrue(load_module(env).LLM_ENABLED, "absent var must preserve enabled-by-default")

    def test_missing_url_or_model_blocks_startup_when_enabled(self):
        for missing, present in (("LLM_BASE_URL", {"LLM_MODEL": "m"}),
                                  ("LLM_MODEL", {"LLM_BASE_URL": "http://x"})):
            env = dict(BASE_ENV, LLM_ENABLED="true", LLM_API_KEY="k", AUDIO_DIR=AUDIO_DIR,
                       **present)
            mod = load_module(env)

            with self.assertRaises(RuntimeError) as ctx:
                run_lifespan(mod)
            self.assertIn(missing, str(ctx.exception))


class CallErrorCleanup(unittest.TestCase):
    """Twilio failure handling is mode-independent and must not regress."""

    def test_cleanup_is_mode_independent(self):
        for env in (ENABLED_ENV, DISABLED_ENV):
            with self.subTest(LLM_ENABLED=env["LLM_ENABLED"]):
                mod = load_module(env)
                twilio = FakeTwilio(raises=RuntimeError("twilio exploded"))
                with self.assertRaises(mod._stubs["fastapi"].HTTPException) as ctx:
                    outbound(mod, twilio)
                self.assertEqual(ctx.exception.status_code, 500)
                self.assertEqual(len(mod.active_calls), 1)
                record = next(iter(mod.active_calls.values()))
                self.assertEqual(record["status"], "failed")
                self.assertIn("twilio exploded", record["error"])
                self.assertIsNone(record["twilio_sid"])


class UnrelatedPathsIntact(unittest.TestCase):
    """TTS / API / recording surfaces must survive in both modes."""

    def test_surface_present_in_both_modes(self):
        for env in (DISABLED_ENV, ENABLED_ENV):
            mod = load_module(env)
            for name in ("generate_tts_audio", "_send_tts_play", "serve_audio",
                         "get_call_status", "end_call", "health",
                         "verify_api_key", "cleanup_audio_files"):
                self.assertTrue(callable(getattr(mod, name, None)),
                                f"{name} missing with LLM_ENABLED={env['LLM_ENABLED']}")
            self.assertEqual(mod.TTS_MODEL, "qwen-tts")
            self.assertIn("litellm", mod.LITELLM_BASE_URL)
            expected_mode = "conversation-relay" if mod.LLM_ENABLED else "unavailable-message"
            self.assertEqual(run(mod.health())["mode"], expected_mode,
                              f"/health mode wrong with LLM_ENABLED={env['LLM_ENABLED']}")


class ManifestWiring(unittest.TestCase):
    def test_helmrelease_and_externalsecret_wiring(self):
        hr = REPO_ROOT / "kubernetes/apps/ai/voice-bridge/app/helmrelease.yaml"
        values = yaml.safe_load(hr.read_text())["spec"]["values"]
        env = values["controllers"]["voice-bridge"]["containers"]["app"]["env"]
        self.assertEqual(env["LLM_ENABLED"], "false")
        for retired in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
            self.assertNotIn(retired, env)
        reloader = values["defaultPodOptions"]["annotations"]["secret.reloader.stakater.com/reload"]
        self.assertNotIn("voice-bridge-llm", reloader)

        es = REPO_ROOT / "kubernetes/apps/ai/voice-bridge/app/externalsecret.yaml"
        names = [d["metadata"]["name"] for d in yaml.safe_load_all(es.read_text()) if d]
        self.assertNotIn("voice-bridge-llm", names)
        self.assertEqual(len(names), 3, f"expected exactly three ExternalSecrets, got {names}")


class StartupModeAndManifestPath(unittest.TestCase):
    """The banner must name the running mode, and tracebacks the running manifest."""

    def test_startup_banner_names_the_mode_actually_running(self):
        for env, expected, forbidden in ((DISABLED_ENV, "unavailable-message mode", "ConversationRelay"),
                                         (ENABLED_ENV, "ConversationRelay mode", None)):
            with self.subTest(LLM_ENABLED=env["LLM_ENABLED"]):
                mod = load_module(env)  # fresh module and env per case
                with self.assertLogs("voice-bridge", level="INFO") as captured:
                    run_lifespan(mod)
                lines = [ln for ln in captured.output if "Voice Bridge starting" in ln]
                self.assertEqual(len(lines), 1, f"expected one banner, got {lines}")
                self.assertIn(expected, lines[0])
                if forbidden:
                    self.assertNotIn(forbidden, lines[0], "disabled opens no session")

    def test_module_file_and_tracebacks_name_the_manifest_used(self):
        override = pathlib.Path(tempfile.mkdtemp()) / "override-configmap.yaml"
        self.addCleanup(shutil.rmtree, override.parent, ignore_errors=True)
        override.write_text(CONFIGMAP.read_text())
        for supplied, expected in ((None, CONFIGMAP), (override, override)):
            with self.subTest(configmap=expected.name):
                mod = load_module(DISABLED_ENV, configmap_path=supplied)
                self.assertTrue(mod.__file__.startswith(str(expected)), mod.__file__)
                # co_filename is what a traceback or compile() error actually prints.
                self.assertTrue(
                    mod.cleanup_expired_calls.__code__.co_filename.startswith(str(expected)),
                    "tracebacks must point at the manifest that was executed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
