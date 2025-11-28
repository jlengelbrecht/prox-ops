terraform {
  required_version = ">= 1.10.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.18"
    }
  }
}

# OCI Provider Configuration
# Supports two authentication modes:
# 1. Local: Set oci_config_profile (uses ~/.oci/config file)
# 2. CI/CD: Set oci_tenancy_ocid, oci_user_ocid, oci_fingerprint, oci_private_key
provider "oci" {
  # Config file auth (local development)
  config_file_profile = var.oci_private_key == null ? var.oci_config_profile : null

  # Direct auth (CI/CD via environment variables)
  tenancy_ocid = var.oci_tenancy_ocid
  user_ocid    = var.oci_user_ocid
  fingerprint  = var.oci_fingerprint
  private_key  = var.oci_private_key

  region = var.oci_region
}
