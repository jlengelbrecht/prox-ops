# =============================================================================
# OCI Compute Module
# =============================================================================
#
# Generic compute instance module for Oracle Cloud Infrastructure.
# Supports:
#   - Flex shapes (ARM A1.Flex for Always Free tier)
#   - WireGuard configuration via cloud-init
#   - Port forwarding for NAT proxy use cases
#
# =============================================================================

# =============================================================================
# Data Sources
# =============================================================================

data "oci_core_images" "ubuntu" {
  compartment_id           = var.compartment_id
  operating_system         = var.os_image
  operating_system_version = var.os_version
  shape                    = var.shape
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# =============================================================================
# WireGuard Key Management
# =============================================================================
# Supports two modes:
# 1. Static key: Pass wg_private_key AND wg_public_key for persistent keys across VPS recreation
#    (No wg binary required - public key is pre-computed and passed as variable)
# 2. Dynamic key: Generate new keys if wg_private_key is not provided (legacy behavior)
#
# For one-click deployment, use static keys stored in GitHub Secrets.
# =============================================================================

# Determine if we're using static keys (preferred for one-click deploy)
# Requires BOTH private and public key to avoid needing wg binary
locals {
  use_static_wg_key = var.enable_wireguard && var.wg_private_key != "" && var.wg_public_key != ""
}

# Dynamic key generation (only when static keys NOT provided)
resource "terraform_data" "wireguard_keys" {
  count = var.enable_wireguard && !local.use_static_wg_key ? 1 : 0

  provisioner "local-exec" {
    command = <<-EOT
      mkdir -p ${path.module}/.keys
      wg genkey > ${path.module}/.keys/${var.instance_name}-private.key
      cat ${path.module}/.keys/${var.instance_name}-private.key | wg pubkey > ${path.module}/.keys/${var.instance_name}-public.key
    EOT
  }
}

data "local_file" "wg_private_key" {
  count      = var.enable_wireguard && !local.use_static_wg_key ? 1 : 0
  filename   = "${path.module}/.keys/${var.instance_name}-private.key"
  depends_on = [terraform_data.wireguard_keys]
}

data "local_file" "wg_public_key" {
  count      = var.enable_wireguard && !local.use_static_wg_key ? 1 : 0
  filename   = "${path.module}/.keys/${var.instance_name}-public.key"
  depends_on = [terraform_data.wireguard_keys]
}

# Unified key references for use elsewhere in the module
# When using static keys, both private and public are passed as variables (no wg binary needed)
locals {
  wg_private_key = var.enable_wireguard ? (
    local.use_static_wg_key
    ? var.wg_private_key
    : trimspace(data.local_file.wg_private_key[0].content)
  ) : ""

  wg_public_key = var.enable_wireguard ? (
    local.use_static_wg_key
    ? var.wg_public_key  # Use provided public key directly (no wg binary needed)
    : trimspace(data.local_file.wg_public_key[0].content)
  ) : ""
}

# =============================================================================
# Networking
# =============================================================================

resource "oci_core_vcn" "main" {
  compartment_id = var.compartment_id
  display_name   = "${var.instance_name}-vcn"
  cidr_blocks    = [var.vcn_cidr]
  dns_label      = replace(var.instance_name, "-", "")

  freeform_tags = var.freeform_tags
}

resource "oci_core_internet_gateway" "main" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.instance_name}-igw"
  enabled        = true

  freeform_tags = var.freeform_tags
}

resource "oci_core_route_table" "main" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.instance_name}-rt"

  route_rules {
    network_entity_id = oci_core_internet_gateway.main.id
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
  }

  freeform_tags = var.freeform_tags
}

resource "oci_core_security_list" "main" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.main.id
  display_name   = "${var.instance_name}-sl"

  # Egress: Allow all outbound
  egress_security_rules {
    protocol    = "all"
    destination = "0.0.0.0/0"
  }

  # Ingress: SSH
  dynamic "ingress_security_rules" {
    for_each = var.ssh_allowed_cidrs
    content {
      protocol = "6" # TCP
      source   = ingress_security_rules.value
      tcp_options {
        min = 22
        max = 22
      }
    }
  }

  # Ingress: WireGuard (if enabled) - restricted to specific CIDRs
  dynamic "ingress_security_rules" {
    for_each = var.enable_wireguard ? var.wg_peer_allowed_cidrs : []
    content {
      protocol = "17" # UDP
      source   = ingress_security_rules.value
      udp_options {
        min = var.wg_listen_port
        max = var.wg_listen_port
      }
    }
  }

  # Ingress: HTTPS (443) from Cloudflare IPs only - for nginx reverse proxy
  # This significantly reduces attack surface by only allowing Cloudflare CDN traffic
  dynamic "ingress_security_rules" {
    for_each = var.enable_nginx_proxy ? var.cloudflare_ipv4_ranges : []
    content {
      protocol = "6" # TCP
      source   = ingress_security_rules.value
      tcp_options {
        min = 443
        max = 443
      }
    }
  }

  # Ingress: HTTP (80) from Cloudflare IPs only - for HTTP to HTTPS redirect
  # Must also be restricted to prevent bypassing Cloudflare protection
  dynamic "ingress_security_rules" {
    for_each = var.enable_nginx_proxy ? var.cloudflare_ipv4_ranges : []
    content {
      protocol = "6" # TCP
      source   = ingress_security_rules.value
      tcp_options {
        min = 80
        max = 80
      }
    }
  }

  # Ingress: Additional ports
  dynamic "ingress_security_rules" {
    for_each = [for p in var.additional_ingress_ports : p if p.protocol == "tcp"]
    content {
      protocol = "6" # TCP
      source   = "0.0.0.0/0"
      tcp_options {
        min = ingress_security_rules.value.port
        max = ingress_security_rules.value.port
      }
    }
  }

  dynamic "ingress_security_rules" {
    for_each = [for p in var.additional_ingress_ports : p if p.protocol == "udp"]
    content {
      protocol = "17" # UDP
      source   = "0.0.0.0/0"
      udp_options {
        min = ingress_security_rules.value.port
        max = ingress_security_rules.value.port
      }
    }
  }

  freeform_tags = var.freeform_tags
}

resource "oci_core_subnet" "main" {
  compartment_id    = var.compartment_id
  vcn_id            = oci_core_vcn.main.id
  cidr_block        = var.subnet_cidr
  display_name      = "${var.instance_name}-subnet"
  dns_label         = "subnet"
  route_table_id    = oci_core_route_table.main.id
  security_list_ids = [oci_core_security_list.main.id]

  freeform_tags = var.freeform_tags
}

# =============================================================================
# Compute Instance
# =============================================================================

resource "oci_core_instance" "main" {
  availability_domain = var.availability_domain
  compartment_id      = var.compartment_id
  display_name        = var.instance_name
  shape               = var.shape

  # shape_config only for Flex shapes
  dynamic "shape_config" {
    for_each = can(regex("Flex$", var.shape)) ? [1] : []
    content {
      ocpus         = var.ocpus
      memory_in_gbs = var.memory_gb
    }
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu.images[0].id
    boot_volume_size_in_gbs = var.boot_volume_gb
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.main.id
    # Don't auto-assign public IP if using reserved IP
    assign_public_ip = var.use_reserved_public_ip ? false : true
    display_name     = "${var.instance_name}-vnic"
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data = base64encode(templatefile("${path.module}/templates/cloud-init.yaml.tpl", {
      hostname             = var.instance_name
      enable_wireguard     = var.enable_wireguard
      wg_private_key       = local.wg_private_key
      wg_address           = var.wg_address
      wg_listen_port       = var.wg_listen_port
      wg_peer_public_key   = var.wg_peer_public_key
      wg_peer_allowed_ips  = "${var.wg_peer_allowed_ips},${var.wg_forward_target_ip}/32"
      wg_forward_port      = var.wg_forward_port
      wg_forward_target_ip = var.wg_forward_target_ip
      # Nginx reverse proxy configuration
      enable_nginx_proxy   = var.enable_nginx_proxy
      nginx_server_name    = var.nginx_server_name
      nginx_origin_cert    = var.nginx_origin_cert
      nginx_origin_key     = var.nginx_origin_key
      nginx_backend_url    = var.nginx_backend_url
    }))
  }

  freeform_tags = var.freeform_tags

  lifecycle {
    ignore_changes = [
      source_details[0].source_id, # Ignore image updates
    ]
  }
}

# =============================================================================
# Reserved Public IP - Data Sources
# =============================================================================
# The module exposes private_ip_id for external reserved IP management.
# Two modes are supported:
#   1. External management (recommended): Reserved IP created at root level with
#      prevent_destroy=true. The IP persists across module destruction. Pass both
#      use_reserved_public_ip=true and external_reserved_public_ip_id="<any-value>"
#      to enable private IP lookup and prevent module from creating its own reserved IP.
#   2. Module-managed (legacy): Reserved IP created in module with
#      use_reserved_public_ip=true. Destroyed when module is destroyed.
#
# For one-click deployment, use external management at root level.

locals {
  # Determine if we need to look up private IP info
  use_any_reserved_ip = var.use_reserved_public_ip || var.external_reserved_public_ip_id != ""
}

# Get the VNIC attachment for the instance
data "oci_core_vnic_attachments" "main" {
  count          = local.use_any_reserved_ip ? 1 : 0
  compartment_id = var.compartment_id
  instance_id    = oci_core_instance.main.id
}

# Get the VNIC details
data "oci_core_vnic" "main" {
  count   = local.use_any_reserved_ip ? 1 : 0
  vnic_id = data.oci_core_vnic_attachments.main[0].vnic_attachments[0].vnic_id
}

# Get the primary private IP of the VNIC
data "oci_core_private_ips" "main" {
  count   = local.use_any_reserved_ip ? 1 : 0
  vnic_id = data.oci_core_vnic.main[0].id
}

# Module-managed Reserved Public IP (legacy mode - destroyed with module)
# Only used when external_reserved_public_ip_id is NOT provided
resource "oci_core_public_ip" "reserved" {
  count          = var.use_reserved_public_ip && var.external_reserved_public_ip_id == "" ? 1 : 0
  compartment_id = var.compartment_id
  lifetime       = "RESERVED"
  display_name   = "${var.instance_name}-reserved-ip"
  private_ip_id  = data.oci_core_private_ips.main[0].private_ips[0].id

  freeform_tags = var.freeform_tags

  lifecycle {
    prevent_destroy = false
  }
}
