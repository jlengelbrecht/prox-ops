# Worker-only image: the Hindsight API release the API pods run, with the
# worker module patched so its own loop handler is the only SIGINT/SIGTERM
# owner (see patch_worker.py). The API keeps the unmodified upstream image.
# This digest moves only together with the API image pin in hindsight.yaml.
FROM ghcr.io/vectorize-io/hindsight-api:0.9.2@sha256:7b14a1f4062252992d0176758753615e0a2071d9a269995be007be223ab01812

USER root
# The patch script is bind-mounted, so it never ships in the image. The build
# fails, leaving no image, if the installed module differs from the pinned bytes.
RUN --mount=type=bind,source=patch_worker.py,target=/tmp/hindsight-worker-patch/patch_worker.py \
    python3 /tmp/hindsight-worker-patch/patch_worker.py
# The UID/GID the API and worker pods already run as.
USER 1000:1000
