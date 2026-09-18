# hermes-pm-bridge-contract

Frozen request contract for the Hermes PM bridge, published as data (C0) so that shipped code can
consume it without reaching into untracked planning directories:

| File | What it is |
| --- | --- |
| `request.schema.json` | JSON Schema (draft 2020-12) of the inner request envelope Hermes sends as the single `text/plain` part of an A2A `SendMessage` |
| `fixtures.json` | the 27 canonical cases (`valid-*` and `reject-*`), each with its expected `schema_valid` result |
| `schema-validation.json` | the recorded validation run (jsonschema 4.26.0, 27/27) and the sha256 of the two files above |
| `MANIFEST.json` | sha256 of every file, provenance, the 27 fixture ids and their expected syntax results |

The three contract files are byte-for-byte copies of the originals under
`_bmad-output/specs/spec-hermes-pm-bridge/`; `schema-validation.json` records the same sha256 values
that `MANIFEST.json` lists, so any edit is visible. Do not modify them here: a contract change is a
new frozen revision with a new validation run, not an edit.

Consumers: `tools/hermes-pm-egress/proof/run-proof-egress.sh` mounts this directory read-only into the
offline real-client proof (row S1 sends `valid-status.get`); the broker listener (C3) is expected to
validate against `request.schema.json` and test against `fixtures.json`.
