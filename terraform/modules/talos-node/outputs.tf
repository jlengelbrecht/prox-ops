output "vm_id" {
  description = "The ID of the VM"
  value       = proxmox_virtual_environment_vm.talos_node.vm_id
}

output "vm_name" {
  description = "The name of the VM"
  value       = proxmox_virtual_environment_vm.talos_node.name
}

output "mac_address" {
  description = "The MAC address of the VM"
  value       = var.mac_address
}

output "ip_address" {
  description = "The IP address assigned to the VM"
  value       = var.ip_address
}

output "node_name" {
  description = "The Proxmox node where the VM is located"
  value       = proxmox_virtual_environment_vm.talos_node.node_name
}

output "is_controlplane" {
  description = "Whether this is a control plane node"
  value       = var.is_controlplane
}

output "is_gpu" {
  description = "Whether this is a GPU node"
  value       = var.is_gpu
}
