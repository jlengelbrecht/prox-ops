# =============================================================================
# Talos Kubernetes Cluster on Proxmox - Automated Multi-Node Deployment
# =============================================================================
#
# This configuration deploys a Talos Linux Kubernetes cluster across a 4-node
# Proxmox cluster with full automation:
#   - Automatically creates 8 templates (2 per node: controller + worker)
#   - Deploys VMs to specified nodes using local templates
#   - Includes security hardening (TPM 2.0, Secure Boot, Firewall)
#   - Supports GPU passthrough via PCI resource mappings
#
# Cluster Architecture:
# - 4 Proxmox nodes: Baldar, Heimdall, Odin, Thor
# - 8 VM templates (2 per node for redundancy and flexibility)
# - 3 control plane nodes (high availability)
# - 12 worker nodes (including 2 GPU workers)
# - Total: 15 VMs
#
# Template ID Scheme:
#   Baldar:   9000 (controller), 9001 (worker)
#   Heimdall: 9002 (controller), 9003 (worker)
#   Odin:     9004 (controller), 9005 (worker)
#   Thor:     9006 (controller), 9007 (worker)
#
# Security Features (REQUIRED for DMZ):
# - TPM 2.0 state disk
# - Secure Boot with pre-enrolled keys
# - Firewall enabled on all network devices
# - Using secure-boot compatible Talos images
#
# =============================================================================

# =============================================================================
# Local Configuration
# =============================================================================

locals {
  # Template configurations for all nodes
  # Each Proxmox node gets both a controller and worker template
  templates = {
    # Baldar templates
    baldar_controller = {
      node_name      = "baldar"
      node_host      = split("//", split(":", var.proxmox_endpoints.baldar)[1])[1]
      template_id    = var.template_ids.baldar.controller
      template_name  = "talos-${var.talos_version}-controller-baldar"
      schematic_id   = var.talos_schematic_controlplane
      provider_alias = "baldar"
    }
    baldar_worker = {
      node_name      = "baldar"
      node_host      = split("//", split(":", var.proxmox_endpoints.baldar)[1])[1]
      template_id    = var.template_ids.baldar.worker
      template_name  = "talos-${var.talos_version}-worker-baldar"
      schematic_id   = var.talos_schematic_worker
      provider_alias = "baldar"
    }

    # Heimdall templates
    heimdall_controller = {
      node_name      = "heimdall"
      node_host      = split("//", split(":", var.proxmox_endpoints.heimdall)[1])[1]
      template_id    = var.template_ids.heimdall.controller
      template_name  = "talos-${var.talos_version}-controller-heimdall"
      schematic_id   = var.talos_schematic_controlplane
      provider_alias = "heimdall"
    }
    heimdall_worker = {
      node_name      = "heimdall"
      node_host      = split("//", split(":", var.proxmox_endpoints.heimdall)[1])[1]
      template_id    = var.template_ids.heimdall.worker
      template_name  = "talos-${var.talos_version}-worker-heimdall"
      schematic_id   = var.talos_schematic_worker
      provider_alias = "heimdall"
    }

    # Odin templates
    odin_controller = {
      node_name      = "odin"
      node_host      = split("//", split(":", var.proxmox_endpoints.odin)[1])[1]
      template_id    = var.template_ids.odin.controller
      template_name  = "talos-${var.talos_version}-controller-odin"
      schematic_id   = var.talos_schematic_controlplane
      provider_alias = "odin"
    }
    odin_worker = {
      node_name      = "odin"
      node_host      = split("//", split(":", var.proxmox_endpoints.odin)[1])[1]
      template_id    = var.template_ids.odin.worker
      template_name  = "talos-${var.talos_version}-worker-odin"
      schematic_id   = var.talos_schematic_worker
      provider_alias = "odin"
    }

    # Thor templates
    thor_controller = {
      node_name      = "thor"
      node_host      = split("//", split(":", var.proxmox_endpoints.thor)[1])[1]
      template_id    = var.template_ids.thor.controller
      template_name  = "talos-${var.talos_version}-controller-thor"
      schematic_id   = var.talos_schematic_controlplane
      provider_alias = "thor"
    }
    thor_worker = {
      node_name      = "thor"
      node_host      = split("//", split(":", var.proxmox_endpoints.thor)[1])[1]
      template_id    = var.template_ids.thor.worker
      template_name  = "talos-${var.talos_version}-worker-thor"
      schematic_id   = var.talos_schematic_worker
      provider_alias = "thor"
    }
  }

  # Map provider aliases to the actual provider references
  # This allows dynamic provider selection based on node assignment
  provider_map = {
    baldar   = "proxmox.baldar"
    heimdall = "proxmox.heimdall"
    odin     = "proxmox.odin"
    thor     = "proxmox.thor"
  }
}

# =============================================================================
# Create Talos Templates on All Nodes
# =============================================================================
# Creates 8 templates total (2 per node × 4 nodes)
# Each template is created via SSH provisioners using qm commands
# Templates are created in parallel across all nodes for speed

# NOTE: This module block is commented out because Terraform doesn't support
# dynamic provider selection with for_each. Instead, we use explicit module blocks
# below (lines 163-333) with provider aliases for each template.
#
# module "talos_templates" {
#   source   = "./modules/talos-template"
#   for_each = local.templates
#
#   talos_version    = var.talos_version
#   schematic_id     = each.value.schematic_id
#   template_vm_id   = each.value.template_id
#   template_name    = each.value.template_name
#   proxmox_node     = each.value.node_name
#   proxmox_host     = each.value.node_host
#   proxmox_ssh_user = var.proxmox_ssh_user
#   network_bridge   = var.network_bridge
#   vm_storage_pool  = var.vm_storage_pool
#   cpu_type         = var.cpu_type
#
#   # Security configurations (MANDATORY for DMZ)
#   enable_secure_boot = var.enable_secure_boot
#   enable_tpm         = var.enable_tpm
#   enable_firewall    = var.enable_firewall
# }

# =============================================================================
# Explicit Template Module Blocks with Provider Aliases
# =============================================================================
# Terraform doesn't support dynamic provider selection in for_each,
# so we create explicit module blocks for each template with the correct provider

# =============================================================================
# Sequential Template Deployment
# =============================================================================
# Templates are deployed one at a time using depends_on to avoid parallel
# upload timeouts. Each template waits for the previous one to complete.

# --- Baldar Templates ---
module "template_baldar_controller" {
  source = "./modules/talos-template"
  providers = {
    proxmox = proxmox.baldar
  }

  talos_version      = var.talos_version
  schematic_id       = var.talos_schematic_controlplane
  template_vm_id     = var.template_ids.baldar.controller
  template_name      = "talos-${var.talos_version}-controller-baldar"
  proxmox_node       = "Baldar"
  proxmox_host       = split("//", split(":", var.proxmox_endpoints.baldar)[1])[1]
  proxmox_ssh_user   = var.proxmox_ssh_user
  network_bridge     = var.network_bridge
  vm_storage_pool    = var.vm_storage_pool
  cpu_type           = var.cpu_type
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  # First template - no dependencies
}

module "template_baldar_worker" {
  source = "./modules/talos-template"
  providers = {
    proxmox = proxmox.baldar
  }

  talos_version      = var.talos_version
  schematic_id       = var.talos_schematic_worker
  template_vm_id     = var.template_ids.baldar.worker
  template_name      = "talos-${var.talos_version}-worker-baldar"
  proxmox_node       = "Baldar"
  proxmox_host       = split("//", split(":", var.proxmox_endpoints.baldar)[1])[1]
  proxmox_ssh_user   = var.proxmox_ssh_user
  network_bridge     = var.network_bridge
  vm_storage_pool    = var.vm_storage_pool
  cpu_type           = var.cpu_type
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  # Wait for Baldar controller before deploying worker
  depends_on = [module.template_baldar_controller]
}

# --- Heimdall Templates ---
module "template_heimdall_controller" {
  source = "./modules/talos-template"
  providers = {
    proxmox = proxmox.heimdall
  }

  talos_version      = var.talos_version
  schematic_id       = var.talos_schematic_controlplane
  template_vm_id     = var.template_ids.heimdall.controller
  template_name      = "talos-${var.talos_version}-controller-heimdall"
  proxmox_node       = "Heimdall"
  proxmox_host       = split("//", split(":", var.proxmox_endpoints.heimdall)[1])[1]
  proxmox_ssh_user   = var.proxmox_ssh_user
  network_bridge     = var.network_bridge
  vm_storage_pool    = var.vm_storage_pool
  cpu_type           = var.cpu_type
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  # Wait for Baldar worker before starting
  depends_on = [module.template_baldar_worker]
}

module "template_heimdall_worker" {
  source = "./modules/talos-template"
  providers = {
    proxmox = proxmox.heimdall
  }

  talos_version      = var.talos_version
  schematic_id       = var.talos_schematic_worker
  template_vm_id     = var.template_ids.heimdall.worker
  template_name      = "talos-${var.talos_version}-worker-heimdall"
  proxmox_node       = "Heimdall"
  proxmox_host       = split("//", split(":", var.proxmox_endpoints.heimdall)[1])[1]
  proxmox_ssh_user   = var.proxmox_ssh_user
  network_bridge     = var.network_bridge
  vm_storage_pool    = var.vm_storage_pool
  cpu_type           = var.cpu_type
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  # Wait for Heimdall controller before deploying worker
  depends_on = [module.template_heimdall_controller]
}

# --- Odin Templates ---
module "template_odin_controller" {
  source = "./modules/talos-template"
  providers = {
    proxmox = proxmox.odin
  }

  talos_version      = var.talos_version
  schematic_id       = var.talos_schematic_controlplane
  template_vm_id     = var.template_ids.odin.controller
  template_name      = "talos-${var.talos_version}-controller-odin"
  proxmox_node       = "Odin"
  proxmox_host       = split("//", split(":", var.proxmox_endpoints.odin)[1])[1]
  proxmox_ssh_user   = var.proxmox_ssh_user
  network_bridge     = var.network_bridge
  vm_storage_pool    = var.vm_storage_pool
  cpu_type           = var.cpu_type
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  # Wait for Heimdall worker before starting
  depends_on = [module.template_heimdall_worker]
}

module "template_odin_worker" {
  source = "./modules/talos-template"
  providers = {
    proxmox = proxmox.odin
  }

  talos_version      = var.talos_version
  schematic_id       = var.talos_schematic_worker
  template_vm_id     = var.template_ids.odin.worker
  template_name      = "talos-${var.talos_version}-worker-odin"
  proxmox_node       = "Odin"
  proxmox_host       = split("//", split(":", var.proxmox_endpoints.odin)[1])[1]
  proxmox_ssh_user   = var.proxmox_ssh_user
  network_bridge     = var.network_bridge
  vm_storage_pool    = var.vm_storage_pool
  cpu_type           = var.cpu_type
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  # Wait for Odin controller before deploying worker
  depends_on = [module.template_odin_controller]
}

# --- Thor Templates ---
module "template_thor_controller" {
  source = "./modules/talos-template"
  providers = {
    proxmox = proxmox.thor
  }

  talos_version      = var.talos_version
  schematic_id       = var.talos_schematic_controlplane
  template_vm_id     = var.template_ids.thor.controller
  template_name      = "talos-${var.talos_version}-controller-thor"
  proxmox_node       = "Thor"
  proxmox_host       = split("//", split(":", var.proxmox_endpoints.thor)[1])[1]
  proxmox_ssh_user   = var.proxmox_ssh_user
  network_bridge     = var.network_bridge
  vm_storage_pool    = var.vm_storage_pool
  cpu_type           = var.cpu_type
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  # Wait for Odin worker before starting
  depends_on = [module.template_odin_worker]
}

module "template_thor_worker" {
  source = "./modules/talos-template"
  providers = {
    proxmox = proxmox.thor
  }

  talos_version      = var.talos_version
  schematic_id       = var.talos_schematic_worker
  template_vm_id     = var.template_ids.thor.worker
  template_name      = "talos-${var.talos_version}-worker-thor"
  proxmox_node       = "Thor"
  proxmox_host       = split("//", split(":", var.proxmox_endpoints.thor)[1])[1]
  proxmox_ssh_user   = var.proxmox_ssh_user
  network_bridge     = var.network_bridge
  vm_storage_pool    = var.vm_storage_pool
  cpu_type           = var.cpu_type
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  # Wait for Thor controller before deploying worker (last in chain)
  depends_on = [module.template_thor_controller]
}

# =============================================================================
# Deploy Control Plane Nodes
# =============================================================================
# Deploys 3 control plane VMs using templates from their assigned Proxmox nodes

module "control_plane_nodes" {
  source   = "./modules/talos-node"
  for_each = { for node in var.control_nodes : node.name => node }

  # Determine which template to use based on node assignment
  depends_on = [
    module.template_baldar_controller,
    module.template_heimdall_controller,
    module.template_odin_controller,
    module.template_thor_controller
  ]

  hostname     = each.value.hostname
  vm_id        = each.value.vm_id
  proxmox_node = each.value.proxmox_node

  # Use the controller template from the assigned Proxmox node
  template_vm_id = (
    each.value.proxmox_node == "Baldar" ? var.template_ids.baldar.controller :
    each.value.proxmox_node == "Heimdall" ? var.template_ids.heimdall.controller :
    each.value.proxmox_node == "Odin" ? var.template_ids.odin.controller :
    var.template_ids.thor.controller
  )

  is_controlplane = true
  is_gpu          = false
  mac_address     = each.value.mac_addr
  ip_address      = each.value.ip_address
  network_bridge  = var.network_bridge

  cpu_cores    = var.controlplane_cpu_cores
  cpu_sockets  = var.controlplane_cpu_sockets
  cpu_type     = var.cpu_type
  memory_mb    = var.controlplane_memory_mb
  disk_size_gb = var.controlplane_disk_size_gb

  vm_storage_pool   = var.vm_storage_pool
  auto_start        = true
  enable_ballooning = false
  enable_protection = false

  # Security configs
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  additional_tags = ["talos", "kubernetes", "controlplane"]
}

# =============================================================================
# Deploy Worker Nodes
# =============================================================================
# Deploys 12 worker VMs (including 2 GPU workers) using templates from their assigned nodes

module "worker_nodes" {
  source   = "./modules/talos-node"
  for_each = { for node in var.worker_nodes : node.name => node }

  depends_on = [
    module.template_baldar_worker,
    module.template_heimdall_worker,
    module.template_odin_worker,
    module.template_thor_worker
  ]

  hostname     = each.value.hostname
  vm_id        = each.value.vm_id
  proxmox_node = each.value.proxmox_node

  # Use the worker template from the assigned Proxmox node
  template_vm_id = (
    each.value.proxmox_node == "Baldar" ? var.template_ids.baldar.worker :
    each.value.proxmox_node == "Heimdall" ? var.template_ids.heimdall.worker :
    each.value.proxmox_node == "Odin" ? var.template_ids.odin.worker :
    var.template_ids.thor.worker
  )

  is_controlplane = false
  is_gpu          = each.value.is_gpu
  mac_address     = each.value.mac_addr
  ip_address      = each.value.ip_address
  network_bridge  = var.network_bridge

  # Use GPU resources for GPU nodes, regular worker resources otherwise
  cpu_cores    = each.value.is_gpu ? var.gpu_worker_cpu_cores : var.worker_cpu_cores
  cpu_sockets  = each.value.is_gpu ? var.gpu_worker_cpu_sockets : var.worker_cpu_sockets
  cpu_type     = var.cpu_type
  memory_mb    = each.value.is_gpu ? var.gpu_worker_memory_mb : var.worker_memory_mb
  disk_size_gb = each.value.is_gpu ? var.gpu_worker_disk_size_gb : var.worker_disk_size_gb

  vm_storage_pool   = var.vm_storage_pool
  auto_start        = true
  enable_ballooning = false
  enable_protection = false

  # Security configs
  enable_secure_boot = var.enable_secure_boot
  enable_tpm         = var.enable_tpm
  enable_firewall    = var.enable_firewall

  additional_tags = concat(
    ["talos", "kubernetes", "worker"],
    each.value.is_gpu ? ["gpu", each.value.gpu_model] : []
  )

  # GPU passthrough configuration using PCI resource mappings
  # See: .claude/.ai-docs/openai-deepresearch/GPU_MAPPINGS_PROXMOX.md
  gpu_passthrough_mapping = each.value.is_gpu ? each.value.gpu_mapping : ""
}
