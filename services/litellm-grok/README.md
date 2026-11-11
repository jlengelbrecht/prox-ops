# LiteLLM Grok image

This image applies the accepted, guarded five-file correction to the exact
LiteLLM 1.108.0 base, pinned by digest in the Dockerfile `FROM` instruction.
The build context sends only `Dockerfile` and
`patch_image.py`; it contains no callback, configuration, credentials or tests.
The patch rejects a different upstream source and a second application before
writing any file. The callback and synthetic fixture are mounted only during
qualification.

The release workflow verifies an offline build against the public pinned base
and runs 26 synthetic ASGI checks plus two exact-base comparisons. Main builds
and pushes a unique run candidate, then freshly pulls and qualifies its
registry digest. An authenticated manifest check confirms repository access
and refuses an existing `sha-<commit>` tag, including on reruns. Only after
confirmed absence does the workflow create the commit tag and move `:main`.
An uncertain registry response fails publication closed. The published digest is the only
reference suitable for later deployment review. The package must allow public
read access in GHCR; the owner must configure that visibility before release.
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
