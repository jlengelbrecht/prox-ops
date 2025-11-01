# =============================================================================
# Talos Kubernetes Cluster on Proxmox - Terraform Configuration
# =============================================================================
#
# This configuration deploys a Talos Linux Kubernetes cluster on Proxmox using
# nocloud images (not ISOs). It creates:
# - 2 VM templates (control plane and worker) with Talos nocloud images
# - 3 control plane nodes
# - 12 worker nodes (including 2 GPU workers)
#
# Prerequisites:
# - Proxmox VE 8.x
# - Terraform >= 1.6
# - SSH access to Proxmox host
# - curl and xz-utils installed locally
#
# =============================================================================

# -----------------------------------------------------------------------------
# Create Talos Templates
# -----------------------------------------------------------------------------

# Control Plane Template
module "talos_template_controlplane" {
  source = "./modules/talos-template"

  talos_version    = var.talos_version
  schematic_id     = var.talos_schematic_controlplane
  template_vm_id   = var.template_vm_id_controlplane
  template_name    = "talos-${var.talos_version}-controlplane"
  proxmox_node     = var.proxmox_node
  proxmox_host     = var.proxmox_endpoint
  proxmox_ssh_user = var.proxmox_ssh_user
  network_bridge   = var.network_bridge
  vm_storage_pool  = var.vm_storage_pool
  cpu_type         = var.cpu_type
}

# Worker Template
module "talos_template_worker" {
  source = "./modules/talos-template"

  talos_version    = var.talos_version
  schematic_id     = var.talos_schematic_worker
  template_vm_id   = var.template_vm_id_worker
  template_name    = "talos-${var.talos_version}-worker"
  proxmox_node     = var.proxmox_node
  proxmox_host     = var.proxmox_endpoint
  proxmox_ssh_user = var.proxmox_ssh_user
  network_bridge   = var.network_bridge
  vm_storage_pool  = var.vm_storage_pool
  cpu_type         = var.cpu_type
}

# -----------------------------------------------------------------------------
# Deploy Control Plane Nodes
# -----------------------------------------------------------------------------

module "control_plane_nodes" {
  source   = "./modules/talos-node"
  for_each = { for node in var.control_nodes : node.name => node }

  depends_on = [module.talos_template_controlplane]

  hostname         = each.value.hostname
  vm_id            = each.value.vm_id
  proxmox_node     = var.proxmox_node
  template_vm_id   = module.talos_template_controlplane.template_id
  is_controlplane  = true
  is_gpu           = false
  mac_address      = each.value.mac_addr
  ip_address       = each.value.ip_address
  network_bridge   = var.network_bridge
  cpu_cores        = var.controlplane_cpu_cores
  cpu_type         = var.cpu_type
  memory_mb        = var.controlplane_memory_mb
  disk_size_gb     = var.controlplane_disk_size_gb
  vm_storage_pool  = var.vm_storage_pool
  auto_start       = true
  enable_ballooning = false
  enable_protection = false
  additional_tags  = ["talos", "kubernetes"]
}

# -----------------------------------------------------------------------------
# Deploy Worker Nodes
# -----------------------------------------------------------------------------

module "worker_nodes" {
  source   = "./modules/talos-node"
  for_each = { for node in var.worker_nodes : node.name => node }

  depends_on = [module.talos_template_worker]

  hostname         = each.value.hostname
  vm_id            = each.value.vm_id
  proxmox_node     = var.proxmox_node
  template_vm_id   = module.talos_template_worker.template_id
  is_controlplane  = false
  is_gpu           = each.value.is_gpu
  mac_address      = each.value.mac_addr
  ip_address       = each.value.ip_address
  network_bridge   = var.network_bridge

  # Use GPU resources for GPU nodes, regular worker resources otherwise
  cpu_cores        = each.value.is_gpu ? var.gpu_worker_cpu_cores : var.worker_cpu_cores
  cpu_type         = var.cpu_type
  memory_mb        = each.value.is_gpu ? var.gpu_worker_memory_mb : var.worker_memory_mb
  disk_size_gb     = each.value.is_gpu ? var.gpu_worker_disk_size_gb : var.worker_disk_size_gb
  vm_storage_pool  = var.vm_storage_pool

  auto_start       = true
  enable_ballooning = false
  enable_protection = false

  additional_tags  = concat(
    ["talos", "kubernetes"],
    each.value.is_gpu ? ["gpu", each.value.gpu_model] : []
  )

  # GPU passthrough configuration (requires manual setup in Proxmox)
  # Uncomment and configure after identifying GPU device IDs
  # gpu_passthrough_devices = each.value.is_gpu ? [
  #   {
  #     device  = "hostpci0"
  #     mapping = "gpu-${each.value.gpu_model}"
  #   }
  # ] : []
}
