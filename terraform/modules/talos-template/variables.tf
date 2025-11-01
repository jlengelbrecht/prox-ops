variable "talos_version" {
  description = "Talos Linux version"
  type        = string
}

variable "schematic_id" {
  description = "Talos factory schematic ID"
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
  description = "Proxmox node name"
  type        = string
}

variable "proxmox_host" {
  description = "Proxmox host IP/hostname for SSH"
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
  default     = "vmbr0"
}

variable "vm_storage_pool" {
  description = "Storage pool for VM disks"
  type        = string
}

variable "cpu_type" {
  description = "CPU type for the VM"
  type        = string
  default     = "x86-64-v2-AES"
}
