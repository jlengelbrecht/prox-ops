# yubihsm-tools

Debian trixie-slim plus `yubihsm-shell`, `libyubihsm2`/`libyubihsm-usb2`, `yubihsm-pkcs11`,
`pkcs11-provider`, and `openssl`, for a short-lived pod that reaches the YubiHSM 2 through
the device plugin on one worker.

The PKCS#11 module is at `/usr/lib/x86_64-linux-gnu/pkcs11/yubihsm_pkcs11.so`; the OpenSSL
provider is at `/usr/lib/x86_64-linux-gnu/ossl-modules/pkcs11.so`.

The image holds no secrets, serial numbers, or connector configuration. It runs as
`65532:65532` and just sleeps until a pod overrides its lifetime with
`activeDeadlineSeconds`.

CI publishes `ghcr.io/jlengelbrecht/yubihsm-tools:main` and `:sha-<commit>` from `main`;
pin consumers by digest (`:main@sha256:...`, from the publish workflow's step summary).

Renovate does not bump the APT pins above: when Debian trixie ships a new `yubihsm-shell`,
update the Dockerfile by hand and re-verify.
