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
  description = "Network bridge for all interfaces - 10G bond (vmbr1)"
  type        = string
  default     = "vmbr1"
}

variable "iot_vlan_tag" {
  description = "VLAN tag for IoT network (eth1)"
  type        = number
  default     = 62
}

variable "dmz_vlan_tag" {
  description = "VLAN tag for DMZ network (eth2)"
  type        = number
  default     = 81
}

# Resource Configuration
variable "cpu_cores" {
  description = "Number of CPU cores"
  type        = number
}

variable "cpu_sockets" {
  description = "Number of CPU sockets"
  type        = number
  default     = 1
}

variable "cpu_type" {
  description = "CPU type"
  type        = string
  default     = "host"
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

# Security Configuration
variable "enable_secure_boot" {
  description = "Enable Secure Boot with TPM 2.0 (REQUIRED for DMZ)"
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

# GPU Configuration
variable "gpu_passthrough_mapping" {
  description = "PCI resource mapping ID for GPU passthrough (e.g., 'thor-gpu')"
  type        = string
  default     = ""
}

variable "gpu_passthrough_devices" {
  description = "List of GPU devices to passthrough (deprecated - use gpu_passthrough_mapping)"
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
  default     = 1800 # 30 minutes
}

variable "timeout_clone" {
  description = "Timeout for VM cloning"
  type        = number
  default     = 600 # 10 minutes
}

variable "timeout_migrate" {
  description = "Timeout for VM migration"
  type        = number
  default     = 600 # 10 minutes
}

variable "timeout_reboot" {
  description = "Timeout for VM reboot"
  type        = number
  default     = 300 # 5 minutes
}

variable "timeout_shutdown" {
  description = "Timeout for VM shutdown"
  type        = number
  default     = 300 # 5 minutes
}

# Tags
variable "additional_tags" {
  description = "Additional tags for the VM"
  type        = list(string)
  default     = []
}
