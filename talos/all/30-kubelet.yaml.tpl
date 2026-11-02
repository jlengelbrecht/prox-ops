{{- if semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion) }}
apiVersion: v1alpha1
kind: KubeletConfig
config:
  serializeImagePulls: false
---
apiVersion: v1alpha1
kind: KubeNodeConfig
nodeIP:
  validSubnets:
    - 10.20.66.0/23
# The Proxmox host is the real failure domain: one host reboot takes down every
# VM on it. The scheduler's default spreading then keeps replicas on different hosts.
labels:
  topology.kubernetes.io/zone: {{ .Node.Data.zone }}
{{- else }}
machine:
  kubelet:
    extraConfig:
      serializeImagePulls: false
    nodeIP:
      validSubnets:
        - 10.20.66.0/23
  nodeLabels:
    topology.kubernetes.io/zone: {{ .Node.Data.zone }}
{{- end }}
