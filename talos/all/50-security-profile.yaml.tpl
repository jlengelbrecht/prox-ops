# Talos 1.14 can run CRI, the kubelet and all pods in a separate PID and mount
# namespace (workload isolation). Configs generated for the 1.14 contract turn
# it on, while clusters upgraded the usual way keep it off. topf regenerates the
# config, so pin it off to keep the upgrade from changing the runtime model.
# Host-level workloads (tetragon with hostPID, rook-ceph, multus) need checking
# before this is enabled deliberately in its own change.
{{- if semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion) }}
apiVersion: v1alpha1
kind: SecurityProfileConfig
workloadIsolation: false
---
# The 1.14 contract also generates a weekly FilesystemTrimConfig, which upgraded
# clusters do not get. Delete it so the upgrade does not start background trims
# on the etcd disks; enable it deliberately in its own change if wanted.
apiVersion: v1alpha1
kind: FilesystemTrimConfig
$patch: delete
{{- end }}
