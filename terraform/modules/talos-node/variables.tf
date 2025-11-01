variable "hostname" {
  description = "Hostname for the node"
  type        = string
}

variable "vm_id" {
  description = "VM ID for the node"
  type        = number
}

variable "proxmox_node" {
  description = "Proxmox node name where VM will be created"
  type        = string
}

variable "template_vm_id" {
  description = "Template VM ID to clone from"
  type        = number
}

variable "is_controlplane" {
  description = "Whether this is a control plane node"
  type        = bool
  default     = false
}

variable "is_gpu" {
  description = "Whether this is a GPU node"
  type        = bool
  default     = false
}

# Network Configuration
variable "mac_address" {
  description = "MAC address for the node"
  type        = string
}

variable "ip_address" {
  description = "IP address for the node (for documentation)"
  type        = string
}

variable "network_bridge" {
  description = "Network bridge name"
  type        = string
  default     = "vmbr0"
}

# Resource Configuration
variable "cpu_cores" {
  description = "Number of CPU cores"
  type        = number
}

variable "cpu_type" {
  description = "CPU type"
  type        = string
  default     = "x86-64-v2-AES"
}

variable "memory_mb" {
  description = "Memory in MB"
  type        = number
}

variable "disk_size_gb" {
  description = "Disk size in GB"
  type        = number
}

variable "vm_storage_pool" {
  description = "Storage pool for VM disks"
  type        = string
}

# GPU Configuration
variable "gpu_passthrough_devices" {
  description = "List of GPU devices to passthrough"
  type = list(object({
    device  = string
    mapping = optional(string)
  }))
  default = []
}

# VM Behavior
variable "auto_start" {
  description = "Start VM after creation"
  type        = bool
  default     = true
}

variable "enable_ballooning" {
  description = "Enable memory ballooning (not recommended for Kubernetes)"
  type        = bool
  default     = false
}

variable "enable_migration" {
  description = "Enable VM migration"
  type        = bool
  default     = false
}

variable "enable_protection" {
  description = "Enable VM deletion protection"
  type        = bool
  default     = false
}

# Timeouts
variable "timeout_create" {
  description = "Timeout for VM creation"
  type        = number
  default     = 1800  # 30 minutes
}

variable "timeout_clone" {
  description = "Timeout for VM cloning"
  type        = number
  default     = 600  # 10 minutes
}

variable "timeout_migrate" {
  description = "Timeout for VM migration"
  type        = number
  default     = 600  # 10 minutes
}

variable "timeout_reboot" {
  description = "Timeout for VM reboot"
  type        = number
  default     = 300  # 5 minutes
}

variable "timeout_shutdown" {
  description = "Timeout for VM shutdown"
  type        = number
  default     = 300  # 5 minutes
}

# Tags
variable "additional_tags" {
  description = "Additional tags for the VM"
  type        = list(string)
  default     = []
}
