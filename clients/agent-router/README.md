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
| `catalog.digest` | `sha256:` of the raw catalog file. When the commit is the export of the active catalog (below), it equals `catalog_version` in `GET /v1/status` |
| `catalog.path` | Repository path of the catalog file. Follow it and never hard-code it: the path can move, the manifest can't |
| `validator.min_version` / `max_version_exclusive` | The validator versions that accept the router's stamps against this catalog |
| `validator.recommended_version` | A validator build verified against this catalog, inside the window. It is not tied to the router's own version: the router can move on while hosts stay on a validator the window still allows |
| `validator.artifacts.<os>_<arch>` | Release file name and sha256 of the recommended build, for the platforms we publish |
| `validator.source` | Where those files come from |

## Where the catalog comes from

From router v0.7.0 the catalog in this repository is a distribution copy of the router's catalog, not its input:

- **Seed.** On the first boot of an empty store, the router imports the file at `catalog.path` once as its seed.
  `GET /v1/status` then reports that file's digest.
- **Authority.** After that, the router's catalog is the one last activated through the console's operator API. A
  merged edit to the file changes nothing the router routes on.
- **Distribution copy.** After each activation, the operator exports the exact activated bytes from the operator API,
  checks their `sha256`, and commits them at `catalog.path` in the same commit as the new `catalog.digest` and
  `catalog.document_version` here.
- **Hosts read Git.** A launch host's catalog sync reads this manifest and follows it, as described below. It does
  not fetch a catalog from the router.

So an activation reaches hosts in three steps: the console activates, the export lands on `main`, and each launch
host runs its catalog sync. Until all three are done, the router issues stamps against a digest the hosts don't
trust yet, and those stamps fail validation. New dispatch stays paused from activation until the active catalog,
`GET /v1/status`, a newly issued stamp and every launch host's trusted catalog all name the same digest.

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

A catalog activation inside the validator window needs no notice beyond the dispatch pause above. Hosts pick up the
exported copy on their next sync. Raising
`min_version` or `max_version_exclusive` means hosts must change their validator, so the owners of the launch
hosts are told before that change merges.
