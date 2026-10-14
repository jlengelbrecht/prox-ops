# yubihsm-tools

Debian trixie-slim plus `yubihsm-shell`, `libyubihsm2`/`libyubihsm-usb2`, `yubihsm-pkcs11`,
`pkcs11-provider`, and `openssl`, for a short-lived pod that reaches the YubiHSM 2 through
the device plugin on one worker.

The image has no yubihsm-connector, so talk to the HSM over USB directly:
`yubihsm-shell --connector yhusb://`, and `connector = yhusb://` in the PKCS#11 module's
configuration file.

The PKCS#11 module is at `/usr/lib/x86_64-linux-gnu/pkcs11/yubihsm_pkcs11.so`; the OpenSSL
provider is at `/usr/lib/x86_64-linux-gnu/ossl-modules/pkcs11.so`.

The image holds no secrets, serial numbers, or connector configuration. It runs as
`65532:65532` and just sleeps until a pod overrides its lifetime with
`activeDeadlineSeconds`.

CI publishes `:sha-<commit>` from `main`, smoke-tests that digest, and only then moves
`ghcr.io/jlengelbrecht/yubihsm-tools:main` to it, if the image's files on `main` still match that commit;
pin consumers by digest (`:main@sha256:...`, from the publish workflow's step summary).

Renovate does not bump the APT pins above: when Debian trixie ships a new `yubihsm-shell`,
update the Dockerfile by hand and re-verify.

## Assertion signer

`/usr/local/bin/xp-assertion-signer` is a POSIX `sh` script run as a sidecar next to a
credential consumer. `run` loops forever, signing a short-lived client-assertion JWT per
configured identity with an HSM-resident key and writing it atomically (temp file, `chmod
0444`, rename) to an output directory, typically a memory-backed volume. `check` is a
probe: it reports whether every expected assertion file exists and is younger than a
max age. `selftest` (used in CI) exercises the same JWT-building code against a
throwaway software key and certificate, with no HSM and no config required.

Configuration is a directory of one-file-per-value (a mounted Secret): `auth-key-id`,
`password`, `tenant-id`, `identities`, and per identity `<name>.client-id`,
`<name>.key-id`, `<name>.crt`. The auth key password is held in a shell variable only:
it is never exported, written to disk, or logged, and it leaves the signer process only
as `yubihsm-shell`'s own `-p` argument. Containers in a pod don't see each other's
processes unless the pod sets `shareProcessNamespace: true`, which the signer's pod must
not do. The argument is still visible to anything else running in the signer container
itself; that is a known limitation of passing the password on the command line.

The signer talks to the HSM over direct USB (`yhusb://`) with `yubihsm-shell` and does
not use the PKCS#11 module or the OpenSSL provider above: they do not work with this
HSM for this operation.
