# =============================================================================
# Required Variables
# =============================================================================

variable "compartment_id" {
  description = "OCI Compartment OCID"
  type        = string
}

variable "availability_domain" {
  description = "OCI Availability Domain"
  type        = string
}

variable "instance_name" {
  description = "Name for the compute instance"
  type        = string
}

variable "ssh_public_key" {
  description = "SSH public key for instance access"
  type        = string
}

# =============================================================================
# Instance Configuration
# =============================================================================

variable "shape" {
  description = "OCI compute shape"
  type        = string
  default     = "VM.Standard.A1.Flex"
}

variable "ocpus" {
  description = "Number of OCPUs (for Flex shapes)"
  type        = number
  default     = 1
}

variable "memory_gb" {
  description = "Memory in GB (for Flex shapes)"
  type        = number
  default     = 6
}

variable "boot_volume_gb" {
  description = "Boot volume size in GB"
  type        = number
  default     = 50
}

variable "os_image" {
  description = "OS image to use (Canonical Ubuntu recommended for ARM)"
  type        = string
  default     = "Canonical Ubuntu"
}

variable "os_version" {
  description = "OS version"
  type        = string
  default     = "22.04"
}

# =============================================================================
# Network Configuration
# =============================================================================

variable "vcn_cidr" {
  description = "CIDR block for VCN"
  type        = string
  default     = "10.0.0.0/16"
}

variable "subnet_cidr" {
  description = "CIDR block for subnet"
  type        = string
  default     = "10.0.1.0/24"
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed for SSH access"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "additional_ingress_ports" {
  description = "Additional ports to open for ingress"
  type = list(object({
    port     = number
    protocol = string
  }))
  default = []
}

# =============================================================================
# WireGuard Configuration
# =============================================================================

variable "enable_wireguard" {
  description = "Whether to configure WireGuard on this instance"
  type        = bool
  default     = false
}

variable "wg_listen_port" {
  description = "WireGuard listen port"
  type        = number
  default     = 51820
}

variable "wg_address" {
  description = "WireGuard interface address"
  type        = string
  default     = "10.200.200.1/24"
}

variable "wg_peer_public_key" {
  description = "WireGuard peer public key"
  type        = string
  default     = ""
}

variable "wg_peer_allowed_ips" {
  description = "WireGuard peer allowed IPs"
  type        = string
  default     = "10.200.200.2/32"
}

variable "wg_forward_port" {
  description = "Port to forward through WireGuard tunnel"
  type        = number
  default     = 0
}

variable "wg_forward_target_ip" {
  description = "Target IP for port forwarding (behind WireGuard peer)"
  type        = string
  default     = ""
}

# =============================================================================
# Tags
# =============================================================================

variable "freeform_tags" {
  description = "Freeform tags to apply to resources"
  type        = map(string)
  default     = {}
}
