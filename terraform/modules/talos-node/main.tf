terraform {
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}

locals {
  # Node description with user's HTML formatting style
  node_desc = <<-EOT
    <div align='center'>
      <a href='https://github.com/jlengelbrecht/repo-resources' target='_blank' rel='noopener noreferrer'>
        <img src='https://raw.githubusercontent.com/jlengelbrecht/repo-resources/main/k8s/talos.png' alt='Logo' style='width:115px;height:128px;'/>
      </a>

      <h2 style='font-size: 24px; margin: 20px 0;'>Talos | ${var.hostname}</h2>

      <p style='margin: 16px 0;'>
        <a href='https://github.com/jlengelbrecht/prox-ops/actions/workflows/renovate.yaml' target='_blank' rel='noopener noreferrer'>
          <img src='https://img.shields.io/github/actions/workflow/status/jlengelbrecht/prox-ops/renovate.yaml?branch=main&label=&logo=renovatebot&style=for-the-badge&color=blue' />
        </a>
      </p>

      <span style='margin: 0 10px;'>
        <i class="fa fa-github fa-fw" style="color: #f5f5f5;"></i>
        <a href='https://github.com/jlengelbrecht/prox-ops' target='_blank' rel='noopener noreferrer' style='text-decoration: none; color: #00617f;'>Prox-Ops</a>
      </span>
      <span style='margin: 0 10px;'>
        <i class="fa fa-comments fa-fw" style="color: #f5f5f5;"></i>
        <a href='https://kubesearch.dev/' target='_blank' rel='noopener noreferrer' style='text-decoration: none; color: #00617f;'>Kubesearch</a>
      </span>
      <span style='margin: 0 10px;'>
        <i class="fa fa-exclamation-circle fa-fw" style="color: #f5f5f5;"></i>
        <a href='https://github.com/jlengelbrecht/prox-ops/issues' target='_blank' rel='noopener noreferrer' style='text-decoration: none; color: #00617f;'>Issues</a>
      </span>
    </div>
  EOT
}

# Clone VM from template with user's proven configuration patterns
resource "proxmox_virtual_environment_vm" "talos_node" {
  name        = var.hostname
  description = local.node_desc
  node_name   = var.proxmox_node
  vm_id       = var.vm_id

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  # CPU Configuration
  cpu {
    cores   = var.cpu_cores
    sockets = var.cpu_sockets
    type    = var.cpu_type
    numa    = true # Enable NUMA for better CPU pinning
  }

  # Memory Configuration - Consolidated dynamic block
  # Controls memory ballooning for Kubernetes (disabled by default)
  memory {
    dedicated = var.memory_mb
  }

  # Disk Configuration
  # Using scsi0 with virtio-scsi-pci (configured in template)
  disk {
    datastore_id = var.vm_storage_pool
    interface    = "scsi0"
    size         = var.disk_size_gb
    iothread     = true
    ssd          = true # emulatessd for better performance
    discard      = "on"
  }

  # Network Configuration
  # ALL interfaces use vmbr1 (10G bond)

  # eth0: Default/native network (no VLAN tag) - Kubernetes cluster communication
  # CRITICAL: MAC address must be preserved (DHCP reservations documented)
  # All nodes (controllers + workers) get eth0 on vmbr1 native VLAN (10.20.66.0/23)
  network_device {
    bridge      = var.network_bridge # vmbr1 (10G bond)
    mac_address = upper(replace(var.mac_address, "-", ":"))
    model       = "virtio"
    firewall    = var.enable_firewall
  }

  # eth1: IoT VLAN 62 - Workers only
  # Used by Multus NetworkAttachmentDefinition (iot-vlan62) for IoT device access
  # Required for: Home Assistant, Zigbee, Z-Wave, IoT workloads
  dynamic "network_device" {
    for_each = var.is_controlplane ? [] : [1]
    content {
      bridge   = var.network_bridge   # vmbr1 (10G bond)
      vlan_id  = var.iot_vlan_tag     # 62
      model    = "virtio"
      firewall = var.enable_firewall
    }
  }

  # eth2: DMZ VLAN 81 - Workers only
  # Used by Multus NetworkAttachmentDefinition (dmz-vlan81) for DMZ workloads
  # Required for: Plex, public-facing services
  dynamic "network_device" {
    for_each = var.is_controlplane ? [] : [1]
    content {
      bridge   = var.network_bridge   # vmbr1 (10G bond)
      vlan_id  = var.dmz_vlan_tag     # 81
      model    = "virtio"
      firewall = var.enable_firewall
    }
  }

  # Boot Configuration
  boot_order = ["scsi0"]

  # BIOS Settings
  bios = "ovmf"

  # EFI Disk - Secure Boot REQUIRES pre-enrolled keys for DMZ
  efi_disk {
    datastore_id      = var.vm_storage_pool
    file_format       = "raw"
    type              = "4m"
    pre_enrolled_keys = var.enable_secure_boot
  }

  # TPM 2.0 State Disk (REQUIRED for Secure Boot)
  dynamic "tpm_state" {
    for_each = var.enable_tpm ? [1] : []
    content {
      datastore_id = var.vm_storage_pool
      version      = "v2.0"
    }
  }

  # Machine type
  machine = "q35"

  # SCSI controller type (required for iothread support)
  scsi_hardware = "virtio-scsi-pci"

  # Agent
  agent {
    enabled = true
    trim    = true
  }

  # Serial console
  serial_device {}

  # VGA settings
  vga {
    type = "serial0"
  }

  # Performance settings
  # Note: Memory configuration is set in the memory block above (lines 61-65)
  # Memory ballooning is controlled via the dedicated parameter

  # GPU passthrough configuration using PCI resource mappings
  # Uses cluster-wide resource mappings for security (no root privileges needed)
  # See: .claude/.ai-docs/openai-deepresearch/GPU_MAPPINGS_PROXMOX.md
  dynamic "hostpci" {
    for_each = var.gpu_passthrough_mapping != "" ? [var.gpu_passthrough_mapping] : []
    content {
      device  = "hostpci0"
      mapping = hostpci.value
      pcie    = true
      rombar  = true
      xvga    = false
    }
  }

  # Legacy GPU passthrough (deprecated - use gpu_passthrough_mapping instead)
  dynamic "hostpci" {
    for_each = var.gpu_passthrough_devices
    content {
      device  = hostpci.value.device
      mapping = hostpci.value.mapping
      pcie    = true
      rombar  = true
      xvga    = false
    }
  }

  # Tags for organization
  tags = concat(
    [var.is_controlplane ? "controlplane" : "worker"],
    var.is_gpu ? ["gpu"] : [],
    var.additional_tags
  )

  # Lifecycle
  lifecycle {
    ignore_changes = [
      # Ignore changes to network device order as Talos manages this
      network_device,
    ]
  }

  # Startup configuration
  started = var.auto_start

  # Migration settings
  migrate = var.enable_migration

  # Protection
  protection = var.enable_protection

  # Timeout settings
  timeout_create      = var.timeout_create
  timeout_clone       = var.timeout_clone
  timeout_migrate     = var.timeout_migrate
  timeout_reboot      = var.timeout_reboot
  timeout_shutdown_vm = var.timeout_shutdown
}

# Output VM information for Talos configuration
output "vm_info" {
  description = "VM information for Talos configuration"
  value = {
    vm_id       = proxmox_virtual_environment_vm.talos_node.vm_id
    name        = proxmox_virtual_environment_vm.talos_node.name
    node_name   = proxmox_virtual_environment_vm.talos_node.node_name
    mac_address = var.mac_address
    ip_address  = var.ip_address
  }
}
