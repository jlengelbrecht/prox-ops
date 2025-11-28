terraform {
  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.18"
    }
    local = {
      source  = "hashicorp/local"
      version = ">= 2.0"
    }
  }
}
