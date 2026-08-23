# syntax=docker/dockerfile:1
#
# agent-router container image. Runtime security baseline (STORY-035-9a):
# non-root, read-only root filesystem, drops ALL capabilities, RuntimeDefault
# seccomp, no embedded credentials, graceful SIGTERM, health/ready endpoints,
# build SHA and version exposed. Enforced here where it can be (USER, no
# writable layers needed); capability-drop, seccomp and read-only rootfs are
# Pod-level and belong to 35.9b's manifests, not this image.
#
# STORY-035-12: this image also builds agent-stamp-validate from the SAME
# source commit, alongside agent-router. Extraction from this image is NOT
# the CLI's normative distribution path (35.12: "Extraction from the
# Kubernetes agent-router image is NOT normative" - 35.16'/35.17' own how
# Orca's real launcher installs/invokes it) - it ships here purely so gate 4
# ("build produces agent-stamp-validate; smoke-run against a fixture from
# the built artifact of the same commit") has one build to prove against.
# agent-stamp-validate is not functionality the running agent-router
# service owns or executes: the two binaries are colocated in this image
# for build convenience only, and nothing in agent-router's ENTRYPOINT ever
# invokes agent-stamp-validate or vice versa.

# renovate: datasource=docker depName=golang
FROM golang:1.24-alpine@sha256:8bee1901f1e530bfb4a7850aa7a479d17ae3a18beb6e09064ed54cfd245b7191 AS build

WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .

ARG VERSION=dev
ARG COMMIT=unknown
RUN CGO_ENABLED=0 go build \
    -trimpath \
    -ldflags "-s -w -X main.version=${VERSION} -X main.commit=${COMMIT}" \
    -o /out/agent-router \
    ./cmd/agent-router
RUN CGO_ENABLED=0 go build \
    -trimpath \
    -ldflags "-s -w" \
    -o /out/agent-stamp-validate \
    ./cmd/agent-stamp-validate

# renovate: datasource=docker depName=gcr.io/distroless/static
FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab

COPY --from=build /out/agent-router /agent-router
COPY --from=build /out/agent-stamp-validate /agent-stamp-validate

# distroless "nonroot" is uid/gid 65532; stated explicitly rather than
# relied on implicitly.
USER 65532:65532

EXPOSE 8080

ENTRYPOINT ["/agent-router"]
