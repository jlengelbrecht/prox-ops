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
{{- else }}
machine:
  kubelet:
    extraConfig:
      serializeImagePulls: false
    nodeIP:
      validSubnets:
        - 10.20.66.0/23
{{- end }}
