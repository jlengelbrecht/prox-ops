# Proxmox Connection Variables
variable "proxmox_endpoint" {
  description = "Proxmox API endpoint (e.g., https://proxmox.example.com:8006)"
  type        = string
}

variable "proxmox_username" {
  description = "Proxmox username (e.g., root@pam)"
  type        = string
}

variable "proxmox_password" {
  description = "Proxmox password"
  type        = string
  sensitive   = true
}

variable "proxmox_insecure" {
  description = "Skip TLS verification for Proxmox API"
  type        = bool
  default     = false
}

variable "proxmox_ssh_user" {
  description = "SSH user for Proxmox host (for template creation)"
  type        = string
  default     = "root"
}

variable "proxmox_node" {
  description = "Proxmox node name where VMs will be created"
  type        = string
}

# Talos Configuration
variable "talos_version" {
  description = "Talos Linux version to deploy"
  type        = string
  default     = "1.11.3"
}

variable "talos_schematic_controlplane" {
  description = "Talos factory schematic ID for control plane nodes"
  type        = string
  default     = "ce4c980550dd2ab1b17bbf2b08801c7eb59418eafe8f279833297925d67c7515"
}

variable "talos_schematic_worker" {
  description = "Talos factory schematic ID for worker nodes"
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
  default     = "vmbr0"
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

# VM Resource Configuration
variable "vm_storage_pool" {
  description = "Proxmox storage pool for VM disks"
  type        = string
  default     = "local-lvm"
}

variable "cpu_type" {
  description = "CPU type for VMs"
  type        = string
  default     = "x86-64-v2-AES"
}

# Control Plane Configuration
variable "controlplane_cpu_cores" {
  description = "CPU cores for control plane nodes"
  type        = number
  default     = 4
}

variable "controlplane_memory_mb" {
  description = "Memory in MB for control plane nodes"
  type        = number
  default     = 8192
}

variable "controlplane_disk_size_gb" {
  description = "Disk size in GB for control plane nodes"
  type        = number
  default     = 100
}

# Worker Configuration
variable "worker_cpu_cores" {
  description = "CPU cores for worker nodes"
  type        = number
  default     = 6
}

variable "worker_memory_mb" {
  description = "Memory in MB for worker nodes"
  type        = number
  default     = 16384
}

variable "worker_disk_size_gb" {
  description = "Disk size in GB for worker nodes"
  type        = number
  default     = 200
}

# GPU Worker Configuration
variable "gpu_worker_cpu_cores" {
  description = "CPU cores for GPU worker nodes"
  type        = number
  default     = 8
}

variable "gpu_worker_memory_mb" {
  description = "Memory in MB for GPU worker nodes"
  type        = number
  default     = 32768
}

variable "gpu_worker_disk_size_gb" {
  description = "Disk size in GB for GPU worker nodes"
  type        = number
  default     = 300
}

# Node Definitions
variable "control_nodes" {
  description = "Control plane node definitions"
  type = list(object({
    name       = string
    ip_address = string
    mac_addr   = string
    vm_id      = number
    hostname   = string
  }))
}

variable "worker_nodes" {
  description = "Worker node definitions"
  type = list(object({
    name       = string
    ip_address = string
    mac_addr   = string
    vm_id      = number
    hostname   = string
    is_gpu     = optional(bool, false)
    gpu_model  = optional(string, "")
  }))
}
