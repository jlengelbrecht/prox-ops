"""Host-side, read-only observation adapter for the Hermes-Orca connector.

Layers, each importable on its own: ``errors``/``fields``/``handles``/``config``
(bounded types, positive projection, HMAC handles, local key and gate
settings); ``runner`` (allowlisted bounded execution behind the optional local
gate and the per-operation budget); ``discovery`` (local-host-anchored
project/coordinator snapshot); ``counts`` (bounded status counts); and the
``adapter`` entry point above them.

Everything fails closed with a bounded error code. Raw subprocess output is
never logged, raised, stored or returned.
"""

from .errors import AdapterError  # noqa: F401
