# =============================================================================
# Proxmox Connection Variables
# =============================================================================

# Proxmox API endpoints for all 4 cluster nodes
variable "proxmox_endpoints" {
  description = "Map of Proxmox node names to their API endpoints"
  type = object({
    baldar   = string
    heimdall = string
    odin     = string
    thor     = string
  })
}

variable "proxmox_username" {
  description = "Proxmox username (e.g., root@pam or terraform@pve!token) - Set via TF_VAR_proxmox_username environment variable"
  type        = string
  default     = ""  # Set via environment variable TF_VAR_proxmox_username
}

variable "proxmox_password" {
  description = "Proxmox password or API token secret - Set via TF_VAR_proxmox_password environment variable"
  type        = string
  sensitive   = true
  default     = ""  # Set via environment variable TF_VAR_proxmox_password
}

variable "proxmox_insecure" {
  description = "Skip TLS verification for Proxmox API (set true for self-signed certs)"
  type        = bool
  default     = true
}

variable "proxmox_ssh_user" {
  description = "SSH user for Proxmox hosts (for template creation via SSH)"
  type        = string
  default     = "root"
}

# Talos Configuration
variable "talos_version" {
  description = "Talos Linux version to deploy"
  type        = string
  default     = "1.11.6"
}

variable "talos_schematic_base" {
  description = "Talos factory schematic ID for base nodes (controllers + regular workers). Extensions: QEMU Guest Agent only"
  type        = string
  default     = "ce4c980550dd2ab1b17bbf2b08801c7eb59418eafe8f279833297925d67c7515"
}

variable "talos_schematic_gpu" {
  description = "Talos factory schematic ID for GPU worker nodes. Extensions: QEMU Guest Agent + NVIDIA drivers + NVIDIA Container Toolkit"
  type        = string
  default     = "990731763242a6b3cf735e49d0f550ce4068b4d0e7f4dfbb49a31799b698877e"
}

# VM Template Configuration
variable "template_vm_id_controlplane" {
  description = "VM ID for control plane template"
  type        = number
  default     = 9000
}

variable "template_vm_id_worker" {
  description = "VM ID for worker template"
  type        = number
  default     = 9001
}

# Network Configuration
variable "network_bridge" {
  description = "Proxmox network bridge"
  type        = string
  default     = "vmbr1"
}

variable "network_gateway" {
  description = "Default gateway for nodes"
  type        = string
  default     = "10.20.66.1"
}

variable "network_netmask" {
  description = "Network netmask"
  type        = string
  default     = "255.255.254.0"  # /23
}

# =============================================================================
# VM Resource Configuration
# =============================================================================

variable "vm_storage_pool" {
  description = "Proxmox storage pool for VM disks (e.g., vms-ceph, local-lvm)"
  type        = string
  default     = "vms-ceph"
}

variable "cpu_type" {
  description = "CPU type for VMs (host provides best performance for homelab)"
  type        = string
  default     = "host"
}

# =============================================================================
# Security Configuration (MANDATORY for DMZ)
# =============================================================================

variable "enable_secure_boot" {
  description = "Enable Secure Boot with TPM 2.0 (REQUIRED for DMZ connectivity)"
  type        = bool
  default     = true
}

variable "enable_tpm" {
  description = "Enable TPM 2.0 state disk (REQUIRED for Secure Boot)"
  type        = bool
  default     = true
}

variable "enable_firewall" {
  description = "Enable firewall on network devices"
  type        = bool
  default     = true
}

# =============================================================================
# Template ID Scheme (2 templates per node × 4 nodes = 8 total)
# =============================================================================
# Each node gets both controller and worker templates for flexibility
# and future auto-scaling capabilities

variable "template_ids" {
  description = "Template VM IDs for each node and type"
  type = object({
    baldar = object({
      controller = number
      worker     = number
    })
    heimdall = object({
      controller = number
      worker     = number
    })
    odin = object({
      controller = number
      worker     = number
    })
    thor = object({
      controller = number
      worker     = number
    })
  })
  default = {
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
}

# =============================================================================
# Control Plane Configuration
# =============================================================================
# Based on existing VM specs: 16GB RAM, 4 cores (2 sockets × 2 cores), 100G disk

variable "controlplane_cpu_cores" {
  description = "CPU cores for control plane nodes"
  type        = number
  default     = 4
}

variable "controlplane_cpu_sockets" {
  description = "CPU sockets for control plane nodes"
  type        = number
  default     = 2
}

variable "controlplane_memory_mb" {
  description = "Memory in MB for control plane nodes"
  type        = number
  default     = 16384  # 16GB
}

variable "controlplane_disk_size_gb" {
  description = "Disk size in GB for control plane nodes"
  type        = number
  default     = 100
}

# =============================================================================
# Worker Configuration
# =============================================================================
# Based on existing VM specs: 32GB RAM, 16 cores (2 sockets × 8 cores), 100G disk

variable "worker_cpu_cores" {
  description = "CPU cores for worker nodes"
  type        = number
  default     = 16
}

variable "worker_cpu_sockets" {
  description = "CPU sockets for worker nodes"
  type        = number
  default     = 2
}

variable "worker_memory_mb" {
  description = "Memory in MB for worker nodes"
  type        = number
  default     = 32768  # 32GB
}

variable "worker_disk_size_gb" {
  description = "Disk size in GB for worker nodes"
  type        = number
  default     = 100
}

# =============================================================================
# GPU Worker Configuration (Same as regular workers in this deployment)
# =============================================================================

variable "gpu_worker_cpu_cores" {
  description = "CPU cores for GPU worker nodes"
  type        = number
  default     = 16
}

variable "gpu_worker_cpu_sockets" {
  description = "CPU sockets for GPU worker nodes"
  type        = number
  default     = 2
}

variable "gpu_worker_memory_mb" {
  description = "Memory in MB for GPU worker nodes"
  type        = number
  default     = 32768
}

variable "gpu_worker_disk_size_gb" {
  description = "Disk size in GB for GPU worker nodes"
  type        = number
  default     = 100
}

# =============================================================================
# Node Definitions
# =============================================================================

variable "control_nodes" {
  description = "Control plane node definitions with Proxmox node assignment"
  type = list(object({
    name         = string
    hostname     = string
    ip_address   = string
    mac_addr     = string
    vm_id        = number
    proxmox_node = string  # Which Proxmox host to deploy on (baldar, heimdall, odin, thor)
  }))
}

variable "worker_nodes" {
  description = "Worker node definitions with GPU and node assignment"
  type = list(object({
    name         = string
    hostname     = string
    ip_address   = string
    mac_addr     = string
    vm_id        = number
    proxmox_node = string  # Which Proxmox host to deploy on
    is_gpu       = optional(bool, false)
    gpu_model    = optional(string, "")
    gpu_mapping  = optional(string, "")  # PCI resource mapping ID (e.g., "thor-gpu")
    additional_network_devices = optional(list(object({
      bridge  = string
      vlan_id = number
      model   = string
    })), [])
  }))
}
