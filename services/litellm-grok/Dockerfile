FROM ghcr.io/berriai/litellm-database:v1.108.0@sha256:80c91fd00cc039e8880474f539544b919f14cd258a22f22ce4218b864c4ff14a
COPY --chmod=0444 patch_image.py /tmp/grok-patch/patch_image.py
RUN python3 /tmp/grok-patch/patch_image.py && rm -rf /tmp/grok-patch
