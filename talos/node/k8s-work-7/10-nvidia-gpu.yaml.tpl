machine:
  kernel:
    modules:
      - name: nvidia
      - name: nvidia_uvm
      - name: nvidia_drm
      - name: nvidia_modeset
  sysctls:
    net.core.bpf_jit_harden: "1"  # NVIDIA GPU requirement
  # No machine.files drop-in for the nvidia containerd runtime: the
  # nvidia-container-toolkit system extension already registers it via
  # /etc/cri/conf.d/10-nvidia-container-runtime.part (verified on work-4/work-10 —
  # the active cri.toml sources the nvidia runtime from that extension file). A
  # manual /etc/cri drop-in is redundant, and writing under /etc fails the
  # writeUserFiles task on a fresh cattle cold boot (Talos 1.11+ read-only fs).
{{- if semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion) }}
---
apiVersion: v1alpha1
kind: KubeNodeConfig
labels:
  nvidia.com/gpu.present: "true"
  gpu.nvidia.com/model: "rtx-a5000"
  gpu.nvidia.com/class: "professional"
{{- else }}
---
machine:
  nodeLabels:
    nvidia.com/gpu.present: "true"
    gpu.nvidia.com/model: "rtx-a5000"
    gpu.nvidia.com/class: "professional"
{{- end }}
