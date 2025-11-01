# Quick VM Setup Guide

Ultra-fast guide to get 15 Talos VMs running in under 30 minutes.

## Prerequisites

- SSH access to Proxmox host
- Talos 1.11.3 ISO downloaded
- Network: 10.20.67.0/24 available
- Proxmox has 216 GB RAM (or 108 GB minimum)
- Proxmox has 1.7 TB storage (or 850 GB minimum)

## Step 1: Download and Upload Talos ISO (5 minutes)

### On your workstation:

```bash
# Create Talos schematic at https://factory.talos.dev/
# - Select Talos 1.11.3
# - Add extension: qemu-guest-agent
# - Copy the schematic ID
# - Download the ISO

# Example (replace SCHEMATIC_ID):
SCHEMATIC_ID="your-schematic-id-here"
wget https://factory.talos.dev/image/${SCHEMATIC_ID}/v1.11.3/metal-amd64.iso -O talos-1.11.3.iso
```

### Upload to Proxmox:

**Method A: SCP (faster)**
```bash
scp talos-1.11.3.iso root@YOUR-PROXMOX-HOST:/var/lib/vz/template/iso/
```

**Method B: Direct download on Proxmox**
```bash
# SSH to Proxmox
ssh root@YOUR-PROXMOX-HOST

cd /var/lib/vz/template/iso/
SCHEMATIC_ID="your-schematic-id-here"
wget https://factory.talos.dev/image/${SCHEMATIC_ID}/v1.11.3/metal-amd64.iso -O talos-1.11.3.iso
```

## Step 2: Create VMs with Script (5 minutes)

### On Proxmox host:

```bash
# Download the creation script
wget https://raw.githubusercontent.com/jlengelbrecht/prox-ops/main/scripts/create-talos-vms.sh -O /root/create-talos-vms.sh

# Or create it manually (see VM_CREATION_GUIDE.md)

# Make executable
chmod +x /root/create-talos-vms.sh

# Edit configuration (IMPORTANT!)
nano /root/create-talos-vms.sh

# Update these variables:
# - STORAGE="local-lvm"        # Check with: pvesm status
# - ISO_NAME="talos-1.11.3.iso"
# - BRIDGE_MAIN="vmbr0"        # Check with: ip link show
# - VLAN_DMZ="81"
# - VLAN_IOT="62"

# Run the script
/root/create-talos-vms.sh
```

**Expected output:**
```
Creating controller: k8s-ctrl-1 (VMID: 8001, IP: 10.20.67.1)
[INFO] Controller k8s-ctrl-1 created successfully
...
[INFO] All VMs started
```

## Step 3: Verify VMs Booted (2 minutes)

### Wait for VMs to boot:

```bash
# Wait ~60 seconds
sleep 60

# Check VM status
qm list | grep k8s
```

### From your workstation, check Talos API:

```bash
# Install nmap if needed
# CachyOS: sudo pacman -S nmap

# Scan for Talos API
nmap -Pn -n -p 50000 10.20.67.0/24

# Expected: 15 nodes with port 50000 open
# Discovered open port 50000/tcp on 10.20.67.1
# Discovered open port 50000/tcp on 10.20.67.2
# ...
# Discovered open port 50000/tcp on 10.20.67.15
```

If you don't see all 15 nodes:
- Wait another minute (VMs still booting)
- Check Proxmox firewall settings
- Check VM consoles for errors

## Step 4: Gather Node Information (10 minutes)

### From your workstation (where talosctl is installed):

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/

# Collect disk information
for ip in {1..15}; do
    echo "=== Node 10.20.67.$ip ==="
    talosctl disks --nodes 10.20.67.$ip --insecure 2>/dev/null | grep -E "DEV|/dev"
    echo ""
done | tee node-disks.txt

# Collect MAC addresses
for ip in {1..15}; do
    echo "=== Node 10.20.67.$ip ==="
    talosctl get links --nodes 10.20.67.$ip --insecure 2>/dev/null | grep eth0 | awk '{print $4}'
    echo ""
done | tee node-macs.txt

# View results
cat node-disks.txt
cat node-macs.txt
```

**Save this information - you'll need it next!**

## Step 5: Configure Cluster Files (10 minutes)

### Edit cluster.yaml:

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/
nano cluster.yaml
```

**Fill in these REQUIRED fields:**

```yaml
---
node_cidr: "10.20.67.0/24"
cluster_api_addr: "10.20.67.10"
cluster_dns_gateway_addr: "10.20.67.20"
cluster_gateway_addr: "10.20.67.21"
repository_name: "jlengelbrecht/prox-ops"
cloudflare_domain: "your-domain.com"              # CHANGE THIS
cloudflare_token: "your-cloudflare-api-token"     # CHANGE THIS
cloudflare_gateway_addr: "10.20.67.22"
```

### Edit nodes.yaml:

```bash
nano nodes.yaml
```

**Fill in all 15 nodes using information from Step 4:**

```yaml
---
nodes:
  - name: "k8s-ctrl-1"
    address: "10.20.67.1"
    controller: true
    disk: "/dev/sda"                              # From node-disks.txt
    mac_addr: "AA:BB:CC:DD:EE:01"                 # From node-macs.txt
    schematic_id: "your-schematic-id-here"        # From factory.talos.dev
    mtu: 1500

  - name: "k8s-ctrl-2"
    address: "10.20.67.2"
    controller: true
    disk: "/dev/sda"
    mac_addr: "AA:BB:CC:DD:EE:02"
    schematic_id: "your-schematic-id-here"
    mtu: 1500

  - name: "k8s-ctrl-3"
    address: "10.20.67.3"
    controller: true
    disk: "/dev/sda"
    mac_addr: "AA:BB:CC:DD:EE:03"
    schematic_id: "your-schematic-id-here"
    mtu: 1500

  # Workers (repeat for all 12)
  - name: "k8s-work-1"
    address: "10.20.67.4"
    controller: false
    disk: "/dev/sda"
    mac_addr: "AA:BB:CC:DD:EE:04"
    schematic_id: "your-schematic-id-here"
    mtu: 1500

  # ... continue for k8s-work-2 through k8s-work-12 (10.20.67.5-15)
```

**Tip:** Use copy-paste and search-replace to speed this up.

## Step 6: Validate Configuration

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/

# Validate and render configurations
task configure
```

**If errors occur:**
- Check cluster.yaml for typos
- Check nodes.yaml for missing fields
- Verify all MAC addresses are unique
- Verify all IPs are in the 10.20.67.1-15 range

**If successful, you'll see:**
```
[INFO] Validating cluster configuration...
[INFO] Validating nodes configuration...
[INFO] Rendering templates...
[INFO] Configuration complete!
```

## Step 7: Next Steps

You're now ready to bootstrap your cluster!

**Follow the IMPLEMENTATION_PLAN.md starting at Section 9 (Cloudflare Setup):**

1. Create Cloudflare tunnel
2. Commit your configuration to Git
3. Bootstrap Talos cluster (`task bootstrap:talos`)
4. Bootstrap applications (`task bootstrap:apps`)

---

## Troubleshooting Quick Fixes

### Problem: nmap shows no nodes with port 50000 open

**Check:**
```bash
# On Proxmox, verify VMs are running
qm list | grep k8s

# Check a VM's console
# Proxmox UI > VM 8001 > Console
# Should show: "Talos 1.11.3" and "API: https://10.20.67.1:50000"
```

**Fix:**
- Wait longer (VMs can take 2-3 minutes on slow storage)
- Check Proxmox firewall: allow port 50000
- Restart VMs: `qm reboot 8001`

### Problem: talosctl times out when gathering info

**Check:**
```bash
# Test basic connectivity
ping 10.20.67.1

# Test Talos API specifically
curl -k https://10.20.67.1:50000
# Should see SSL error (expected) but confirms API is up
```

**Fix:**
- Verify network bridge configuration
- Check Proxmox firewall
- Verify workstation can reach 10.20.67.0/24 network

### Problem: Script fails with "storage not found"

**Check available storage:**
```bash
pvesm status
```

**Update script:**
```bash
# Edit the STORAGE variable to match your storage name
nano /root/create-talos-vms.sh
# Change: STORAGE="local-lvm"  # to your storage name
```

### Problem: Not enough resources

**Check Proxmox resources:**
```bash
free -h        # Check RAM
df -h          # Check disk space
nproc          # Check CPU cores
```

**Reduce VM specs in script:**
```bash
nano /root/create-talos-vms.sh

# For testing, reduce to:
CTRL_CORES=2
CTRL_MEMORY=4096          # 4 GB
CTRL_DISK_SIZE="32G"

WORK_CORES=2
WORK_MEMORY=8192          # 8 GB
WORK_DISK_SIZE="64G"
```

Then delete existing VMs and recreate:
```bash
for vmid in {8001..8003} {8004..8015}; do qm destroy $vmid; done
/root/create-talos-vms.sh
```

---

## Time Summary

With everything ready:
- ISO upload: 5 minutes
- Script execution: 5 minutes
- VMs boot: 2 minutes
- Gather info: 10 minutes
- Configure files: 10 minutes
- **Total: ~32 minutes**

First time setup (including learning):
- Read documentation: 15 minutes
- ISO creation/download: 10 minutes
- Script customization: 10 minutes
- VM creation: 5 minutes
- Troubleshooting: 10 minutes
- Information gathering: 15 minutes
- Configuration: 15 minutes
- **Total: ~80 minutes**

---

## Command Reference

**Check VM status:**
```bash
qm list | grep k8s
```

**Start/Stop VMs:**
```bash
# Start all
for vmid in {8001..8003} {8004..8015}; do qm start $vmid; done

# Stop all
for vmid in {8001..8003} {8004..8015}; do qm stop $vmid; done

# Reboot all
for vmid in {8001..8003} {8004..8015}; do qm reboot $vmid; done
```

**Check Talos connectivity:**
```bash
nmap -Pn -n -p 50000 10.20.67.0/24
```

**Get node info:**
```bash
talosctl disks --nodes 10.20.67.1 --insecure
talosctl get links --nodes 10.20.67.1 --insecure
talosctl version --nodes 10.20.67.1 --insecure
```

**Check Proxmox resources:**
```bash
free -h                    # RAM
df -h                      # Disk
pvesm status               # Storage pools
ip link show               # Network bridges
```

---

## Next Steps After VM Creation

1. Read IMPLEMENTATION_PLAN.md for full bootstrap process
2. Set up Cloudflare tunnel (Section 9)
3. Bootstrap Talos cluster (Section 10.1)
4. Bootstrap applications (Section 10.2)
5. Deploy workloads

**Full documentation:**
- VM_CREATION_GUIDE.md - Detailed VM creation guide
- IMPLEMENTATION_PLAN.md - Complete cluster setup
- VLAN_SETUP.md - Multi-VLAN networking
- STORAGE_SETUP.md - Rook-Ceph storage

---

Good luck with your cluster!
