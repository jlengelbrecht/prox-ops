# Prox-Ops Quick Start Guide

This is a condensed quick-start guide for experienced Kubernetes administrators. For detailed explanations, refer to [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md).

## Prerequisites

- 5 Talos VMs running on Proxmox (IPs: 10.20.67.1-5)
- Proxmox Ceph cluster configured
- Cloudflare account with domain
- CachyOS workstation with Nix

## Step 1: Install Dependencies (5 minutes)

```bash
# Install mise via Nix
nix profile install nixpkgs#mise

# Configure shell integration (add to ~/.bashrc or ~/.zshrc)
eval "$(mise activate bash)"  # or zsh
source ~/.bashrc

# Verify
mise --version
```

## Step 2: Initialize Repository (10 minutes)

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/

# Copy template files
cp -r /home/devbox/repos/onedr0p/cluster-template/{.taskfiles,templates,scripts,.mise.toml,.gitignore,.editorconfig,.gitattributes,.renovaterc.json5,.shellcheckrc,makejinja.toml,Taskfile.yaml,cluster.sample.yaml,nodes.sample.yaml} .

# Install tools via mise
mise trust
pip install pipx
mise install

# Verify tools
talosctl version
kubectl version --client
flux version
task --list

# Initialize config files
task init
```

This creates:
- `cluster.yaml` (cluster configuration)
- `nodes.yaml` (node definitions)
- `age.key` (encryption key)
- `github-deploy.key` (GitHub deploy key)
- `github-push-token.txt` (webhook token)

## Step 3: Discover Node Information (15 minutes)

```bash
# Find all Talos nodes
nmap -Pn -n -p 50000 10.20.67.0/24 -vv | grep 'Discovered'

# For each node, get disk and MAC address
for ip in 10.20.67.{1..5}; do
  echo "=== Node $ip ==="
  echo "Disks:"
  talosctl disks --nodes $ip --insecure
  echo "Interfaces:"
  talosctl get links --nodes $ip --insecure
  echo ""
done
```

Record for each node:
- IP address
- Disk device (e.g., `/dev/sda`) or serial number
- MAC address of primary interface

## Step 4: Create Talos Schematic (5 minutes)

1. Visit https://factory.talos.dev/
2. Select Talos version: **1.11.3** (or latest)
3. Add system extension: **qemu-guest-agent**
4. Click **Generate**
5. Copy the **Schematic ID** (long hex string)

## Step 5: Configure Cluster (20 minutes)

### Edit cluster.yaml

```bash
nano cluster.yaml
```

**Key settings to configure:**

```yaml
node_cidr: "10.20.67.0/24"
node_default_gateway: "10.20.67.1"  # Your gateway IP
cluster_api_addr: "10.20.67.10"     # Kubernetes API VIP
cluster_dns_gateway_addr: "10.20.67.20"
cluster_gateway_addr: "10.20.67.21"
cloudflare_gateway_addr: "10.20.67.22"

repository_name: "jlengelbrecht/prox-ops"
repository_branch: "main"
repository_visibility: "public"

cloudflare_domain: "yourdomain.com"  # CHANGE THIS
cloudflare_token: "your-token-here"  # CHANGE THIS (will be encrypted)
```

### Edit nodes.yaml

```bash
nano nodes.yaml
```

**Example configuration:**

```yaml
nodes:
  - name: "k8s-ctrl-1"
    address: "10.20.67.1"
    controller: true
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:01"
    schematic_id: "your-schematic-id-here"
    mtu: 1500

  - name: "k8s-ctrl-2"
    address: "10.20.67.2"
    controller: true
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:02"
    schematic_id: "your-schematic-id-here"
    mtu: 1500

  - name: "k8s-ctrl-3"
    address: "10.20.67.3"
    controller: true
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:03"
    schematic_id: "your-schematic-id-here"
    mtu: 1500

  - name: "k8s-work-1"
    address: "10.20.67.4"
    controller: false
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:04"
    schematic_id: "your-schematic-id-here"
    mtu: 1500

  - name: "k8s-work-2"
    address: "10.20.67.5"
    controller: false
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:05"
    schematic_id: "your-schematic-id-here"
    mtu: 1500

  # ... Continue for k8s-work-3 through k8s-work-12 (10.20.67.6-15)
  # Repeat the pattern above, incrementing addresses and MAC addresses
```

Replace:
- MAC addresses with actual values
- Disk paths with actual values
- Schematic ID with your generated ID

## Step 6: Cloudflare Setup (10 minutes)

### Create API Token

1. Cloudflare Dashboard > My Profile > API Tokens
2. Create Token > Edit zone DNS template
3. Permissions: `Zone - DNS - Edit`, `Account - Cloudflare Tunnel - Read`
4. Scope to your zone
5. Create Token and **save it**

### Create Tunnel

```bash
cloudflared tunnel login
cloudflared tunnel create --credentials-file cloudflare-tunnel.json kubernetes
```

### Update cluster.yaml

Update `cloudflare_token` in `cluster.yaml` with the API token from above.

## Step 7: Render and Validate Configurations (5 minutes)

```bash
task configure
```

This will:
- Validate configs against schemas
- Render Jinja2 templates
- Generate Talos and Kubernetes manifests
- Encrypt secrets with SOPS

**Fix any errors** before proceeding.

## Step 8: Commit Initial Configuration (5 minutes)

```bash
git add -A
git commit -m "chore: initial cluster configuration"
git push origin main
```

Verify all `*.sops.*` files are encrypted (not plain text).

## Step 9: Bootstrap Talos (10 minutes)

```bash
task bootstrap:talos
```

**What happens:**
1. Generates Talos secrets
2. Applies configurations to nodes
3. Bootstraps Kubernetes cluster
4. Retrieves kubeconfig

**Monitor progress:**
```bash
# In another terminal
watch kubectl get nodes
```

**Commit encrypted secrets:**
```bash
git add -A
git commit -m "chore: add talhelper encrypted secret"
git push
```

## Step 10: Bootstrap Applications (15 minutes)

```bash
task bootstrap:apps
```

**What happens:**
1. Deploys Cilium CNI
2. Deploys CoreDNS
3. Deploys Spegel (OCI mirror)
4. Deploys Flux operator
5. Flux syncs repository

**Monitor progress:**
```bash
kubectl get pods --all-namespaces --watch
```

Wait for all pods to be Running.

## Step 11: Verify Cluster (5 minutes)

```bash
# Check nodes
kubectl get nodes

# Check Cilium
cilium status

# Check Flux
flux check
flux get sources git
flux get kustomizations
flux get helmreleases -A

# Check core services
kubectl get pods -n kube-system
kubectl get pods -n cert-manager
kubectl get pods -n network
kubectl get pods -n flux-system
```

All should show healthy status.

## Step 12: Deploy Multus for VLAN Support (Optional)

See [VLAN_SETUP.md](./VLAN_SETUP.md) for detailed instructions.

Quick setup:

```bash
# Create Multus manifests
mkdir -p kubernetes/apps/kube-system/multus/app

# Add Multus HelmRelease (see VLAN_SETUP.md for full config)
# Add NetworkAttachmentDefinitions for VLAN 81 and 62

git add kubernetes/apps/kube-system/multus/
git commit -m "feat: add multus CNI for VLAN support"
git push

# Force reconcile
task reconcile
```

## Step 13: Deploy Rook-Ceph Storage (Optional)

See [STORAGE_SETUP.md](./STORAGE_SETUP.md) for detailed instructions.

You'll need:
- Ceph monitor IPs from Proxmox
- Ceph admin keyring
- Ceph cluster FSID

## Next Steps

1. **Configure Split DNS**: Point `*.yourdomain.com` to internal gateway (10.20.67.21) on your DNS server
2. **Deploy Applications**: Add apps to `kubernetes/apps/` following Flux structure
3. **Set Up Monitoring**: Deploy Prometheus, Grafana, Loki
4. **Configure Backups**: Set up Velero or similar
5. **Harden Security**: Network policies, RBAC, Pod Security Standards

## Common Issues

### Nodes stuck in NotReady
- Wait for Cilium to deploy (5-10 minutes)
- Check: `kubectl logs -n kube-system -l app.kubernetes.io/name=cilium`

### Flux not syncing
- Check GitHub deploy key is added to repo settings
- Check: `flux logs --follow`
- Force sync: `flux reconcile source git flux-system`

### Can't access services
- Verify LoadBalancer IPs: `kubectl get svc -A | grep LoadBalancer`
- Test internal: `curl -H "Host: echo.yourdomain.com" http://10.20.67.21`

## Useful Commands

```bash
# Force Flux sync
task reconcile

# Get cluster status
task template:debug

# Talos logs
talosctl dmesg --follow --nodes 10.20.67.1

# Reset cluster (WARNING: destroys all data)
talosctl reset --graceful=false --reboot --nodes 10.20.67.1,10.20.67.2,10.20.67.3,10.20.67.4,10.20.67.5,10.20.67.6,10.20.67.7,10.20.67.8,10.20.67.9,10.20.67.10,10.20.67.11,10.20.67.12,10.20.67.13,10.20.67.14,10.20.67.15
```

## Documentation

- **Full Implementation Plan**: [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md)
- **VLAN Setup Guide**: [VLAN_SETUP.md](./VLAN_SETUP.md)
- **Storage Setup Guide**: [STORAGE_SETUP.md](./STORAGE_SETUP.md)
- **Talos Documentation**: https://www.talos.dev/
- **Flux Documentation**: https://fluxcd.io/
- **Cilium Documentation**: https://docs.cilium.io/

## Estimated Total Time

- **Basic Cluster** (Steps 1-11): ~90 minutes
- **With VLAN Support** (+ Step 12): +30 minutes
- **With Rook-Ceph** (+ Step 13): +45 minutes

**Total**: 2-3 hours for full setup
