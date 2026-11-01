# Install disk for every node. topf injects the installer image itself from
# talosVersion, platform and schematicId in topf.yaml.
#
# Version-dependent patches (*.yaml.tpl) branch on the same version topf uses
# for the config contract: the node's running version when topf talks to it
# (apply, render --online), otherwise talosVersion from topf.yaml. During a
# rolling upgrade each node therefore gets the document set its running Talos
# understands.
#
# Talos 1.14 replaces .machine.install with an UnattendedInstallConfig document
# and rejects configs that carry both.
{{- if semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion) }}
apiVersion: v1alpha1
kind: UnattendedInstallConfig
provisioning:
  diskSelector:
    match: disk.dev_path == "/dev/sda"
  wipe: false
{{- else }}
machine:
  install:
    disk: /dev/sda
    wipe: false
{{- end }}
