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

variable "wg_peer_allowed_cidrs" {
  description = "CIDR blocks allowed for WireGuard connections (recommend restricting to home IP)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
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

variable "wg_private_key" {
  description = "Static WireGuard private key (if provided, skips key generation)"
  type        = string
  default     = ""
  sensitive   = true

  validation {
    condition     = var.wg_private_key == "" || can(regex("^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$", var.wg_private_key))
    error_message = "wg_private_key must be a valid WireGuard private key (44 characters base64-encoded)."
  }
}

variable "wg_public_key" {
  description = "Static WireGuard public key (must match wg_private_key, avoids need for wg binary)"
  type        = string
  default     = ""

  validation {
    condition     = var.wg_public_key == "" || can(regex("^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$", var.wg_public_key))
    error_message = "wg_public_key must be a valid WireGuard public key (44 characters base64-encoded)."
  }
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
# Reserved Public IP
# =============================================================================

variable "use_reserved_public_ip" {
  description = "Use a reserved public IP that persists across instance recreation"
  type        = bool
  default     = false
}

# =============================================================================
# Nginx Reverse Proxy Configuration
# =============================================================================

variable "enable_nginx_proxy" {
  description = "Enable nginx reverse proxy with Cloudflare TLS"
  type        = bool
  default     = false
}

variable "nginx_server_name" {
  description = "Server name for nginx (e.g., streaming.homelab0.org)"
  type        = string
  default     = ""
}

variable "nginx_origin_cert" {
  description = "Cloudflare Origin Certificate (PEM format)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "nginx_origin_key" {
  description = "Cloudflare Origin Certificate private key (PEM format)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "nginx_backend_url" {
  description = "Backend URL for nginx proxy (e.g., http://10.200.200.2:32400)"
  type        = string
  default     = ""
}

# Cloudflare IPv4 ranges for security list (updated 2025-11)
# Source: https://www.cloudflare.com/ips-v4
variable "cloudflare_ipv4_ranges" {
  description = "Cloudflare IPv4 ranges for firewall rules"
  type        = list(string)
  default = [
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22"
  ]
}

# =============================================================================
# Tags
# =============================================================================

variable "freeform_tags" {
  description = "Freeform tags to apply to resources"
  type        = map(string)
  default     = {}
}
