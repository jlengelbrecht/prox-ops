FROM ghcr.io/berriai/litellm-database:v1.110.4@sha256:d8cd8c5b8bde7e3959b22f5adbd5492d5a12be143173ebb1c7a4f30d07028005
COPY --chmod=0444 patch_image.py /tmp/grok-patch/patch_image.py
RUN python3 /tmp/grok-patch/patch_image.py && rm -rf /tmp/grok-patch
