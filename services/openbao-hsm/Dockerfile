# OpenBao for the cluster, with what its YubiHSM 2 seal needs. The official
# image is Alpine (musl); the kms-pkcs11 plugin and Yubico's PKCS#11 module are
# glibc, so the server runs here on Debian instead:
#   - bao: the static binary from the official image, checksum-verified
#   - openbao-plugin-kms-pkcs11: baked in (checksum-verified), so the pods never
#     download a plugin and need no registry egress; the server config declares
#     it with `command` + `sha256sum`, and OpenBao re-checks the sha256 before
#     every exec
#   - yubihsm_pkcs11.so with the HTTP backend, to reach yubihsm-connector
#   - our own minimal entrypoint at the path the Helm chart calls (the official
#     one needs dumb-init and bash syntax)
# User openbao, uid 100 / gid 1000, as the chart's defaults expect. No secrets
# or configuration in the image.
#
# On a version bump (Renovate updates the FROM digests), update the matching
# sha256 lines below and the plugin's sha256sum in the server config.
FROM ghcr.io/openbao/openbao:2.7.2@sha256:5046911075e137e154ce49ab051502f4ec7cf367d484ae4a4dce665d9b90561f AS bao
FROM ghcr.io/openbao/openbao-plugin-kms-pkcs11:v0.2.1@sha256:babba58243c7db7ffb9b906e25f35643704e8814792f5b678b3c215acba284f5 AS kms

FROM docker.io/library/debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/jlengelbrecht/prox-ops" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="2.7.2-hsm.1" \
      org.opencontainers.image.description="OpenBao 2.7.2 on Debian with the kms-pkcs11 plugin and the YubiHSM 2 PKCS#11 module; no secrets or config"
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        yubihsm-pkcs11=2.6.0-5 \
        libyubihsm2=2.6.0-5 \
        libyubihsm-http2=2.6.0-5 \
        ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 1000 openbao && \
    useradd --uid 100 --gid 1000 --home-dir /home/openbao --no-create-home --shell /usr/sbin/nologin openbao
COPY --from=bao --chmod=0555 /usr/bin/bao /usr/bin/bao
COPY --from=kms --chmod=0555 /openbao-plugin-kms-pkcs11 /usr/lib/openbao/plugins/openbao-plugin-kms-pkcs11
RUN echo "9c5f36287c673f386e1533e9c044169a248e4ee50dd54810fa766e95bd41b788  /usr/bin/bao" | sha256sum -c - && \
    echo "e88632ee52600513bd0e77242823ef57641f1eaac019d8c66515c8bca81e007d  /usr/lib/openbao/plugins/openbao-plugin-kms-pkcs11" | sha256sum -c - && \
    mkdir -p /openbao/config /openbao/data /openbao/audit /openbao/logs /home/openbao && \
    chown 100:1000 /openbao/data /openbao/audit /openbao/logs /home/openbao
COPY --chmod=0555 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
USER 100:1000
# The PKCS#11 module reads ./yubihsm_pkcs11.conf from the working directory
# before YUBIHSM_PKCS11_CONF, so the working directory must not contain one.
WORKDIR /home/openbao
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["bao", "version"]
