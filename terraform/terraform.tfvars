# =============================================================================
# Terraform Variables - Multi-Node Proxmox Cluster Configuration
# =============================================================================
#
# Talos Linux Kubernetes Cluster Deployment on 4-Node Proxmox Cluster
#
# IMPORTANT SECURITY NOTES:
# - Credentials are NOT stored in this file (uses environment variables)
# - Set TF_VAR_proxmox_username and TF_VAR_proxmox_password before deploying
# - See: terraform/setup-credentials.sh for helper script
#
# =============================================================================

# -----------------------------------------------------------------------------
# Proxmox API Endpoints (All 4 Cluster Nodes)
# -----------------------------------------------------------------------------
proxmox_endpoints = {
  baldar   = "https://10.20.66.4:8006"
  heimdall = "https://10.20.66.8:8006"
  odin     = "https://10.20.66.6:8006"
  thor     = "https://10.20.66.7:8006"
}

# -----------------------------------------------------------------------------
# Proxmox Authentication
# -----------------------------------------------------------------------------
# CREDENTIALS ARE SET VIA ENVIRONMENT VARIABLES (not stored in this file)
#
# Before running terraform, set:
#   export TF_VAR_proxmox_username="root@pam!terraform"
#   export TF_VAR_proxmox_password="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
#
# Or use the helper script:
#   source terraform/setup-credentials.sh
#
# The variables are defined in variables.tf with empty defaults

# Skip TLS verification (true for self-signed certs in homelab)
proxmox_insecure = true

# SSH user for Proxmox nodes (used for template creation via qm commands)
proxmox_ssh_user = "root"

# -----------------------------------------------------------------------------
# Talos Linux Configuration
# -----------------------------------------------------------------------------
talos_version = "1.14.5"

# Talos Factory Schematics (SECURE BOOT ENABLED)
# Generated at: https://factory.talos.dev/
#
# Base schematic: QEMU Guest Agent + net.ifnames=0 (for controllers + regular workers)
# GPU schematic: QEMU Guest Agent + NVIDIA drivers + NVIDIA Container Toolkit + net.ifnames=0 (for GPU workers only)
talos_schematic_base = "f683308f6e49ec36d715bd41c90d800910552d8cbc015c8363ec350594535fa1"
talos_schematic_gpu  = "26a847a67a3a6575fdcb3a9a45d6f1add29827e0f549955806b532d590004982"

# -----------------------------------------------------------------------------
# Template ID Scheme
# -----------------------------------------------------------------------------
template_ids = {
  baldar = {
    controller = 9000
    worker     = 9001
  }
  heimdall = {
    controller = 9002
    worker     = 9003
  }
  odin = {
    controller = 9004
    worker     = 9005
  }
  thor = {
    controller = 9006
    worker     = 9007
  }
}

# -----------------------------------------------------------------------------
# Network Configuration
# -----------------------------------------------------------------------------
network_bridge  = "vmbr1"
network_gateway = "10.20.66.1"
network_netmask = "255.255.254.0" # /23

# -----------------------------------------------------------------------------
# Storage Configuration
# -----------------------------------------------------------------------------
vm_storage_pool = "vms-ceph"
cpu_type        = "host"

# -----------------------------------------------------------------------------
# Security Configuration
# -----------------------------------------------------------------------------
# IMPORTANT: enable_secure_boot controls the "Pre-Enroll Keys" checkbox in Proxmox
#
# When FALSE (current setting):
#   - UEFI Secure Boot: ENABLED ✓
#   - Pre-Enroll Keys: UNCHECKED ✓
#   - Allows Talos to enroll its own Secure Boot keys during first boot
#   - This is the CORRECT setting for Talos Linux
#
# Proxmox Configuration Equivalent:
#   - Machine: q35
#   - BIOS: OVMF (UEFI)
#   - EFI Storage: vms-ceph
#   - Pre-Enroll keys: UNCHECKED (this is what enable_secure_boot=false does!)
#   - QEMU Agent: enabled
#   - TPM: enabled with vms-ceph storage
#
enable_secure_boot = false # Pre-enrolled keys DISABLED - Talos enrolls its own
enable_tpm         = true  # TPM 2.0 enabled for Secure Boot support
enable_firewall    = true  # VM-level firewall enabled

# -----------------------------------------------------------------------------
# Control Plane Resources
# -----------------------------------------------------------------------------
controlplane_cpu_cores    = 4 # 8 vCPUs: etcd and kube-apiserver headroom (2026-11-02)
controlplane_cpu_sockets  = 2
controlplane_memory_mb    = 16384 # 16GB: control plane used ~70% of 8GB (2026-11-02)
controlplane_disk_size_gb = 100

# -----------------------------------------------------------------------------
# Worker Resources
# -----------------------------------------------------------------------------
worker_cpu_cores    = 8 # CHANGED: 16 → 8 (total 16 vCPUs)
worker_cpu_sockets  = 2
worker_memory_mb    = 32768 # 32GB (unchanged)
worker_disk_size_gb = 100

# -----------------------------------------------------------------------------
# GPU Worker Resources
# -----------------------------------------------------------------------------
gpu_worker_cpu_cores    = 8 # CHANGED: 16 → 8 (total 16 vCPUs)
gpu_worker_cpu_sockets  = 2
gpu_worker_memory_mb    = 65536 # CHANGED: 32768 → 65536 (64GB for GPU workloads)
gpu_worker_disk_size_gb = 100

# -----------------------------------------------------------------------------
# Control Plane Node Definitions (3 nodes)
# -----------------------------------------------------------------------------
# VM IDs: 901-903
# Distributed across: Baldar, Heimdall, Odin

control_nodes = [
  {
    name         = "k8s-ctrl-1"
    hostname     = "k8s-ctrl-1"
    ip_address   = "10.20.67.1"
    mac_addr     = "bc:24:11:af:26:d4"
    vm_id        = 901
    proxmox_node = "Baldar"
  },
  {
    name         = "k8s-ctrl-2"
    hostname     = "k8s-ctrl-2"
    ip_address   = "10.20.67.2"
    mac_addr     = "aa:54:cd:f6:a1:d0"
    vm_id        = 902
    proxmox_node = "Heimdall"
  },
  {
    name         = "k8s-ctrl-3"
    hostname     = "k8s-ctrl-3"
    ip_address   = "10.20.67.3"
    mac_addr     = "be:9c:fd:2c:54:85"
    vm_id        = 903
    proxmox_node = "Thor"
  }
]

# -----------------------------------------------------------------------------
# Worker Node Definitions (10 nodes total: 8 regular + 2 GPU)
# -----------------------------------------------------------------------------
# VM IDs: 904-915
#
# GPU Workers:
#   - k8s-work-4 (VM 907): Heimdall with RTX A2000 (heimdall-gpu mapping)
#   - k8s-work-7 (VM 910): Odin with RTX A5000 (odin-gpu mapping)
#
# Distribution strategy:
#   - Baldar: 3 workers (work-1, work-2, work-3)
#   - Heimdall: 3 workers (work-4 GPU, work-5, work-6)
#   - Odin: 1 large worker (work-7 GPU)
#   - Thor: 3 workers (work-10, work-11, work-12) and ctrl-3

worker_nodes = [
  # Baldar workers (3)
  {
    name         = "k8s-work-1"
    hostname     = "k8s-work-1"
    ip_address   = "10.20.67.4"
    mac_addr     = "b2:dc:2e:72:8c:ec"
    vm_id        = 904
    proxmox_node = "Baldar"
    # Baldar has 125.8G for four VMs plus two Ceph OSDs; its workers are cut
    # from 32G to 24G to leave headroom for ctrl-1 at 16G.
    memory_mb    = 24576
    is_gpu       = false
    gpu_model    = ""
    gpu_mapping  = ""
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },
  {
    name         = "k8s-work-2"
    hostname     = "k8s-work-2"
    ip_address   = "10.20.67.5"
    mac_addr     = "06:64:c3:76:9a:98"
    vm_id        = 905
    proxmox_node = "Baldar"
    # Baldar has 125.8G for four VMs plus two Ceph OSDs; its workers are cut
    # from 32G to 24G to leave headroom for ctrl-1 at 16G.
    memory_mb    = 24576
    is_gpu       = false
    gpu_model    = ""
    gpu_mapping  = ""
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },
  {
    name         = "k8s-work-3"
    hostname     = "k8s-work-3"
    ip_address   = "10.20.67.6"
    mac_addr     = "36:30:ad:44:40:cd"
    vm_id        = 906
    proxmox_node = "Baldar"
    # Baldar has 125.8G for four VMs plus two Ceph OSDs; its workers are cut
    # from 32G to 24G to leave headroom for ctrl-1 at 16G.
    memory_mb    = 24576
    is_gpu       = false
    gpu_model    = ""
    gpu_mapping  = ""
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },

  # Heimdall workers (3) - GPU Worker on Heimdall (RTX A2000)
  {
    name         = "k8s-work-4"
    hostname     = "k8s-work-4"
    ip_address   = "10.20.67.7"
    mac_addr     = "22:d1:14:e2:ee:49"
    vm_id        = 907
    proxmox_node = "Heimdall"
    is_gpu       = true
    gpu_model    = "rtx-a2000"
    gpu_mapping  = "heimdall-gpu"
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },
  {
    name         = "k8s-work-5"
    hostname     = "k8s-work-5"
    ip_address   = "10.20.67.8"
    mac_addr     = "bc:24:11:e7:ef:7f"
    vm_id        = 908
    proxmox_node = "Heimdall"
    is_gpu       = false
    gpu_model    = ""
    gpu_mapping  = ""
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },
  {
    name         = "k8s-work-6"
    hostname     = "k8s-work-6"
    ip_address   = "10.20.67.9"
    mac_addr     = "0e:eb:7c:a2:2e:cc"
    vm_id        = 909
    proxmox_node = "Heimdall"
    is_gpu       = false
    gpu_model    = ""
    gpu_mapping  = ""
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },

  # Odin worker (1) - GPU worker with the RTX A5000, sized to the host
  {
    name         = "k8s-work-7"
    hostname     = "k8s-work-7"
    ip_address   = "10.20.67.10"
    mac_addr     = "bc:24:11:6b:75:fe"
    vm_id        = 910
    proxmox_node = "Odin"
    is_gpu       = true
    gpu_model    = "rtx-a5000"
    gpu_mapping  = "odin-gpu"
    # Odin's only worker: 72 vCPU (2 x 36) and 304 GiB. GPU passthrough pins all
    # of the VM's RAM, so the host keeps a fixed 72.5 GiB: about 31 GiB measured
    # (Proxmox services, Ceph mon, 2 OSDs at osd_memory_target 4 GiB, kernel and
    # disk buffers, ZFS ARC capped at 1 GiB, QEMU and IOMMU overhead) plus a 38 GiB
    # (10%) margin. At 344 GiB the host ran out of memory and hung (2026-11-08).
    # 8 threads stay free for Proxmox and the Ceph OSDs.
    cpu_cores    = 36
    memory_mb    = 311296
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },

  # Thor workers (3)
  {
    name         = "k8s-work-10"
    hostname     = "k8s-work-10"
    ip_address   = "10.20.67.13"
    mac_addr     = "BC:24:11:57:87:B2"
    vm_id        = 913
    proxmox_node = "Thor"
    is_gpu       = false
    gpu_model    = ""
    gpu_mapping  = ""
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },
  {
    name         = "k8s-work-11"
    hostname     = "k8s-work-11"
    ip_address   = "10.20.67.14"
    mac_addr     = "BC:24:11:7C:38:8E"
    vm_id        = 914
    proxmox_node = "Thor"
    is_gpu       = false
    gpu_model    = ""
    gpu_mapping  = ""
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  },
  {
    name         = "k8s-work-12"
    hostname     = "k8s-work-12"
    ip_address   = "10.20.67.15"
    mac_addr     = "bc:24:11:a9:aa:be"
    vm_id        = 915
    proxmox_node = "Thor"
    is_gpu       = false
    gpu_model    = ""
    gpu_mapping  = ""
    usb_mapping  = "yubihsm"
    # WI-014-1: Testing 3-NIC configuration (eth0: VLAN 66, eth1: VLAN 62, eth2: VLAN 81)
    additional_network_devices = [
      {
        bridge  = "vmbr1"
        vlan_id = 62
        model   = "virtio"
      },
      {
        bridge  = "vmbr1"
        vlan_id = 81
        model   = "virtio"
      }
    ]
  }
]

# =============================================================================
# DEPLOYMENT SUMMARY
# =============================================================================
#
# Total VMs: 15 (3 controllers + 12 workers)
# VM ID Range: 901-915
# Template IDs: 9000-9007 (8 templates: 2 per Proxmox node)
#
# Node Distribution:
#   Baldar:   k8s-ctrl-1, k8s-work-1, k8s-work-2, k8s-work-3 (4 VMs)
#   Heimdall: k8s-ctrl-2, k8s-work-4 (GPU-RTX A2000), k8s-work-5, k8s-work-6 (4 VMs)
#   Odin:     k8s-work-7 (GPU-RTX A5000) (1 VM)
#   Thor:     k8s-ctrl-3, k8s-work-10, k8s-work-11, k8s-work-12 (4 VMs)
#
# GPU Configuration:
#   - k8s-work-4 (907): Heimdall, RTX A2000, PCI mapping "heimdall-gpu"
#   - k8s-work-7 (910): Odin, RTX A5000, PCI mapping "odin-gpu"
#
# BEFORE DEPLOYING:
# [ ] 1. Set environment variables (see setup-credentials.sh)
# [ ] 2. SSH keys loaded: ssh-add ~/.ssh/id_rsa
# [ ] 3. Test SSH connectivity to all 4 Proxmox nodes
# [ ] 4. GPU PCI resource mappings created on Proxmox cluster
# [ ] 5. Granted PVEMappingUser permissions to terraform token
#
# =============================================================================
