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
  # Talos image configuration
  # Download from factory.talos.dev using schematic ID
  image_url      = "https://factory.talos.dev/image/${var.schematic_id}/v${var.talos_version}/nocloud-amd64.raw.xz"
  image_filename = "${var.schematic_id}.raw.xz"
  image_path     = "/tmp/talos-images/${local.image_filename}"
  raw_image_path = "/tmp/talos-images/${var.schematic_id}.raw"

  # Template naming
  template_desc  = "Talos ${var.talos_version} - Schematic: ${var.schematic_id}"
}

# Download and decompress Talos nocloud image
# Inspired by TechDufus approach with rsync for reliability
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

      # Download image if not already present (idempotent)
      if [ ! -f "${local.raw_image_path}" ]; then
        echo "[1/3] Downloading Talos ${var.talos_version} image for schematic ${var.schematic_id}..."
        curl -fsSL --progress-bar "${local.image_url}" -o "${local.image_path}"

        echo "[2/3] Decompressing image (this may take a few minutes)..."
        xz -d -k -f "${local.image_path}"

        echo "[3/3] Image ready: ${local.raw_image_path}"
        ls -lh "${local.raw_image_path}"
      else
        echo "Image already exists: ${local.raw_image_path}"
        ls -lh "${local.raw_image_path}"
      fi
    EOT
  }
}

# Upload image to Proxmox and create template
# Combines TechDufus SSH approach with user's proven Proxmox patterns
resource "null_resource" "create_template" {
  depends_on = [null_resource.download_talos_image]

  triggers = {
    talos_version    = var.talos_version
    schematic_id     = var.schematic_id
    vm_id            = var.template_vm_id
    proxmox_node     = var.proxmox_node
    proxmox_host     = var.proxmox_host
    proxmox_ssh_user = var.proxmox_ssh_user
    image_checksum   = "${var.talos_version}-${var.schematic_id}"
  }

  # Clean up existing template and prepare directories
  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = var.proxmox_host
      user        = var.proxmox_ssh_user
      private_key = file("~/.ssh/proxmox_terraform")
    }

    inline = [
      "echo '[Template Creation] Step 1: Cleaning up existing template if present...'",
      "qm destroy ${var.template_vm_id} || true",
      "mkdir -p /var/lib/vz/template/talos",
      "echo '[Template Creation] Step 1: Complete'",
    ]
  }

  # Upload the raw disk image with retry logic
  # Uses SCP with 3 retry attempts to handle transient network issues
  # during large file transfers (1.7GB Talos images)
  provisioner "local-exec" {
    command = <<-EOT
      set -e

      echo "[Template Upload] Uploading ${var.schematic_id}.raw to ${var.proxmox_host}..."
      echo "[Template Upload] File size: $(du -h ${local.raw_image_path} | cut -f1)"

      # Retry logic: 3 attempts with 10 second delays
      for attempt in {1..3}; do
        echo "[Template Upload] Attempt $attempt of 3..."

        if scp -i ~/.ssh/proxmox_terraform \
               -o StrictHostKeyChecking=no \
               -o UserKnownHostsFile=/dev/null \
               -o ServerAliveInterval=30 \
               -o ServerAliveCountMax=3 \
               ${local.raw_image_path} \
               ${var.proxmox_ssh_user}@${var.proxmox_host}:/var/lib/vz/template/talos/${var.schematic_id}.raw; then
          echo "[Template Upload] ✓ Upload successful on attempt $attempt"
          exit 0
        else
          if [ $attempt -lt 3 ]; then
            echo "[Template Upload] ✗ Upload failed, retrying in 10 seconds..."
            sleep 10
          else
            echo "[Template Upload] ✗ Upload failed after 3 attempts"
            exit 1
          fi
        fi
      done
    EOT
  }

  # Create VM template with settings from user's existing configurations
  provisioner "remote-exec" {
    connection {
      type        = "ssh"
      host        = var.proxmox_host
      user        = var.proxmox_ssh_user
      private_key = file("~/.ssh/proxmox_terraform")
    }

    inline = [
      "echo '[Template Creation] Step 2: Creating base VM...'",
      # Create base VM with minimal resources (will be overridden by clones)
      # CRITICAL: --machine q35 MUST be set during creation (cannot be changed after)
      "qm create ${var.template_vm_id} --name ${var.template_name} --machine q35 --memory 2048 --cores 2 --net0 virtio,bridge=${var.network_bridge}",

      "echo '[Template Creation] Step 3: Importing disk...'",
      # Import the raw disk image
      "qm importdisk ${var.template_vm_id} /var/lib/vz/template/talos/${var.schematic_id}.raw ${var.vm_storage_pool}",

      "echo '[Template Creation] Step 4: Configuring VM...'",
      # Attach disk with virtio-scsi (better performance, matches user pattern)
      "qm set ${var.template_vm_id} --scsihw virtio-scsi-pci --scsi0 ${var.vm_storage_pool}:vm-${var.template_vm_id}-disk-0",

      # Configure boot order
      "qm set ${var.template_vm_id} --boot order=scsi0",

      # Set BIOS to OVMF (UEFI) - required for Talos
      "qm set ${var.template_vm_id} --bios ovmf",

      # Add EFI disk - Secure Boot REQUIRES pre-enrolled keys for DMZ security
      "qm set ${var.template_vm_id} --efidisk0 ${var.vm_storage_pool}:0,efitype=4m,pre-enrolled-keys=${var.enable_secure_boot ? 1 : 0}",

      # Add TPM 2.0 state disk (REQUIRED for Secure Boot)
      var.enable_tpm ? "qm set ${var.template_vm_id} --tpmstate0 ${var.vm_storage_pool}:1,version=v2.0" : "echo 'TPM disabled'",

      # Configure CPU type (host provides best performance and feature support)
      "qm set ${var.template_vm_id} --cpu ${var.cpu_type}",

      # Configure serial console for Talos dashboard access
      "qm set ${var.template_vm_id} --serial0 socket --vga serial0",

      # Enable QEMU guest agent with fstrim for cloned disks
      "qm set ${var.template_vm_id} --agent enabled=1,fstrim_cloned_disks=1",

      # Disable memory ballooning (critical for Kubernetes stability)
      "qm set ${var.template_vm_id} --balloon 0",

      # Enable NUMA for better CPU pinning and performance
      "qm set ${var.template_vm_id} --numa 1",

      # Enable firewall on network device (REQUIRED for DMZ security)
      var.enable_firewall ? "qm set ${var.template_vm_id} --ipconfig0 ip=dhcp,firewall=1" : "echo 'Firewall disabled'",

      # Set description
      "qm set ${var.template_vm_id} --description '${local.template_desc}'",

      "echo '[Template Creation] Step 5: Converting to template...'",
      # Convert VM to template (makes it read-only and clonable)
      "qm template ${var.template_vm_id}",

      "echo '[Template Creation] Complete: ${var.template_name} (ID: ${var.template_vm_id})'"
    ]
  }

  # Cleanup on destroy
  provisioner "remote-exec" {
    when = destroy

    connection {
      type        = "ssh"
      host        = self.triggers.proxmox_host
      user        = self.triggers.proxmox_ssh_user
      private_key = file("~/.ssh/proxmox_terraform")
    }

    inline = [
      "echo '[Template Cleanup] Removing template ${self.triggers.vm_id}...'",
      "qm destroy ${self.triggers.vm_id} || true",
      "echo '[Template Cleanup] Complete'"
    ]
  }
}
