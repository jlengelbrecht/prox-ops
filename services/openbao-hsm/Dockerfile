# OpenBao for the cluster, with what its YubiHSM 2 seal needs. The official
# image is Alpine (musl); the kms-pkcs11 plugin and Yubico's PKCS#11 module are
# glibc, so the server runs here on Debian instead:
#   - bao: the static binary from the official image
#   - openbao-plugin-kms-pkcs11: baked in, so the pods never download a plugin
#     and need no registry egress. The build writes /etc/openbao/plugin-kms.hcl,
#     which declares it with `command` and the sha256 computed here; OpenBao
#     re-checks that sha256 before every exec. Load it as a second config file
#     (-config=/etc/openbao/plugin-kms.hcl)
#   - yubihsm_pkcs11.so with the HTTP backend, to reach yubihsm-connector
#   - our own minimal entrypoint at the path the Helm chart calls (the official
#     one needs dumb-init and bash syntax)
#   - /bin/sh is busybox ash, not dash. The chart starts the server with
#     `/bin/sh -ec "<script>"`, so PID 1 is sh with bao as its child (as in the
#     official image) and the kubelet's SIGTERM never reaches bao. The chart
#     relies on its preStop, `/bin/sh -c "sleep 5 && kill -SIGTERM $(pidof bao)"`,
#     to stop bao gracefully. dash, and bash in sh mode, reject `kill -SIGTERM`,
#     so every stop ended in SIGKILL; ash accepts it.
# User openbao, uid 100 / gid 1000, as the chart's defaults expect. No secrets
# or configuration in the image.
#
# The images are pinned by digest and the Debian packages by version. A
# Renovate digest bump is the whole update; nothing else has to change.
FROM ghcr.io/openbao/openbao:2.7.2@sha256:5046911075e137e154ce49ab051502f4ec7cf367d484ae4a4dce665d9b90561f AS bao
FROM ghcr.io/openbao/openbao-plugin-kms-pkcs11:v0.3.0@sha256:3439b94593dc2e32fddb6d418d992b6f8c5d81c47f3a3687eb5ef938deaf4e25 AS kms

FROM docker.io/library/debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/jlengelbrecht/prox-ops" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.description="OpenBao on Debian with the kms-pkcs11 plugin and the YubiHSM 2 PKCS#11 module; no secrets or config"
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        yubihsm-pkcs11=2.6.0-5 \
        libyubihsm2=2.6.0-5 \
        libyubihsm-http2=2.6.0-5 \
        busybox=1:1.37.0-6+b9 \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 1000 openbao && \
    useradd --uid 100 --gid 1000 --home-dir /home/openbao --no-create-home --shell /usr/sbin/nologin openbao && \
    ln -sf busybox /usr/bin/sh
COPY --from=bao --chmod=0555 /usr/bin/bao /usr/bin/bao
COPY --from=kms --chmod=0555 /openbao-plugin-kms-pkcs11 /usr/lib/openbao/plugins/openbao-plugin-kms-pkcs11
RUN sum=$(sha256sum /usr/lib/openbao/plugins/openbao-plugin-kms-pkcs11 | cut -d' ' -f1) && \
    mkdir -p /etc/openbao && \
    printf '%s\n' \
      '# Generated at image build. The YubiHSM 2 seal plugin, baked into this image.' \
      '# The PKCS#11 module reads its connector settings from the env path below;' \
      '# the plugin gets no other environment.' \
      'plugin_directory = "/usr/lib/openbao/plugins"' \
      'plugin "kms" "pkcs11" {' \
      '  command   = "openbao-plugin-kms-pkcs11"' \
      "  sha256sum = \"$sum\"" \
      '  args      = ["-alias=yubihsm=/usr/lib/x86_64-linux-gnu/pkcs11/yubihsm_pkcs11.so"]' \
      '  env       = ["YUBIHSM_PKCS11_CONF=/openbao/userconfig/yubihsm/yubihsm_pkcs11.conf"]' \
      '}' > /etc/openbao/plugin-kms.hcl && \
    chmod 0444 /etc/openbao/plugin-kms.hcl && \
    mkdir -p /openbao/config /openbao/data /openbao/audit /openbao/logs /home/openbao && \
    chown 100:1000 /openbao/data /openbao/audit /openbao/logs /home/openbao
COPY --chmod=0555 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
USER 100:1000
# The PKCS#11 module reads ./yubihsm_pkcs11.conf from the working directory
# before YUBIHSM_PKCS11_CONF, so the working directory must not contain one.
WORKDIR /home/openbao
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["bao", "version"]
