#!/usr/bin/env bash

# Talos UEFI Boot Issue Diagnostic Script
# Checks Proxmox VMs for common boot configuration issues

set -euo pipefail

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROXMOX_HOST="${PROXMOX_HOST:-}"
VM_IDS="${VM_IDS:-}"
NODE_IPS=(10.20.67.{1..15})

echo -e "${BLUE}=== Talos UEFI Boot Issue Diagnostic ===${NC}\n"

# Check if running on Proxmox or remote
if [ -d /etc/pve ]; then
    echo -e "${GREEN}✓ Running on Proxmox host${NC}"
    ON_PROXMOX=true
else
    echo -e "${YELLOW}! Not running on Proxmox host${NC}"
    if [ -z "$PROXMOX_HOST" ]; then
        echo -e "${RED}✗ Set PROXMOX_HOST environment variable to check VMs${NC}"
        echo "  Example: export PROXMOX_HOST=root@proxmox.example.com"
        ON_PROXMOX=false
    else
        echo -e "${BLUE}  Will check via SSH to: $PROXMOX_HOST${NC}"
        ON_PROXMOX=ssh
    fi
fi

# Function to run command on Proxmox
run_on_proxmox() {
    if [ "$ON_PROXMOX" = true ]; then
        eval "$1"
    elif [ "$ON_PROXMOX" = "ssh" ]; then
        ssh "$PROXMOX_HOST" "$1"
    else
        echo -e "${YELLOW}Skipped (not on Proxmox): $1${NC}"
        return 1
    fi
}

# Function to get VM IDs
get_vm_ids() {
    if [ -n "$VM_IDS" ]; then
        echo "$VM_IDS"
        return
    fi

    # Try to auto-detect Talos VMs
    if [ "$ON_PROXMOX" = true ] || [ "$ON_PROXMOX" = "ssh" ]; then
        echo -e "\n${BLUE}Detecting Talos VMs...${NC}"
        local vms
        vms=$(run_on_proxmox "qm list | grep -i talos | awk '{print \$1}'" 2>/dev/null || echo "")
        if [ -z "$vms" ]; then
            vms=$(run_on_proxmox "qm list | grep -E 'k8s-(ctrl|work)' | awk '{print \$1}'" 2>/dev/null || echo "")
        fi
        if [ -n "$vms" ]; then
            echo -e "${GREEN}Found VMs: $vms${NC}"
            echo "$vms"
        else
            echo -e "${YELLOW}Could not auto-detect VMs${NC}"
            echo ""
        fi
    fi
}

# Function to check VM configuration
check_vm_config() {
    local vmid=$1
    local config

    echo -e "\n${BLUE}--- VM $vmid Configuration ---${NC}"

    if [ "$ON_PROXMOX" != true ] && [ "$ON_PROXMOX" != "ssh" ]; then
        echo -e "${YELLOW}Skipped (not on Proxmox)${NC}"
        return
    fi

    config=$(run_on_proxmox "qm config $vmid" 2>/dev/null || echo "ERROR")

    if [ "$config" = "ERROR" ]; then
        echo -e "${RED}✗ Could not read VM $vmid config${NC}"
        return
    fi

    # Check BIOS type
    local bios
    bios=$(echo "$config" | grep "^bios:" | awk '{print $2}' || echo "seabios")
    if [ "$bios" = "ovmf" ]; then
        echo -e "${GREEN}✓ BIOS: OVMF (UEFI)${NC}"
    else
        echo -e "${RED}✗ BIOS: $bios (should be OVMF for UEFI)${NC}"
    fi

    # Check EFI disk
    local efidisk
    efidisk=$(echo "$config" | grep "^efidisk0:" || echo "")
    if [ -n "$efidisk" ]; then
        echo -e "${GREEN}✓ EFI Disk: Present${NC}"
        echo "  $efidisk"

        # Check for secure boot
        if echo "$efidisk" | grep -q "pre-enrolled-keys=1"; then
            echo -e "${YELLOW}⚠ Secure Boot: ENABLED (pre-enrolled-keys=1)${NC}"
            echo -e "  ${YELLOW}→ Must use installer-secureboot variant${NC}"
        elif echo "$efidisk" | grep -q "pre-enrolled-keys=0"; then
            echo -e "${GREEN}✓ Secure Boot: DISABLED (pre-enrolled-keys=0)${NC}"
            echo -e "  ${GREEN}→ Can use standard installer${NC}"
        else
            echo -e "${YELLOW}⚠ Secure Boot: Unknown (no pre-enrolled-keys)${NC}"
        fi
    else
        echo -e "${RED}✗ EFI Disk: MISSING${NC}"
        echo -e "  ${RED}→ Add EFI disk: qm set $vmid --efidisk0 local-lvm:0,efitype=4m,pre-enrolled-keys=0${NC}"
    fi

    # Check CD-ROM
    local cdrom
    cdrom=$(echo "$config" | grep "^ide2:" || echo "")
    if [ -n "$cdrom" ]; then
        if echo "$cdrom" | grep -q "none"; then
            echo -e "${GREEN}✓ CD-ROM: Not mounted${NC}"
        else
            echo -e "${YELLOW}⚠ CD-ROM: Mounted${NC}"
            echo "  $cdrom"
            echo -e "  ${YELLOW}→ Remove after installation: qm set $vmid --ide2 none${NC}"
        fi
    else
        echo -e "${GREEN}✓ CD-ROM: Not configured${NC}"
    fi

    # Check boot order
    local boot
    boot=$(echo "$config" | grep "^boot:" || echo "")
    if [ -n "$boot" ]; then
        echo -e "${BLUE}Boot Order:${NC}"
        echo "  $boot"

        if echo "$boot" | grep -q "scsi0"; then
            echo -e "${GREEN}  ✓ scsi0 in boot order${NC}"
        else
            echo -e "${RED}  ✗ scsi0 NOT in boot order${NC}"
        fi

        if echo "$boot" | grep -q "ide2"; then
            echo -e "${YELLOW}  ⚠ ide2 in boot order (should remove after install)${NC}"
        fi
    else
        echo -e "${YELLOW}⚠ Boot Order: Not explicitly set${NC}"
        echo -e "  ${YELLOW}→ Set boot order: qm set $vmid --boot order=scsi0${NC}"
    fi

    # Check disk
    local disk
    disk=$(echo "$config" | grep "^scsi0:" || echo "")
    if [ -n "$disk" ]; then
        echo -e "${GREEN}✓ Disk (scsi0): Present${NC}"
        echo "  $disk"
    else
        echo -e "${RED}✗ Disk (scsi0): MISSING${NC}"
    fi

    # Summary for this VM
    echo ""
    if [ "$bios" = "ovmf" ] && [ -n "$efidisk" ]; then
        if echo "$efidisk" | grep -q "pre-enrolled-keys=1"; then
            echo -e "${YELLOW}→ RECOMMENDATION: Use installer-secureboot variant in talconfig.yaml${NC}"
        else
            echo -e "${GREEN}→ RECOMMENDATION: Use standard installer in talconfig.yaml${NC}"
        fi
    else
        echo -e "${RED}→ RECOMMENDATION: Fix BIOS and EFI disk configuration${NC}"
    fi
}

# Function to check Talos node connectivity
check_talos_node() {
    local ip=$1

    echo -e "\n${BLUE}--- Node $ip Connectivity ---${NC}"

    # Check ping
    if ping -c 1 -W 2 "$ip" &>/dev/null; then
        echo -e "${GREEN}✓ Ping: Reachable${NC}"
    else
        echo -e "${RED}✗ Ping: Not reachable${NC}"
        return
    fi

    # Check port 50000
    if timeout 3 bash -c "echo > /dev/tcp/$ip/50000" 2>/dev/null; then
        echo -e "${GREEN}✓ Port 50000: Open${NC}"
    else
        echo -e "${RED}✗ Port 50000: Closed/Filtered${NC}"
        return
    fi

    # Check talosctl (if available)
    if command -v talosctl &>/dev/null; then
        local version
        version=$(talosctl version --nodes "$ip" --insecure --short 2>&1 || echo "ERROR")

        if echo "$version" | grep -q "Server:"; then
            echo -e "${GREEN}✓ Talos API: Responding (maintenance mode)${NC}"
            echo "$version" | grep "Tag:" | sed 's/^/  /'
        elif echo "$version" | grep -q "certificate"; then
            echo -e "${YELLOW}⚠ Talos API: Configured (not in maintenance mode)${NC}"
            echo -e "  ${YELLOW}→ Node already has configuration applied${NC}"
        else
            echo -e "${RED}✗ Talos API: Not responding${NC}"
            echo -e "  $version" | head -1
        fi
    else
        echo -e "${YELLOW}⚠ talosctl not found (skipping API check)${NC}"
    fi
}

# Function to check talconfig.yaml
check_talconfig() {
    local talconfig="/home/devbox/repos/jlengelbrecht/prox-ops/talos/talconfig.yaml"

    echo -e "\n${BLUE}=== Checking talconfig.yaml ===${NC}\n"

    if [ ! -f "$talconfig" ]; then
        echo -e "${RED}✗ talconfig.yaml not found at: $talconfig${NC}"
        return
    fi

    echo -e "${GREEN}✓ talconfig.yaml found${NC}\n"

    # Check for installer type
    echo -e "${BLUE}Installer URLs in talconfig:${NC}"
    grep "talosImageURL:" "$talconfig" | sort -u | while read -r line; do
        echo "  $line"
        if echo "$line" | grep -q "installer-secureboot"; then
            echo -e "    ${GREEN}→ Secure Boot variant${NC}"
        elif echo "$line" | grep -q "installer/"; then
            echo -e "    ${YELLOW}→ Standard variant (NOT secure boot)${NC}"
        fi
    done

    echo ""

    # Check secureboot setting
    echo -e "${BLUE}Secure Boot settings in talconfig:${NC}"
    grep -A 1 "machineSpec:" "$talconfig" | grep "secureboot:" | sort -u | while read -r line; do
        echo "  $line"
        if echo "$line" | grep -q "true"; then
            echo -e "    ${GREEN}→ Secure boot enabled in config${NC}"
        else
            echo -e "    ${YELLOW}→ Secure boot disabled in config${NC}"
        fi
    done

    echo ""

    # Summary
    local has_secureboot_url
    has_secureboot_url=$(grep "talosImageURL:" "$talconfig" | grep -c "installer-secureboot" || echo 0)
    local has_standard_url
    has_standard_url=$(grep "talosImageURL:" "$talconfig" | grep -c "installer/" | grep -v "secureboot" || echo 0)
    local has_secureboot_flag
    has_secureboot_flag=$(grep "secureboot: true" "$talconfig" | wc -l || echo 0)

    echo -e "${BLUE}Summary:${NC}"
    echo -e "  Nodes with installer-secureboot URL: $has_secureboot_url"
    echo -e "  Nodes with standard installer URL: $has_standard_url"
    echo -e "  Nodes with secureboot: true flag: $has_secureboot_flag"

    if [ "$has_secureboot_url" -gt 0 ] && [ "$has_secureboot_flag" -gt 0 ]; then
        echo -e "\n${GREEN}✓ Configuration matches: Secure boot enabled${NC}"
    elif [ "$has_secureboot_url" -eq 0 ] && [ "$has_secureboot_flag" -eq 0 ]; then
        echo -e "\n${GREEN}✓ Configuration matches: Secure boot disabled${NC}"
    else
        echo -e "\n${RED}✗ Configuration mismatch!${NC}"
        if [ "$has_secureboot_flag" -gt 0 ]; then
            echo -e "${RED}  secureboot: true set but using standard installer${NC}"
            echo -e "${RED}  → Change to installer-secureboot URLs${NC}"
        else
            echo -e "${YELLOW}  Using secureboot installer but secureboot: false${NC}"
            echo -e "${YELLOW}  → Either change to standard installer OR set secureboot: true${NC}"
        fi
    fi
}

# Main execution
echo -e "Date: $(date)\n"

# Check talconfig
check_talconfig

# Get VM IDs
if [ "$ON_PROXMOX" = true ] || [ "$ON_PROXMOX" = "ssh" ]; then
    echo -e "\n${BLUE}=== Checking Proxmox VMs ===${NC}"

    VM_LIST=$(get_vm_ids)

    if [ -z "$VM_LIST" ]; then
        echo -e "${YELLOW}No VMs specified. Set VM_IDS environment variable.${NC}"
        echo "Example: export VM_IDS='100 101 102 103 104 105 106 107 108 109 110 111 112 113 114'"
    else
        for vmid in $VM_LIST; do
            check_vm_config "$vmid"
        done
    fi
fi

# Check node connectivity
echo -e "\n${BLUE}=== Checking Talos Node Connectivity ===${NC}"
for ip in "${NODE_IPS[@]}"; do
    check_talos_node "$ip"
done

# Final recommendations
echo -e "\n${BLUE}=== RECOMMENDATIONS ===${NC}\n"

echo -e "${YELLOW}Based on diagnostic results:${NC}\n"

echo -e "1. ${BLUE}If VMs show 'Secure Boot: ENABLED':${NC}"
echo -e "   → Use ${GREEN}installer-secureboot${NC} variant"
echo -e "   → Set ${GREEN}secureboot: true${NC} in talconfig.yaml"
echo -e "   → See: ${BLUE}FIX_CHECKLIST.md${NC} Option A\n"

echo -e "2. ${BLUE}If VMs show 'Secure Boot: DISABLED':${NC}"
echo -e "   → Use ${GREEN}standard installer${NC}"
echo -e "   → Set ${GREEN}secureboot: false${NC} in talconfig.yaml"
echo -e "   → See: ${BLUE}FIX_CHECKLIST.md${NC} Option B\n"

echo -e "3. ${BLUE}If EFI Disk is MISSING:${NC}"
echo -e "   → Add EFI disk first (see ${BLUE}TALOS_UEFI_FIX.md${NC} Appendix A)"
echo -e "   → Then proceed with Option A or B\n"

echo -e "4. ${BLUE}After installation completes:${NC}"
echo -e "   → Remove ISO: ${GREEN}qm set <vmid> --ide2 none${NC}"
echo -e "   → Set boot order: ${GREEN}qm set <vmid> --boot order=scsi0${NC}\n"

echo -e "${BLUE}Full documentation:${NC}"
echo -e "  ${GREEN}./TALOS_UEFI_FIX.md${NC} - Complete fix guide"
echo -e "  ${GREEN}./FIX_CHECKLIST.md${NC} - Quick action checklist"

echo -e "\n${BLUE}=== End of Diagnostic ===${NC}"
