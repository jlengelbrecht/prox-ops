# Talos VM Creation Guide for Proxmox

Complete guide for creating 15 Talos Linux VMs in Proxmox for your Kubernetes cluster.

## Table of Contents

1. [VM Specifications](#1-vm-specifications)
2. [Talos ISO Setup](#2-talos-iso-setup)
3. [Network Configuration](#3-network-configuration)
4. [VM Creation Methods](#4-vm-creation-methods)
5. [Automated Script (RECOMMENDED)](#5-automated-script-recommended)
6. [Manual Creation via Proxmox UI](#6-manual-creation-via-proxmox-ui)
7. [Post-Creation Checklist](#7-post-creation-checklist)
8. [Common Pitfalls](#8-common-pitfalls)

---

## 1. VM Specifications

### Controller Nodes (3 VMs)

**Recommended for Homelab:**
- **CPU**: 4 cores (host CPU type for best performance)
- **RAM**: 8 GB (8192 MB)
- **Disk**: 64 GB (thin provisioned on SSD/NVMe)
- **Network**: 1 NIC on bridge with 10.20.67.0/24
- **VM IDs**: 8001, 8002, 8003
- **IPs**: 10.20.67.1, 10.20.67.2, 10.20.67.3

**Minimum (Testing):**
- CPU: 2 cores
- RAM: 4 GB
- Disk: 32 GB

**Production-like:**
- CPU: 4-6 cores
- RAM: 16 GB
- Disk: 128 GB

### Worker Nodes (12 VMs)

**Recommended for Homelab:**
- **CPU**: 4 cores (host CPU type)
- **RAM**: 16 GB (16384 MB)
- **Disk**: 128 GB (thin provisioned on SSD/NVMe)
- **Network**: 3 NICs
  - NIC 1: Bridge for main network (10.20.67.0/24)
  - NIC 2: Bridge for DMZ VLAN 81
  - NIC 3: Bridge for IoT VLAN 62
- **VM IDs**: 8004-8015
- **IPs**: 10.20.67.4 through 10.20.67.15

**Minimum (Testing):**
- CPU: 2 cores
- RAM: 8 GB
- Disk: 64 GB
- Network: 1 NIC (multi-VLAN disabled)

**Production-like:**
- CPU: 8 cores
- RAM: 32 GB
- Disk: 256 GB

### Disk Configuration

**Bus Type: VirtIO SCSI**
- Best performance for Proxmox VMs
- Use SCSI controller (VirtIO SCSI single)
- Enable discard/TRIM for thin provisioning
- Enable SSD emulation if on SSD storage

**Storage Backend:**
- Local-LVM (thin provisioned)
- Ceph RBD (if using Proxmox Ceph cluster)
- ZFS (if available)

**Do NOT use:**
- IDE (legacy, poor performance)
- SATA (slower than VirtIO)

---

## 2. Talos ISO Setup

### Download Talos ISO

**Option 1: Factory Image (RECOMMENDED for extensions)**

Visit: https://factory.talos.dev/

1. Select Talos version: **1.11.3**
2. Add system extensions:
   - **qemu-guest-agent** (REQUIRED for Proxmox)
   - Optional: iscsi-tools (if using iSCSI storage)
3. Generate schematic
4. Note the Schematic ID (you'll need this for nodes.yaml)
5. Download the ISO:

```bash
# Example with schematic ID
wget https://factory.talos.dev/image/<SCHEMATIC_ID>/v1.11.3/metal-amd64.iso -O talos-1.11.3.iso
```

**Option 2: Official Release Image (basic)**

```bash
wget https://github.com/siderolabs/talos/releases/download/v1.11.3/metal-amd64.iso -O talos-1.11.3.iso
```

**Note**: Factory images include extensions needed for Proxmox integration. Use Option 1.

### Upload ISO to Proxmox

**Method 1: Web UI**

1. Navigate to Proxmox node > Datacenter > Storage > local
2. Click on "ISO Images"
3. Click "Upload"
4. Select your downloaded `talos-1.11.3.iso`
5. Wait for upload to complete

**Method 2: SCP (faster for large files)**

```bash
# From your workstation
scp talos-1.11.3.iso root@<proxmox-host>:/var/lib/vz/template/iso/
```

**Method 3: Direct Download on Proxmox**

```bash
# SSH to Proxmox host
ssh root@<proxmox-host>

# Download directly
cd /var/lib/vz/template/iso/
wget https://factory.talos.dev/image/<SCHEMATIC_ID>/v1.11.3/metal-amd64.iso -O talos-1.11.3.iso
```

### Verify ISO

```bash
# On Proxmox host
ls -lh /var/lib/vz/template/iso/talos-1.11.3.iso
```

---

## 3. Network Configuration

### Bridge Setup

Verify your Proxmox bridge configuration:

```bash
# On Proxmox host
ip link show vmbr0
```

Expected output:
```
2: vmbr0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT
```

### Main Network Bridge (10.20.67.0/24)

**Bridge**: vmbr0 (or your configured bridge)
- No VLAN tag (native/untagged)
- Connected to subnet: 10.20.67.0/24
- Gateway: 10.20.67.1 (or your network gateway)

### DMZ VLAN 81 (Workers Only)

**Option A: VLAN-aware bridge**
- Bridge: vmbr0
- VLAN tag: 81
- Proxmox handles VLAN tagging

**Option B: Dedicated bridge**
- Bridge: vmbr1 (dedicated to VLAN 81)
- No VLAN tag needed
- Switch port configured with VLAN 81

### IoT VLAN 62 (Workers Only)

**Option A: VLAN-aware bridge**
- Bridge: vmbr0
- VLAN tag: 62

**Option B: Dedicated bridge**
- Bridge: vmbr2 (dedicated to VLAN 62)
- No VLAN tag needed

### IP Assignment Strategy

**RECOMMENDED: Static IPs via Talos Configuration**

Talos nodes will be configured with static IPs in the node configuration. No DHCP required.

**Alternative: DHCP Reservations**

If using DHCP:
1. Configure DHCP reservations based on MAC addresses
2. Reserve IPs 10.20.67.1-15 for the 15 VMs
3. Talos will get IP via DHCP initially

---

## 4. VM Creation Methods

### Comparison Table

| Method | Speed | Reliability | Repeatability | Recommended For |
|--------|-------|-------------|---------------|-----------------|
| **Automated Script** | Fast (5 min) | High | Perfect | Homelab |
| Manual UI | Slow (60 min) | Medium | Low | Learning/Testing |
| Terraform | Medium (15 min) | High | Perfect | Production |
| Clone from Template | Fast (10 min) | High | Good | Quick deployment |

### RECOMMENDATION: Automated Script

**Why?**
- Creates all 15 VMs in ~5 minutes
- Consistent configuration
- Easy to repeat if needed
- No manual errors
- Can be version controlled

**See Section 5 for complete script.**

---

## 5. Automated Script (RECOMMENDED)

### Prerequisites

SSH access to Proxmox host as root.

### Create VM Creation Script

Save this script on your Proxmox host as `/root/create-talos-vms.sh`:

```bash
#!/usr/bin/env bash

set -euo pipefail

#############################################
# Talos Kubernetes Cluster VM Creation Script
# Creates 15 VMs (3 controllers + 12 workers)
#############################################

# Configuration
STORAGE="local-lvm"              # Storage pool for disks
ISO_STORAGE="local"              # Storage for ISO images
ISO_NAME="talos-1.11.3.iso"      # Talos ISO filename
BRIDGE_MAIN="vmbr0"              # Main network bridge
BRIDGE_DMZ="vmbr0"               # DMZ bridge (or vmbr1 if dedicated)
BRIDGE_IOT="vmbr0"               # IoT bridge (or vmbr2 if dedicated)
VLAN_DMZ="81"                    # DMZ VLAN tag (leave empty if bridge handles it)
VLAN_IOT="62"                    # IoT VLAN tag (leave empty if bridge handles it)

# Controller specifications
CTRL_VMID_START=8001
CTRL_COUNT=3
CTRL_CORES=4
CTRL_MEMORY=8192                 # 8 GB
CTRL_DISK_SIZE="64G"
CTRL_IP_START="10.20.67.1"

# Worker specifications
WORK_VMID_START=8004
WORK_COUNT=12
WORK_CORES=4
WORK_MEMORY=16384                # 16 GB
WORK_DISK_SIZE="128G"
WORK_IP_START="10.20.67.4"

# CPU type (host for best performance)
CPU_TYPE="host"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

#############################################
# Functions
#############################################

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_prerequisites() {
    log_info "Checking prerequisites..."

    # Check if ISO exists
    if ! pvesm list "$ISO_STORAGE" | grep -q "$ISO_NAME"; then
        log_error "ISO '$ISO_NAME' not found in storage '$ISO_STORAGE'"
        log_error "Please upload Talos ISO first"
        exit 1
    fi

    # Check if storage exists
    if ! pvesm status | grep -q "^$STORAGE"; then
        log_error "Storage '$STORAGE' not found"
        exit 1
    fi

    # Check if bridges exist
    if ! ip link show "$BRIDGE_MAIN" &>/dev/null; then
        log_error "Bridge '$BRIDGE_MAIN' not found"
        exit 1
    fi

    log_info "Prerequisites check passed"
}

create_controller() {
    local vmid=$1
    local name=$2
    local ip=$3

    log_info "Creating controller: $name (VMID: $vmid, IP: $ip)"

    # Create VM
    qm create "$vmid" \
        --name "$name" \
        --memory "$CTRL_MEMORY" \
        --cores "$CTRL_CORES" \
        --cpu "$CPU_TYPE" \
        --sockets 1 \
        --net0 "virtio,bridge=$BRIDGE_MAIN" \
        --scsihw virtio-scsi-single \
        --scsi0 "$STORAGE:$CTRL_DISK_SIZE,discard=on,ssd=1" \
        --ide2 "$ISO_STORAGE:iso/$ISO_NAME,media=cdrom" \
        --boot order=scsi0 \
        --onboot 1 \
        --ostype l26 \
        --agent enabled=1 \
        --description "Talos Kubernetes Controller Node\nIP: $ip\nRole: Control Plane" \
        --tags "talos,kubernetes,controller" \
        2>&1 | grep -v "Warning: \$/" || true

    log_info "Controller $name created successfully"
}

create_worker() {
    local vmid=$1
    local name=$2
    local ip=$3

    log_info "Creating worker: $name (VMID: $vmid, IP: $ip)"

    # Create VM base configuration
    qm create "$vmid" \
        --name "$name" \
        --memory "$WORK_MEMORY" \
        --cores "$WORK_CORES" \
        --cpu "$CPU_TYPE" \
        --sockets 1 \
        --net0 "virtio,bridge=$BRIDGE_MAIN" \
        --scsihw virtio-scsi-single \
        --scsi0 "$STORAGE:$WORK_DISK_SIZE,discard=on,ssd=1" \
        --ide2 "$ISO_STORAGE:iso/$ISO_NAME,media=cdrom" \
        --boot order=scsi0 \
        --onboot 1 \
        --ostype l26 \
        --agent enabled=1 \
        --description "Talos Kubernetes Worker Node\nIP: $ip\nRole: Worker" \
        --tags "talos,kubernetes,worker" \
        2>&1 | grep -v "Warning: \$/" || true

    # Add DMZ VLAN network interface
    if [ -n "$VLAN_DMZ" ]; then
        qm set "$vmid" --net1 "virtio,bridge=$BRIDGE_DMZ,tag=$VLAN_DMZ" >/dev/null
    else
        qm set "$vmid" --net1 "virtio,bridge=$BRIDGE_DMZ" >/dev/null
    fi

    # Add IoT VLAN network interface
    if [ -n "$VLAN_IOT" ]; then
        qm set "$vmid" --net2 "virtio,bridge=$BRIDGE_IOT,tag=$VLAN_IOT" >/dev/null
    else
        qm set "$vmid" --net2 "virtio,bridge=$BRIDGE_IOT" >/dev/null
    fi

    log_info "Worker $name created successfully (3 NICs: Main, DMZ VLAN $VLAN_DMZ, IoT VLAN $VLAN_IOT)"
}

ip_increment() {
    local ip=$1
    local increment=$2

    # Split IP into octets
    IFS='.' read -r -a octets <<< "$ip"

    # Add increment to last octet
    octets[3]=$((octets[3] + increment))

    # Handle overflow (simple case, assumes /24)
    if [ "${octets[3]}" -gt 254 ]; then
        log_error "IP overflow detected"
        exit 1
    fi

    echo "${octets[0]}.${octets[1]}.${octets[2]}.${octets[3]}"
}

create_all_vms() {
    log_info "Starting VM creation process..."
    echo ""

    # Create controllers
    log_info "Creating $CTRL_COUNT controller nodes..."
    for i in $(seq 0 $((CTRL_COUNT - 1))); do
        vmid=$((CTRL_VMID_START + i))
        name="k8s-ctrl-$((i + 1))"
        ip=$(ip_increment "$CTRL_IP_START" "$i")

        # Check if VM already exists
        if qm status "$vmid" &>/dev/null; then
            log_warn "VM $vmid already exists, skipping..."
            continue
        fi

        create_controller "$vmid" "$name" "$ip"
    done

    echo ""

    # Create workers
    log_info "Creating $WORK_COUNT worker nodes..."
    for i in $(seq 0 $((WORK_COUNT - 1))); do
        vmid=$((WORK_VMID_START + i))
        name="k8s-work-$((i + 1))"
        ip=$(ip_increment "$WORK_IP_START" "$i")

        # Check if VM already exists
        if qm status "$vmid" &>/dev/null; then
            log_warn "VM $vmid already exists, skipping..."
            continue
        fi

        create_worker "$vmid" "$name" "$ip"
    done
}

start_all_vms() {
    log_info "Starting all VMs..."

    for vmid in $(seq $CTRL_VMID_START $((CTRL_VMID_START + CTRL_COUNT - 1))); do
        if qm status "$vmid" 2>/dev/null | grep -q "stopped"; then
            log_info "Starting VM $vmid..."
            qm start "$vmid" >/dev/null 2>&1
        fi
    done

    for vmid in $(seq $WORK_VMID_START $((WORK_VMID_START + WORK_COUNT - 1))); do
        if qm status "$vmid" 2>/dev/null | grep -q "stopped"; then
            log_info "Starting VM $vmid..."
            qm start "$vmid" >/dev/null 2>&1
        fi
    done

    log_info "All VMs started"
}

print_summary() {
    echo ""
    echo "======================================"
    echo "VM Creation Summary"
    echo "======================================"
    echo ""
    echo "Controller Nodes (VMs $CTRL_VMID_START-$((CTRL_VMID_START + CTRL_COUNT - 1))):"
    for i in $(seq 0 $((CTRL_COUNT - 1))); do
        vmid=$((CTRL_VMID_START + i))
        name="k8s-ctrl-$((i + 1))"
        ip=$(ip_increment "$CTRL_IP_START" "$i")
        echo "  - $name (VMID: $vmid) -> $ip"
    done

    echo ""
    echo "Worker Nodes (VMs $WORK_VMID_START-$((WORK_VMID_START + WORK_COUNT - 1))):"
    for i in $(seq 0 $((WORK_COUNT - 1))); do
        vmid=$((WORK_VMID_START + i))
        name="k8s-work-$((i + 1))"
        ip=$(ip_increment "$WORK_IP_START" "$i")
        echo "  - $name (VMID: $vmid) -> $ip (3 NICs)"
    done

    echo ""
    echo "Next Steps:"
    echo "1. Wait for VMs to boot into Talos maintenance mode (~1 minute)"
    echo "2. Verify nodes are accessible:"
    echo "   nmap -Pn -n -p 50000 10.20.67.0/24"
    echo "3. Gather node information (disks and MAC addresses):"
    echo "   for ip in {1..15}; do talosctl disks --nodes 10.20.67.\$ip --insecure; done"
    echo "   for ip in {1..15}; do talosctl get links --nodes 10.20.67.\$ip --insecure; done"
    echo "4. Update nodes.yaml with discovered information"
    echo "5. Continue with cluster configuration"
    echo ""
}

#############################################
# Main
#############################################

main() {
    echo ""
    echo "======================================"
    echo "Talos Kubernetes Cluster VM Creator"
    echo "======================================"
    echo ""
    echo "This script will create:"
    echo "  - $CTRL_COUNT controller nodes (VMID $CTRL_VMID_START+, ${CTRL_CORES}C/${CTRL_MEMORY}MB, ${CTRL_DISK_SIZE} disk)"
    echo "  - $WORK_COUNT worker nodes (VMID $WORK_VMID_START+, ${WORK_CORES}C/${WORK_MEMORY}MB, ${WORK_DISK_SIZE} disk, 3 NICs)"
    echo ""

    read -p "Continue? (yes/no): " -r
    echo
    if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
        log_info "Aborted by user"
        exit 0
    fi

    check_prerequisites
    create_all_vms
    start_all_vms
    print_summary
}

main "$@"
```

### Make Script Executable

```bash
# On Proxmox host
chmod +x /root/create-talos-vms.sh
```

### Customize Script (IMPORTANT)

Edit the script and adjust these variables:

```bash
STORAGE="local-lvm"              # Your storage pool name
ISO_NAME="talos-1.11.3.iso"      # Your ISO filename
BRIDGE_MAIN="vmbr0"              # Your main bridge
BRIDGE_DMZ="vmbr0"               # DMZ bridge (or vmbr1)
BRIDGE_IOT="vmbr0"               # IoT bridge (or vmbr2)
VLAN_DMZ="81"                    # DMZ VLAN tag
VLAN_IOT="62"                    # IoT VLAN tag
```

**Check your storage name:**

```bash
pvesm status
```

### Run the Script

```bash
# On Proxmox host
/root/create-talos-vms.sh
```

**Expected output:**

```
======================================
Talos Kubernetes Cluster VM Creator
======================================

This script will create:
  - 3 controller nodes (VMID 8001+, 4C/8192MB, 64G disk)
  - 12 worker nodes (VMID 8004+, 4C/16384MB, 128G disk, 3 NICs)

Continue? (yes/no): yes

[INFO] Checking prerequisites...
[INFO] Prerequisites check passed
[INFO] Starting VM creation process...

[INFO] Creating 3 controller nodes...
[INFO] Creating controller: k8s-ctrl-1 (VMID: 8001, IP: 10.20.67.1)
[INFO] Controller k8s-ctrl-1 created successfully
[INFO] Creating controller: k8s-ctrl-2 (VMID: 8002, IP: 10.20.67.2)
...
```

**Duration: ~5 minutes**

---

## 6. Manual Creation via Proxmox UI

Only use this method if you want to learn the process or create VMs one at a time.

### Create Controller Node (Example: k8s-ctrl-1)

1. **Navigate to Proxmox UI**
   - https://your-proxmox-host:8006

2. **Click "Create VM" (top right)**

3. **General Tab**
   - Node: (your Proxmox node)
   - VM ID: 8001
   - Name: k8s-ctrl-1
   - Click "Next"

4. **OS Tab**
   - ISO image: talos-1.11.3.iso
   - Type: Linux
   - Version: 6.x - 2.6 Kernel
   - Click "Next"

5. **System Tab**
   - Machine: q35
   - BIOS: Default (SeaBIOS)
   - SCSI Controller: VirtIO SCSI single
   - Qemu Agent: CHECKED
   - Click "Next"

6. **Disks Tab**
   - Bus/Device: SCSI 0
   - Storage: local-lvm (or your storage)
   - Disk size: 64 GB
   - Cache: Default (No cache)
   - Discard: CHECKED
   - SSD emulation: CHECKED (if on SSD)
   - Click "Next"

7. **CPU Tab**
   - Sockets: 1
   - Cores: 4
   - Type: host
   - Click "Next"

8. **Memory Tab**
   - Memory: 8192 MB
   - Minimum memory: (leave empty)
   - Click "Next"

9. **Network Tab**
   - Bridge: vmbr0
   - Model: VirtIO (paravirtualized)
   - VLAN Tag: (leave empty for main network)
   - Firewall: (optional)
   - Click "Next"

10. **Confirm**
    - Start after created: UNCHECKED (we'll start all at once)
    - Click "Finish"

### Repeat for All Controllers

Create k8s-ctrl-2 (VMID 8002) and k8s-ctrl-3 (VMID 8003) with same settings.

### Create Worker Node (Example: k8s-work-1)

Follow same steps but with:
- VM ID: 8004
- Name: k8s-work-1
- Cores: 4
- Memory: 16384 MB (16 GB)
- Disk: 128 GB

**After creation, add additional NICs:**

1. Select VM 8004
2. Hardware > Add > Network Device
   - Bridge: vmbr0
   - VLAN Tag: 81 (DMZ)
   - Model: VirtIO
   - Click "Add"

3. Hardware > Add > Network Device
   - Bridge: vmbr0
   - VLAN Tag: 62 (IoT)
   - Model: VirtIO
   - Click "Add"

### Repeat for All Workers

Create k8s-work-2 through k8s-work-12 (VMIDs 8005-8015).

**Duration: ~60 minutes (manual)**

---

## 7. Post-Creation Checklist

### Verify VMs Created

**Check VM list:**

```bash
# On Proxmox host
qm list | grep k8s
```

Expected output:
```
   8001 k8s-ctrl-1         stopped    8192         0.00       64.00
   8002 k8s-ctrl-2         stopped    8192         0.00       64.00
   8003 k8s-ctrl-3         stopped    8192         0.00       64.00
   8004 k8s-work-1         stopped   16384         0.00      128.00
   ...
   8015 k8s-work-12        stopped   16384         0.00      128.00
```

### Start All VMs

```bash
# Start all controller VMs
for vmid in 8001 8002 8003; do qm start $vmid; done

# Start all worker VMs
for vmid in {8004..8015}; do qm start $vmid; done

# Or start all at once
for vmid in {8001..8003} {8004..8015}; do qm start $vmid; done
```

### Verify Talos Boot

**Wait ~1 minute for VMs to boot.**

Check console of a VM:
1. Proxmox UI > Select VM
2. Console tab
3. You should see Talos maintenance mode with API endpoint info

Expected console output:
```
Talos 1.11.3
Node: 10.20.67.X (maintenance mode)
API: https://10.20.67.X:50000
```

### Verify Network Connectivity

**From your workstation (where you have nmap and talosctl):**

```bash
# Scan for Talos API (port 50000)
nmap -Pn -n -p 50000 10.20.67.0/24

# Expected: 15 nodes with port 50000 open
# Discovered open port 50000/tcp on 10.20.67.1
# Discovered open port 50000/tcp on 10.20.67.2
# ... (should see all 15 IPs)
```

**If nodes are not responding:**
- Check VM is running in Proxmox UI
- Check VM console for errors
- Verify network bridge configuration
- Check firewall rules (Proxmox firewall might block)

### Gather Node Information

**Get disk information for each node:**

```bash
# Controllers
talosctl disks --nodes 10.20.67.1 --insecure
talosctl disks --nodes 10.20.67.2 --insecure
talosctl disks --nodes 10.20.67.3 --insecure

# Workers
for ip in {4..15}; do
    echo "=== Node 10.20.67.$ip ==="
    talosctl disks --nodes 10.20.67.$ip --insecure
done
```

Note the disk device path (e.g., `/dev/sda`) or serial number for each node.

**Get MAC addresses:**

```bash
# Controllers
talosctl get links --nodes 10.20.67.1 --insecure
talosctl get links --nodes 10.20.67.2 --insecure
talosctl get links --nodes 10.20.67.3 --insecure

# Workers
for ip in {4..15}; do
    echo "=== Node 10.20.67.$ip ==="
    talosctl get links --nodes 10.20.67.$ip --insecure
done
```

Note the MAC address of eth0 (primary interface) for each node.

**Save this information - you'll need it for nodes.yaml.**

### Verify Worker Multi-NIC Setup

```bash
# Check a worker node has 3 interfaces
talosctl get links --nodes 10.20.67.4 --insecure

# Expected output should show:
# - lo (loopback)
# - eth0 (primary network)
# - eth1 (DMZ VLAN)
# - eth2 (IoT VLAN)
```

---

## 8. Common Pitfalls

### Pitfall 1: Running Out of Resources

**Problem**: Creating 15 VMs with generous specs exhausts Proxmox host resources.

**Solution**:
- Check available resources first:
  ```bash
  free -h  # Check RAM
  df -h    # Check disk space
  ```
- Calculate total requirements:
  - Controllers: 3 x 8 GB = 24 GB RAM
  - Workers: 12 x 16 GB = 192 GB RAM
  - **Total: 216 GB RAM minimum**
- If insufficient, reduce per-VM allocation:
  - Controllers: 4 GB (minimum)
  - Workers: 8 GB (minimum)

### Pitfall 2: Incorrect Storage Pool

**Problem**: Script fails with "storage not found" error.

**Solution**:
```bash
# List available storage pools
pvesm status

# Common names: local-lvm, local-zfs, ceph-pool
# Update STORAGE variable in script
```

### Pitfall 3: Wrong Network Bridge

**Problem**: VMs have no network connectivity after boot.

**Solution**:
```bash
# List bridges
ip link show | grep vmbr

# Common: vmbr0 (default)
# Update BRIDGE_MAIN in script
```

### Pitfall 4: ISO Not Found

**Problem**: VM creation fails, can't find Talos ISO.

**Solution**:
```bash
# List ISO images
pvesm list local | grep iso

# Verify ISO filename matches ISO_NAME in script
```

### Pitfall 5: VM ID Conflicts

**Problem**: "VM ID already in use" error.

**Solution**:
```bash
# Check existing VMs
qm list

# Choose different VMID range (e.g., 9001-9015)
# Update CTRL_VMID_START and WORK_VMID_START in script
```

### Pitfall 6: VLANs Not Working

**Problem**: Worker nodes don't get additional network interfaces or VLAN traffic doesn't work.

**Root causes**:
- VLAN not configured on switch
- Proxmox bridge not VLAN-aware
- Wrong VLAN tag

**Solution**:

**Check if bridge is VLAN-aware:**
```bash
# On Proxmox host
cat /etc/network/interfaces

# Look for: bridge-vlan-aware yes
```

**If not VLAN-aware, edit /etc/network/interfaces:**
```
auto vmbr0
iface vmbr0 inet static
    address 192.168.X.X/24
    gateway 192.168.X.1
    bridge-ports enp0s0
    bridge-stp off
    bridge-fd 0
    bridge-vlan-aware yes       # Add this line
    bridge-vids 2-4094          # Add this line
```

Then restart networking:
```bash
systemctl restart networking
```

**Alternative: Use dedicated bridges:**
- Create vmbr1 for VLAN 81
- Create vmbr2 for VLAN 62
- Update script to use vmbr1 and vmbr2
- Set VLAN_DMZ="" and VLAN_IOT="" (no tagging needed)

### Pitfall 7: Thin Provisioning Not Working

**Problem**: VMs consume full disk size immediately.

**Solution**:

Ensure storage supports thin provisioning:
```bash
# LVM-thin: supports thin provisioning
# Directory: no thin provisioning
# Ceph RBD: supports thin provisioning

# If using local-lvm, verify it's LVM-thin:
lvs
```

### Pitfall 8: VMs Don't Boot

**Problem**: VMs stuck in BIOS or boot loop.

**Causes**:
- Boot order incorrect
- ISO not attached
- Disk not created

**Solution**:
```bash
# Check VM configuration
qm config 8001

# Should show:
# boot: order=scsi0
# ide2: local:iso/talos-1.11.3.iso,media=cdrom
# scsi0: local-lvm:vm-8001-disk-0,size=64G

# Fix boot order if needed:
qm set 8001 --boot order=scsi0
```

### Pitfall 9: Proxmox Firewall Blocking

**Problem**: nmap shows no Talos API (port 50000) accessible.

**Solution**:
```bash
# Check firewall status
pve-firewall status

# If enabled, add rule to allow port 50000
# Datacenter > Firewall > Add rule:
# Direction: IN
# Action: ACCEPT
# Protocol: tcp
# Destination port: 50000
# Source: (your workstation IP)
```

### Pitfall 10: Not Enough IP Addresses

**Problem**: Network subnet too small for 15 VMs + services.

**Solution**:

10.20.67.0/24 provides 254 usable IPs:
- VMs: 15 IPs (10.20.67.1-15)
- API VIP: 1 IP (10.20.67.10)
- Services: ~80 IPs (10.20.67.20-99)
- Remaining: ~158 IPs

This is sufficient. No action needed.

---

## Next Steps

After VMs are created and running:

1. **Verify all nodes are accessible:**
   ```bash
   nmap -Pn -n -p 50000 10.20.67.0/24
   # Should show 15 nodes with port 50000 open
   ```

2. **Gather node information:**
   - Disk paths or serial numbers (talosctl disks)
   - MAC addresses (talosctl get links)

3. **Create Talos schematic at https://factory.talos.dev/**
   - Select Talos 1.11.3
   - Add qemu-guest-agent extension
   - Note the schematic ID

4. **Update cluster configuration:**
   - Edit `/home/devbox/repos/jlengelbrecht/prox-ops/cluster.yaml`
   - Edit `/home/devbox/repos/jlengelbrecht/prox-ops/nodes.yaml`
   - Fill in all discovered information

5. **Continue with cluster bootstrap:**
   - Follow IMPLEMENTATION_PLAN.md
   - Run `task configure`
   - Run `task bootstrap:talos`

---

## Summary

**Recommended approach for homelab:**

1. Download Talos ISO from factory.talos.dev (with qemu-guest-agent)
2. Upload ISO to Proxmox
3. Run automated script to create all 15 VMs
4. Start VMs and wait for Talos to boot
5. Verify network connectivity (nmap)
6. Gather node information (talosctl)
7. Continue with cluster configuration

**Time estimate:**
- ISO download/upload: 5 minutes
- Run script: 5 minutes
- VMs boot: 2 minutes
- Gather info: 10 minutes
- **Total: ~22 minutes**

Much faster than 60+ minutes of manual clicking!

---

## Troubleshooting Resources

**Proxmox Documentation:**
- https://pve.proxmox.com/wiki/Qm

**Talos Documentation:**
- https://www.talos.dev/latest/talos-guides/install/virtualized-platforms/proxmox/

**Support Channels:**
- Proxmox Forum: https://forum.proxmox.com/
- Talos Slack: https://slack.dev.talos-systems.io/

---

**Good luck with your VM creation!**
