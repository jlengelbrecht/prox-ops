# Dependency Installation Checklist

This document provides a checklist for installing all required dependencies for the prox-ops Kubernetes cluster.

## Current System Status

**Operating System**: CachyOS (Arch-based Linux)
**Package Manager**: Nix (via home-manager)

## Already Installed

Based on system check:
- [x] kubectl (via Nix)
- [x] helm (via Nix)
- [x] git

## Required Tools Overview

All tools will be managed via **mise** (developer environment manager), which provides version pinning and reproducible environments.

## Installation Method

**Option 1**: Mise (Recommended)
- Install mise once
- All other tools managed automatically via `.mise.toml`
- Version pinned for reproducibility
- Per-project environments

**Option 2**: Nix (Fallback)
- Install tools directly via Nix
- Good for system-wide availability
- Some tools may not be available in nixpkgs

## Step-by-Step Installation

### 1. Install Mise

Mise is the central tool that manages all other dependencies.

**Via Nix (Recommended for CachyOS)**:

```bash
nix profile install nixpkgs#mise
```

**Verification**:
```bash
mise --version
```

Expected output: `mise 2025.x.x` or similar

**Configure Shell Integration**:

For Bash (`~/.bashrc`):
```bash
# Mise activation
eval "$(mise activate bash)"
```

For Zsh (`~/.zshrc`):
```bash
# Mise activation
eval "$(mise activate zsh)"
```

**Reload shell**:
```bash
source ~/.bashrc  # or source ~/.zshrc
```

**Verify activation**:
```bash
mise doctor
```

### 2. Verify Repository Setup

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/
```

Ensure `.mise.toml` exists:
```bash
ls -la .mise.toml
```

If not present, copy from template:
```bash
cp /home/devbox/repos/onedr0p/cluster-template/.mise.toml .
```

### 3. Trust Mise Configuration

```bash
mise trust
```

This allows mise to read the `.mise.toml` configuration.

### 4. Install Python Dependencies

Mise uses pipx for Python tools:

```bash
pip install --user pipx
```

Or via Nix:
```bash
nix profile install nixpkgs#python3Packages.pipx
```

### 5. Install All Tools via Mise

```bash
mise install
```

This will install all tools defined in `.mise.toml`:
- Python 3.14
- makejinja (via pipx)
- talhelper (via aqua)
- cilium-cli (via aqua)
- gh (GitHub CLI, via aqua)
- cloudflared (via aqua)
- cue (via aqua)
- age (via aqua)
- flux (via aqua)
- sops (via aqua)
- task (go-task, via aqua)
- helm (via aqua)
- helmfile (via aqua)
- jq (via aqua)
- kustomize (via aqua)
- kubectl (via aqua)
- yq (via aqua)
- talosctl (via aqua)
- kubeconform (via aqua)

**Duration**: 5-10 minutes depending on internet speed

### 6. Verify Installation

Run the verification script:

```bash
echo "Checking installed tools..."
echo ""

tools=(
  "mise:Mise"
  "python:Python"
  "makejinja:Makejinja"
  "talhelper:Talhelper"
  "talosctl:Talosctl"
  "kubectl:Kubectl"
  "flux:Flux"
  "cilium:Cilium CLI"
  "helm:Helm"
  "helmfile:Helmfile"
  "sops:SOPS"
  "age:Age"
  "gh:GitHub CLI"
  "cloudflared:Cloudflared"
  "task:Task"
  "jq:JQ"
  "yq:YQ"
  "kustomize:Kustomize"
  "kubeconform:Kubeconform"
  "cue:CUE"
)

for tool_pair in "${tools[@]}"; do
  IFS=':' read -r cmd name <<< "$tool_pair"
  if command -v "$cmd" &> /dev/null; then
    version=$(eval "$cmd version" 2>&1 | head -1 || eval "$cmd --version" 2>&1 | head -1)
    echo "[✓] $name: $version"
  else
    echo "[✗] $name: NOT FOUND"
  fi
done
```

**Expected Output** (all tools with checkmarks):
```
[✓] Mise: mise 2025.x.x
[✓] Python: Python 3.14.x
[✓] Makejinja: makejinja, version 2.8.x
[✓] Talhelper: v3.0.38
[✓] Talosctl: Client: Version: v1.11.3
[✓] Kubectl: Client Version: v1.34.0
[✓] Flux: flux version 2.7.2
[✓] Cilium CLI: cilium-cli: v0.18.7
[✓] Helm: v3.19.0
[✓] Helmfile: Version: 1.1.7
[✓] SOPS: sops 3.11.0
[✓] Age: v1.2.1
[✓] GitHub CLI: gh version 2.82.0
[✓] Cloudflared: cloudflared version 2025.10.0
[✓] Task: Task version: v3.45.4
[✓] JQ: jq-1.8.1
[✓] YQ: yq (https://github.com/mikefarah/yq/) version v4.48.1
[✓] Kustomize: v5.7.1
[✓] Kubeconform: v0.7.0
[✓] CUE: cue version v0.14.2
```

### 7. Verify Task Automation

```bash
task --list
```

**Expected Output**:
```
task: Available tasks for this project:
* default:
* reconcile:                       Force Flux to pull in changes from your Git repository
* bootstrap:apps:                  Bootstrap apps into the Talos cluster
* bootstrap:talos:                 Bootstrap the Talos cluster
* talos:apply:                     Apply Talos configuration to a node
* template:configure:              Render and validate configuration files
* template:debug:                  Gather common resources in your cluster
* template:init:                   Initialize configuration files
```

## Troubleshooting

### Issue: Mise not found after installation

**Solution**:
```bash
# Ensure Nix profile bin is in PATH
echo 'export PATH="$HOME/.nix-profile/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc

# Verify
which mise
```

### Issue: Mise tools not found

**Problem**: Shell activation not configured

**Solution**:
```bash
# Add to ~/.bashrc or ~/.zshrc
eval "$(mise activate bash)"  # or zsh

# Reload shell
source ~/.bashrc
```

### Issue: Python compilation errors

**Solution**:
```bash
# Disable Python compilation in mise
mise settings python.compile=0

# Retry installation
mise install
```

### Issue: Tool version mismatch

**Solution**:
```bash
# Update all tools
mise upgrade

# Or specific tool
mise upgrade python
```

### Issue: GitHub rate limit (when installing from GitHub)

**Solution**:
```bash
# Unset GITHUB_TOKEN if set
unset GITHUB_TOKEN

# Or authenticate with gh
gh auth login

# Retry installation
mise install
```

## Alternative: Install via Nix (Without Mise)

If mise installation fails, you can install tools directly via Nix:

**Create Nix profile** (add to `~/.config/home-manager/home.nix`):

```nix
{ config, pkgs, ... }:

{
  home.packages = with pkgs; [
    # Kubernetes tools
    talosctl
    kubectl
    kubernetes-helm
    fluxcd
    cilium-cli
    kustomize

    # Utilities
    jq
    yq
    age
    sops
    go-task
    cloudflared

    # GitHub
    gh

    # Python
    python3
    python3Packages.pipx
  ];
}
```

**Apply configuration**:
```bash
home-manager switch
```

**Install remaining tools manually**:
```bash
# makejinja (via pipx)
pipx install makejinja

# talhelper (manual download)
wget https://github.com/budimanjojo/talhelper/releases/download/v3.0.38/talhelper_linux_amd64 -O ~/.local/bin/talhelper
chmod +x ~/.local/bin/talhelper

# kubeconform (manual download)
wget https://github.com/yannh/kubeconform/releases/download/v0.7.0/kubeconform-linux-amd64.tar.gz
tar xf kubeconform-linux-amd64.tar.gz
mv kubeconform ~/.local/bin/
```

## Post-Installation Next Steps

After all dependencies are installed:

1. **Initialize repository**: `task init`
2. **Configure cluster**: Edit `cluster.yaml` and `nodes.yaml`
3. **Validate configuration**: `task configure`
4. **Bootstrap cluster**: Follow [QUICKSTART.md](./QUICKSTART.md)

## Tool Versions Reference

These versions are from the template `.mise.toml` (as of January 2025):

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.14.0 | Makejinja runtime |
| makejinja | 2.8.1 | Template rendering |
| talhelper | 3.0.38 | Talos config helper |
| cilium-cli | 0.18.7 | Cilium management |
| gh | 2.82.0 | GitHub CLI |
| cloudflared | 2025.10.0 | Cloudflare tunnel |
| cue | 0.14.2 | Config validation |
| age | 1.2.1 | Encryption |
| flux | 2.7.2 | GitOps |
| sops | 3.11.0 | Secret encryption |
| task | 3.45.4 | Task automation |
| helm | 3.19.0 | Package manager |
| helmfile | 1.1.7 | Helm deployment |
| jq | 1.8.1 | JSON processor |
| kustomize | 5.7.1 | Manifest customization |
| kubectl | 1.34.0 | Kubernetes CLI |
| yq | 4.48.1 | YAML processor |
| talosctl | 1.11.3 | Talos CLI |
| kubeconform | 0.7.0 | Manifest validation |

## Additional Notes

- All tools are installed in `~/.local/share/mise/installs/`
- Tool versions are pinned in `.mise.toml` for reproducibility
- Mise automatically uses the correct version when in the repository directory
- You can override versions in `.mise.toml` if needed

## Support

If you encounter issues:
1. Check [Mise documentation](https://mise.jdx.dev/)
2. Check individual tool documentation
3. Verify network connectivity (for downloads)
4. Check disk space (mise downloads many tools)

## Disk Space Requirements

Approximate disk space needed:
- Mise tools: ~2GB
- Docker images (during deployment): ~5GB
- Total: ~7GB free space recommended
