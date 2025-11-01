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

provider "proxmox" {
  endpoint = var.proxmox_endpoint
  username = var.proxmox_username
  password = var.proxmox_password
  insecure = var.proxmox_insecure

  ssh {
    agent    = true
    username = var.proxmox_ssh_user
  }
}
