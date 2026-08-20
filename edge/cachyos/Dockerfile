# llama-swap + llama.cpp (ROCm) for the CachyOS RX 7900 XTX edge worker.
#
# Why an image of our own rather than an upstream one: llama-swap publishes
# container images for cpu/cuda/vulkan/intel/musa only — there is no ROCm
# variant. Building on llama.cpp's own ROCm server image and copying the
# llama-swap release binary in is the shape llama-swap itself calls its
# "legacy" image, and it keeps the llama.cpp build number identical to the one
# the cluster's A5000 placement runs (server-cuda-b9917), which is what makes
# 35.7's comparison a comparison rather than two unrelated measurements.
#
# EDGE-WORKER-CONTRACT §3 (ruling R3): llama-swap is the LAN-facing service —
# it terminates TLS and enforces the API key. llama-server is spawned by
# llama-swap inside this container and binds 127.0.0.1 only; nothing publishes
# its port. See llama-swap.yaml and docker-compose.yaml.

# renovate: datasource=github-releases depName=mostlygeek/llama-swap
ARG LLAMA_SWAP_VERSION=250
ARG LLAMA_SWAP_SHA256=1675b0bcdb0791f6172d22993ab22a8097c25a0adda4bb8467d2c31871fb77a0

FROM curlimages/curl:8.10.1 AS swapbin
ARG LLAMA_SWAP_VERSION
ARG LLAMA_SWAP_SHA256
# curlimages/curl defaults to an unprivileged user that cannot write to a
# WORKDIR docker just created as root.
USER root
WORKDIR /tmp/llama-swap
RUN curl -fsSL -o llama-swap.tar.gz \
      "https://github.com/mostlygeek/llama-swap/releases/download/v${LLAMA_SWAP_VERSION}/llama-swap_${LLAMA_SWAP_VERSION}_linux_amd64.tar.gz" \
    && echo "${LLAMA_SWAP_SHA256}  llama-swap.tar.gz" | sha256sum -c - \
    && tar -xzf llama-swap.tar.gz llama-swap \
    && chmod 0755 llama-swap

# renovate: datasource=docker depName=ghcr.io/ggml-org/llama.cpp
FROM ghcr.io/ggml-org/llama.cpp:server-rocm-b9917 AS runtime
COPY --from=swapbin /tmp/llama-swap/llama-swap /usr/local/bin/llama-swap

# Baked in so the image runs on its own, and bind-mounted over by
# docker-compose.yaml so the code the container runs is the code in Git — a
# supervisor or guard fix should be an edit and a restart, not a rebuild.
COPY scripts/ /edge/scripts/

# llama.cpp's server image sets LLAMA_ARG_HOST=0.0.0.0 so that its own
# ENTRYPOINT serves the world. We do not use that ENTRYPOINT, and a stray
# 0.0.0.0 default on the upstream llama-server would defeat AC1, so the
# default is pinned to loopback here as well as on the llama-server command
# line in llama-swap.yaml. Two independent statements of the same requirement,
# on purpose: whichever one a future edit drops, the other still holds.
ENV LLAMA_ARG_HOST=127.0.0.1

WORKDIR /app

# The base image's healthcheck probes http://localhost:8080/health, which is
# llama-server's port on a plaintext scheme — neither of which exists here.
# Disabled rather than left to report a wrong answer; docker-compose.yaml
# defines the real one, against llama-swap's TLS port and the edge CA.
HEALTHCHECK NONE

# Not llama-swap directly: the supervisor is what withdraws the listener when
# the host claims the GPU, and it needs to be PID 1 to do that and still reap
# the child. See scripts/edge-supervisor.sh.
ENTRYPOINT ["/edge/scripts/edge-supervisor.sh"]
