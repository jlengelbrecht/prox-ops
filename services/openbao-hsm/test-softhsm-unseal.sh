#!/bin/sh
# Proves that this image's bao and kms-pkcs11 plugin can seal and auto-unseal
# with a PKCS#11 token: init, stop, start, and the server must unseal itself.
# Runs as the image's user, in a throwaway image that adds softhsm2 and opensc.
# Same plugin and an RSA key, like the YubiHSM seal. Two things differ: Yubico's
# module can't run without a device, and SoftHSM's OAEP only supports SHA-1.
set -eu

lib=/usr/lib/softhsm/libsofthsm2.so
work=$(mktemp -d)
export SOFTHSM2_CONF="$work/softhsm2.conf"
export BAO_ADDR=http://127.0.0.1:8200
mkdir "$work/tokens" "$work/data"
printf 'directories.tokendir = %s\nobjectstore.backend = file\n' "$work/tokens" >"$SOFTHSM2_CONF"
softhsm2-util --init-token --free --label openbao --so-pin 0000 --pin 1234 >/dev/null
pkcs11-tool --module "$lib" --token-label openbao --login --pin 1234 \
  --keypairgen --key-type rsa:2048 --id 0211 --label openbao-seal >/dev/null

# The plugin stanza the build generated, with the test module and token.
sum=$(sha256sum /usr/lib/openbao/plugins/openbao-plugin-kms-pkcs11 | cut -d' ' -f1)
grep -q "sha256sum = \"$sum\"" /etc/openbao/plugin-kms.hcl
cat >"$work/config.hcl" <<HCL
plugin_directory = "/usr/lib/openbao/plugins"
plugin "kms" "pkcs11" {
  command   = "openbao-plugin-kms-pkcs11"
  sha256sum = "$sum"
  args      = ["-alias=softhsm=$lib"]
  env       = ["SOFTHSM2_CONF=$SOFTHSM2_CONF"]
}
listener "tcp" {
  address     = "127.0.0.1:8200"
  tls_disable = true
}
storage "raft" {
  path    = "$work/data"
  node_id = "test"
}
seal "pkcs11" {
  lib           = "softhsm"
  token_label   = "openbao"
  pin           = "1234"
  key_label     = "openbao-seal"
  key_id        = "0x0211"
  mechanism     = "CKM_RSA_PKCS_OAEP"
  rsa_oaep_hash = "sha1"
}
api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://127.0.0.1:8201"
disable_mlock = true
HCL

start() {
  bao server -config="$work/config.hcl" >"$work/server.log" 2>&1 &
  server=$!
  i=0
  until bao status >/dev/null 2>&1 || [ $? -eq 2 ]; do
    i=$((i + 1))
    if [ "$i" -gt 30 ] || ! kill -0 "$server" 2>/dev/null; then
      cat "$work/server.log"
      echo "error: the server did not start" >&2
      exit 1
    fi
    sleep 1
  done
}

start
bao operator init -recovery-shares=1 -recovery-threshold=1 >/dev/null
kill "$server"
wait "$server" || true

start
i=0
until bao status >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 30 ]; then
    cat "$work/server.log"
    echo "error: the server did not unseal itself" >&2
    exit 1
  fi
  sleep 1
done
if ! grep -q "unsealed with stored key" "$work/server.log"; then
  cat "$work/server.log"
  echo "error: the server is up but did not unseal through the seal" >&2
  exit 1
fi
kill "$server"
wait "$server" || true
echo "OK: initialised, restarted and unsealed itself through PKCS#11"
