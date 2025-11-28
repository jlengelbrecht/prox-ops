# =============================================================================
# Oracle Cloud Infrastructure - Main Configuration
# =============================================================================
#
# This configuration manages OCI resources including:
#   - Plex proxy instance (WireGuard NAT proxy for IP privacy)
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

  # Use reserved public IP - persists across instance recreation
  # This eliminates the need to update NetworkPolicy and 1Password
  # every time the VPS is recreated
  use_reserved_public_ip = true

  # WireGuard configuration for Plex proxy
  enable_wireguard       = true
  wg_peer_allowed_cidrs  = var.wg_peer_allowed_cidrs
  wg_listen_port        = 51820
  wg_peer_public_key    = var.k8s_wg_public_key
  wg_forward_port       = 32400
  wg_forward_target_ip  = var.plex_loadbalancer_ip

  # Additional ports to open (Plex)
  additional_ingress_ports = [
    { port = 32400, protocol = "tcp" },
    { port = 32400, protocol = "udp" },
  ]

  freeform_tags = var.freeform_tags
}
