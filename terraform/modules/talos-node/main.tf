terraform {
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
  }
}

# Clone VM from template
resource "proxmox_virtual_environment_vm" "talos_node" {
  name        = var.hostname
  description = "Talos Linux ${var.is_controlplane ? "Control Plane" : "Worker"} Node"
  node_name   = var.proxmox_node
  vm_id       = var.vm_id

  clone {
    vm_id = var.template_vm_id
    full  = true
  }

  # CPU Configuration
  cpu {
    cores = var.cpu_cores
    type  = var.cpu_type
  }

  # Memory Configuration
  memory {
    dedicated = var.memory_mb
  }

  # Disk Configuration
  disk {
    datastore_id = var.vm_storage_pool
    interface    = "scsi0"
    size         = var.disk_size_gb
    iothread     = true
    ssd          = true
    discard      = "on"
  }

  # Network Configuration
  network_device {
    bridge      = var.network_bridge
    mac_address = upper(replace(var.mac_address, "-", ":"))
    model       = "virtio"
  }

  # Boot Configuration
  boot_order = ["scsi0"]

  # BIOS Settings
  bios = "ovmf"

  # EFI Disk
  efi_disk {
    datastore_id      = var.vm_storage_pool
    file_format       = "raw"
    type              = "4m"
    pre_enrolled_keys = false
  }

  # Machine type
  machine = "q35"

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
  # Disable memory ballooning for Kubernetes
  dynamic "memory" {
    for_each = var.enable_ballooning ? [] : [1]
    content {
      dedicated = var.memory_mb
    }
  }

  # GPU passthrough configuration for GPU nodes
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
  timeout_create  = var.timeout_create
  timeout_clone   = var.timeout_clone
  timeout_migrate = var.timeout_migrate
  timeout_reboot  = var.timeout_reboot
  timeout_shutdown_vm = var.timeout_shutdown
}

# Output VM information for Talos configuration
output "vm_info" {
  description = "VM information for Talos configuration"
  value = {
    vm_id      = proxmox_virtual_environment_vm.talos_node.vm_id
    name       = proxmox_virtual_environment_vm.talos_node.name
    node_name  = proxmox_virtual_environment_vm.talos_node.node_name
    mac_address = var.mac_address
    ip_address = var.ip_address
  }
}
