# NVIDIA GPU Support for Talos Kubernetes Cluster

**Document Created**: 2025-10-31
**Cluster**: prox-ops (15-node Talos Kubernetes)
**GPU Nodes**: 2 workers with NVIDIA GPUs

---

## Table of Contents

1. [Overview](#overview)
2. [GPU Hardware Inventory](#gpu-hardware-inventory)
3. [Architecture and Design Decisions](#architecture-and-design-decisions)
4. [What Was Configured](#what-was-configured)
5. [Prerequisites](#prerequisites)
6. [Post-Bootstrap GPU Enablement](#post-bootstrap-gpu-enablement)
7. [Verification and Testing](#verification-and-testing)
8. [Using GPUs in Workloads](#using-gpus-in-workloads)
9. [Advanced Features](#advanced-features)
10. [Troubleshooting](#troubleshooting)
11. [Maintenance and Updates](#maintenance-and-updates)

---

## Overview

This cluster includes GPU support for AI/ML workloads, rendering, and compute-intensive tasks using NVIDIA professional GPUs. The implementation uses the **NVIDIA Device Plugin** approach (not GPU Operator) due to Talos Linux's security model and read-only filesystem.

### Key Components

- **Talos GPU Extension**: NVIDIA proprietary drivers and container toolkit
- **NVIDIA Device Plugin**: Exposes GPUs to Kubernetes scheduler
- **RuntimeClass**: Configures containerd to use NVIDIA runtime
- **GPU Feature Discovery**: Automatically labels nodes with GPU properties

---

## GPU Hardware Inventory

| Node | IP Address | GPU Model | GPU Class | VRAM | Use Cases |
|------|------------|-----------|-----------|------|-----------|
| k8s-work-3 | 10.20.67.6 | NVIDIA RTX A2000 | Professional | 6GB GDDR6 | AI inference, video encoding, CAD |
| k8s-work-12 | 10.20.67.15 | NVIDIA RTX A5000 | Professional | 24GB GDDR6 | AI training, rendering, simulation |

### GPU Specifications

**NVIDIA RTX A2000** (Worker 3):
- CUDA Cores: 3,328
- Tensor Cores: 104 (3rd gen)
- RT Cores: 26 (2nd gen)
- Memory: 6GB GDDR6 ECC
- Memory Bandwidth: 192 GB/s
- TDP: 70W
- Best for: AI inference, small model training, video transcoding

**NVIDIA RTX A5000** (Worker 12):
- CUDA Cores: 8,192
- Tensor Cores: 256 (3rd gen)
- RT Cores: 64 (2nd gen)
- Memory: 24GB GDDR6 ECC
- Memory Bandwidth: 768 GB/s
- TDP: 230W
- Best for: Large model training, 3D rendering, complex simulations

---

## Architecture and Design Decisions

### Why NVIDIA Device Plugin (Not GPU Operator)?

**Decision**: Use NVIDIA Device Plugin + manual driver installation via Talos extensions

**Rationale**:
1. **Talos Compatibility**: Talos has a read-only filesystem and no shell access
2. **Security**: Talos only loads signed kernel modules
3. **GPU Operator Issues**: The NVIDIA GPU Operator expects a traditional Linux environment with:
   - Writable filesystem for driver installation
   - `/bin/sh` binary (absent in Talos)
   - Ability to load unsigned kernel modules
4. **Community Experience**: Talos community reports GPU Operator results in broken state
5. **Simplicity**: Device plugin is lightweight and well-tested with Talos

### Talos Schematic Approach

**GPU Workers** (work-3, work-12):
- Schematic ID: `990731763242a6b3cf735e49d0f550ce4068b4d0e7f4dfbb49a31799b698877e`
- Extensions:
  - `siderolabs/nonfree-kmod-nvidia-production` (NVIDIA drivers)
  - `siderolabs/nvidia-container-toolkit-production` (Container runtime integration)
  - `siderolabs/qemu-guest-agent` (Proxmox integration)

**Non-GPU Workers** (work-1,2,4-11) and Controllers:
- Schematic ID: `ce4c980550dd2ab1b17bbf2b08801c7eb59418eafe8f279833297925d67c7515`
- Extensions:
  - `siderolabs/qemu-guest-agent` only
- **Why separate schematic?**: Cleaner, more secure, smaller image size

### Node Labeling Strategy

GPU nodes are automatically labeled with:
- `nvidia.com/gpu.present: "true"` - Indicates GPU presence
- `gpu.nvidia.com/model: "rtx-a2000"` or `"rtx-a5000"` - GPU model
- `gpu.nvidia.com/class: "professional"` - GPU category
- Additional labels from GPU Feature Discovery (GFD):
  - `nvidia.com/gpu.family` - GPU architecture (e.g., "ampere")
  - `nvidia.com/gpu.product` - Full product name
  - `nvidia.com/gpu.count` - Number of GPUs
  - `nvidia.com/gpu.memory` - VRAM in MB
  - `nvidia.com/cuda.driver.version` - Driver version
  - `nvidia.com/cuda.runtime.version` - CUDA runtime version

---

## What Was Configured

### 1. Talos Configuration

**File**: `/home/devbox/repos/jlengelbrecht/prox-ops/nodes.yaml`

Updated GPU workers (work-3, work-12) to use GPU schematic:

```yaml
- name: "k8s-work-3"
  address: "10.20.67.6"
  schematic_id: "990731763242a6b3cf735e49d0f550ce4068b4d0e7f4dfbb49a31799b698877e"  # GPU schematic

- name: "k8s-work-12"
  address: "10.20.67.15"
  schematic_id: "990731763242a6b3cf735e49d0f550ce4068b4d0e7f4dfbb49a31799b698877e"  # GPU schematic
```

**Files**: `templates/config/talos/patches/k8s-work-3/nvidia-gpu.yaml.j2` and `k8s-work-12/nvidia-gpu.yaml.j2`

Machine configuration patches for GPU workers:

```yaml
machine:
  kernel:
    modules:
      - name: nvidia
      - name: nvidia_uvm
      - name: nvidia_drm
      - name: nvidia_modeset
  sysctls:
    net.core.bpf_jit_harden: "1"  # NVIDIA requirement
  nodeLabels:
    nvidia.com/gpu.present: "true"
    gpu.nvidia.com/model: "rtx-a2000"  # or "rtx-a5000"
    gpu.nvidia.com/class: "professional"
```

### 2. Kubernetes Configuration

**Directory**: `templates/config/kubernetes/apps/kube-system/nvidia-device-plugin/`

Created complete NVIDIA Device Plugin deployment:

- **HelmRepository**: Points to NVIDIA's official Helm repo
- **HelmRelease**: Configures device plugin with:
  - Node affinity (only runs on GPU nodes)
  - RuntimeClass: `nvidia`
  - GPU Feature Discovery (GFD) enabled
  - Tolerations for potential GPU taints
  - Resource limits
  - System-critical priority

**File**: `templates/config/kubernetes/apps/kube-system/runtimeclass/nvidia-runtimeclass.yaml.j2`

RuntimeClass definition for GPU workloads:

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: nvidia
handler: nvidia
scheduling:
  nodeSelector:
    nvidia.com/gpu.present: "true"
```

### 3. Integration

Updated `templates/config/kubernetes/apps/kube-system/kustomization.yaml.j2` to include:
- RuntimeClass resource
- NVIDIA Device Plugin kustomization

---

## Prerequisites

Before enabling GPU support, ensure:

1. **Cluster is bootstrapped**: All 15 nodes are running and healthy
   ```bash
   kubectl get nodes
   # All nodes should show "Ready"
   ```

2. **Flux is operational**: GitOps is syncing
   ```bash
   flux check
   flux get kustomizations
   ```

3. **GPU workers have correct schematic**: Verify via Talos API
   ```bash
   talosctl get extensions --nodes 10.20.67.6
   talosctl get extensions --nodes 10.20.67.15
   # Should show: nonfree-kmod-nvidia-production, nvidia-container-toolkit-production
   ```

4. **Network connectivity**: GPU workers can pull Helm charts and images

---

## Post-Bootstrap GPU Enablement

After cluster bootstrap, GPU support is automatically enabled via Flux GitOps. Here's what happens:

### Automatic Deployment Sequence

1. **Flux Detects Configuration** (immediate):
   ```bash
   flux reconcile source git flux-system
   ```

2. **RuntimeClass Created** (within 1 minute):
   ```bash
   kubectl get runtimeclass nvidia
   # Should show: NAME: nvidia, HANDLER: nvidia
   ```

3. **NVIDIA Device Plugin DaemonSet Deployed** (2-5 minutes):
   - Helm chart pulled from NVIDIA repo
   - DaemonSet scheduled only on GPU nodes (work-3, work-12)
   - Device plugin pods start and register GPUs with Kubernetes

4. **GPU Feature Discovery Deployed** (same time):
   - GFD pods start on GPU nodes
   - Automatically label nodes with GPU properties

### Manual Verification Steps

**Step 1: Check RuntimeClass**

```bash
kubectl get runtimeclass
```

Expected output:
```
NAME     HANDLER   AGE
nvidia   nvidia    5m
```

**Step 2: Check HelmRelease Status**

```bash
kubectl get helmrelease -n kube-system nvidia-device-plugin
```

Expected output:
```
NAME                   AGE   READY   STATUS
nvidia-device-plugin   5m    True    Release reconciliation succeeded
```

**Step 3: Check Device Plugin Pods**

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=nvidia-device-plugin
```

Expected output:
```
NAME                           READY   STATUS    RESTARTS   AGE
nvidia-device-plugin-xxxxx     1/1     Running   0          5m   # On work-3
nvidia-device-plugin-yyyyy     1/1     Running   0          5m   # On work-12
```

**Step 4: Check GPU Feature Discovery Pods**

```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=gpu-feature-discovery
```

Expected output:
```
NAME                           READY   STATUS    RESTARTS   AGE
gpu-feature-discovery-xxxxx    1/1     Running   0          5m   # On work-3
gpu-feature-discovery-yyyyy    1/1     Running   0          5m   # On work-12
```

**Step 5: Verify Node Labels**

```bash
kubectl get nodes --show-labels | grep nvidia
```

Or for specific node:
```bash
kubectl describe node k8s-work-3 | grep -A 20 "Labels:"
```

Expected labels:
```
nvidia.com/gpu.present=true
nvidia.com/gpu.count=1
nvidia.com/gpu.family=ampere
nvidia.com/gpu.product=NVIDIA-RTX-A2000
nvidia.com/gpu.memory=6144
nvidia.com/cuda.driver.version=XXX.XX
gpu.nvidia.com/model=rtx-a2000
gpu.nvidia.com/class=professional
```

**Step 6: Check GPU Capacity**

```bash
kubectl get nodes k8s-work-3 k8s-work-12 -o json | jq '.items[] | {name: .metadata.name, gpus: .status.capacity["nvidia.com/gpu"]}'
```

Expected output:
```json
{
  "name": "k8s-work-3",
  "gpus": "1"
}
{
  "name": "k8s-work-12",
  "gpus": "1"
}
```

---

## Verification and Testing

### Test 1: Basic GPU Detection

Deploy a simple CUDA test pod:

```bash
kubectl run nvidia-test \
  --rm -it \
  --restart=Never \
  --image=nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04 \
  --overrides='{"spec": {"runtimeClassName": "nvidia", "nodeSelector": {"nvidia.com/gpu.present": "true"}}}' \
  -- nvidia-smi
```

**Expected output**:
```
+-----------------------------------------------------------------------------+
| NVIDIA-SMI 550.XX.XX    Driver Version: 550.XX.XX    CUDA Version: 12.4   |
|-------------------------------+----------------------+----------------------+
| GPU  Name        Persistence-M| Bus-Id        Disp.A | Volatile Uncorr. ECC |
| Fan  Temp  Perf  Pwr:Usage/Cap|         Memory-Usage | GPU-Util  Compute M. |
|===============================+======================+======================|
|   0  NVIDIA RTX A2000    Off  | 00000000:XX:XX.0 Off |                  Off |
| 30%   32C    P8    10W /  70W |      0MiB /  6144MiB |      0%      Default |
+-------------------------------+----------------------+----------------------+
```

### Test 2: GPU-Accelerated Workload (CUDA)

Create a file: `gpu-cuda-test.yaml`

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: cuda-vector-add
spec:
  runtimeClassName: nvidia
  restartPolicy: OnFailure
  containers:
    - name: cuda-test
      image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1-ubuntu20.04
      resources:
        limits:
          nvidia.com/gpu: 1
  nodeSelector:
    nvidia.com/gpu.present: "true"
```

Deploy and check logs:

```bash
kubectl apply -f gpu-cuda-test.yaml
kubectl wait --for=condition=complete --timeout=60s pod/cuda-vector-add
kubectl logs cuda-vector-add
```

**Expected output**:
```
[Vector addition of 50000 elements]
Copy input data from the host memory to the CUDA device
CUDA kernel launch with 196 blocks of 256 threads
Copy output data from the CUDA device to the host memory
Test PASSED
Done
```

### Test 3: Specific GPU Selection

Test targeting specific GPU model:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: rtx-a5000-test
spec:
  runtimeClassName: nvidia
  restartPolicy: Never
  containers:
    - name: cuda
      image: nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04
      command: ["nvidia-smi", "-L"]
      resources:
        limits:
          nvidia.com/gpu: 1
  nodeSelector:
    gpu.nvidia.com/model: "rtx-a5000"  # Only run on A5000
```

```bash
kubectl apply -f rtx-a5000-test.yaml
kubectl logs rtx-a5000-test
```

**Expected output**:
```
GPU 0: NVIDIA RTX A5000 (UUID: GPU-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX)
```

### Test 4: TensorFlow GPU Test

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: tensorflow-gpu-test
spec:
  runtimeClassName: nvidia
  restartPolicy: Never
  containers:
    - name: tensorflow
      image: tensorflow/tensorflow:latest-gpu
      command:
        - python
        - -c
        - |
          import tensorflow as tf
          print("TensorFlow version:", tf.__version__)
          print("GPU Available:", tf.test.is_gpu_available())
          print("GPU Devices:", tf.config.list_physical_devices('GPU'))
      resources:
        limits:
          nvidia.com/gpu: 1
  nodeSelector:
    nvidia.com/gpu.present: "true"
```

```bash
kubectl apply -f tensorflow-gpu-test.yaml
kubectl logs tensorflow-gpu-test
```

**Expected output**:
```
TensorFlow version: 2.X.X
GPU Available: True
GPU Devices: [PhysicalDevice(name='/physical_device:GPU:0', device_type='GPU')]
```

### Test 5: PyTorch GPU Test

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: pytorch-gpu-test
spec:
  runtimeClassName: nvidia
  restartPolicy: Never
  containers:
    - name: pytorch
      image: pytorch/pytorch:latest
      command:
        - python
        - -c
        - |
          import torch
          print("PyTorch version:", torch.__version__)
          print("CUDA available:", torch.cuda.is_available())
          print("CUDA version:", torch.version.cuda)
          print("GPU count:", torch.cuda.device_count())
          if torch.cuda.is_available():
              print("GPU name:", torch.cuda.get_device_name(0))
      resources:
        limits:
          nvidia.com/gpu: 1
  nodeSelector:
    nvidia.com/gpu.present: "true"
```

```bash
kubectl apply -f pytorch-gpu-test.yaml
kubectl logs pytorch-gpu-test
```

**Expected output**:
```
PyTorch version: 2.X.X
CUDA available: True
CUDA version: 12.X
GPU count: 1
GPU name: NVIDIA RTX A2000
```

---

## Using GPUs in Workloads

### Deployment Pattern

To use GPUs in your workloads:

1. **Specify RuntimeClass**: `runtimeClassName: nvidia`
2. **Request GPU Resources**: `nvidia.com/gpu: 1` in limits
3. **Use Node Selector** (optional): Target specific GPU nodes/models

### Example: Stable Diffusion Inference

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: stable-diffusion-api
  namespace: ai-workloads
spec:
  replicas: 1
  selector:
    matchLabels:
      app: stable-diffusion
  template:
    metadata:
      labels:
        app: stable-diffusion
    spec:
      runtimeClassName: nvidia
      containers:
        - name: sd-api
          image: ghcr.io/example/stable-diffusion-api:latest
          ports:
            - containerPort: 8000
          resources:
            requests:
              memory: "16Gi"
              cpu: "4"
              nvidia.com/gpu: 1
            limits:
              memory: "24Gi"
              nvidia.com/gpu: 1
          env:
            - name: MODEL_PATH
              value: "/models/stable-diffusion-v1-5"
      nodeSelector:
        gpu.nvidia.com/model: "rtx-a5000"  # Use the bigger GPU
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
```

### Example: Ollama (Local LLM)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ollama
  namespace: ai-workloads
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ollama
  template:
    metadata:
      labels:
        app: ollama
    spec:
      runtimeClassName: nvidia
      containers:
        - name: ollama
          image: ollama/ollama:latest
          ports:
            - containerPort: 11434
          resources:
            requests:
              memory: "8Gi"
              nvidia.com/gpu: 1
            limits:
              memory: "12Gi"
              nvidia.com/gpu: 1
          volumeMounts:
            - name: models
              mountPath: /root/.ollama
      volumes:
        - name: models
          persistentVolumeClaim:
            claimName: ollama-models
      nodeSelector:
        nvidia.com/gpu.present: "true"
```

### Example: Video Transcoding (FFmpeg)

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: video-transcode
spec:
  template:
    spec:
      runtimeClassName: nvidia
      restartPolicy: Never
      containers:
        - name: ffmpeg
          image: jrottenberg/ffmpeg:nvidia
          command:
            - ffmpeg
            - -hwaccel
            - cuda
            - -i
            - /input/video.mp4
            - -c:v
            - h264_nvenc
            - -preset
            - fast
            - /output/video-transcoded.mp4
          resources:
            limits:
              nvidia.com/gpu: 1
          volumeMounts:
            - name: input
              mountPath: /input
            - name: output
              mountPath: /output
      volumes:
        - name: input
          persistentVolumeClaim:
            claimName: video-input
        - name: output
          persistentVolumeClaim:
            claimName: video-output
      nodeSelector:
        nvidia.com/gpu.present: "true"
```

---

## Advanced Features

### Time Slicing (Share GPU Among Multiple Pods)

If you want to share a single GPU among multiple pods (for inference workloads with low GPU utilization):

**Not currently configured**. To enable:

1. Create a ConfigMap with time-slicing config:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: device-plugin-config
  namespace: kube-system
data:
  config.yaml: |
    version: v1
    sharing:
      timeSlicing:
        resources:
          - name: nvidia.com/gpu
            replicas: 4  # Make each GPU available to 4 pods
```

2. Update HelmRelease to reference the ConfigMap
3. Each pod will get 1/4 of GPU time

**Trade-off**: Increased concurrency but potential performance impact.

### MIG (Multi-Instance GPU)

Not applicable for RTX A2000/A5000. MIG is only available on:
- NVIDIA A100
- NVIDIA A30
- NVIDIA H100

If you upgrade to these GPUs in the future, MIG allows partitioning a single GPU into multiple isolated GPU instances.

### GPU Monitoring

To monitor GPU utilization, install DCGM Exporter:

```bash
# Add to your flux kustomization
kubectl apply -f - <<EOF
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: dcgm-exporter
  namespace: monitoring
spec:
  chart:
    spec:
      chart: dcgm-exporter
      version: 3.4.2
      sourceRef:
        kind: HelmRepository
        name: nvidia
        namespace: flux-system
  interval: 1h
  values:
    affinity:
      nodeAffinity:
        requiredDuringSchedulingIgnoredDuringExecution:
          nodeSelectorTerms:
            - matchExpressions:
                - key: nvidia.com/gpu.present
                  operator: In
                  values:
                    - "true"
    serviceMonitor:
      enabled: true
EOF
```

Then create Grafana dashboards to visualize:
- GPU utilization
- GPU memory usage
- GPU temperature
- GPU power consumption
- Per-pod GPU metrics

---

## Troubleshooting

### Issue 1: Device Plugin Pods Not Starting

**Symptoms**:
```bash
kubectl get pods -n kube-system -l app.kubernetes.io/name=nvidia-device-plugin
# No pods or pods in Pending state
```

**Diagnosis**:

```bash
# Check HelmRelease status
kubectl describe helmrelease -n kube-system nvidia-device-plugin

# Check if GPU nodes are labeled
kubectl get nodes -l nvidia.com/gpu.present=true

# Check if nodes have GPU schematic
talosctl get extensions --nodes 10.20.67.6
talosctl get extensions --nodes 10.20.67.15
```

**Solutions**:

1. **No GPU nodes labeled**: Apply Talos patches and reboot GPU workers
2. **HelmRelease failed**: Check Flux logs, verify Helm repo is accessible
3. **Extensions missing**: Nodes need to be reinstalled with GPU schematic

### Issue 2: GPUs Not Showing in Node Capacity

**Symptoms**:
```bash
kubectl describe node k8s-work-3 | grep nvidia.com/gpu
# No output or capacity: 0
```

**Diagnosis**:

```bash
# Check device plugin logs
kubectl logs -n kube-system -l app.kubernetes.io/name=nvidia-device-plugin --tail=100

# Check if NVIDIA kernel modules are loaded
talosctl read /proc/modules --nodes 10.20.67.6 | grep nvidia

# Verify NVIDIA driver is present
talosctl read /proc/driver/nvidia/version --nodes 10.20.67.6
```

**Solutions**:

1. **Kernel modules not loaded**: Check Talos machine config, verify patches applied
2. **Driver not loaded**: Reboot GPU worker
3. **Device plugin error**: Check logs for permission or configuration issues

### Issue 3: Pods Fail to Start with GPU Requests

**Symptoms**:
```bash
kubectl get pods
# Pod stuck in Pending state
```

**Diagnosis**:

```bash
kubectl describe pod <pod-name>
# Check Events section for scheduling failures
```

**Common errors and solutions**:

1. **"0/15 nodes are available: 15 Insufficient nvidia.com/gpu"**
   - No GPUs available or already allocated
   - Check: `kubectl get nodes -o json | jq '.items[] | {name: .metadata.name, capacity: .status.capacity["nvidia.com/gpu"], allocatable: .status.allocatable["nvidia.com/gpu"]}'`

2. **"RuntimeClass 'nvidia' not found"**
   - RuntimeClass not created
   - Check: `kubectl get runtimeclass nvidia`
   - Solution: Ensure Flux has synced the RuntimeClass manifest

3. **"Node didn't match Pod's node affinity/selector"**
   - Node selector mismatch
   - Verify node labels match pod's nodeSelector

### Issue 4: CUDA Version Mismatch

**Symptoms**:
Container logs show:
```
Failed to initialize CUDA: CUDA driver version is insufficient for CUDA runtime version
```

**Solution**:
- Talos includes the NVIDIA driver in the schematic
- Container images include CUDA runtime
- Ensure container image CUDA runtime version ≤ driver version
- Check driver version: `talosctl read /proc/driver/nvidia/version --nodes 10.20.67.6`
- Use container images with compatible CUDA runtime (usually older versions work with newer drivers)

### Issue 5: GPU Memory Errors

**Symptoms**:
```
RuntimeError: CUDA out of memory
```

**Diagnosis**:

```bash
# Check GPU memory usage
kubectl exec -it <pod-name> -- nvidia-smi

# Check if multiple pods are sharing GPU (shouldn't happen without time-slicing)
kubectl get pods -A -o wide | grep k8s-work-3
```

**Solutions**:

1. **Reduce batch size**: Adjust application memory usage
2. **Use A5000 instead of A2000**: Move workload to node with 24GB GPU
3. **Insufficient VRAM for model**: Choose smaller model or quantized version
4. **Memory leak**: Restart pod

### Issue 6: Slow GPU Performance

**Possible causes**:

1. **CPU bottleneck**: Increase CPU requests/limits
2. **Memory bandwidth**: Check if system RAM is saturated
3. **PCIe bandwidth**: Verify GPU is in PCIe slot with enough lanes
4. **Thermal throttling**: Check GPU temperature via nvidia-smi
5. **Multiple pods competing**: Verify time-slicing isn't enabled unintentionally

---

## Maintenance and Updates

### Updating NVIDIA Drivers

**Important**: Driver updates require updating the Talos schematic and upgrading nodes.

**Process**:

1. **Check available driver versions**:
   - Visit: https://factory.talos.dev/
   - Search for: `nonfree-kmod-nvidia-production`
   - Note latest version

2. **Create new schematic**:
   - At https://factory.talos.dev/
   - Select Talos version (e.g., 1.11.3)
   - Add extensions:
     - `siderolabs/nonfree-kmod-nvidia-production` (new version)
     - `siderolabs/nvidia-container-toolkit-production` (matching version)
     - `siderolabs/qemu-guest-agent`
   - Copy new schematic ID

3. **Update nodes.yaml**:
   ```yaml
   - name: "k8s-work-3"
     schematic_id: "<new-schematic-id>"
   - name: "k8s-work-12"
     schematic_id: "<new-schematic-id>"
   ```

4. **Regenerate Talos configs**:
   ```bash
   task configure
   ```

5. **Upgrade nodes** (one at a time):
   ```bash
   # Drain node
   kubectl drain k8s-work-3 --ignore-daemonsets --delete-emptydir-data

   # Upgrade node
   talosctl upgrade --nodes 10.20.67.6 \
     --image factory.talos.dev/installer/<new-schematic-id>:v1.11.3

   # Wait for node to come back (5-10 minutes)
   kubectl wait --for=condition=Ready node/k8s-work-3 --timeout=10m

   # Uncordon node
   kubectl uncordon k8s-work-3

   # Verify GPU still works
   kubectl run nvidia-test-work3 --rm -it --restart=Never \
     --image=nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04 \
     --overrides='{"spec": {"runtimeClassName": "nvidia", "nodeSelector": {"kubernetes.io/hostname": "k8s-work-3"}}}' \
     -- nvidia-smi

   # Repeat for work-12
   ```

### Updating Device Plugin

Device plugin updates via Helm chart version in `helmrelease.yaml.j2`:

```yaml
spec:
  chart:
    spec:
      chart: nvidia-device-plugin
      version: 0.17.1  # Update this version
```

After updating:
```bash
git add templates/config/kubernetes/apps/kube-system/nvidia-device-plugin/app/helmrelease.yaml.j2
git commit -m "feat: update nvidia-device-plugin to vX.Y.Z"
git push

# Flux will automatically update
flux reconcile helmrelease -n kube-system nvidia-device-plugin
```

### Monitoring GPU Health

**Daily health check script**:

```bash
#!/bin/bash
# gpu-health-check.sh

echo "=== GPU Node Status ==="
kubectl get nodes -l nvidia.com/gpu.present=true

echo -e "\n=== GPU Capacity ==="
kubectl get nodes -l nvidia.com/gpu.present=true -o custom-columns=\
NAME:.metadata.name,\
GPU-CAPACITY:.status.capacity.nvidia\\.com/gpu,\
GPU-ALLOCATABLE:.status.allocatable.nvidia\\.com/gpu

echo -e "\n=== Device Plugin Pods ==="
kubectl get pods -n kube-system -l app.kubernetes.io/name=nvidia-device-plugin

echo -e "\n=== GPU Workloads ==="
kubectl get pods -A -o json | jq -r '
  .items[] |
  select(.spec.containers[].resources.limits["nvidia.com/gpu"] != null) |
  "\(.metadata.namespace)/\(.metadata.name) on \(.spec.nodeName) - \(.status.phase)"
'

echo -e "\n=== GPU Status (work-3) ==="
kubectl run gpu-check-work3 --rm -i --restart=Never \
  --image=nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04 \
  --overrides='{"spec": {"runtimeClassName": "nvidia", "nodeSelector": {"kubernetes.io/hostname": "k8s-work-3"}}}' \
  -- nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv

echo -e "\n=== GPU Status (work-12) ==="
kubectl run gpu-check-work12 --rm -i --restart=Never \
  --image=nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04 \
  --overrides='{"spec": {"runtimeClassName": "nvidia", "nodeSelector": {"kubernetes.io/hostname": "k8s-work-12"}}}' \
  -- nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv
```

**Usage**:
```bash
chmod +x gpu-health-check.sh
./gpu-health-check.sh
```

---

## Additional Resources

### Official Documentation

- **Talos NVIDIA GPU Guide**: https://docs.siderolabs.com/talos/v1.11/configure-your-talos-cluster/hardware-and-drivers/nvidia-gpu-proprietary
- **NVIDIA Device Plugin**: https://github.com/NVIDIA/k8s-device-plugin
- **NVIDIA Container Toolkit**: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/
- **Talos Extensions**: https://github.com/siderolabs/extensions

### Community Resources

- **Talos GitHub Discussions**: https://github.com/siderolabs/talos/discussions
- **NVIDIA GPU Operator Issues**: https://github.com/NVIDIA/gpu-operator/issues
- **Kubernetes GPU Discussion**: https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/

### Example GPU Workloads

- **Ollama (LLM)**: https://ollama.ai/
- **Stable Diffusion**: https://github.com/AUTOMATIC1111/stable-diffusion-webui
- **Jupyter with GPU**: https://jupyter-docker-stacks.readthedocs.io/
- **TensorFlow Serving**: https://www.tensorflow.org/tfx/serving/docker
- **PyTorch Serve**: https://pytorch.org/serve/
- **Plex Hardware Transcoding**: https://support.plex.tv/articles/

---

## Summary

Your Talos Kubernetes cluster now has GPU support for AI/ML workloads with:

- 2 GPU workers (RTX A2000 6GB, RTX A5000 24GB)
- NVIDIA Device Plugin for GPU scheduling
- RuntimeClass for GPU container runtime
- Automatic GPU feature discovery and node labeling
- Production-ready configuration following Talos best practices

To use GPUs in your workloads:
1. Add `runtimeClassName: nvidia` to pod spec
2. Request `nvidia.com/gpu: 1` in resources.limits
3. Optionally use node selectors to target specific GPU models

For questions or issues, refer to the troubleshooting section or official Talos documentation.

---

**Document Version**: 1.0
**Last Updated**: 2025-10-31
**Cluster**: prox-ops
**Talos Version**: 1.11.3
**Device Plugin Version**: 0.17.1
