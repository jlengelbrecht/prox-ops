# Multi-VLAN Networking Setup Guide

This guide explains how to configure multi-VLAN networking in your Kubernetes cluster using Cilium as the primary CNI and Multus for secondary networks.

## Overview

**Goal**: Enable pods to attach to specific VLANs (DMZ VLAN 81, IoT VLAN 62) while maintaining cluster connectivity via Cilium.

**Architecture**:
```
┌─────────────────────────┐
│   Standard Pod          │
│  ┌───────────────────┐  │
│  │ eth0: Cilium      │  │  <- Cluster networking only
│  │ 10.42.x.x         │  │
│  └───────────────────┘  │
└─────────────────────────┘

┌─────────────────────────┐
│   Multi-Network Pod     │
│  ┌───────────────────┐  │
│  │ eth0: Cilium      │  │  <- Primary: cluster communication
│  │ 10.42.x.x         │  │
│  └───────────────────┘  │
│  ┌───────────────────┐  │
│  │ net1: MacVLAN     │  │  <- Secondary: VLAN 81 or 62
│  │ VLAN IP           │  │
│  └───────────────────┘  │
└─────────────────────────┘
```

## Prerequisites

- Kubernetes cluster deployed with Cilium
- Worker nodes with additional NICs for VLANs
- Proxmox configured with VLANs 81 (DMZ) and 62 (IoT)

## Architecture Decision

**Option 1: MacVLAN** (Recommended for dedicated NICs)
- Use when worker nodes have separate physical NICs for VLANs
- Best performance
- Simpler configuration

**Option 2: VLAN Subinterfaces**
- Use when worker nodes share physical NIC with VLAN tags
- More complex configuration
- Requires VLAN tagging in Proxmox

This guide uses **Option 1 (MacVLAN)** with dedicated NICs.

## Step 1: Verify Worker Node NICs

Check that worker nodes have additional network interfaces:

```bash
# Check from Talos
talosctl get links --nodes 10.20.67.4

# Expected output should show:
# - eth0: Primary (cluster network)
# - eth1: DMZ VLAN 81
# - eth2: IoT VLAN 62
```

If interfaces are not present, add them in Proxmox:
1. Select worker VM
2. Hardware > Add > Network Device
3. Bridge: vmbr0 (or VLAN-specific bridge)
4. VLAN Tag: 81 (for DMZ) or 62 (for IoT)

## Step 2: Configure Talos Worker Nodes for Additional NICs

Worker nodes need to bring up additional interfaces.

### Create Worker Patch

Create file: `/home/devbox/repos/jlengelbrecht/prox-ops/talos/patches/worker/multi-nic.yaml`

```bash
mkdir -p talos/patches/worker
```

**File content:**

```yaml
---
# Additional network interfaces for VLAN connectivity
# These interfaces will be used by Multus to attach pods to VLANs

# DMZ VLAN 81 interface
- op: add
  path: /machine/network/interfaces/-
  value:
    interface: eth1
    dhcp: true  # Or set static IP if needed

# IoT VLAN 62 interface
- op: add
  path: /machine/network/interfaces/-
  value:
    interface: eth2
    dhcp: true  # Or set static IP if needed
```

**Alternative (Static IP):**

```yaml
---
- op: add
  path: /machine/network/interfaces/-
  value:
    interface: eth1
    addresses:
      - "192.168.81.10/24"  # Static IP on DMZ VLAN
    routes:
      - network: "0.0.0.0/0"
        gateway: "192.168.81.1"
        metric: 1024  # Higher metric than default route

- op: add
  path: /machine/network/interfaces/-
  value:
    interface: eth2
    addresses:
      - "192.168.62.10/24"  # Static IP on IoT VLAN
    routes:
      - network: "0.0.0.0/0"
        gateway: "192.168.62.1"
        metric: 1024
```

### Apply Talos Configuration

After creating the patch, re-render Talos configs:

```bash
task configure
```

Apply to worker nodes:

```bash
cd talos
talhelper genconfig
talhelper gencommand apply --extra-flags="--mode=try" | bash
```

Verify interfaces are up:

```bash
talosctl get links --nodes 10.20.67.4
talosctl get addresses --nodes 10.20.67.4
```

## Step 3: Deploy Multus CNI

Multus is a meta-CNI that enables attaching multiple network interfaces to pods.

### Create Directory Structure

```bash
mkdir -p kubernetes/apps/kube-system/multus/app
```

### Create Namespace Reference

Multus will be deployed in the `kube-system` namespace (already exists).

### Create Multus HelmRelease

**File**: `kubernetes/apps/kube-system/multus/app/helmrelease.yaml`

```yaml
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: multus
  namespace: kube-system
spec:
  interval: 30m
  chart:
    spec:
      chart: multus-cni
      version: 4.1.1  # Check for latest version
      sourceRef:
        kind: HelmRepository
        name: rke2-charts
        namespace: flux-system
  values:
    config:
      cni_conf:
        confDir: /etc/cni/net.d
        binDir: /opt/cni/bin
        kubeconfig: /etc/cni/net.d/multus.d/multus.kubeconfig
    image:
      repository: ghcr.io/k8snetworkplumbingwg/multus-cni
      tag: v4.1.1
    resources:
      requests:
        cpu: 50m
        memory: 64Mi
      limits:
        cpu: 100m
        memory: 128Mi
```

### Create HelmRepository

**File**: `kubernetes/apps/kube-system/multus/app/helmrepository.yaml`

```yaml
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: rke2-charts
  namespace: flux-system
spec:
  interval: 1h
  url: https://rke2-charts.rancher.io
```

### Create Kustomization

**File**: `kubernetes/apps/kube-system/multus/app/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - helmrepository.yaml
  - helmrelease.yaml
```

### Create Flux Kustomization

**File**: `kubernetes/apps/kube-system/multus/ks.yaml`

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: multus
  namespace: flux-system
spec:
  interval: 30m
  path: ./kubernetes/apps/kube-system/multus/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  wait: true
```

### Update kube-system Kustomization

Edit: `kubernetes/apps/kube-system/kustomization.yaml`

Add:
```yaml
resources:
  # ... existing resources ...
  - multus/ks.yaml
```

## Step 4: Create NetworkAttachmentDefinitions

NetworkAttachmentDefinitions (NADs) define how pods attach to secondary networks.

### DMZ VLAN 81 Network

**File**: `kubernetes/apps/kube-system/multus/app/dmz-network.yaml`

```yaml
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: dmz-vlan81
  namespace: kube-system
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "macvlan",
      "master": "eth1",
      "mode": "bridge",
      "ipam": {
        "type": "dhcp"
      }
    }
```

**Explanation**:
- `type: macvlan`: Creates MacVLAN interface
- `master: eth1`: Uses worker node's eth1 interface
- `mode: bridge`: L2 bridging mode
- `ipam: dhcp`: Get IP from VLAN 81 DHCP server

**Alternative (Static IPAM):**

```yaml
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "macvlan",
      "master": "eth1",
      "mode": "bridge",
      "ipam": {
        "type": "host-local",
        "subnet": "192.168.81.0/24",
        "rangeStart": "192.168.81.100",
        "rangeEnd": "192.168.81.200",
        "gateway": "192.168.81.1",
        "routes": [
          { "dst": "0.0.0.0/0" }
        ]
      }
    }
```

### IoT VLAN 62 Network

**File**: `kubernetes/apps/kube-system/multus/app/iot-network.yaml`

```yaml
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: iot-vlan62
  namespace: kube-system
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "macvlan",
      "master": "eth2",
      "mode": "bridge",
      "ipam": {
        "type": "dhcp"
      }
    }
```

### Update Multus Kustomization

Edit: `kubernetes/apps/kube-system/multus/app/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - helmrepository.yaml
  - helmrelease.yaml
  - dmz-network.yaml
  - iot-network.yaml
```

## Step 5: Deploy Multus and NADs

```bash
# Commit changes
git add kubernetes/apps/kube-system/multus/
git commit -m "feat: add multus CNI with DMZ and IoT VLANs"
git push

# Force Flux sync
task reconcile
```

### Verify Deployment

```bash
# Check Multus DaemonSet
kubectl get ds -n kube-system multus-cni

# Check NetworkAttachmentDefinitions
kubectl get network-attachment-definitions -n kube-system

# Expected output:
# NAME          AGE
# dmz-vlan81    1m
# iot-vlan62    1m
```

## Step 6: Test Multi-Network Pod

Create a test pod with VLAN attachment:

**File**: `test-dmz-pod.yaml` (temporary, not committed)

```yaml
---
apiVersion: v1
kind: Pod
metadata:
  name: test-dmz
  namespace: default
  annotations:
    k8s.v1.cni.cncf.io/networks: dmz-vlan81
spec:
  nodeName: k8s-work-1  # Force schedule to worker with VLAN
  containers:
  - name: netshoot
    image: nicolaka/netshoot:latest
    command: ["sleep", "3600"]
    resources:
      requests:
        cpu: 50m
        memory: 64Mi
      limits:
        cpu: 100m
        memory: 128Mi
```

Deploy and test:

```bash
kubectl apply -f test-dmz-pod.yaml

# Wait for pod to be Running
kubectl get pods test-dmz -w

# Check network interfaces
kubectl exec test-dmz -- ip addr show

# Expected output:
# 1: lo: ...
# 2: eth0@if... (Cilium network - 10.42.x.x)
# 3: net1@if... (DMZ VLAN - 192.168.81.x)

# Test VLAN connectivity
kubectl exec test-dmz -- ping -c 3 <ip-in-vlan-81>

# Clean up
kubectl delete pod test-dmz
```

## Step 7: Deploy Real Workload (Example: DMZ Web Server)

### Create DMZ Namespace

```bash
mkdir -p kubernetes/apps/dmz
```

**File**: `kubernetes/apps/dmz/namespace.yaml`

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: dmz
  labels:
    pod-security.kubernetes.io/enforce: baseline
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

**File**: `kubernetes/apps/dmz/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - nginx/ks.yaml
```

### Create Nginx Deployment

```bash
mkdir -p kubernetes/apps/dmz/nginx/app
```

**File**: `kubernetes/apps/dmz/nginx/app/deployment.yaml`

```yaml
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: nginx-dmz
  namespace: dmz
spec:
  replicas: 2
  selector:
    matchLabels:
      app: nginx-dmz
  template:
    metadata:
      labels:
        app: nginx-dmz
      annotations:
        k8s.v1.cni.cncf.io/networks: dmz-vlan81  # Attach DMZ VLAN
    spec:
      nodeSelector:
        node-role.kubernetes.io/worker: ""  # Schedule only on workers
      containers:
      - name: nginx
        image: nginx:1.27-alpine
        ports:
        - containerPort: 80
          name: http
        resources:
          requests:
            cpu: 50m
            memory: 64Mi
          limits:
            cpu: 200m
            memory: 128Mi
        livenessProbe:
          httpGet:
            path: /
            port: http
          initialDelaySeconds: 10
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /
            port: http
          initialDelaySeconds: 5
          periodSeconds: 5
```

**File**: `kubernetes/apps/dmz/nginx/app/service.yaml`

```yaml
---
apiVersion: v1
kind: Service
metadata:
  name: nginx-dmz
  namespace: dmz
spec:
  type: ClusterIP  # Internal access via Cilium
  selector:
    app: nginx-dmz
  ports:
  - port: 80
    targetPort: http
    name: http
```

**File**: `kubernetes/apps/dmz/nginx/app/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - deployment.yaml
  - service.yaml
```

**File**: `kubernetes/apps/dmz/nginx/ks.yaml`

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: nginx-dmz
  namespace: flux-system
spec:
  interval: 30m
  path: ./kubernetes/apps/dmz/nginx/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  wait: true
```

### Deploy

```bash
git add kubernetes/apps/dmz/
git commit -m "feat: add nginx to DMZ VLAN"
git push

task reconcile
```

### Verify

```bash
kubectl get pods -n dmz
kubectl get svc -n dmz

# Check pod has two interfaces
kubectl exec -n dmz <nginx-pod-name> -- ip addr show
```

## Step 8: Network Policy (Optional but Recommended)

Restrict communication to/from DMZ pods:

**File**: `kubernetes/apps/dmz/network-policy.yaml`

```yaml
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: dmz-isolation
  namespace: dmz
spec:
  podSelector: {}  # Applies to all pods in dmz namespace
  policyTypes:
  - Ingress
  - Egress

  ingress:
  # Allow from ingress controller
  - from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: network
    ports:
    - protocol: TCP
      port: 80
    - protocol: TCP
      port: 443

  # Allow from same namespace
  - from:
    - podSelector: {}

  egress:
  # Allow DNS
  - to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
    ports:
    - protocol: UDP
      port: 53

  # Allow to same namespace
  - to:
    - podSelector: {}

  # Allow to external (for updates, etc.)
  - to:
    - namespaceSelector: {}
    ports:
    - protocol: TCP
      port: 443
```

## Troubleshooting

### Issue: Pod has no net1 interface

**Check**:
```bash
# Verify Multus is running
kubectl get ds -n kube-system multus-cni

# Check pod events
kubectl describe pod <pod-name>

# Check Multus logs
kubectl logs -n kube-system -l app=multus
```

**Common causes**:
- NetworkAttachmentDefinition doesn't exist
- Annotation typo (must be exact: `k8s.v1.cni.cncf.io/networks`)
- Master interface (eth1/eth2) doesn't exist on node

### Issue: net1 interface has no IP (DHCP)

**Check**:
```bash
# Verify DHCP server is running on VLAN
# Check node can reach DHCP server
talosctl get addresses --nodes 10.20.67.4

# Switch to static IPAM in NetworkAttachmentDefinition
```

### Issue: Can't communicate on VLAN

**Check**:
```bash
# From pod
kubectl exec <pod> -- ip route show
kubectl exec <pod> -- ping <vlan-gateway>

# Check VLAN configuration in Proxmox
# Verify switch/router VLAN configuration
```

### Issue: Pods on different nodes can't communicate via VLAN

This is expected with MacVLAN bridge mode. To enable:
1. Use MacVLAN VEPA mode (requires switch support)
2. Use IPVLAN instead of MacVLAN
3. Use network bridge on nodes

## Advanced: IPVLAN (Alternative to MacVLAN)

If you need pod-to-pod communication across nodes via VLAN:

**File**: `kubernetes/apps/kube-system/multus/app/dmz-network-ipvlan.yaml`

```yaml
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: dmz-vlan81-ipvlan
  namespace: kube-system
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "ipvlan",
      "master": "eth1",
      "mode": "l2",
      "ipam": {
        "type": "host-local",
        "subnet": "192.168.81.0/24",
        "rangeStart": "192.168.81.100",
        "rangeEnd": "192.168.81.200",
        "gateway": "192.168.81.1"
      }
    }
```

## Summary

You now have:
- Multus CNI for multi-network support
- NetworkAttachmentDefinitions for DMZ (VLAN 81) and IoT (VLAN 62)
- Example deployments showing how to attach pods to VLANs
- Network policies for VLAN isolation

Pods can now:
- Communicate within the cluster via Cilium (eth0)
- Communicate on specific VLANs via MacVLAN (net1)
- Be isolated by namespace and network policy

Next steps:
- Deploy IoT workloads (Home Assistant, etc.) with IoT VLAN attachment
- Deploy DMZ workloads (public services) with DMZ VLAN attachment
- Configure firewall rules on your router/firewall for VLAN traffic
