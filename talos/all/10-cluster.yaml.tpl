machine:
  certSANs:
    - 127.0.0.1
    - 10.20.67.20
{{- if semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion) }}
---
apiVersion: v1alpha1
kind: KubeNetworkConfig
podSubnets:
  - 10.42.0.0/16
serviceSubnets:
  - 10.43.0.0/16
---
# Built-in CNI disabled: Cilium is installed separately. From Talos 1.14 the
# presence of this document is what deploys Flannel, so delete it.
apiVersion: v1alpha1
kind: KubeFlannelCNIConfig
$patch: delete
{{- else }}
cluster:
  network:
    # Built-in CNI disabled: Cilium is installed separately.
    cni:
      name: none
    podSubnets:
      - 10.42.0.0/16
    serviceSubnets:
      - 10.43.0.0/16
{{- end }}
