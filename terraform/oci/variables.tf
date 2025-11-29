# =============================================================================
# OCI Provider Configuration
# =============================================================================
# Two authentication modes supported:
# 1. Local: Set oci_config_profile (uses ~/.oci/config file)
# 2. CI/CD: Set TF_VAR_oci_tenancy_ocid, TF_VAR_oci_user_ocid,
#           TF_VAR_oci_fingerprint, TF_VAR_oci_private_key
# =============================================================================

# Config file authentication (local development)
variable "oci_config_file" {
  description = "Path to OCI config file"
  type        = string
  default     = "~/.oci/config"
}

variable "oci_config_profile" {
  description = "OCI config profile name (used when oci_private_key is null)"
  type        = string
  default     = "DEFAULT"
}

# Direct authentication (CI/CD)
variable "oci_tenancy_ocid" {
  description = "OCI tenancy OCID (for CI/CD authentication)"
  type        = string
  default     = null
  sensitive   = true
}

variable "oci_user_ocid" {
  description = "OCI user OCID (for CI/CD authentication)"
  type        = string
  default     = null
  sensitive   = true
}

variable "oci_fingerprint" {
  description = "OCI API key fingerprint (for CI/CD authentication)"
  type        = string
  default     = null
  sensitive   = true
}

variable "oci_private_key" {
  description = "OCI API private key content (for CI/CD authentication)"
  type        = string
  default     = null
  sensitive   = true
}

variable "oci_region" {
  description = "OCI region"
  type        = string
  default     = "us-ashburn-1"
}

variable "compartment_id" {
  description = "OCI Compartment OCID (use tenancy OCID for root compartment)"
  type        = string
}

# =============================================================================
# SSH Configuration
# =============================================================================

variable "ssh_public_key" {
  description = "SSH public key for instance access"
  type        = string
}

variable "ssh_allowed_cidrs" {
  description = "CIDR blocks allowed for SSH access (recommend restricting to home IP)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "wg_peer_allowed_cidrs" {
  description = "CIDR blocks allowed for WireGuard connections (recommend restricting to home IP)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

# =============================================================================
# Plex Proxy Instance Configuration
# =============================================================================

variable "plex_proxy_enabled" {
  description = "Whether to create the Plex proxy instance"
  type        = bool
  default     = true
}

variable "availability_domain_index" {
  description = "Index of availability domain to use (0, 1, or 2 for AD-1, AD-2, AD-3)"
  type        = number
  default     = 0
}

variable "plex_proxy_name" {
  description = "Name for the Plex proxy instance"
  type        = string
  default     = "plex-proxy"
}

variable "k8s_wg_public_key" {
  description = "WireGuard public key from K8s cluster (set after K8s WG gateway is deployed)"
  type        = string
  default     = ""
}

variable "vps_wg_private_key" {
  description = "Static WireGuard private key for VPS (enables one-click deploy without 1Password updates)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "vps_wg_public_key" {
  description = "Static WireGuard public key for VPS (must match vps_wg_private_key)"
  type        = string
  default     = ""

  validation {
    condition     = var.vps_wg_public_key == "" || can(regex("^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$", var.vps_wg_public_key))
    error_message = "vps_wg_public_key must be a valid WireGuard public key (44 characters base64-encoded)."
  }
}

variable "plex_loadbalancer_ip" {
  description = "Plex LoadBalancer IP in K8s cluster (set via TF_VAR_plex_loadbalancer_ip secret)"
  type        = string
  sensitive   = true
}

# =============================================================================
# Nginx Reverse Proxy Configuration
# =============================================================================

variable "enable_nginx_proxy" {
  description = "Enable nginx reverse proxy with TLS"
  type        = bool
  default     = false
}

variable "cloudflare_dns_only" {
  description = <<-EOT
    Use Cloudflare DNS-only mode (no proxy). When true, opens port 443/80 to all IPs.

    RECOMMENDED for Plex streaming - Cloudflare ToS prohibits proxying video content
    on free/standard plans (requires Enterprise agreement).

    NOTE: DNS-only mode requires a publicly-trusted certificate (e.g., Let's Encrypt).
    Cloudflare Origin certificates only work when Cloudflare proxy is enabled.

    When false (proxy mode), requires Cloudflare SSL mode set to "Full (strict)"
    to prevent redirect loops.
  EOT
  type        = bool
  default     = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token for Let's Encrypt DNS-01 challenge (required when cloudflare_dns_only=true). Token needs Zone:DNS:Edit permission."
  type        = string
  default     = ""
  sensitive   = true
}

variable "letsencrypt_email" {
  description = "Email for Let's Encrypt certificate registration and renewal notices"
  type        = string
  default     = ""
}

variable "nginx_server_name" {
  description = "Server name for nginx (e.g., streaming.homelab0.org)"
  type        = string
  default     = "streaming.homelab0.org"
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
  description = "Backend URL for nginx reverse proxy (set via TF_VAR_nginx_backend_url secret)"
  type        = string
  # Default matches WireGuard tunnel IP range (10.200.200.0/24) defined in cloud-init.
  # The K8s WireGuard gateway is assigned 10.200.200.2 in the tunnel.
  default     = "http://10.200.200.2:32400"
  sensitive   = true
}

# =============================================================================
# Tags
# =============================================================================

variable "freeform_tags" {
  description = "Freeform tags to apply to all resources"
  type        = map(string)
  default = {
    project   = "prox-ops"
    managedby = "terraform"
  }
}
