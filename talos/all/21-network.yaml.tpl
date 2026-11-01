{{- if semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion) }}
apiVersion: v1alpha1
kind: ResolverConfig
nameservers:
  - address: 1.1.1.1
  - address: 1.0.0.1
searchDomains:
  disableDefault: true
{{- else }}
machine:
  network:
    disableSearchDomain: true
    nameservers:
      - 1.1.1.1
      - 1.0.0.1
{{- end }}
