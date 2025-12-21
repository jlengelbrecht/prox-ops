terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 7.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
  }
}
