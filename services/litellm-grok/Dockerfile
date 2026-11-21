FROM ghcr.io/berriai/litellm-database:v1.110.2@sha256:3b239acc68e601c436e5feeec7ca0f01a41cd5dffe50975142b7fa7155c1b2ff
COPY --chmod=0444 patch_image.py /tmp/grok-patch/patch_image.py
RUN python3 /tmp/grok-patch/patch_image.py && rm -rf /tmp/grok-patch
