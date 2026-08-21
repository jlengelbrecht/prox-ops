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
#   4. reports whether the guard, the heartbeat and the container are
#      currently running, and if so prints ACTIVATION REQUIRED with the
#      canonical restart/recreate procedure (STORY-035-6a cycle 8) -- this
#      step only ever rewrites files on disk, it never restarts anything
#      itself, so a running process keeps executing what it loaded before
#      this ran until that procedure is followed
#
# It does NOT enable, start, or touch .env / secrets / PKI, and it does not
# remove the pre-fix checkout-path drop-in if one is present on this host --
# see README.md "Bring-up" and "Migrating off the checkout-path drop-in" for
# the rest of the sequence.
#
# Usage: scripts/install.sh

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CACHYOS_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)

# Fixed, not derived from XDG_LIBEXEC_HOME: the units' ExecStart= hardcodes
# %h/.local/libexec/edge-cachyos (systemd specifiers cannot read arbitrary
# env vars), so honouring XDG_LIBEXEC_HOME here would let this script install
# to one directory while both daemons execute from another -- stale or
# missing scripts, a guard that can never renew its lease, a node stuck
# withdrawn. This path is the single authority both sides agree on.
LIBEXEC_DIR="$HOME/.local/libexec/edge-cachyos"
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
# Canonical, not XDG-derived: the units hardcode
# EnvironmentFile=%h/.config/edge-cachyos/edge.env and systemd's %h cannot
# expand XDG_CONFIG_HOME, so honouring it here would let the preflight
# validate a file the guard never reads -- the sensor-validation guarantee
# would pass while the running guard loaded an unvalidated ROCM_SMI. Same
# reasoning as LIBEXEC_DIR above: installer path authority == systemd
# runtime path authority.
HOST_ENV_FILE="$HOME/.config/edge-cachyos/edge.env"
ROCM_SMI_PATH="/opt/rocm/bin/rocm-smi"
if [ -r "$HOST_ENV_FILE" ]; then
    configured=$(sed -n 's/^ROCM_SMI=//p' "$HOST_ENV_FILE" | tail -n1)
    # An `if`, not `[ -n ... ] && ROCM_SMI_PATH=...`: under `set -e`, a bare
    # `&&` list outside a conditional's own test propagates its left side's
    # failure and would abort the script whenever the env file exists but
    # does not set ROCM_SMI, instead of falling back to the default.
    if [ -n "$configured" ]; then
        ROCM_SMI_PATH="$configured"
    fi
fi
# Must be an absolute path to a regular, executable file. `-x` alone is not
# enough: it is also true for a directory (ROCM_SMI=/usr/bin would pass) and
# for a relative path that happens to resolve against wherever this script is
# run from -- neither is a binary the boot-time unit can actually execute.
case "$ROCM_SMI_PATH" in
    /*) ;;
    *)
        echo "FATAL: ROCM_SMI '$ROCM_SMI_PATH' is not an absolute path." >&2
        echo "A bare command name or relative path is not resolved the same way at boot as it is" >&2
        echo "from an interactive shell (STORY-035-6a cycle 5). Fix ROCM_SMI in $HOST_ENV_FILE" >&2
        echo "to an absolute path, then re-run install.sh." >&2
        exit 1
        ;;
esac
if [ ! -f "$ROCM_SMI_PATH" ] || [ ! -x "$ROCM_SMI_PATH" ]; then
    echo "FATAL: GPU sensor binary '$ROCM_SMI_PATH' does not exist or is not an executable file." >&2
    echo "A guard that cannot sample the GPU can never renew its lease and can never serve (STORY-035-6a)." >&2
    echo "Fix ROCM_SMI in $HOST_ENV_FILE, or install rocm-smi at that path, then re-run install.sh." >&2
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

# Activation-boundary messaging (STORY-035-6a cycle 8)
# ----------------------------------------------------
# This install step only ever rewrites files on disk. None of the three
# runtime consumers reload their script/config from disk once running --
# edge-interactive-guard.sh and edge-heartbeat.sh are systemd units that hold
# their code from process start, and edge-supervisor.sh is PID 1 inside a
# running container, which keeps its own copy the same way -- so a plain
# "done" here is misleading: it reads as "the fix is live" when a running
# process is still executing whatever it had loaded before this install ran.
# An unqualified "done" is exactly what let cycle 7's install look successful
# while the running supervisor kept serving a future-dated lease. This does
# NOT restart anything itself -- that would turn an idempotent file install
# into a disruptive runtime operation -- it only tells the operator, plainly,
# which of the two states they are actually in.
echo
echo "INSTALL COMPLETE      runtime files updated on disk"

running_guard=0
running_heartbeat=0
running_container=0

if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user is-active --quiet edge-interactive-guard.service 2>/dev/null; then
        running_guard=1
    fi
    if systemctl --user is-active --quiet edge-heartbeat.service 2>/dev/null; then
        running_heartbeat=1
    fi
fi

if command -v docker >/dev/null 2>&1; then
    container_state=$(docker inspect -f '{{.State.Running}}' edge-llama-swap 2>/dev/null || true)
    if [ "$container_state" = "true" ]; then
        running_container=1
    fi
fi

if [ "$running_guard" -eq 1 ] || [ "$running_heartbeat" -eq 1 ] || [ "$running_container" -eq 1 ]; then
    echo "ACTIVATION REQUIRED   running processes must be restarted/recreated"
    echo
    echo "The following are still running and hold their PREVIOUS code/config in"
    echo "memory -- none of them reload from disk on their own:"
    # `if`, not a bare `[ ... ] && echo ...`: under `set -e`, a standalone
    # command's overall exit status is the `&&` list's, so a false test
    # (nothing to print) would abort the whole script instead of just
    # skipping the line -- same trap as the ROCM_SMI fallback above.
    if [ "$running_guard" -eq 1 ]; then
        echo "  - edge-interactive-guard.service"
    fi
    if [ "$running_heartbeat" -eq 1 ]; then
        echo "  - edge-heartbeat.service"
    fi
    if [ "$running_container" -eq 1 ]; then
        echo "  - edge-llama-swap container (edge-supervisor.sh, PID 1)"
    fi
    echo
    echo "Canonical activation procedure (see README.md 'Activating an install'):"
    echo "  systemctl --user restart edge-interactive-guard.service edge-heartbeat.service"
    echo "  docker compose up -d --force-recreate   # from the deployment's compose invocation"
else
    echo "ACTIVATION REQUIRED   none -- guard, heartbeat and the container are not currently running"
    echo "review $UNIT_DIR/edge-*.service, then:"
    echo "  systemctl --user enable --now edge-interactive-guard.service edge-heartbeat.service"
fi
