# yubihsm-tools

Debian trixie-slim plus `yubihsm-shell`, `libyubihsm2`/`libyubihsm-usb2`, `yubihsm-pkcs11`,
`pkcs11-provider`, and `openssl`, for a short-lived pod that reaches the YubiHSM 2 through
the device plugin on one worker.

A pod that holds the device can talk to it over USB directly:
`yubihsm-shell --connector yhusb://`, and `connector = yhusb://` in the PKCS#11 module's
configuration file.

The image also carries `yubihsm-connector` (Debian trixie, pinned), which bridges the USB
device to HTTP. Run it as the only process that holds the device and every other client reaches the HSM
with `yubihsm-shell --connector http://<service>:12345` instead. The connector has no
authentication of its own and listens on plain HTTP: bind it to the pod IP
(`-l <pod-ip>:12345`), not to all addresses, and treat the NetworkPolicy in front of its
Service as the access control. Sessions are authenticated and encrypted between the client
and the device, but the session handshake travels over that HTTP, so use a long random
auth-key password. The image includes the HTTP backend (`libyubihsm-http2`) that
`yubihsm-shell -C http://...` needs.

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

By default the signer talks to the HSM over direct USB (`yhusb://`). An optional `connector`
config file (`http://host:port`, `https://...` or `yhusb://`) points `yubihsm-shell` at a
yubihsm-connector instead, so the pod needs no device. Signing goes through `yubihsm-shell` and does
not use the PKCS#11 module or the OpenSSL provider above. In our setup, logging in
through the OpenSSL provider failed with a password that `yubihsm-shell` accepted, and
that wasn't debugged further; signing through `yubihsm-shell` avoids the extra layers.

## Re-enrolment after a factory reset

`/usr/local/bin/hsm-reset-enrol` (bash) re-creates the cluster's objects on a factory-reset
YubiHSM 2 through yubihsm-connector: the admin key 0x0002 with a wide delegated set, the three
identity keys 0x0101-0x0103 (rsa2048, `sign-pkcs` and `sign-pss`, not exportable), the signer
auth key 0x0003, and deletes the factory key. `--certs` then builds one self-signed certificate
per identity key, signed on the HSM. The owner drives it from the devbox; secrets arrive on
stdin only and never appear on an argv. `--dry-run` prints the plan, `--preflight` checks
that the HSM is factory-fresh without changing anything, and `--selftest` runs the
certificate builder against a throwaway software key. The header of the script documents the
modes, input and secret handling.

## OpenBao's HSM objects

`/usr/local/bin/openbao-hsm-enrol` (bash) creates OpenBao's objects on an enrolled YubiHSM 2 through yubihsm-connector, using the admin key 0x0002:
- the seal key 0x0211, rsa3072, `decrypt-oaep` and `exportable-under-wrap`;
- the backup wrap key 0x0020, **imported** from the owner's key material so a copy exists outside the HSM;
- OpenBao's auth key 0x0011, domains 9 and 10.

It then exports the seal key wrapped under 0x0020, and proves restore with a throwaway key: wrap, delete, re-import the wrap key from the owner's copy, `put-wrapped`, compare the public key. The owner drives it from the devbox with `scripts/openbao-hsm-enrol.sh`, and secrets arrive on stdin only. `--dry-run` prints the plan and `--check` is a read-only preflight. yubihsm-shell 2.6.0's action-mode `decrypt-oaep` is not implemented, so the restore test compares public keys rather than decrypting.
