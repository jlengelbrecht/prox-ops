terraform {
  required_version = ">= 1.6.0"

  required_providers {
    proxmox = {
      source  = "bpg/proxmox"
      version = "~> 0.67.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }

  # Optional: Use S3-compatible backend for state management
  # backend "s3" {
  #   bucket                      = "terraform-state"
  #   key                         = "prox-ops/talos-cluster.tfstate"
  #   region                      = "us-east-1"
  #   endpoint                    = "https://s3.your-domain.com"
  #   skip_credentials_validation = true
  #   skip_region_validation      = true
  #   skip_metadata_api_check     = true
  #   force_path_style            = true
  # }
}

# =============================================================================
# Multi-Provider Configuration for 4-Node Proxmox Cluster
# =============================================================================
# Each Proxmox node gets its own provider instance to enable parallel template
# creation and VM deployment across the cluster. This allows templates to be
# created locally on each node simultaneously.

# Default provider (Baldar) - used when no alias specified
provider "proxmox" {
  endpoint  = var.proxmox_endpoints.baldar
  api_token = "${var.proxmox_username}=${var.proxmox_password}"
  insecure  = var.proxmox_insecure

  ssh {
    agent    = true
    username = var.proxmox_ssh_user
  }
}

# Baldar node provider (explicit alias for clarity)
provider "proxmox" {
  alias     = "baldar"
  endpoint  = var.proxmox_endpoints.baldar
  api_token = "${var.proxmox_username}=${var.proxmox_password}"
  insecure  = var.proxmox_insecure

  ssh {
    agent    = true
    username = var.proxmox_ssh_user
  }
}

# Heimdall node provider
provider "proxmox" {
  alias     = "heimdall"
  endpoint  = var.proxmox_endpoints.heimdall
  api_token = "${var.proxmox_username}=${var.proxmox_password}"
  insecure  = var.proxmox_insecure

  ssh {
    agent    = true
    username = var.proxmox_ssh_user
  }
}

# Odin node provider
provider "proxmox" {
  alias     = "odin"
  endpoint  = var.proxmox_endpoints.odin
  api_token = "${var.proxmox_username}=${var.proxmox_password}"
  insecure  = var.proxmox_insecure

  ssh {
    agent    = true
    username = var.proxmox_ssh_user
  }
}

# Thor node provider
provider "proxmox" {
  alias     = "thor"
  endpoint  = var.proxmox_endpoints.thor
  api_token = "${var.proxmox_username}=${var.proxmox_password}"
  insecure  = var.proxmox_insecure

  ssh {
    agent    = true
    username = var.proxmox_ssh_user
  }
}
