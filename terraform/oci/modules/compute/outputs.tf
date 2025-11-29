# =============================================================================
# Outputs
# =============================================================================

output "instance_id" {
  description = "OCID of the compute instance"
  value       = oci_core_instance.main.id
}

output "instance_name" {
  description = "Name of the compute instance"
  value       = oci_core_instance.main.display_name
}

output "public_ip" {
  description = "Public IP address of the instance (reserved or ephemeral)"
  value       = var.use_reserved_public_ip ? oci_core_public_ip.reserved[0].ip_address : oci_core_instance.main.public_ip
}

output "reserved_public_ip_id" {
  description = "OCID of the reserved public IP (if using reserved IP)"
  value       = var.use_reserved_public_ip ? oci_core_public_ip.reserved[0].id : null
}

output "private_ip" {
  description = "Private IP address of the instance"
  value       = oci_core_instance.main.private_ip
}

output "availability_domain" {
  description = "Availability domain of the instance"
  value       = oci_core_instance.main.availability_domain
}

output "vcn_id" {
  description = "OCID of the VCN"
  value       = oci_core_vcn.main.id
}

output "subnet_id" {
  description = "OCID of the subnet"
  value       = oci_core_subnet.main.id
}

output "wireguard_public_key" {
  description = "WireGuard public key for this instance (static if wg_private_key provided)"
  # Use nonsensitive() because while local.wg_public_key inherits sensitivity from
  # local.use_static_wg_key (which checks wg_private_key != ""), the public key itself
  # is NOT sensitive - it's meant to be shared with peers.
  value = var.enable_wireguard ? nonsensitive(local.wg_public_key) : null
}
