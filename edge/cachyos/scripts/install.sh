#!/usr/bin/env bash
#
# Idempotent install/refresh of the CachyOS edge worker's host-side daemons
# into a stable, checkout-independent location.
#
# The systemd units (systemd/*.service) point ExecStart at
# ~/.local/libexec/edge-cachyos/, never at this checkout. systemd's
# ExecStart= does not expand environment variables in the binary path
# (`ExecStart=$SOMEVAR/script.sh` is silently wrong, not merely undocumented),
# so the only two shapes that work are a fixed path or an indirection like
# this install step -- run it once after cloning, and again after every pull
# that touches scripts/, systemd/, model-id-map.json or llama-swap.yaml. It is
# safe to re-run any number of times; every file it writes is replaced,
# nothing is appended.
#
# The install layout mirrors this directory's on purpose --
# ~/.local/libexec/edge-cachyos/{model-id-map.json,README.md,llama-swap.yaml,
# scripts/*.sh} -- because edge-heartbeat.sh resolves its model map as
# "$SCRIPT_DIR/../model-id-map.json" by default. Mirroring the relative
# layout means that default keeps working unmodified after install, instead
# of every script needing to learn a second, flattened directory shape.
#
# This same libexec tree is also what the CONTAINER mounts at runtime --
# docker-compose.yaml reads EDGE_LIBEXEC_DIR from .env and bind-mounts
# ${EDGE_LIBEXEC_DIR}/scripts and ${EDGE_LIBEXEC_DIR}/llama-swap.yaml, never a
# path relative to wherever `docker compose` happens to run from. That is what
# makes this script, not the checkout, the thing a deployed node depends on:
# re-run it after a pull and both the host units and the container pick up the
# change on their next start/restart.
#
# What it does:
#   0. validates the configured GPU sensor binary (ROCM_SMI) exists and is
#      executable, and refuses to install if not (STORY-035-6a cycle 5) --
#      see "Preflight: GPU sensor binary" below
#   1. copies scripts/*.sh, model-id-map.json, llama-swap.yaml and README.md
#      to ~/.local/libexec/edge-cachyos/ (scripts/ subdirectory for the *.sh),
#      made executable
#   2. installs systemd/*.service to ~/.config/systemd/user/
#   3. daemon-reload
#
# It does NOT enable, start, or touch .env / secrets / PKI, and it does not
# remove the pre-fix checkout-path drop-in if one is present on this host --
# see README.md "Bring-up" and "Migrating off the checkout-path drop-in" for
# the rest of the sequence.
#
# Usage: scripts/install.sh

set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CACHYOS_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)

LIBEXEC_DIR="${XDG_LIBEXEC_HOME:-$HOME/.local/libexec}/edge-cachyos"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# Preflight: GPU sensor binary (STORY-035-6a cycle 5)
# ----------------------------------------------------
# The guard's lease now asserts "the guard recently completed a valid GPU
# safety sample", not merely "the process is alive" -- so a guard that can
# never find rocm-smi can never renew its lease, and the node it drives can
# never serve at all once the fix below ships. Installing a guard pointed at
# a sensor binary that does not exist is a deployment failure, not a runtime
# warning: catch it here, before the units are (re)installed, rather than
# after a boot with a permanently blind interlock.
#
# Reads the value from the deployed host env file, since that -- not this
# checkout's env.example -- is what EnvironmentFile= in the systemd units
# actually feeds the running guard. Falls back to the documented absolute
# default if the file does not exist yet or does not set ROCM_SMI, so a
# fresh clone with no host config yet still gets checked against something.
HOST_ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/edge-cachyos/edge.env"
ROCM_SMI_PATH="/opt/rocm/bin/rocm-smi"
if [ -r "$HOST_ENV_FILE" ]; then
    configured=$(sed -n 's/^ROCM_SMI=//p' "$HOST_ENV_FILE" | tail -n1)
    [ -n "$configured" ] && ROCM_SMI_PATH="$configured"
fi
if [ ! -x "$ROCM_SMI_PATH" ]; then
    echo "FATAL: GPU sensor binary '$ROCM_SMI_PATH' does not exist or is not executable." >&2
    echo "A guard that cannot sample the GPU can never renew its lease and can never serve (STORY-035-6a)." >&2
    echo "Fix ROCM_SMI in $HOST_ENV_FILE (must be an absolute path; a bare command name" >&2
    echo "is not resolved through PATH at boot), or install rocm-smi at that path, then re-run install.sh." >&2
    exit 1
fi
echo "GPU sensor binary OK: $ROCM_SMI_PATH"

echo "installing runtime scripts to $LIBEXEC_DIR"
mkdir -p "$LIBEXEC_DIR/scripts"
install -m 755 "$CACHYOS_DIR"/scripts/*.sh "$LIBEXEC_DIR/scripts/"
install -m 644 "$CACHYOS_DIR/model-id-map.json" "$LIBEXEC_DIR/model-id-map.json"
install -m 644 "$CACHYOS_DIR/README.md" "$LIBEXEC_DIR/README.md"
install -m 644 "$CACHYOS_DIR/llama-swap.yaml" "$LIBEXEC_DIR/llama-swap.yaml"

echo "installing user units to $UNIT_DIR"
mkdir -p "$UNIT_DIR"
install -m 644 "$CACHYOS_DIR"/systemd/*.service "$UNIT_DIR/"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
else
    echo "WARN systemctl not found on PATH; run 'systemctl --user daemon-reload' yourself" >&2
fi

echo "done. review $UNIT_DIR/edge-*.service, then:"
echo "  systemctl --user enable --now edge-interactive-guard.service edge-heartbeat.service"
