# =============================================================================
# Oracle Cloud Infrastructure - Main Configuration
# =============================================================================
#
# This configuration manages OCI resources including:
#   - Plex proxy instance (WireGuard NAT proxy for IP privacy)
#   - Static reserved public IP (persists across instance recreation)
#   - Future: Additional OCI resources as needed
#
# =============================================================================

# =============================================================================
# Data Sources
# =============================================================================

# Get availability domains for the tenancy
data "oci_identity_availability_domains" "ads" {
  compartment_id = var.compartment_id
}

# =============================================================================
# Plex Proxy Instance
# =============================================================================
# Creates a minimal Always Free ARM instance as WireGuard NAT proxy
# for Plex, hiding home IP from external users

module "plex_proxy" {
  source = "./modules/compute"
  count  = var.plex_proxy_enabled ? 1 : 0

  compartment_id      = var.compartment_id
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[var.availability_domain_index].name

  instance_name = var.plex_proxy_name
  # Use x86 E2.1.Micro (Always Free) - ARM A1.Flex often out of capacity
  shape         = "VM.Standard.E2.1.Micro"
  ocpus         = 1
  memory_gb     = 1  # E2.1.Micro is fixed at 1GB

  ssh_public_key    = var.ssh_public_key
  ssh_allowed_cidrs = var.ssh_allowed_cidrs

  # Enable private IP lookup for external reserved IP attachment
  # Set external_reserved_public_ip_id to any non-empty value to prevent
  # the module from creating its own reserved IP (we manage it at root level)
  use_reserved_public_ip         = true
  external_reserved_public_ip_id = "managed-at-root-level"

  # WireGuard configuration for Plex proxy
  enable_wireguard       = true
  wg_private_key         = var.vps_wg_private_key  # Static key for one-click deploy
  wg_public_key          = var.vps_wg_public_key   # Pre-computed public key (no wg binary needed)
  wg_peer_allowed_cidrs  = var.wg_peer_allowed_cidrs
  wg_listen_port        = 51820
  wg_peer_public_key    = var.k8s_wg_public_key
  wg_forward_port       = 32400
  wg_forward_target_ip  = var.plex_loadbalancer_ip

  # Nginx reverse proxy with Cloudflare TLS
  # When enabled: port 443 from Cloudflare IPs only (no 32400)
  # When disabled: port 32400 open to all (legacy DNAT mode)
  enable_nginx_proxy   = var.enable_nginx_proxy
  nginx_server_name    = var.nginx_server_name
  nginx_origin_cert    = var.nginx_origin_cert
  nginx_origin_key     = var.nginx_origin_key
  nginx_backend_url    = var.nginx_backend_url

  # Additional ports to open (only when NOT using nginx proxy)
  # When nginx is enabled, only 443 is opened (from Cloudflare IPs)
  additional_ingress_ports = var.enable_nginx_proxy ? [] : [
    { port = 32400, protocol = "tcp" },
    { port = 32400, protocol = "udp" },
  ]

  freeform_tags = var.freeform_tags
}

# =============================================================================
# Static Reserved Public IP
# =============================================================================
# This reserved IP is managed OUTSIDE the module lifecycle.
# It persists across instance destruction/recreation, ensuring:
#   - NetworkPolicy never needs updating
#   - 1Password secrets never need updating
#   - Cloudflare DNS never needs updating
#   - True one-click deployment with stable IP
#
# CRITICAL: prevent_destroy = true ensures this IP is NEVER deleted
# The IP is reassigned to new instances automatically via private_ip_id

resource "oci_core_public_ip" "plex_proxy_static" {
  count          = var.plex_proxy_enabled ? 1 : 0
  compartment_id = var.compartment_id
  lifetime       = "RESERVED"
  display_name   = "${var.plex_proxy_name}-static-ip"

  # Attach to the instance's primary private IP
  # When instance is destroyed, this becomes null/unassigned
  # When new instance is created, this updates to new private_ip_id
  private_ip_id = module.plex_proxy[0].primary_private_ip_id

  freeform_tags = var.freeform_tags

  lifecycle {
    # CRITICAL: Never destroy this IP - it must persist across all operations
    # This is the key to true static IP behavior
    prevent_destroy = true

    # The private_ip_id will change when instance is recreated - allow this
    # All other attributes should remain stable
  }
}
