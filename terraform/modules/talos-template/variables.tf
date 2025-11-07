variable "talos_version" {
  description = "Talos Linux version"
  type        = string
}

variable "schematic_id" {
  description = "Talos factory schematic ID (must support secure boot for DMZ)"
  type        = string
}

variable "template_vm_id" {
  description = "VM ID for the template"
  type        = number
}

variable "template_name" {
  description = "Name for the template"
  type        = string
}

variable "proxmox_node" {
  description = "Proxmox node name (Baldar, Heimdall, Odin, Thor)"
  type        = string
}

variable "proxmox_host" {
  description = "Proxmox host IP/hostname for SSH connection"
  type        = string
}

variable "proxmox_ssh_user" {
  description = "SSH user for Proxmox host"
  type        = string
  default     = "root"
}

variable "network_bridge" {
  description = "Network bridge name"
  type        = string
  default     = "vmbr1"
}

variable "vm_storage_pool" {
  description = "Storage pool for VM disks (e.g., vms-ceph)"
  type        = string
}

variable "cpu_type" {
  description = "CPU type for the VM"
  type        = string
  default     = "host"
}

variable "enable_secure_boot" {
  description = "Enable Secure Boot with pre-enrolled keys (REQUIRED for DMZ)"
  type        = bool
  default     = true
}

variable "enable_tpm" {
  description = "Enable TPM 2.0 state disk (REQUIRED for Secure Boot)"
  type        = bool
  default     = true
}

variable "enable_firewall" {
  description = "Enable firewall on network device"
  type        = bool
  default     = true
}
