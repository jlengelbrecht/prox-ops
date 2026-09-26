# agent-router client manifest

`manifest.json` tells a launch host which catalog the deployed agent-router runs and which
`agent-stamp-validate` versions accept the stamps it issues. It lives in this public repository so every
host can read it without a credential, including hosts that can't reach the router's network.

## Contract

- **Location.** `clients/agent-router/manifest.json` on `main` of `jlengelbrecht/prox-ops`. This path is stable.
  Everything else a host needs is named inside the manifest.
- **Schema `prox-ops/agent-router-client/v1`.** Fields may be added within v1. Anything else gets a new
  `schema` value, and hosts refuse a schema they don't know.

| field | meaning |
| --- | --- |
| `plane` | The plane name the router reports in `/v1/whoami` |
| `catalog.document_version` | The catalog's own `version` field |
| `catalog.digest` | `sha256:` of the raw catalog file. Once Flux has applied the commit (minutes), it equals `catalog_version` in `GET /v1/status` |
| `catalog.path` | Repository path of the catalog file. Follow it and never hard-code it: the path can move, the manifest can't |
| `validator.min_version` / `max_version_exclusive` | The validator versions that accept the router's stamps against this catalog |
| `validator.recommended_version` | The version the deployed router ships |
| `validator.artifacts.<os>_<arch>` | Release file name and sha256 of the recommended build, for the platforms we publish |
| `validator.source` | Where those files come from |

## Fetching and verifying

1. Resolve `main` to a commit SHA, for example with
   `git ls-remote https://github.com/jlengelbrecht/prox-ops refs/heads/main`.
2. Fetch the manifest and then the catalog from that same commit:
   `https://raw.githubusercontent.com/jlengelbrecht/prox-ops/<sha>/<path>`.
3. Check that `sha256` of the catalog bytes equals `catalog.digest`, and that your installed validator is inside the
   version window.
4. On any mismatch, or if anything is unreachable, keep the last verified catalog and validator pair and report it.

Don't mix files from different commits. The manifest and the catalog are checked against each other in CI on every
change, so a single commit is always consistent.

## Change notices

A catalog change inside the validator window needs no notice. Hosts pick it up on their next poll. Raising
`min_version` or `max_version_exclusive` means hosts must change their validator, so the owners of the launch
hosts are told before that change merges.
