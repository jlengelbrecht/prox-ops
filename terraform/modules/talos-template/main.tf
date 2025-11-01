terraform {
  required_providers {
    proxmox = {
      source = "bpg/proxmox"
    }
    null = {
      source = "hashicorp/null"
    }
  }
}

locals {
  image_url      = "https://github.com/siderolabs/talos/releases/download/v${var.talos_version}/${var.schematic_id}.raw.xz"
  image_filename = "${var.schematic_id}.raw.xz"
  image_path     = "/tmp/talos-images/${local.image_filename}"
  raw_image_path = "/tmp/talos-images/${var.schematic_id}.raw"
}

# Download and decompress Talos nocloud image
resource "null_resource" "download_talos_image" {
  triggers = {
    talos_version  = var.talos_version
    schematic_id   = var.schematic_id
    image_checksum = "${var.talos_version}-${var.schematic_id}"
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      mkdir -p /tmp/talos-images

      # Download image if not already present
      if [ ! -f "${local.raw_image_path}" ]; then
        echo "Downloading Talos image for schematic ${var.schematic_id}..."
        curl -fsSL "${local.image_url}" -o "${local.image_path}"

        echo "Decompressing image..."
        xz -d -k "${local.image_path}"

        echo "Image ready: ${local.raw_image_path}"
        ls -lh "${local.raw_image_path}"
      else
        echo "Image already exists: ${local.raw_image_path}"
      fi
    EOT
  }
}

# Upload image to Proxmox and create template
resource "null_resource" "create_template" {
  depends_on = [null_resource.download_talos_image]

  triggers = {
    talos_version  = var.talos_version
    schematic_id   = var.schematic_id
    vm_id          = var.template_vm_id
    proxmox_node   = var.proxmox_node
    image_checksum = "${var.talos_version}-${var.schematic_id}"
  }

  # Upload image via SSH and create template
  provisioner "remote-exec" {
    connection {
      type = "ssh"
      host = var.proxmox_host
      user = var.proxmox_ssh_user
    }

    inline = [
      # Remove existing template if it exists
      "qm destroy ${var.template_vm_id} || true",

      # Create directory for images
      "mkdir -p /var/lib/vz/template/talos",
    ]
  }

  # Upload the raw disk image
  provisioner "file" {
    connection {
      type = "ssh"
      host = var.proxmox_host
      user = var.proxmox_ssh_user
    }

    source      = local.raw_image_path
    destination = "/var/lib/vz/template/talos/${var.schematic_id}.raw"
  }

  # Create VM template
  provisioner "remote-exec" {
    connection {
      type = "ssh"
      host = var.proxmox_host
      user = var.proxmox_ssh_user
    }

    inline = [
      # Create base VM
      "qm create ${var.template_vm_id} --name ${var.template_name} --memory 2048 --cores 2 --net0 virtio,bridge=${var.network_bridge}",

      # Import disk
      "qm importdisk ${var.template_vm_id} /var/lib/vz/template/talos/${var.schematic_id}.raw ${var.vm_storage_pool}",

      # Attach disk to VM
      "qm set ${var.template_vm_id} --scsihw virtio-scsi-pci --scsi0 ${var.vm_storage_pool}:vm-${var.template_vm_id}-disk-0",

      # Configure boot settings
      "qm set ${var.template_vm_id} --boot order=scsi0",

      # Set BIOS to OVMF (UEFI)
      "qm set ${var.template_vm_id} --bios ovmf",

      # Add EFI disk
      "qm set ${var.template_vm_id} --efidisk0 ${var.vm_storage_pool}:0,efitype=4m,pre-enrolled-keys=0",

      # Configure CPU type
      "qm set ${var.template_vm_id} --cpu ${var.cpu_type}",

      # Configure serial console
      "qm set ${var.template_vm_id} --serial0 socket --vga serial0",

      # Set agent
      "qm set ${var.template_vm_id} --agent enabled=1,fstrim_cloned_disks=1",

      # Disable memory ballooning (recommended for Kubernetes)
      "qm set ${var.template_vm_id} --balloon 0",

      # Convert to template
      "qm template ${var.template_vm_id}",

      "echo 'Template ${var.template_name} (ID: ${var.template_vm_id}) created successfully'"
    ]
  }

  # Cleanup on destroy
  provisioner "remote-exec" {
    when = destroy

    connection {
      type = "ssh"
      host = self.triggers.proxmox_node
      user = var.proxmox_ssh_user
    }

    inline = [
      "qm destroy ${self.triggers.vm_id} || true",
      "echo 'Template ${self.triggers.vm_id} removed'"
    ]
  }
}
