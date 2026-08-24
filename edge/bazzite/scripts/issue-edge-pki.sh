#!/usr/bin/env bash
#
# Issue the dedicated edge PKI for a LAN edge worker: a private CA and one
# server leaf for this host.
#
# EDGE-WORKER-CONTRACT §2 (ruling R4) sets the bar this script exists to meet:
#
#   * The trust anchor must be a real CA (basicConstraints CA:TRUE). The 35.4
#     spike measured agentgateway rejecting a self-signed leaf used as its own
#     anchor with `invalid peer certificate: CaUsedAsEndEntity`, so a bare
#     self-signed certificate is not an option, only a two-level chain.
#   * The leaf's SAN must match the hostname the gateway backend references,
#     which is why --hostname is required and why an IP is refused.
#   * The host key is dedicated. The Kubernetes wildcard private key must never
#     be reused for an edge host.
#   * The output never enters Git. Point --out at a directory outside any
#     checkout (a docker volume, or a root-owned host directory).
#
# This is the provisional procedure. There is no automated non-Kubernetes PKI
# flow in this estate today — cert-manager cannot mount a Certificate onto a
# host it does not schedule — so issuance and renewal are operator actions run
# from here. See README.md §"TLS and PKI (provisional)".
#
# Usage:
#   issue-edge-pki.sh --hostname NAME --out DIR [options]
#
# Required:
#   --hostname NAME   DNS name the cluster will use for this edge host. Becomes
#                     the leaf's CN and its only subjectAltName. Must be a name,
#                     not an address.
#   --out DIR         Output directory. Created if absent.
#
# Optional:
#   --ca-days N       CA validity in days (default: 3650)
#   --leaf-days N     Leaf validity in days (default: 397)
#   --leaf-only       Reuse the existing CA in --out and reissue only the leaf.
#                     This is the renewal path: the cluster-side ConfigMap
#                     holding the CA does not change, so nothing needs a Git
#                     commit to rotate the host certificate.
#   --force           Overwrite existing files instead of refusing.
#
# Outputs, in DIR:
#   edge-ca.crt       trust anchor. Copy this to the cluster (35.8 references it
#                     from policies.tls.caCertificateRefs) and pass it to
#                     conformance.sh with --ca-cert. It is public material.
#   edge-ca.key       CA signing key. Secret. Back it up out of band; losing it
#                     means reissuing the anchor everywhere it is trusted.
#   edge-host.crt     server leaf for --hostname.
#   edge-host.key     server key. Secret, and specific to this host.
#
# Exit status: 0 on success, 2 on usage/environment error, 1 on failure.

set -euo pipefail

HOSTNAME_ARG=""
OUT_DIR=""
CA_DAYS=3650
LEAF_DAYS=397
LEAF_ONLY=0
FORCE=0

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

die() {
    echo "FATAL: $1" >&2
    exit "${2:-1}"
}

require_value() {
    if [ "$2" -lt 2 ]; then
        die "$1 requires a value" 2
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --hostname) require_value "$1" "$#"; HOSTNAME_ARG="$2"; shift 2 ;;
        --out) require_value "$1" "$#"; OUT_DIR="$2"; shift 2 ;;
        --ca-days) require_value "$1" "$#"; CA_DAYS="$2"; shift 2 ;;
        --leaf-days) require_value "$1" "$#"; LEAF_DAYS="$2"; shift 2 ;;
        --leaf-only) LEAF_ONLY=1; shift ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$HOSTNAME_ARG" ] || die "--hostname is required" 2
[ -n "$OUT_DIR" ] || die "--out is required" 2

command -v openssl >/dev/null 2>&1 || die "openssl not found on PATH" 2

# An IP SAN would satisfy TLS and defeat the point. 35.4's decision is the
# hostname form: it gets agentgateway's automatic Host rewrite, keeps a re-IP a
# DNS change rather than a Git change, and is what the leaf can actually be
# validated against.
case "$HOSTNAME_ARG" in
    *[!0-9.]*) : ;;
    *) die "--hostname looks like an IPv4 address; the contract requires a DNS name" 2 ;;
esac

mkdir -p "$OUT_DIR"

CA_KEY="$OUT_DIR/edge-ca.key"
CA_CRT="$OUT_DIR/edge-ca.crt"
LEAF_KEY="$OUT_DIR/edge-host.key"
LEAF_CRT="$OUT_DIR/edge-host.crt"

if [ "$LEAF_ONLY" -eq 1 ]; then
    [ -r "$CA_KEY" ] || die "--leaf-only needs an existing $CA_KEY"
    [ -r "$CA_CRT" ] || die "--leaf-only needs an existing $CA_CRT"
fi

guard_existing() {
    local path="$1"
    if [ -e "$path" ] && [ "$FORCE" -eq 0 ]; then
        die "$path already exists; pass --force to overwrite (this invalidates everything trusting it)"
    fi
}

if [ "$LEAF_ONLY" -eq 0 ]; then
    guard_existing "$CA_KEY"
    guard_existing "$CA_CRT"
fi
guard_existing "$LEAF_KEY"
guard_existing "$LEAF_CRT"

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/edge-pki.XXXXXX")
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

# Keys are written under this umask so they are never briefly world-readable.
umask 077

if [ "$LEAF_ONLY" -eq 0 ]; then
    echo "issuing CA (${CA_DAYS}d) -> $CA_CRT"
    openssl ecparam -name prime256v1 -genkey -noout -out "$CA_KEY"
    # pathlen:0 says this CA may sign leaves and no intermediates, which is all
    # an edge anchor should ever be able to do.
    openssl req -x509 -new -key "$CA_KEY" -sha256 -days "$CA_DAYS" \
        -subj "/CN=homelab0 edge CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -addext "subjectKeyIdentifier=hash" \
        -out "$CA_CRT"
fi

echo "issuing leaf for $HOSTNAME_ARG (${LEAF_DAYS}d) -> $LEAF_CRT"
openssl ecparam -name prime256v1 -genkey -noout -out "$LEAF_KEY"
openssl req -new -key "$LEAF_KEY" -subj "/CN=$HOSTNAME_ARG" -out "$WORK_DIR/leaf.csr"

cat >"$WORK_DIR/leaf.ext" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$HOSTNAME_ARG
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EXT

openssl x509 -req -in "$WORK_DIR/leaf.csr" \
    -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial \
    -days "$LEAF_DAYS" -sha256 -extfile "$WORK_DIR/leaf.ext" \
    -out "$LEAF_CRT"

chmod 0600 "$LEAF_KEY"
chmod 0644 "$LEAF_CRT" "$CA_CRT"
[ "$LEAF_ONLY" -eq 1 ] || chmod 0600 "$CA_KEY"

# Verifying here rather than at first request means a chain mistake surfaces
# now, in the issuing step, instead of as a TLS handshake failure that reads
# like a network problem.
openssl verify -CAfile "$CA_CRT" "$LEAF_CRT" >/dev/null \
    || die "the issued leaf does not verify against the issued CA"

echo
echo "CA subject : $(openssl x509 -noout -subject -in "$CA_CRT")"
echo "CA is a CA : $(openssl x509 -noout -text -in "$CA_CRT" | grep -A1 'Basic Constraints' | tr -d '\n ' )"
echo "leaf SAN   : $(openssl x509 -noout -ext subjectAltName -in "$LEAF_CRT" | tail -n1 | tr -d ' ')"
echo "leaf until : $(openssl x509 -noout -enddate -in "$LEAF_CRT")"
echo
echo "Next steps:"
echo "  * keep $LEAF_KEY and $CA_KEY out of any checkout; nothing here belongs in Git"
echo "  * hand $CA_CRT to the cluster side (35.8, policies.tls.caCertificateRefs)"
echo "  * test with: edge/conformance.sh --ca-cert $CA_CRT ..."
