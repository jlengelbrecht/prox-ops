cluster:
  etcd:
    extraArgs:
      listen-metrics-urls: http://0.0.0.0:2381
    advertisedSubnets:
      - 10.20.66.0/23
{{- if semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion) }}
---
apiVersion: v1alpha1
kind: KubeAPIServerConfig
certExtraSANs:
  - 127.0.0.1
  - 10.20.67.20
extraArgs:
  # https://kubernetes.io/docs/tasks/extend-kubernetes/configure-aggregation-layer/
  enable-aggregator-routing: "true"
---
apiVersion: v1alpha1
kind: KubeControllerManagerConfig
extraArgs:
  bind-address: 0.0.0.0
---
apiVersion: v1alpha1
kind: KubeSchedulerConfig
extraArgs:
  bind-address: 0.0.0.0
---
apiVersion: v1alpha1
kind: KubeCoreDNSConfig
enabled: false
---
apiVersion: v1alpha1
kind: KubeProxyConfig
enabled: false
---
# Replaces .cluster.allowSchedulingOnControlPlanes: false and .machine.nodeTaints.
apiVersion: v1alpha1
kind: KubeNodeConfig
taints:
  node-role.kubernetes.io/control-plane: NoSchedule
{{- else }}
---
cluster:
  allowSchedulingOnControlPlanes: false
  apiServer:
    certSANs:
      - 127.0.0.1
      - 10.20.67.20
    extraArgs:
      # https://kubernetes.io/docs/tasks/extend-kubernetes/configure-aggregation-layer/
      enable-aggregator-routing: true
  controllerManager:
    extraArgs:
      bind-address: 0.0.0.0
  coreDNS:
    disabled: true
  proxy:
    disabled: true
  scheduler:
    extraArgs:
      bind-address: 0.0.0.0
machine:
  nodeTaints:
    node-role.kubernetes.io/control-plane: NoSchedule
{{- end }}
