#!/usr/bin/env bash

# Talos Secure Boot Boot Order Fix Script
# Fixes: "Talos is already installed to disk but booted from another media and talos.halt_if_installed kernel parameter is set"
#
# This script provides automated fixes for the Talos secure boot + Proxmox UEFI boot order issue
# See: TALOS_SECUREBOOT_BOOT_ORDER_ISSUE.md for full details

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Configuration
PROXMOX_HOST="${PROXMOX_HOST:-}"
PROXMOX_USER="${PROXMOX_USER:-root}"
VM_IDS=()
NODE_IPS=()

# Functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

show_usage() {
    cat << EOF
Talos Secure Boot Boot Order Fix Script

USAGE:
    $0 [OPTIONS] SOLUTION

SOLUTIONS:
    remove-iso          Remove ISO from all VMs and reboot (FASTEST - 5 minutes)
    patch-config        Apply machine config patch to disable halt_if_installed (RECOMMENDED - 15 minutes)
    hybrid              Combination: Remove ISO immediately, then apply patch (BEST FOR PRODUCTION)

OPTIONS:
    --vm-ids "100 101 102 ..."      Space-separated list of VM IDs to fix (required for remove-iso)
    --node-ips "IP1 IP2 IP3 ..."    Space-separated list of node IPs (required for patch-config)
    --proxmox-host HOSTNAME         Proxmox host to SSH into (required for remove-iso)
    --proxmox-user USER             Proxmox SSH user (default: root)
    -h, --help                      Show this help message

EXAMPLES:
    # Fix via ISO removal (Proxmox access required)
    $0 --vm-ids "100 101 102 103 104" --proxmox-host pve1 remove-iso

    # Fix via config patch (talosctl access required)
    $0 --node-ips "10.20.67.1 10.20.67.2 10.20.67.3" patch-config

    # Hybrid approach (recommended)
    $0 --vm-ids "100 101 102 103 104" --proxmox-host pve1 --node-ips "10.20.67.1 10.20.67.2 10.20.67.3" hybrid

ENVIRONMENT VARIABLES:
    PROXMOX_HOST        Proxmox hostname/IP (alternative to --proxmox-host)
    TALOSCONFIG         Path to talosconfig file (default: talos/clusterconfig/talosconfig)

REQUIREMENTS:
    - For remove-iso: SSH access to Proxmox host
    - For patch-config: talosctl installed and configured
    - For hybrid: Both of the above

SEE ALSO:
    TALOS_SECUREBOOT_BOOT_ORDER_ISSUE.md - Full technical analysis and solutions
EOF
}

check_requirements() {
    local solution=$1

    case "$solution" in
        remove-iso|hybrid)
            if [[ -z "$PROXMOX_HOST" ]]; then
                log_error "Proxmox host not specified. Use --proxmox-host or set PROXMOX_HOST environment variable."
                exit 1
            fi

            if [[ ${#VM_IDS[@]} -eq 0 ]]; then
                log_error "No VM IDs specified. Use --vm-ids option."
                exit 1
            fi

            # Test SSH connectivity
            log_info "Testing SSH connectivity to Proxmox host: $PROXMOX_HOST"
            if ! ssh -o ConnectTimeout=5 "${PROXMOX_USER}@${PROXMOX_HOST}" "exit" 2>/dev/null; then
                log_error "Cannot connect to Proxmox host via SSH. Check credentials and network access."
                exit 1
            fi
            log_success "SSH connectivity confirmed"
            ;;

        patch-config|hybrid)
            # Check talosctl
            if ! command -v talosctl &> /dev/null; then
                log_error "talosctl not found. Install it first: mise install talosctl"
                exit 1
            fi

            # Check node IPs
            if [[ ${#NODE_IPS[@]} -eq 0 ]]; then
                log_error "No node IPs specified. Use --node-ips option."
                exit 1
            fi

            # Check talosconfig
            local talosconfig="${TALOSCONFIG:-$REPO_ROOT/talos/clusterconfig/talosconfig}"
            if [[ ! -f "$talosconfig" ]]; then
                log_warning "Talosconfig not found at: $talosconfig"
                log_warning "Nodes may not be configured yet. Config patch will be prepared but not applied."
            fi
            ;;
    esac
}

solution_remove_iso() {
    log_info "SOLUTION: Remove ISO from VMs and reboot"
    log_info "This will remove CD/DVD drives from ${#VM_IDS[@]} VMs and reboot them"
    echo ""

    # Confirm
    read -p "Continue? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Cancelled"
        exit 0
    fi

    # Remove ISOs
    log_info "Removing ISOs from VMs..."
    for vmid in "${VM_IDS[@]}"; do
        log_info "  VM $vmid: Removing ide2 (CDROM)..."
        if ssh "${PROXMOX_USER}@${PROXMOX_HOST}" "qm set $vmid --delete ide2" 2>/dev/null; then
            log_success "  VM $vmid: ISO removed"
        else
            log_warning "  VM $vmid: Failed to remove ISO (may not exist)"
        fi
    done

    echo ""
    log_info "Rebooting VMs..."
    for vmid in "${VM_IDS[@]}"; do
        log_info "  VM $vmid: Rebooting..."
        if ssh "${PROXMOX_USER}@${PROXMOX_HOST}" "qm reboot $vmid" 2>/dev/null; then
            log_success "  VM $vmid: Reboot initiated"
        else
            log_error "  VM $vmid: Reboot failed"
        fi
    done

    echo ""
    log_success "ISO removal and reboot complete"
    log_info "VMs will boot from disk in 2-3 minutes"
    log_info ""
    log_info "NEXT STEPS:"
    log_info "1. Wait 3-5 minutes for all VMs to boot"
    log_info "2. Verify with: kubectl get nodes"
    log_info "3. Consider applying config patch for permanent fix (see TALOS_SECUREBOOT_BOOT_ORDER_ISSUE.md)"
}

solution_patch_config() {
    log_info "SOLUTION: Apply machine config patch to disable halt_if_installed"
    log_info "This will create a config patch and apply it to ${#NODE_IPS[@]} nodes"
    echo ""

    # Create patch directory if it doesn't exist
    local patch_dir="$REPO_ROOT/talos/patches/global"
    mkdir -p "$patch_dir"

    local patch_file="$patch_dir/disable-halt-if-installed.yaml"

    # Create patch file
    log_info "Creating patch file: $patch_file"
    cat > "$patch_file" << 'EOF'
---
# Disable talos.halt_if_installed to allow booting from ISO with disk installed
# This is needed for Proxmox VMs where UEFI NVRAM may boot ISO before disk
#
# Background:
# - Talos secure boot ISOs have talos.halt_if_installed=1 embedded in UKI
# - This parameter cannot be overridden at boot time (secure boot design)
# - UEFI NVRAM may prioritize CDROM boot entry over disk
# - This patch disables the halt for the INSTALLED system (not the ISO)
#
# See: TALOS_SECUREBOOT_BOOT_ORDER_ISSUE.md for full details

machine:
  install:
    extraKernelArgs:
      - -talos.halt_if_installed
EOF

    log_success "Patch file created"

    # Check if talconfig.yaml exists
    local talconfig="$REPO_ROOT/talos/talconfig.yaml"
    if [[ ! -f "$talconfig" ]]; then
        log_error "talconfig.yaml not found at: $talconfig"
        exit 1
    fi

    # Check if patch is already in talconfig.yaml
    if grep -q "disable-halt-if-installed.yaml" "$talconfig"; then
        log_success "Patch already referenced in talconfig.yaml"
    else
        log_warning "Patch NOT referenced in talconfig.yaml"
        log_info "You need to add this line to the 'patches:' section in talconfig.yaml:"
        echo ""
        echo "  - \"@./patches/global/disable-halt-if-installed.yaml\""
        echo ""
        read -p "Add automatically? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            # Add patch to talconfig.yaml (before controlPlane section)
            # This is a simple append to patches section
            # For production, consider using yq or manual editing
            log_warning "Automatic addition not implemented. Please add manually."
            log_info "Edit: $talconfig"
            exit 0
        else
            log_info "Please add patch manually to talconfig.yaml"
            exit 0
        fi
    fi

    # Regenerate configs
    log_info "Regenerating Talos configurations..."
    cd "$REPO_ROOT"
    if command -v task &> /dev/null; then
        if task talos:generate; then
            log_success "Configurations regenerated"
        else
            log_error "Failed to regenerate configurations"
            exit 1
        fi
    else
        log_error "Task command not found. Install with: mise install task"
        exit 1
    fi

    # Apply configs to nodes
    log_info "Applying configurations to nodes..."
    echo ""

    local talosconfig="${TALOSCONFIG:-$REPO_ROOT/talos/clusterconfig/talosconfig}"
    if [[ ! -f "$talosconfig" ]]; then
        log_error "Talosconfig not found. Cannot apply configurations."
        log_info "Configurations have been generated in: $REPO_ROOT/talos/clusterconfig/"
        log_info "Apply them manually with: talosctl apply-config --nodes <IP> --file <config.yaml>"
        exit 1
    fi

    export TALOSCONFIG="$talosconfig"

    for ip in "${NODE_IPS[@]}"; do
        log_info "  Node $ip: Applying configuration..."

        # Find config file for this IP
        local config_file=$(grep -l "\"$ip\"" "$REPO_ROOT"/talos/clusterconfig/*.yaml | head -1)
        if [[ -z "$config_file" ]]; then
            log_error "  Node $ip: Config file not found"
            continue
        fi

        if talosctl apply-config --nodes "$ip" --file "$config_file" 2>/dev/null; then
            log_success "  Node $ip: Configuration applied"
        else
            log_error "  Node $ip: Failed to apply configuration"
        fi
    done

    echo ""
    log_success "Configuration patch complete"
    log_info ""
    log_info "NEXT STEPS:"
    log_info "1. Reboot all nodes: talosctl reboot --nodes <all-IPs>"
    log_info "2. Wait 3-5 minutes for nodes to reboot"
    log_info "3. Verify with: kubectl get nodes"
    log_info "4. Commit changes: git add talos/ && git commit -m 'fix: disable halt_if_installed'"
}

solution_hybrid() {
    log_info "SOLUTION: Hybrid approach (Remove ISO + Patch Config)"
    log_info "This combines immediate fix (ISO removal) with permanent fix (config patch)"
    echo ""

    # Phase 1: Remove ISO
    log_info "PHASE 1: Remove ISOs and reboot (immediate fix)"
    solution_remove_iso

    echo ""
    echo "========================================="
    echo ""

    # Wait for user to confirm nodes are back online
    log_info "PHASE 2: Apply config patch (permanent fix)"
    log_info "Wait for nodes to come back online before proceeding"
    echo ""
    read -p "Are all nodes online? Verify with 'kubectl get nodes' (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Skipping phase 2. You can run it later with: $0 --node-ips \"...\" patch-config"
        exit 0
    fi

    solution_patch_config
}

# Parse arguments
SOLUTION=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --vm-ids)
            shift
            IFS=' ' read -ra VM_IDS <<< "$1"
            shift
            ;;
        --node-ips)
            shift
            IFS=' ' read -ra NODE_IPS <<< "$1"
            shift
            ;;
        --proxmox-host)
            PROXMOX_HOST="$2"
            shift 2
            ;;
        --proxmox-user)
            PROXMOX_USER="$2"
            shift 2
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        remove-iso|patch-config|hybrid)
            SOLUTION="$1"
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            show_usage
            exit 1
            ;;
    esac
done

# Validate solution specified
if [[ -z "$SOLUTION" ]]; then
    log_error "No solution specified"
    show_usage
    exit 1
fi

# Check requirements
check_requirements "$SOLUTION"

# Execute solution
case "$SOLUTION" in
    remove-iso)
        solution_remove_iso
        ;;
    patch-config)
        solution_patch_config
        ;;
    hybrid)
        solution_hybrid
        ;;
esac
