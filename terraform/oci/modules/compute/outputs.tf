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
  description = "Public IP address of the instance"
  value       = oci_core_instance.main.public_ip
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
  description = "WireGuard public key for this instance"
  value       = var.enable_wireguard ? trimspace(data.local_file.wg_public_key[0].content) : null
}
