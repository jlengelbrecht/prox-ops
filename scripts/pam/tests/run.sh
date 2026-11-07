#!/usr/bin/env bash
# The agent PAM regression suite, against a throwaway dev server. It pins the OpenBao control
# group behaviour this code relies on, so rerun it on every OpenBao upgrade: bump VERSION and
# SHA256 (or point BAO_BIN at the new binary).
# Nothing here reaches the cluster, 1Password or a live OpenBao.
#
# Needs python3 with cryptography, and chromium for the page tests (CHROMIUM names the binary).
#
# Usage: scripts/pam/tests/run.sh [unittest arguments, e.g. test_client]
set -euo pipefail

VERSION=2.7.2
SHA256=582b1f3c2fdc5afd443b2d57a225ef654de75adea609fdb66731fd582a9cbc96
HERE=$(cd "$(dirname "$0")" && pwd)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if [[ -z ${BAO_BIN:-} ]]; then
  archive=openbao_${VERSION}_linux_amd64.tar.gz
  curl -fsSL -o "$work/$archive" "https://github.com/openbao/openbao/releases/download/v$VERSION/$archive"
  echo "$SHA256  $work/$archive" | sha256sum -c --quiet
  tar -xzf "$work/$archive" -C "$work" bao
  BAO_BIN=$work/bao
fi
export BAO_BIN

cd "$HERE"
if [[ $# -gt 0 ]]; then
  exec python3 -m unittest "$@"
fi
python3 -m unittest discover -v
