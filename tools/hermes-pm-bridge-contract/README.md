# hermes-pm-bridge-contract

Frozen request contract for the Hermes PM bridge, published as data (C0) so that shipped code can
consume it without reaching into untracked planning directories:

| File | What it is |
| --- | --- |
| `request.schema.json` | JSON Schema (draft 2020-12) of the inner request envelope Hermes sends as the single `text/plain` part of an A2A `SendMessage` |
| `fixtures.json` | the 27 canonical cases (`valid-*` and `reject-*`), each with its expected `schema_valid` result |
| `schema-validation.json` | the recorded validation run (jsonschema 4.26.0, draft 2020-12, format checks on, 27/27) and the sha256 of the two files above |
| `MANIFEST.json` | sha256 of every file, provenance, the 27 fixture ids and their expected syntax results |

The three contract files are byte-for-byte copies of the originals under
`_bmad-output/specs/spec-hermes-pm-bridge/`; `schema-validation.json` records the same sha256 values
that `MANIFEST.json` lists, so any edit is visible. Do not modify them here: a contract change is a
new frozen revision with a new validation run, not an edit.

## Validating against this contract

`request.schema.json` uses `format: uuid` and `format: date-time`. In draft 2020-12 `format` is an
annotation by default, so a validator with default settings accepts `reject-invalid-uuid` and
`reject-bad-calendar-date` even though the fixtures expect rejection. Consumers must validate with a
draft 2020-12 validator that has format assertion enabled and that enforces UUID syntax and
calendar-valid RFC 3339 date-times for those two formats. All 27 fixture verdicts must reproduce; those
two cases are the ones only a format check rejects.

The recorded run in `schema-validation.json` (`format_checks: true`) is Python `jsonschema` 4.26.0 with
format checking enabled, which in that library is
`Draft202012Validator(schema, format_checker=FormatChecker())`. Its `date-time` checker only exists when
`rfc3339-validator` is installed; without it `FormatChecker()` silently has no `date-time` entry and
`reject-bad-calendar-date` (`2026-02-30T20:10:00Z`, which matches the `pattern`) is accepted. Check that
both `uuid` and `date-time` are in `FormatChecker().checkers` before trusting a result. Another validator
is fine if it meets the same requirement and reproduces 27/27.

## Consumers

This change publishes the data only; no consumer ships with it. The two `consumers` entries in
`MANIFEST.json` name the intended ones, each arriving with its own source change: the egress proof
(`tools/hermes-pm-egress/proof/run-proof-egress.sh`) is planned to mount this directory read-only into
the offline real-client proof (row S1 sends `valid-status.get`), and the broker listener (C3) is
expected to validate against `request.schema.json` and test against `fixtures.json` under the
requirement above.
