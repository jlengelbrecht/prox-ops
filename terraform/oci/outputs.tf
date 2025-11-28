# =============================================================================
# Outputs
# =============================================================================

# Plex Proxy Outputs
output "plex_proxy_public_ip" {
  description = "Public IP of the Plex proxy instance"
  value       = var.plex_proxy_enabled ? module.plex_proxy[0].public_ip : null
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
  value       = var.plex_proxy_enabled ? "${module.plex_proxy[0].public_ip}:51820" : null
}

output "plex_external_url" {
  description = "External Plex URL to configure in Plex settings"
  value       = var.plex_proxy_enabled ? "http://${module.plex_proxy[0].public_ip}:32400" : null
}

output "plex_proxy_ssh_command" {
  description = "SSH command to connect to the Plex proxy instance"
  value       = var.plex_proxy_enabled ? "ssh ubuntu@${module.plex_proxy[0].public_ip}" : null
}
