# Parent must supply the locally cached, verified exact deployment image digest.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY --chmod=0444 patch_image.py /tmp/grok-patch/patch_image.py
RUN python3 /tmp/grok-patch/patch_image.py && rm -rf /tmp/grok-patch
