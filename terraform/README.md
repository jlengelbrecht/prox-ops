# Terraform Infrastructure for Talos Kubernetes on Proxmox

This directory contains Terraform infrastructure-as-code for deploying Talos Linux Kubernetes cluster on Proxmox using nocloud images.

## Quick Start

```bash
# 1. Create configuration from example
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars  # Fill in your Proxmox details

# 2. Run deployment script
./deploy.sh check   # Check prerequisites
./deploy.sh init    # Initialize Terraform
./deploy.sh plan    # Review changes
./deploy.sh apply   # Deploy infrastructure
```

## What This Does

This Terraform configuration:

1. Downloads Talos nocloud images from factory.talos.dev
2. Creates 2 VM templates in Proxmox (control plane & worker)
3. Deploys 15 VMs from those templates:
   - 3 control plane nodes (8001-8003)
   - 12 worker nodes (8004-8015)
     - Including 2 GPU workers (k8s-work-6, k8s-work-12)
4. Configures each VM with correct CPU, memory, disk, and network

## Architecture

```
terraform/
├── deploy.sh                    # Deployment automation script
├── versions.tf                  # Provider versions
├── variables.tf                 # Variable definitions
├── terraform.tfvars.example     # Configuration template
├── terraform.tfvars             # YOUR CONFIG (do not commit!)
├── main.tf                      # Main infrastructure config
├── outputs.tf                   # Output definitions
│
└── modules/
    ├── talos-template/          # Creates VM templates from nocloud images
    │   ├── main.tf              # - Downloads Talos nocloud .raw.xz
    │   ├── variables.tf         # - Uploads to Proxmox via SSH
    │   └── outputs.tf           # - Creates UEFI VM template
    │
    └── talos-node/              # Deploys VMs from templates
        ├── main.tf              # - Clones VM from template
        ├── variables.tf         # - Configures resources per node type
        └── outputs.tf           # - Sets MAC address for static IP
```

## Requirements

### Tools

- Terraform >= 1.6.0
- curl (for downloading images)
- xz-utils (for decompressing images)
- SSH client (for Proxmox access)
- jq (optional, for deploy.sh)

### Proxmox

- Proxmox VE 8.x
- API access (credentials in terraform.tfvars)
- SSH access (for template creation)
- Storage with sufficient space:
  - ~4GB for templates
  - ~2TB for VMs (depending on your disk sizes)

### Network

- Available IP range: 10.20.67.1-15
- Gateway: 10.20.66.1
- Network bridge: vmbr0 (or your bridge name)

## Configuration

### terraform.tfvars

Create from example and configure:

```hcl
# Proxmox connection
proxmox_endpoint = "https://YOUR_PROXMOX_IP:8006"
proxmox_username = "root@pam"
proxmox_password = "YOUR_PASSWORD"
proxmox_node     = "YOUR_NODE_NAME"

# Storage
vm_storage_pool = "local-lvm"

# All other values are pre-configured from your current cluster
```

## State Management

### Local State (Current Setup)

This configuration uses **local state storage** by default (`terraform.tfstate` file in this directory).

**Important:**
- State file contains sensitive information (IPs, MACs, resource IDs)
- **NEVER commit `terraform.tfstate` to git** (already in .gitignore)
- Backup state file regularly

**Backup Strategy:**

```bash
# Manual backup before major changes
cp terraform.tfstate terraform.tfstate.backup-$(date +%Y%m%d-%H%M%S)

# Automated backup (add to cron or systemd timer)
rsync -av terraform.tfstate* ~/backups/terraform/
```

**State File Location:**
- `terraform/terraform.tfstate` - Current state
- `terraform/terraform.tfstate.backup` - Previous state (auto-created by Terraform)

### Remote State (Optional Future Setup)

If you want to restore remote state storage (S3/Minio):

1. Uncomment the backend block in `versions.tf`
2. Configure S3 endpoint and credentials
3. Migrate state: `terraform init -migrate-state`

See: `.ai-docs/terraform/README_TERRAFORM.md` for remote backend setup details.

### Node Definitions

Node definitions are in `terraform.tfvars`:

```hcl
control_nodes = [
  {
    name       = "k8s-ctrl-1"
    ip_address = "10.20.67.1"
    mac_addr   = "bc:24:11:af:26:d4"
    vm_id      = 8001
    # ...
  }
]

worker_nodes = [
  {
    name      = "k8s-work-6"
    vm_id     = 8009
    is_gpu    = true        # GPU worker flag
    gpu_model = "rtx-a2000"
    # ...
  }
]
```

## Usage

### Deploy Cluster

```bash
# Using deploy script (recommended)
./deploy.sh init
./deploy.sh plan
./deploy.sh apply

# Or manually
terraform init
terraform plan -out=tfplan
terraform apply tfplan
```

### Check Status

```bash
# Using deploy script
./deploy.sh status

# Or manually
terraform show
terraform output
```

### Update Resources

```bash
# Edit configuration
nano terraform.tfvars

# Apply changes
terraform plan
terraform apply
```

### Destroy Infrastructure

```bash
# Using deploy script
./deploy.sh destroy

# Or manually
terraform destroy
```

## After Terraform Deployment

Terraform only creates the VMs. You still need to:

1. **Apply Talos configuration:**
   ```bash
   cd /home/devbox/repos/jlengelbrecht/prox-ops
   cd talos
   talhelper genconfig
   # Apply configs to all nodes...
   ```

2. **Bootstrap Kubernetes:**
   ```bash
   talosctl bootstrap --nodes 10.20.67.1
   talosctl kubeconfig --nodes 10.20.67.1
   ```

3. **Deploy applications:**
   ```bash
   task bootstrap:apps
   ```

See `TERRAFORM_MIGRATION_GUIDE.md` for complete instructions.

## Integration with Existing Config

This Terraform config integrates with your existing setup:

```
Terraform Layer (this directory):
├── Creates VMs in Proxmox
├── Manages VM resources (CPU, memory, disk)
└── Handles infrastructure lifecycle

Talos Layer (talos/talconfig.yaml):
├── Configures Talos on VMs
├── Manages node networking
├── Applies patches (GPU, etc.)
└── Bootstraps Kubernetes

Application Layer (kubernetes/):
├── Flux GitOps
├── Helm releases
├── Kustomize manifests
└── Application configs
```

Each layer is independent and managed separately.

## Common Tasks

### Update Talos Version

```bash
# 1. Update version in terraform.tfvars
nano terraform.tfvars
# Change: talos_version = "1.11.4"

# 2. Recreate templates
terraform apply  # Only recreates templates

# 3. Upgrade running nodes via talosctl
talosctl upgrade --nodes 10.20.67.1-15 --image ...
```

### Add a New Node

```bash
# 1. Add to terraform.tfvars worker_nodes list
nano terraform.tfvars

# 2. Deploy new VM
terraform apply

# 3. Add to talos/talconfig.yaml
nano ../talos/talconfig.yaml

# 4. Apply Talos config to new node
talhelper genconfig
talosctl apply-config --insecure --nodes 10.20.67.16 ...
```

### Change Node Resources

```bash
# 1. Update resource variables
nano terraform.tfvars
# Change: worker_memory_mb = 32768

# 2. Apply changes
terraform apply

# 3. Restart affected nodes
talosctl reboot --nodes 10.20.67.4
```

### GPU Passthrough

GPU passthrough requires manual Proxmox configuration:

1. Enable IOMMU in Proxmox
2. Identify GPU PCI device IDs
3. Uncomment GPU passthrough section in main.tf
4. Apply Terraform changes

See `TERRAFORM_MIGRATION_GUIDE.md` for detailed steps.

## State Management

### Local State (Default)

State stored in `terraform.tfstate` file.

**Important:**
- DO NOT delete terraform.tfstate
- DO NOT commit terraform.tfstate to git
- DO backup terraform.tfstate regularly

### Remote State (Recommended)

Configure S3-compatible backend in `versions.tf`:

```hcl
terraform {
  backend "s3" {
    bucket   = "terraform-state"
    key      = "prox-ops/talos-cluster.tfstate"
    endpoint = "https://s3.your-domain.com"
    # ...
  }
}
```

Migrate state:
```bash
terraform init -migrate-state
```

## Troubleshooting

### Template Creation Fails

Check SSH connectivity:
```bash
ssh root@YOUR_PROXMOX_IP "echo connected"
```

Check disk space:
```bash
ssh root@YOUR_PROXMOX_IP "df -h"
```

### VM Clone Timeout

Increase timeout in `modules/talos-node/variables.tf`:
```hcl
variable "timeout_clone" {
  default = 1800  # 30 minutes
}
```

### MAC Address Conflicts

Check for existing VMs with same MAC:
```bash
pvesh get /cluster/resources --type vm
```

### VM Doesn't Boot

Check VM configuration:
```bash
pvesh get /nodes/YOUR_NODE/qemu/8001/config
```

Should show:
- boot: order=scsi0
- bios: ovmf
- efidisk0 configured

## Files in This Directory

```
terraform/
├── README.md                    # This file
├── deploy.sh                    # Automated deployment script
├── versions.tf                  # Terraform and provider versions
├── variables.tf                 # Variable definitions
├── terraform.tfvars.example     # Configuration template
├── terraform.tfvars             # Your configuration (gitignored)
├── main.tf                      # Main infrastructure definition
├── outputs.tf                   # Output values
├── .gitignore                   # Git ignore rules
├── terraform.tfstate            # Terraform state (gitignored)
├── terraform.tfstate.backup     # State backup (gitignored)
├── .terraform/                  # Provider plugins (gitignored)
└── modules/                     # Reusable modules
    ├── talos-template/          # Template creation module
    └── talos-node/              # Node deployment module
```

## Security Notes

**Sensitive Files:**

These files contain sensitive data and are gitignored:
- `terraform.tfvars` - Proxmox credentials
- `terraform.tfstate` - Infrastructure state (may contain secrets)
- `.terraform/` - Provider plugins

**Best Practices:**

1. Use Proxmox API tokens instead of root password
2. Store state in remote backend with encryption
3. Use SOPS/age for encrypting terraform.tfvars if committing
4. Rotate API tokens regularly
5. Limit API token permissions to minimum required

## Resources

- **Terraform Proxmox Provider:** https://github.com/bpg/terraform-provider-proxmox
- **Talos Linux:** https://www.talos.dev/
- **Migration Guide:** ../TERRAFORM_MIGRATION_GUIDE.md
- **Project README:** ../README.md

## Support

For issues or questions:

1. Check `TERRAFORM_MIGRATION_GUIDE.md`
2. Review Terraform plan output carefully
3. Check Proxmox logs: `/var/log/pve/tasks/`
4. Check Terraform state: `terraform show`

## License

Same as parent repository.

---

**Version:** 1.0
**Last Updated:** 2025-10-31
**Talos Version:** 1.11.3
**Terraform Version:** >= 1.6.0
