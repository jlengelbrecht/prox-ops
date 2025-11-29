# =============================================================================
# Outputs
# =============================================================================

# Plex Proxy Outputs
output "plex_proxy_public_ip" {
  description = "Public IP of the Plex proxy instance (static, persists across recreation)"
  value       = var.plex_proxy_enabled ? oci_core_public_ip.plex_proxy_static[0].ip_address : null
}

output "plex_proxy_private_ip" {
  description = "Private IP of the Plex proxy instance"
  value       = var.plex_proxy_enabled ? module.plex_proxy[0].private_ip : null
}

output "plex_proxy_wireguard_public_key" {
  description = "WireGuard public key for the Plex proxy (configure on K8s side)"
  value       = var.plex_proxy_enabled ? module.plex_proxy[0].wireguard_public_key : null
}

output "plex_proxy_wireguard_endpoint" {
  description = "WireGuard endpoint for K8s configuration"
  value       = var.plex_proxy_enabled ? "${oci_core_public_ip.plex_proxy_static[0].ip_address}:51820" : null
}

output "plex_external_url" {
  description = "External Plex URL to configure in Plex settings"
  value       = var.plex_proxy_enabled ? "http://${oci_core_public_ip.plex_proxy_static[0].ip_address}:32400" : null
}

output "plex_proxy_ssh_command" {
  description = "SSH command to connect to the Plex proxy instance"
  value       = var.plex_proxy_enabled ? "ssh ubuntu@${oci_core_public_ip.plex_proxy_static[0].ip_address}" : null
}

output "plex_proxy_static_ip_ocid" {
  description = "OCID of the static reserved public IP (for reference/debugging)"
  value       = var.plex_proxy_enabled ? oci_core_public_ip.plex_proxy_static[0].id : null
}
