#!/usr/bin/env bash
#
# Idempotent install/refresh of the Bazzite edge worker's host-side daemons
# and quadlet into stable, checkout-independent locations.
#
# Inherited from edge/cachyos/scripts/install.sh (see its header for the
# layout-independence rationale, which applies unchanged), with two Bazzite
# differences:
#
#   * the GPU sensor preflight validates NVIDIA_SMI rather than ROCM_SMI;
#   * the container is a quadlet, not a compose project: this script renders
#     systemd/edge-llama-swap.container.template with the values from the
#     deployed ~/.config/edge-bazzite/edge.env and installs the result to
#     ~/.config/containers/systemd/, where `systemctl --user daemon-reload`
#     regenerates edge-llama-swap.service. Rendering at install time is what
#     keeps LAN addresses out of Git while quadlet gets literal values.
#
# What it does:
#   0. validates NVIDIA_SMI is an absolute, executable path (STORY-035-6a
#      cycle 5, inherited)
#   1. copies scripts/*.sh, model-id-map.json, llama-swap.yaml and README.md
#      to ~/.local/libexec/edge-bazzite/
#   2. installs systemd/*.service and drop-ins to ~/.config/systemd/user/
#   3. renders and installs the quadlet (requires the deployed edge.env)
#   4. daemon-reload
#   5. reports ACTIVATION REQUIRED for anything already running — this script
#      only ever rewrites files on disk; nothing reloads on its own
#
# It does NOT enable, start, or touch edge.env / secrets / PKI.
#
# Usage: scripts/install.sh

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BAZZITE_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)

# Fixed, not derived from XDG variables: the units' ExecStart= hardcodes
# %h/.local/libexec/edge-bazzite and systemd specifiers cannot read arbitrary
# env vars. This path is the single authority both sides agree on.
LIBEXEC_DIR="$HOME/.local/libexec/edge-bazzite"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
QUADLET_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
HOST_ENV_FILE="$HOME/.config/edge-bazzite/edge.env"

# Preflight: GPU sensor binary (STORY-035-6a cycle 5, inherited)
# ---------------------------------------------------------------
# A guard that can never find nvidia-smi can never renew its lease and the
# node can never serve. Catch it here, before the units are (re)installed.
NVIDIA_SMI_PATH="/usr/bin/nvidia-smi"
if [ -r "$HOST_ENV_FILE" ]; then
    configured=$(sed -n 's/^NVIDIA_SMI=//p' "$HOST_ENV_FILE" | tail -n1)
    if [ -n "$configured" ]; then
        NVIDIA_SMI_PATH="$configured"
    fi
fi
case "$NVIDIA_SMI_PATH" in
    /*) ;;
    *)
        echo "FATAL: NVIDIA_SMI '$NVIDIA_SMI_PATH' is not an absolute path." >&2
        echo "A bare command name is not resolved the same way at boot as from an interactive" >&2
        echo "shell (STORY-035-6a cycle 5). Fix NVIDIA_SMI in $HOST_ENV_FILE, then re-run." >&2
        exit 1
        ;;
esac
if [ ! -f "$NVIDIA_SMI_PATH" ] || [ ! -x "$NVIDIA_SMI_PATH" ]; then
    echo "FATAL: GPU sensor binary '$NVIDIA_SMI_PATH' does not exist or is not executable." >&2
    echo "A guard that cannot sample the GPU can never renew its lease and can never serve." >&2
    exit 1
fi
echo "GPU sensor binary OK: $NVIDIA_SMI_PATH"

echo "installing runtime scripts to $LIBEXEC_DIR"
mkdir -p "$LIBEXEC_DIR/scripts"
install -m 755 "$BAZZITE_DIR"/scripts/*.sh "$LIBEXEC_DIR/scripts/"
install -m 644 "$BAZZITE_DIR/model-id-map.json" "$LIBEXEC_DIR/model-id-map.json"
install -m 644 "$BAZZITE_DIR/README.md" "$LIBEXEC_DIR/README.md"
install -m 644 "$BAZZITE_DIR/llama-swap.yaml" "$LIBEXEC_DIR/llama-swap.yaml"

echo "installing user units to $UNIT_DIR"
mkdir -p "$UNIT_DIR"
install -m 644 "$BAZZITE_DIR"/systemd/*.service "$UNIT_DIR/"
for dropin_dir in "$BAZZITE_DIR"/systemd/*.service.d; do
    [ -d "$dropin_dir" ] || continue
    unit_dropin_dir="$UNIT_DIR/$(basename "$dropin_dir")"
    mkdir -p "$unit_dropin_dir"
    install -m 644 "$dropin_dir"/*.conf "$unit_dropin_dir/"
done

# Quadlet rendering
# -----------------
# Every @TOKEN@ in the template must resolve from the deployed edge.env, and
# an unrendered token must never reach quadlet — it would generate a unit
# with a literal '@EDGE_...@' mount source that fails in a way that reads
# like a podman bug rather than a config gap.
if [ ! -r "$HOST_ENV_FILE" ]; then
    echo "SKIP quadlet render: $HOST_ENV_FILE does not exist yet (copy env.example there first)."
    echo "     The host units are installed; re-run install.sh after configuring to render the container unit."
else
    # shellcheck disable=SC1090
    values=$(
        set -a
        . "$HOST_ENV_FILE"
        set +a
        for var in EDGE_IMAGE EDGE_BIND_ADDR EDGE_PORT EDGE_HOSTNAME \
                   EDGE_MODEL_SOURCE EDGE_PKI_SOURCE EDGE_SECRETS_SOURCE \
                   EDGE_STATE_HOST_DIR EDGE_LIBEXEC_DIR \
                   EDGE_GUARD_LEASE_TTL EDGE_CACHE_SCAN_INTERVAL; do
            eval "v=\${$var:-}"
            if [ -z "$v" ]; then
                echo "MISSING $var" >&2
                exit 1
            fi
            printf '%s=%s\n' "$var" "$v"
        done
    ) || { echo "FATAL: the variables above are unset in $HOST_ENV_FILE; cannot render the quadlet" >&2; exit 1; }

    if [ "$(printf '%s\n' "$values" | sed -n 's/^EDGE_LIBEXEC_DIR=//p')" != "$LIBEXEC_DIR" ]; then
        echo "FATAL: EDGE_LIBEXEC_DIR in $HOST_ENV_FILE does not equal $LIBEXEC_DIR." >&2
        echo "install.sh always installs there and the units hardcode it; make them agree." >&2
        exit 1
    fi

    echo "rendering quadlet to $QUADLET_DIR/edge-llama-swap.container"
    mkdir -p "$QUADLET_DIR"
    rendered=$(cat "$BAZZITE_DIR/systemd/edge-llama-swap.container.template")
    while IFS='=' read -r k v; do
        rendered=${rendered//"@$k@"/"$v"}
    done <<<"$values"
    if printf '%s' "$rendered" | grep -q '@EDGE_[A-Z_]*@'; then
        echo "FATAL: unrendered tokens remain in the quadlet:" >&2
        printf '%s' "$rendered" | grep -o '@EDGE_[A-Z_]*@' | sort -u >&2
        exit 1
    fi
    printf '%s\n' "$rendered" >"$QUADLET_DIR/edge-llama-swap.container"
    chmod 644 "$QUADLET_DIR/edge-llama-swap.container"
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload
else
    echo "WARN systemctl not found on PATH; run 'systemctl --user daemon-reload' yourself" >&2
fi

# Activation-boundary messaging (STORY-035-6a cycle 8, inherited): this
# script only ever rewrites files on disk. None of the three runtime
# consumers reload from disk on their own.
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
    if systemctl --user is-active --quiet edge-llama-swap.service 2>/dev/null; then
        running_container=1
    fi
fi

if [ "$running_guard" -eq 1 ] || [ "$running_heartbeat" -eq 1 ] || [ "$running_container" -eq 1 ]; then
    echo "ACTIVATION REQUIRED   running processes must be restarted/recreated"
    echo
    echo "The following are still running and hold their PREVIOUS code/config in"
    echo "memory -- none of them reload from disk on their own:"
    if [ "$running_guard" -eq 1 ]; then
        echo "  - edge-interactive-guard.service"
    fi
    if [ "$running_heartbeat" -eq 1 ]; then
        echo "  - edge-heartbeat.service"
    fi
    if [ "$running_container" -eq 1 ]; then
        echo "  - edge-llama-swap.service (edge-supervisor.sh, PID 1)"
    fi
    echo
    echo "Canonical activation procedure:"
    echo "  systemctl --user restart edge-interactive-guard.service edge-heartbeat.service"
    echo "  systemctl --user restart edge-llama-swap.service"
else
    echo "ACTIVATION REQUIRED   none -- guard, heartbeat and the container are not currently running"
    echo "review $UNIT_DIR/edge-*.service, then:"
    echo "  systemctl --user enable --now edge-interactive-guard.service edge-heartbeat.service"
    echo "  systemctl --user start edge-llama-swap.service   # quadlet units cannot be 'enabled'; the [Install] section starts it at boot"
fi
