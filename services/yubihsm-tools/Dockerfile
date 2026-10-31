# A setup/signing tools image for the YubiHSM 2. Used once, interactively, from a
# short-lived pod to replace the factory credential, create keys and print
# certificates; and xp-assertion-signer, run as a sidecar, to sign client
# assertions with a non-exportable HSM key. Carries no secrets or configuration
# — only Yubico's packages from Debian trixie and the signer script. The
# PKCS#11 module is at /usr/lib/x86_64-linux-gnu/pkcs11/yubihsm_pkcs11.so and
# the OpenSSL provider is at /usr/lib/x86_64-linux-gnu/ossl-modules/pkcs11.so;
# the signer uses neither (it calls yubihsm-shell directly).
FROM docker.io/library/debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/jlengelbrecht/prox-ops" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="0.2.0" \
      org.opencontainers.image.description="YubiHSM 2 setup and signing tools (yubihsm-shell, PKCS#11 module, OpenSSL provider, xp-assertion-signer); no secrets or config"
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        yubihsm-shell=2.6.0-5 \
        libyubihsm2=2.6.0-5 \
        libyubihsm-usb2=2.6.0-5 \
        yubihsm-pkcs11=2.6.0-5 \
        pkcs11-provider=1.0-3 \
        openssl && \
    rm -rf /var/lib/apt/lists/*
COPY --chmod=0555 xp-assertion-signer /usr/local/bin/xp-assertion-signer
USER 65532:65532
WORKDIR /tmp
CMD ["sleep", "infinity"]
