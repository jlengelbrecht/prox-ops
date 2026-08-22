# syntax=docker/dockerfile:1
#
# agent-router container image. Runtime security baseline (STORY-035-9a):
# non-root, read-only root filesystem, drops ALL capabilities, RuntimeDefault
# seccomp, no embedded credentials, graceful SIGTERM, health/ready endpoints,
# build SHA and version exposed. Enforced here where it can be (USER, no
# writable layers needed); capability-drop, seccomp and read-only rootfs are
# Pod-level and belong to 35.9b's manifests, not this image.

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

# renovate: datasource=docker depName=gcr.io/distroless/static
FROM gcr.io/distroless/static-debian12:nonroot@sha256:1c2c046bc09ed40fad370b599a0b1ae7987f55b01e247cf27a7c27cd97e5bbc7

COPY --from=build /out/agent-router /agent-router

# distroless "nonroot" is uid/gid 65532; stated explicitly rather than
# relied on implicitly.
USER 65532:65532

EXPOSE 8080

ENTRYPOINT ["/agent-router"]
