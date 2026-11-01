# Disable PodSecurity admission enforcement (cluster-wide permissive).
# Rationale: privileged workloads (rook-ceph, multus, tetragon, nvidia device
# plugin) require host access. Strategic-merge `enforce: privileged` keeps the
# live cluster behavior without a JSON6902 remove, which topf does not support.
{{- if semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion) }}
apiVersion: v1alpha1
kind: KubeAdmissionControlConfig
name: PodSecurity
configuration:
  apiVersion: pod-security.admission.config.k8s.io/v1alpha1
  kind: PodSecurityConfiguration
  defaults:
    enforce: privileged
    enforce-version: latest
    audit: privileged
    audit-version: latest
    warn: privileged
    warn-version: latest
  # The generated kube-system exemption stays: list fields merge, so an empty
  # list cannot clear it. With every level privileged it has no effect.
{{- else }}
cluster:
  apiServer:
    admissionControl:
      - name: PodSecurity
        configuration:
          apiVersion: pod-security.admission.config.k8s.io/v1alpha1
          kind: PodSecurityConfiguration
          defaults:
            enforce: privileged
            enforce-version: latest
            audit: privileged
            audit-version: latest
            warn: privileged
            warn-version: latest
          exemptions:
            namespaces: []
            runtimeClasses: []
            usernames: []
{{- end }}
