FROM ghcr.io/berriai/litellm-database:v1.109.1@sha256:932c54440654f8f8466a85887612d95dcfb04ae2fc01257844332f4bf3b96986
COPY --chmod=0444 patch_image.py /tmp/grok-patch/patch_image.py
RUN python3 /tmp/grok-patch/patch_image.py && rm -rf /tmp/grok-patch
