# =============================================================================
# Terraform Outputs
# =============================================================================

# -----------------------------------------------------------------------------
# Template Information
# -----------------------------------------------------------------------------

output "template_ids" {
  description = "All template VM IDs across all nodes"
  value = {
    baldar = {
      controller = var.template_ids.baldar.controller
      worker     = var.template_ids.baldar.worker
    }
    heimdall = {
      controller = var.template_ids.heimdall.controller
      worker     = var.template_ids.heimdall.worker
    }
    odin = {
      controller = var.template_ids.odin.controller
      worker     = var.template_ids.odin.worker
    }
    thor = {
      controller = var.template_ids.thor.controller
      worker     = var.template_ids.thor.worker
    }
  }
}

# -----------------------------------------------------------------------------
# Control Plane Nodes
# -----------------------------------------------------------------------------

output "control_plane_nodes" {
  description = "Control plane node information"
  value = {
    for name, node in module.control_plane_nodes : name => {
      vm_id       = node.vm_id
      vm_name     = node.vm_name
      ip_address  = node.ip_address
      mac_address = node.mac_address
      node_name   = node.node_name
    }
  }
}

# -----------------------------------------------------------------------------
# Worker Nodes
# -----------------------------------------------------------------------------

output "worker_nodes" {
  description = "Worker node information"
  value = {
    for name, node in module.worker_nodes : name => {
      vm_id       = node.vm_id
      vm_name     = node.vm_name
      ip_address  = node.ip_address
      mac_address = node.mac_address
      node_name   = node.node_name
      is_gpu      = node.is_gpu
    }
  }
}

# -----------------------------------------------------------------------------
# GPU Worker Nodes
# -----------------------------------------------------------------------------

output "gpu_worker_nodes" {
  description = "GPU worker node information"
  value = {
    for name, node in module.worker_nodes : name => {
      vm_id       = node.vm_id
      vm_name     = node.vm_name
      ip_address  = node.ip_address
      mac_address = node.mac_address
      node_name   = node.node_name
    } if node.is_gpu
  }
}

# -----------------------------------------------------------------------------
# Cluster Summary
# -----------------------------------------------------------------------------

output "cluster_summary" {
  description = "Cluster deployment summary"
  value = {
    total_nodes         = length(var.control_nodes) + length(var.worker_nodes)
    control_plane_count = length(var.control_nodes)
    worker_count        = length(var.worker_nodes)
    gpu_worker_count    = length([for node in var.worker_nodes : node if node.is_gpu])
    talos_version       = var.talos_version
    proxmox_nodes       = ["Baldar", "Heimdall", "Odin", "Thor"]
  }
}

# -----------------------------------------------------------------------------
# Next Steps Information
# -----------------------------------------------------------------------------

output "next_steps" {
  description = "Next steps after Terraform deployment"
  value       = <<-EOT

  Terraform deployment complete!

  Next steps:

  1. Wait for all VMs to finish cloning (check Proxmox web UI)

  2. Apply Talos configuration to all nodes:
     cd /home/devbox/repos/jlengelbrecht/prox-ops
     task bootstrap:talos

  3. Bootstrap the Kubernetes cluster:
     talosctl bootstrap --nodes 10.20.67.1

  4. Get kubeconfig:
     talosctl kubeconfig --nodes 10.20.67.1

  5. Deploy applications:
     task bootstrap:apps

  For more details, see: /home/devbox/repos/jlengelbrecht/prox-ops/TERRAFORM_MIGRATION_GUIDE.md

  EOT
}
