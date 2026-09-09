"""Extract the voice-bridge server module from its ConfigMap and import it.

`kubernetes/apps/ai/voice-bridge/app/configmap.yaml` carries the service as a
Python string under `data["server.py"]`, so it cannot be imported directly.
`load_module()` execs the exact source bytes from that ConfigMap: every function
body, decorator application, module-level constant and branch under test is the
shipped code.

Only the third-party import surface (fastapi, pydantic, twilio, slowapi, httpx)
is stubbed, because those packages are not installed for this test run and
adding them would be a dependency change. The stubs are inert -- route
decorators return the function unchanged, so tests call the undecorated
coroutine. Control flow is steered by rebinding module globals, so the handler's
own dispatch decides what is reached.

These tests therefore cover control flow inside voice-bridge. They cover nothing
about Twilio's runtime, FastAPI routing, pydantic validation or audible speech.

The ConfigMap is located relative to this file so the tests read the checkout
they ship in. Set VOICE_BRIDGE_CONFIGMAP to point at a different manifest.

Usage:
    from extract_app import load_module
    mod = load_module({"LLM_ENABLED": "false", ...})
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.machinery
import os
import pathlib
import sys
import types

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CONFIGMAP = pathlib.Path(os.environ.get(
    "VOICE_BRIDGE_CONFIGMAP",
    REPO_ROOT / "kubernetes/apps/ai/voice-bridge/app/configmap.yaml",
))
DATA_KEY = "server.py"


def extract_source(configmap_path: pathlib.Path | None = None) -> str:
    """Return the exact `data["server.py"]` string from the ConfigMap."""
    path = pathlib.Path(configmap_path) if configmap_path else CONFIGMAP
    doc = yaml.safe_load(path.read_text())
    data = doc["data"]
    if DATA_KEY not in data:
        raise KeyError(f"{path}: data has no {DATA_KEY!r} (keys: {sorted(data)})")
    return data[DATA_KEY]


# --------------------------------------------------------------------------
# Inert stubs for the third-party import surface
# --------------------------------------------------------------------------

class _Recorder:
    """Attribute bag that records constructor kwargs."""

    def __init__(self, *args, **kwargs):
        self._args = args
        self._kwargs = kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)


def _identity_decorator(*_a, **_k):
    def deco(fn):
        return fn
    return deco


class _StubFastAPI(_Recorder):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.state = types.SimpleNamespace()
        self.exception_handlers = {}

    def add_exception_handler(self, exc, handler):
        self.exception_handlers[exc] = handler

    get = post = put = delete = websocket = middleware = staticmethod(_identity_decorator)


class _StubLimiter(_Recorder):
    limit = staticmethod(_identity_decorator)


class _StubBaseModel:
    """Accepts kwargs, sets them as attributes. No validation is performed."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self):
        return f"{type(self).__name__}({self.__dict__!r})"


class _StubHTTPException(Exception):
    def __init__(self, status_code=None, detail=None, **kw):
        super().__init__(f"{status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class _StubAsyncClient(_Recorder):
    """httpx.AsyncClient stand-in. Any request method raises loudly."""

    closed = False

    async def aclose(self):
        self.closed = True

    def _forbidden(self, *_a, **_k):
        raise AssertionError("outbound HTTP attempted by voice-bridge")

    post = get = stream = request = _forbidden


def _module(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__spec__ = importlib.machinery.ModuleSpec(name, None)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _build_stubs() -> dict[str, types.ModuleType]:
    fastapi = _module(
        "fastapi",
        FastAPI=_StubFastAPI,
        WebSocket=type("WebSocket", (), {}),
        WebSocketDisconnect=type("WebSocketDisconnect", (Exception,), {}),
        HTTPException=_StubHTTPException,
        Request=type("Request", (), {}),
        Depends=lambda dep=None: dep,
    )
    fastapi.responses = _module("fastapi.responses", FileResponse=_Recorder)
    fastapi.security = _module(
        "fastapi.security",
        HTTPBearer=_Recorder,
        HTTPAuthorizationCredentials=_Recorder,
    )
    pydantic = _module(
        "pydantic", BaseModel=_StubBaseModel, field_validator=_identity_decorator
    )
    twilio = _module("twilio")
    twilio.rest = _module("twilio.rest", Client=_Recorder)
    slowapi = _module(
        "slowapi",
        Limiter=_StubLimiter,
        _rate_limit_exceeded_handler=lambda *a, **k: None,
    )
    slowapi.util = _module("slowapi.util", get_remote_address=lambda r: "127.0.0.1")
    slowapi.errors = _module(
        "slowapi.errors", RateLimitExceeded=type("RateLimitExceeded", (Exception,), {})
    )
    httpx = _module(
        "httpx",
        AsyncClient=_StubAsyncClient,
        TimeoutException=type("TimeoutException", (Exception,), {}),
        HTTPError=type("HTTPError", (Exception,), {}),
    )
    return {
        "fastapi": fastapi,
        "fastapi.responses": fastapi.responses,
        "fastapi.security": fastapi.security,
        "pydantic": pydantic,
        "twilio": twilio,
        "twilio.rest": twilio.rest,
        "slowapi": slowapi,
        "slowapi.util": slowapi.util,
        "slowapi.errors": slowapi.errors,
        "httpx": httpx,
    }


BASE_ENV = {
    "TWILIO_ACCOUNT_SID": "AC00000000000000000000000000000000",
    "TWILIO_AUTH_TOKEN": "test-auth-token",
    "TWILIO_PHONE_NUMBER": "+15550000000",
    "LITELLM_API_KEY": "test-litellm-key",
    "VOICE_BRIDGE_API_KEY": "test-bridge-key",
}


def load_module(env: dict[str, str], name: str = "voice_bridge_under_test",
                configmap_path: pathlib.Path | None = None) -> types.ModuleType:
    """Exec the extracted source in a fresh module with `env` as the ONLY env.

    `env` fully replaces os.environ for the duration of module execution, so a
    key absent from `env` is genuinely unset for the module's `os.getenv` reads.
    """
    source = extract_source(configmap_path)
    stubs = _build_stubs()
    saved_env = dict(os.environ)
    saved_modules = {k: sys.modules.get(k) for k in stubs}
    mod = types.ModuleType(name)
    mod.__file__ = str(CONFIGMAP) + "::data.server.py"
    try:
        os.environ.clear()
        os.environ.update(env)
        sys.modules.update(stubs)
        code = compile(source, mod.__file__, "exec")
        exec(code, mod.__dict__)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        for k, v in saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    mod._stubs = stubs
    mod._env = dict(env)
    return mod


def run(coro):
    """Run a coroutine on a private event loop."""
    return asyncio.run(coro)


@contextlib.contextmanager
def module_env(mod):
    """Re-apply the module's load-time env for code reading os.getenv at runtime.

    `lifespan()` re-reads the required-variable list on every startup, so a test
    that exercises startup must run under the environment the module was loaded
    with.
    """
    saved = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(mod._env)
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def run_env(mod, factory):
    """run(factory()) with the module's load-time environment applied."""
    with module_env(mod):
        return asyncio.run(factory())


if __name__ == "__main__":
    src = extract_source()
    print(f"configmap={CONFIGMAP}")
    print(f"data_key={DATA_KEY} lines={len(src.splitlines())} bytes={len(src)}")
    compile(src, "server.py", "exec")
    print("compile=OK")
    m = load_module(dict(BASE_ENV, LLM_API_KEY="k", AUDIO_DIR="/tmp/vb-audio-probe"))
    print(f"import=OK callables={sum(callable(v) for v in vars(m).values())}")
