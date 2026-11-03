# openbao-hsm

OpenBao 2.7.2 on Debian trixie, with what a YubiHSM 2 seal needs. It is built by `.github/workflows/openbao-hsm.yaml` and published as `ghcr.io/jlengelbrecht/openbao-hsm:sha-<commit>`, with `:main` moved to the tested digest. Consumers pin it by digest.

## Why not the official image

The official image is Alpine (musl). The `kms-pkcs11` plugin and Yubico's `yubihsm_pkcs11.so` are glibc, and the module dlopens libcurl and libcrypto, so they can't run there. This image contains:

| Path | What | Source |
|---|---|---|
| `/usr/bin/bao` | static OpenBao binary | official image, digest-pinned, sha256 checked at build |
| `/usr/lib/openbao/plugins/openbao-plugin-kms-pkcs11` | kms-pkcs11 v0.2.1 | plugin image, digest-pinned, sha256 checked at build |
| `/usr/lib/x86_64-linux-gnu/pkcs11/yubihsm_pkcs11.so` | Yubico PKCS#11 module, HTTP backend | Debian `yubihsm-pkcs11` / `libyubihsm-http2` 2.6.0-5 |
| `/usr/local/bin/docker-entrypoint.sh` | minimal entrypoint at the path the Helm chart calls | this directory |

It runs as `openbao`, uid 100 / gid 1000, the OpenBao Helm chart's defaults. The working directory is `/home/openbao`, which must not contain a `yubihsm_pkcs11.conf`: the module reads one from the working directory before `YUBIHSM_PKCS11_CONF`. The image carries no secrets or configuration.

## Using the plugin

Declare the baked-in binary with `command`, not `image`. Nothing is downloaded, and OpenBao re-checks the sha256 before every exec:

```hcl
plugin_directory = "/usr/lib/openbao/plugins"
plugin "kms" "pkcs11" {
  command   = "openbao-plugin-kms-pkcs11"
  version   = "v0.2.1"
  sha256sum = "e88632ee52600513bd0e77242823ef57641f1eaac019d8c66515c8bca81e007d"
  args      = ["-alias=yubihsm=/usr/lib/x86_64-linux-gnu/pkcs11/yubihsm_pkcs11.so"]
  env       = ["YUBIHSM_PKCS11_CONF=/openbao/config/yubihsm_pkcs11.conf"]
}
```

The plugin's environment is only that `env` list, so the module's config path has to be passed there.

## Updating

Renovate bumps the `FROM` digests. Each bump also needs the matching `sha256sum -c` line in the Dockerfile, the smoke test in the workflow, and the `sha256sum` in the server config. A bump that misses one fails the build, so nothing ships with a stale checksum. Debian package pins are bumped by hand, as for yubihsm-tools.
