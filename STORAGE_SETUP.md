# Rook-Ceph Storage Setup with Proxmox Ceph

This guide explains how to integrate your existing Proxmox Ceph cluster with Kubernetes using Rook-Ceph in external cluster mode.

## Overview

**Architecture**: Rook-Ceph External Cluster Mode

```
┌─────────────────────────────────────────────────────┐
│           Kubernetes Cluster                         │
│                                                      │
│  ┌────────────────────────────────────────────────┐ │
│  │  Rook-Ceph Operator                            │ │
│  │  - Manages StorageClasses                      │ │
│  │  - Manages CSI drivers                         │ │
│  │  - No Ceph daemons in Kubernetes               │ │
│  └──────────────────┬─────────────────────────────┘ │
│                     │                                │
│  ┌──────────────────▼────────────────────────────┐  │
│  │  CSI Provisioners                             │  │
│  │  - RBD (block storage)                        │  │
│  │  - CephFS (shared filesystem)                 │  │
│  └──────────────────┬────────────────────────────┘  │
└────────────────────┼──────────────────────────────┘
                     │
                     │ Ceph Protocol (MON: 6789, OSD: 6800-7300)
                     │
┌────────────────────▼──────────────────────────────┐
│        Proxmox Ceph Cluster                       │
│  ┌──────────────────────────────────────────────┐ │
│  │  Ceph Monitors (MONs)                        │ │
│  │  Ceph Managers (MGRs)                        │ │
│  │  Ceph OSDs (Object Storage Daemons)          │ │
│  │                                               │ │
│  │  Storage Pools:                               │ │
│  │  - RBD pool for block devices                │ │
│  │  - CephFS metadata + data pools              │ │
│  └──────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────┘
```

**Benefits**:
- Lower resource usage (no Ceph daemons in Kubernetes)
- Easier management (Proxmox UI)
- Better performance (direct connection)
- Independent lifecycle (Ceph survives Kubernetes issues)

**Trade-offs**:
- Requires network connectivity to Proxmox Ceph cluster
- Ceph configuration not in GitOps (managed via Proxmox)
- Need to manually sync changes between Proxmox and Kubernetes

## Prerequisites

- Proxmox Ceph cluster deployed and healthy
- Network connectivity from Kubernetes nodes to Ceph monitors
- Ceph admin credentials

## Step 1: Gather Ceph Cluster Information

SSH into one of your Proxmox nodes:

```bash
ssh root@proxmox-node-1
```

### Get Ceph Cluster FSID

```bash
ceph fsid
```

**Example output**: `4b5c8c0a-1234-5678-90ab-cdef12345678`

Save this value as `CEPH_CLUSTER_FSID`.

### Get Monitor Addresses

```bash
ceph mon dump
```

**Example output**:
```
epoch 3
fsid 4b5c8c0a-1234-5678-90ab-cdef12345678
...
0: [v2:10.20.67.11:3300/0,v1:10.20.67.11:6789/0] mon.pve1
1: [v2:10.20.67.12:3300/0,v1:10.20.67.12:6789/0] mon.pve2
2: [v2:10.20.67.13:3300/0,v1:10.20.67.13:6789/0] mon.pve3
```

Extract monitor IPs (v1 protocol, port 6789):
- `10.20.67.11:6789`
- `10.20.67.12:6789`
- `10.20.67.13:6789`

Save these as `CEPH_MON_ENDPOINTS`.

### Get Admin Keyring

```bash
ceph auth get-key client.admin
```

**Example output**: `AQBKKptoAAAAABAAabc123def456ghi789jkl012==`

Save this as `CEPH_ADMIN_KEY`.

### List Existing Pools

```bash
ceph osd pool ls
```

**Typical Proxmox output**:
```
device_health_metrics
rbd
cephfs_data
cephfs_metadata
```

### Create Kubernetes-Specific Pools (Optional)

It's recommended to create dedicated pools for Kubernetes:

```bash
# Create RBD pool for block storage
ceph osd pool create kubernetes-rbd 64 64

# Initialize RBD pool
rbd pool init kubernetes-rbd

# Create CephFS pools (if using shared storage)
ceph osd pool create kubernetes-cephfs-data 64 64
ceph osd pool create kubernetes-cephfs-metadata 64 64

# Create CephFS filesystem
ceph fs new kubernetes-cephfs kubernetes-cephfs-metadata kubernetes-cephfs-data

# Set quotas (optional, adjust to your needs)
ceph osd pool set-quota kubernetes-rbd max_bytes 500G
```

**PG Count Guide**:
- 64 PGs for <5 OSDs
- 128 PGs for 5-10 OSDs
- 256 PGs for 10-50 OSDs

Save pool names:
- `CEPH_RBD_POOL`: `kubernetes-rbd`
- `CEPH_FS_NAME`: `kubernetes-cephfs`
- `CEPH_FS_DATA_POOL`: `kubernetes-cephfs-data`

### Create CSI User (Recommended)

Create a dedicated user for Kubernetes CSI drivers:

```bash
# Create user with appropriate capabilities
ceph auth get-or-create client.kubernetes \
  mon 'profile rbd' \
  osd 'profile rbd pool=kubernetes-rbd' \
  mgr 'profile rbd pool=kubernetes-rbd' \
  -o /etc/ceph/ceph.client.kubernetes.keyring

# Get the key
ceph auth get-key client.kubernetes
```

Save as `CEPH_CSI_KEY`.

**For CephFS** (if using):
```bash
ceph auth get-or-create client.kubernetes-cephfs \
  mon 'allow r' \
  mgr 'allow rw' \
  osd 'allow rw pool=kubernetes-cephfs-data, allow rw pool=kubernetes-cephfs-metadata' \
  mds 'allow rw' \
  -o /etc/ceph/ceph.client.kubernetes-cephfs.keyring

ceph auth get-key client.kubernetes-cephfs
```

Save as `CEPH_CEPHFS_CSI_KEY`.

## Step 2: Verify Network Connectivity

From a Kubernetes node, test connectivity to Ceph monitors:

```bash
# Test from Talos node
talosctl -n 10.20.67.4 shell

# Inside Talos shell
nc -zv 10.20.67.11 6789
nc -zv 10.20.67.12 6789
nc -zv 10.20.67.13 6789

# Or use telnet
telnet 10.20.67.11 6789
```

All connections should succeed.

## Step 3: Create Rook-Ceph Directory Structure

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/

mkdir -p kubernetes/apps/rook-ceph/rook-ceph-operator/app
mkdir -p kubernetes/apps/rook-ceph/rook-ceph-cluster/app
```

## Step 4: Create Namespace

**File**: `kubernetes/apps/rook-ceph/namespace.yaml`

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: rook-ceph
  labels:
    pod-security.kubernetes.io/enforce: privileged
    pod-security.kubernetes.io/audit: privileged
    pod-security.kubernetes.io/warn: privileged
```

## Step 5: Create External Cluster Secrets

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/external-cluster-secrets.sops.yaml`

Replace values with your actual Ceph cluster information:

```yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: rook-ceph-mon-endpoints
  namespace: rook-ceph
data:
  data: 10.20.67.11:6789,10.20.67.12:6789,10.20.67.13:6789
  maxMonId: "2"
  mapping: |
    {
      "node": {
        "pve1": {
          "Name": "pve1",
          "Hostname": "pve1",
          "Address": "10.20.67.11"
        },
        "pve2": {
          "Name": "pve2",
          "Hostname": "pve2",
          "Address": "10.20.67.12"
        },
        "pve3": {
          "Name": "pve3",
          "Hostname": "pve3",
          "Address": "10.20.67.13"
        }
      }
    }
---
apiVersion: v1
kind: Secret
metadata:
  name: rook-ceph-mon
  namespace: rook-ceph
type: Opaque
stringData:
  cluster-name: rook-ceph
  fsid: 4b5c8c0a-1234-5678-90ab-cdef12345678  # CHANGE THIS
  admin-secret: AQBKKptoAAAAABAAabc123def456ghi789jkl012==  # CHANGE THIS
  mon-secret: ""
---
apiVersion: v1
kind: Secret
metadata:
  name: rook-csi-rbd-node
  namespace: rook-ceph
type: Opaque
stringData:
  userID: kubernetes  # CSI user
  userKey: <CEPH_CSI_KEY>  # CHANGE THIS
---
apiVersion: v1
kind: Secret
metadata:
  name: rook-csi-rbd-provisioner
  namespace: rook-ceph
type: Opaque
stringData:
  userID: kubernetes
  userKey: <CEPH_CSI_KEY>  # CHANGE THIS
---
# CephFS secrets (if using CephFS)
apiVersion: v1
kind: Secret
metadata:
  name: rook-csi-cephfs-node
  namespace: rook-ceph
type: Opaque
stringData:
  adminID: kubernetes-cephfs
  adminKey: <CEPH_CEPHFS_CSI_KEY>  # CHANGE THIS
---
apiVersion: v1
kind: Secret
metadata:
  name: rook-csi-cephfs-provisioner
  namespace: rook-ceph
type: Opaque
stringData:
  adminID: kubernetes-cephfs
  adminKey: <CEPH_CEPHFS_CSI_KEY>  # CHANGE THIS
```

**Encrypt with SOPS**:

```bash
sops --encrypt --in-place kubernetes/apps/rook-ceph/rook-ceph-cluster/app/external-cluster-secrets.sops.yaml
```

## Step 6: Deploy Rook-Ceph Operator

### Create HelmRepository

**File**: `kubernetes/apps/rook-ceph/rook-ceph-operator/app/helmrepository.yaml`

```yaml
---
apiVersion: source.toolkit.fluxcd.io/v1
kind: HelmRepository
metadata:
  name: rook-ceph
  namespace: flux-system
spec:
  interval: 1h
  url: https://charts.rook.io/release
```

### Create HelmRelease

**File**: `kubernetes/apps/rook-ceph/rook-ceph-operator/app/helmrelease.yaml`

```yaml
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: rook-ceph-operator
  namespace: rook-ceph
spec:
  interval: 30m
  chart:
    spec:
      chart: rook-ceph
      version: v1.15.5  # Check for latest version
      sourceRef:
        kind: HelmRepository
        name: rook-ceph
        namespace: flux-system
  values:
    crds:
      enabled: true

    csi:
      enableRbdDriver: true
      enableCephfsDriver: true
      enableGrpcMetrics: true

    monitoring:
      enabled: true

    resources:
      requests:
        cpu: 100m
        memory: 128Mi
      limits:
        cpu: 500m
        memory: 512Mi

    nodeSelector:
      node-role.kubernetes.io/worker: ""

    tolerations:
      - key: node-role.kubernetes.io/control-plane
        operator: Exists
        effect: NoSchedule
```

### Create Kustomization Files

**File**: `kubernetes/apps/rook-ceph/rook-ceph-operator/app/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - helmrepository.yaml
  - helmrelease.yaml
```

**File**: `kubernetes/apps/rook-ceph/rook-ceph-operator/ks.yaml`

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: rook-ceph-operator
  namespace: flux-system
spec:
  interval: 30m
  path: ./kubernetes/apps/rook-ceph/rook-ceph-operator/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  wait: true
  timeout: 5m
```

## Step 7: Create CephCluster Resource (External Mode)

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/cephcluster.yaml`

```yaml
---
apiVersion: ceph.rook.io/v1
kind: CephCluster
metadata:
  name: rook-ceph
  namespace: rook-ceph
spec:
  external:
    enable: true

  dataDirHostPath: /var/lib/rook

  monitoring:
    enabled: true
    externalMgrEndpoints:
      - ip: 10.20.67.11  # Proxmox node IPs
      - ip: 10.20.67.12
      - ip: 10.20.67.13
    externalMgrPrometheusPort: 9283

  cephVersion:
    image: quay.io/ceph/ceph:v18.2.4  # Match your Proxmox Ceph version

  healthCheck:
    daemonHealth:
      mon:
        disabled: true
      osd:
        disabled: true
      status:
        disabled: false
```

### Create Kustomization Files

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - external-cluster-secrets.sops.yaml
  - cephcluster.yaml
```

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/ks.yaml`

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: rook-ceph-cluster
  namespace: flux-system
spec:
  interval: 30m
  path: ./kubernetes/apps/rook-ceph/rook-ceph-cluster/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  dependsOn:
    - name: rook-ceph-operator
  wait: true
  timeout: 10m
  decryption:
    provider: sops
    secretRef:
      name: sops-age
```

## Step 8: Create StorageClasses

### RBD StorageClass (Block Storage)

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/storageclass-rbd.yaml`

```yaml
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: rook-ceph-block
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: rook-ceph.rbd.csi.ceph.com
parameters:
  clusterID: rook-ceph
  pool: kubernetes-rbd
  imageFormat: "2"
  imageFeatures: layering

  csi.storage.k8s.io/provisioner-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/provisioner-secret-namespace: rook-ceph
  csi.storage.k8s.io/controller-expand-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/controller-expand-secret-namespace: rook-ceph
  csi.storage.k8s.io/node-stage-secret-name: rook-csi-rbd-node
  csi.storage.k8s.io/node-stage-secret-namespace: rook-ceph

  csi.storage.k8s.io/fstype: ext4

allowVolumeExpansion: true
reclaimPolicy: Delete
volumeBindingMode: Immediate
```

### CephFS StorageClass (Shared Storage)

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/storageclass-cephfs.yaml`

```yaml
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: rook-cephfs
provisioner: rook-ceph.cephfs.csi.ceph.com
parameters:
  clusterID: rook-ceph
  fsName: kubernetes-cephfs
  pool: kubernetes-cephfs-data

  csi.storage.k8s.io/provisioner-secret-name: rook-csi-cephfs-provisioner
  csi.storage.k8s.io/provisioner-secret-namespace: rook-ceph
  csi.storage.k8s.io/controller-expand-secret-name: rook-csi-cephfs-provisioner
  csi.storage.k8s.io/controller-expand-secret-namespace: rook-ceph
  csi.storage.k8s.io/node-stage-secret-name: rook-csi-cephfs-node
  csi.storage.k8s.io/node-stage-secret-namespace: rook-ceph

allowVolumeExpansion: true
reclaimPolicy: Delete
volumeBindingMode: Immediate
```

### Update Kustomization

Edit: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - external-cluster-secrets.sops.yaml
  - cephcluster.yaml
  - storageclass-rbd.yaml
  - storageclass-cephfs.yaml
```

## Step 9: Create Root Kustomization

**File**: `kubernetes/apps/rook-ceph/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - rook-ceph-operator/ks.yaml
  - rook-ceph-cluster/ks.yaml
```

## Step 10: Deploy Rook-Ceph

```bash
# Commit all changes
git add kubernetes/apps/rook-ceph/
git commit -m "feat: add rook-ceph with external Proxmox Ceph cluster"
git push

# Force Flux sync
task reconcile
```

### Monitor Deployment

```bash
# Watch operator deployment
kubectl get pods -n rook-ceph -w

# Expected pods:
# - rook-ceph-operator-xxxxx
# - rook-ceph-osd-xxxxx (CSI drivers)
# - csi-rbdplugin-xxxxx (on each node)
# - csi-cephfsplugin-xxxxx (on each node)

# Check CephCluster status
kubectl get cephcluster -n rook-ceph

# Should show:
# NAME        DATADIRHOSTPATH   MONCOUNT   AGE   PHASE   MESSAGE                          HEALTH
# rook-ceph   /var/lib/rook                10m   Ready   Cluster connected successfully   HEALTH_OK

# Check StorageClasses
kubectl get storageclass

# Should show:
# NAME                   PROVISIONER                     RECLAIMPOLICY   VOLUMEBINDINGMODE
# rook-ceph-block (default)   rook-ceph.rbd.csi.ceph.com      Delete          Immediate
# rook-cephfs            rook-ceph.cephfs.csi.ceph.com   Delete          Immediate
```

## Step 11: Test Storage

### Test RBD (Block Storage)

**File**: `test-rbd-pvc.yaml` (temporary)

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-rbd
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: rook-ceph-block
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: test-rbd-pod
  namespace: default
spec:
  containers:
  - name: test
    image: busybox
    command: ["sh", "-c", "echo 'Hello from RBD' > /mnt/test.txt && sleep 3600"]
    volumeMounts:
    - name: storage
      mountPath: /mnt
  volumes:
  - name: storage
    persistentVolumeClaim:
      claimName: test-rbd
```

Deploy and test:

```bash
kubectl apply -f test-rbd-pvc.yaml

# Wait for PVC to be Bound
kubectl get pvc test-rbd -w

# Wait for pod to be Running
kubectl get pod test-rbd-pod -w

# Verify data
kubectl exec test-rbd-pod -- cat /mnt/test.txt
# Expected: Hello from RBD

# Clean up
kubectl delete -f test-rbd-pvc.yaml
```

### Test CephFS (Shared Storage)

**File**: `test-cephfs-pvc.yaml` (temporary)

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-cephfs
  namespace: default
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: rook-cephfs
  resources:
    requests:
      storage: 1Gi
---
apiVersion: v1
kind: Pod
metadata:
  name: test-cephfs-pod-1
  namespace: default
spec:
  containers:
  - name: test
    image: busybox
    command: ["sh", "-c", "echo 'Hello from CephFS pod 1' > /mnt/test1.txt && sleep 3600"]
    volumeMounts:
    - name: storage
      mountPath: /mnt
  volumes:
  - name: storage
    persistentVolumeClaim:
      claimName: test-cephfs
---
apiVersion: v1
kind: Pod
metadata:
  name: test-cephfs-pod-2
  namespace: default
spec:
  containers:
  - name: test
    image: busybox
    command: ["sh", "-c", "sleep 10 && cat /mnt/test1.txt && sleep 3600"]
    volumeMounts:
    - name: storage
      mountPath: /mnt
  volumes:
  - name: storage
    persistentVolumeClaim:
      claimName: test-cephfs
```

Deploy and test:

```bash
kubectl apply -f test-cephfs-pvc.yaml

# Wait for PVC to be Bound
kubectl get pvc test-cephfs -w

# Wait for pods to be Running
kubectl get pods -l 'app in (test-cephfs-pod-1,test-cephfs-pod-2)' -w

# Verify shared access
kubectl logs test-cephfs-pod-2
# Expected: Hello from CephFS pod 1

# Clean up
kubectl delete -f test-cephfs-pvc.yaml
```

## Troubleshooting

### Issue: PVCs stuck in Pending

**Check**:
```bash
kubectl describe pvc <pvc-name>

# Look for events like:
# - "waiting for a volume to be created"
# - "error connecting to ceph cluster"
```

**Solutions**:
```bash
# Check CSI driver pods
kubectl get pods -n rook-ceph | grep csi

# Check CSI driver logs
kubectl logs -n rook-ceph -l app=csi-rbdplugin -c csi-rbdplugin

# Verify secrets exist
kubectl get secrets -n rook-ceph

# Verify network connectivity
talosctl -n 10.20.67.4 shell
nc -zv 10.20.67.11 6789
```

### Issue: CephCluster not connected

**Check**:
```bash
kubectl describe cephcluster -n rook-ceph rook-ceph

# Look for error messages
```

**Common causes**:
- Incorrect monitor endpoints
- Incorrect admin key
- Network connectivity issues
- Firewall blocking ports 6789, 3300, 6800-7300

**Solutions**:
```bash
# Verify Ceph is accessible from Proxmox
ssh root@proxmox-node
ceph -s

# Check secrets are correct
kubectl get secret -n rook-ceph rook-ceph-mon -o yaml | grep admin-secret | base64 -d

# Recreate secrets if needed
kubectl delete secret -n rook-ceph rook-ceph-mon
# Edit and reapply external-cluster-secrets.sops.yaml
```

### Issue: CSI drivers crashing

**Check**:
```bash
kubectl logs -n rook-ceph -l app=csi-rbdplugin -c csi-rbdplugin --tail=100

# Common errors:
# - "failed to connect to monitors"
# - "authentication failed"
# - "pool does not exist"
```

**Solutions**:
```bash
# Verify pool exists in Ceph
ssh root@proxmox-node
ceph osd pool ls | grep kubernetes-rbd

# Create pool if missing
ceph osd pool create kubernetes-rbd 64 64
rbd pool init kubernetes-rbd

# Verify user permissions
ceph auth get client.kubernetes
```

## Monitoring and Maintenance

### Check Ceph Health from Kubernetes

Deploy Ceph tools pod:

```bash
kubectl create -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: rook-ceph-tools
  namespace: rook-ceph
spec:
  containers:
  - name: rook-ceph-tools
    image: quay.io/ceph/ceph:v18.2.4
    command: ["sleep", "infinity"]
    volumeMounts:
    - name: ceph-config
      mountPath: /etc/ceph
      readOnly: true
  volumes:
  - name: ceph-config
    secret:
      secretName: rook-ceph-mon
EOF

# Wait for pod to be ready
kubectl wait --for=condition=ready pod/rook-ceph-tools -n rook-ceph --timeout=60s

# Check Ceph status
kubectl exec -n rook-ceph rook-ceph-tools -- ceph status

# Check pool status
kubectl exec -n rook-ceph rook-ceph-tools -- ceph osd pool ls detail

# Check RBD images
kubectl exec -n rook-ceph rook-ceph-tools -- rbd ls -p kubernetes-rbd
```

### Prometheus Metrics

Rook exports Ceph metrics to Prometheus. Deploy Prometheus to monitor:
- Pool usage
- OSD status
- Latency metrics
- IOPS

### Backup Strategy

1. **Volume Snapshots**: Use CSI snapshots for RBD volumes
2. **Velero**: For full cluster backup including volumes
3. **Ceph Snapshots**: Native Ceph RBD snapshots via Proxmox

**Example CSI Snapshot**:

```yaml
---
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: test-snapshot
spec:
  volumeSnapshotClassName: rook-ceph-block
  source:
    persistentVolumeClaimName: test-pvc
```

## Performance Tuning

### Increase PG Count (if needed)

```bash
ssh root@proxmox-node

# Check current PG count
ceph osd pool get kubernetes-rbd pg_num

# Increase (do gradually)
ceph osd pool set kubernetes-rbd pg_num 128
ceph osd pool set kubernetes-rbd pgp_num 128
```

### Enable RBD Fast-Diff

```bash
# On Proxmox
rbd feature enable kubernetes-rbd/<image-name> fast-diff
```

### Tune CSI Driver

Edit HelmRelease to add CSI tuning parameters:

```yaml
values:
  csi:
    rbdPluginUpdateStrategy: RollingUpdate
    cephfsPluginUpdateStrategy: RollingUpdate

    # Increase provisioner replicas for HA
    provisionerReplicas: 2

    # Resource limits
    rbdProvisionerResources:
      requests:
        cpu: 100m
        memory: 128Mi
      limits:
        cpu: 500m
        memory: 512Mi
```

## Summary

You now have:
- Rook-Ceph operator managing CSI drivers
- External Ceph cluster integration with Proxmox Ceph
- RBD StorageClass for block storage (ReadWriteOnce)
- CephFS StorageClass for shared storage (ReadWriteMany)
- Monitoring and tools for Ceph management

Your applications can now request persistent storage using standard Kubernetes PVCs, backed by your Proxmox Ceph cluster.

Next steps:
- Deploy Velero for backup/restore
- Set up monitoring with Prometheus
- Configure volume snapshots
- Deploy stateful applications with persistent storage
