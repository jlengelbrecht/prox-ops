# LiteLLM Grok image

This image applies the accepted, guarded five-file correction to the exact
LiteLLM 1.108.0 base, pinned by digest in the Dockerfile `FROM` instruction.
The build context sends only `Dockerfile` and
`patch_image.py`; it contains no callback, configuration, credentials or tests.
The patch rejects a different upstream source and a second application before
writing any file. The callback and synthetic fixture are mounted only during
qualification.

The release workflow verifies an offline build against the public pinned base
and runs 26 synthetic ASGI checks plus two exact-base comparisons. Only the
serialized trusted main publish job has GHCR write permission in this workflow. It builds a run-unique
candidate with repository source and commit revision labels, pushes it, then
pulls and qualifies its actual registry digest. The authenticated manifest
guard checks the candidate before reading `sha-<commit>`.

Before any commit tag or `:main` promotion, the workflow also reads and hashes the exact
candidate manifest through an anonymous GHCR token exchange, with no Docker daemon or credentials. On
reuse it separately checks anonymous manifest access for the existing commit digest. The
first candidate push creates the package if needed; GitHub defaults a new GHCR
package to private. If anonymous registry read then fails, publication stops with the
candidate present and no commit or `:main` tag promoted. The owner can open the
new `litellm-grok` package settings, set visibility to **Public** if intended,
and rerun the workflow. This action changes package visibility and belongs to
the owner; the workflow does not change visibility or grant access. An existing
package may also need the owner to grant this repository Actions write access
before the candidate push can succeed.

If the commit tag is absent, the job checks again immediately before creating
it from the qualified candidate, then checks the resulting registry digest.
If another writer creates the tag before that second check, the job stops
without pushing to the tag. If a previous attempt already
created it, the job pulls that digest separately, checks both source labels
against this trusted repository and commit, and runs the full qualification
again. A rerun may build a different candidate digest; it reuses the existing
qualified commit digest without rewriting the tag. Conflicting provenance,
failed qualification, missing evidence, authentication errors, and uncertain
registry responses fail closed. GHCR tags remain mutable: this workflow keeps
commit-addressed tags stable while its trusted publication jobs are serialized.
GHCR does not provide an atomic create-if-absent step here, so an external or
manual writer after the second check could still replace a tag. Such writers
are outside this publication boundary; repository maintainers and the owner
are trusted administrators. Before moving `:main`, the job rechecks the commit
tag and compares package source with fetched main at that point in time. A
stale run skips `:main`. The `queue: max` setting permits at most 100 pending
jobs; if the queue overflows, an owner must redispatch after capacity returns.
No global newest-source atomic ordering is promised. Later GitOps deployment
must use the verified content digest, never a mutable commit or `:main` tag.

These labels are repository-workflow provenance within the trusted GHCR and
repository boundary, not a cryptographic attestation. GHCR package write
access and public visibility require owner validation for this publication PR.
An absent or inaccessible package cannot be proven public before its first
candidate push. The anonymous registry gate establishes exact-manifest public read access at
publication time, before commit or `:main` promotion. The owner must confirm
the package access/bootstrap path before merging this PR.
No provider or cluster credentials are used by the synthetic tests.

Local qualification requires a cached base with the declared RepoDigest and a
working Docker daemon. Supply its image ID to the canonical runner:

```sh
python3 tests/litellm/run_grok_pinned_image.py \
  --base-image-id 'sha256:<verified local ID>' \
  --receipt /tmp/grok-image-receipt.json
```

To requalify a separately acquired published digest, pass
`--candidate-image ghcr.io/jlengelbrecht/litellm-grok@sha256:<digest>`.
Synthetic execution uses `--network none`. Keepalive remains disabled and is
outside qualification. A generic API 500 before provider dispatch is a paired
closed rejection, not provider-error qualification. Activation and live
provider validation are later, separate work.
