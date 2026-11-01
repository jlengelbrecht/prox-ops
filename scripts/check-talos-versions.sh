#!/usr/bin/env bash
# Fail if the desired Talos or Kubernetes version differs between the files that
# carry a copy of it. talos/topf.yaml is the source of truth; Renovate bumps every
# copy in one PR and this check catches a copy that was missed or edited by hand.
#
# Usage: scripts/check-talos-versions.sh [repo-root]
set -euo pipefail

ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

# extract <file> <regex with one capture group>: print the captured version,
# or nothing when no line matches. Patterns are anchored at both ends, so a
# pre-release suffix or a missing v prefix reads as nothing and fails the check.
extract() {
  sed -nE "s/$2/\1/p" "$1" | head -n1
}

SEMVER='([0-9]+\.[0-9]+\.[0-9]+)'
# YAML copies carry a v prefix, optionally quoted, optionally followed by a comment.
yaml_value() { echo "[[:space:]]*\"?v${SEMVER}\"?[[:space:]]*(#.*)?$"; }
# Terraform copies are a quoted plain version with nothing after the quote.
tf_value() { echo "[[:space:]]*=[[:space:]]*\"${SEMVER}\"[[:space:]]*$"; }

talosupgrade=kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml
kubeupgrade=kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetesupgrade.yaml
declare -A talos=(
  [talos/topf.yaml]="$(extract talos/topf.yaml "^talosVersion:$(yaml_value)")"
  [talos/talenv.yaml]="$(extract talos/talenv.yaml "^talosVersion:$(yaml_value)")"
  # spec.talos.version: the line right after its Renovate annotation.
  [$talosupgrade]="$(sed -n '/depName=siderolabs\/talos/{n;p;q}' "$talosupgrade" | sed -nE "s/^[[:space:]]+version:$(yaml_value)/\1/p")"
  [terraform/variables.tf]="$(awk '/^variable "talos_version"/{f=1} f&&/^[[:space:]]*default[[:space:]]*=/{print; exit}' terraform/variables.tf | sed -nE "s/^[[:space:]]*default$(tf_value)/\1/p")"
  [terraform/terraform.tfvars]="$(extract terraform/terraform.tfvars "^talos_version$(tf_value)")"
)
declare -A kube=(
  [talos/topf.yaml]="$(extract talos/topf.yaml "^kubernetesVersion:$(yaml_value)")"
  [talos/talenv.yaml]="$(extract talos/talenv.yaml "^kubernetesVersion:$(yaml_value)")"
  # spec.kubernetes.version: the line right after its Renovate annotation.
  [$kubeupgrade]="$(sed -n '/depName=ghcr.io\/siderolabs\/kubelet/{n;p;q}' "$kubeupgrade" | sed -nE "s/^[[:space:]]+version:$(yaml_value)/\1/p")"
)

fail=0
# check <label> <name of assoc array>: all values present and equal to topf.yaml's.
check() {
  local label="$1"
  local -n versions="$2"
  local want="${versions[talos/topf.yaml]}"
  local file bad=0
  for file in "${!versions[@]}"; do
    if [[ -z "${versions[$file]}" ]]; then
      echo "::error file=${file}::Could not read the ${label} version from ${file}"
      bad=1
    elif [[ "${versions[$file]}" != "$want" ]]; then
      echo "::error file=${file}::${label} version drift: ${file} has ${versions[$file]}, talos/topf.yaml has ${want:-nothing}"
      bad=1
    fi
  done
  if [[ "$bad" != 0 ]]; then
    fail=1
  else
    echo "${label} ${want}: ${#versions[@]} copies agree"
  fi
}

check Talos talos
check Kubernetes kube

if [[ "$fail" != 0 ]]; then
  echo "Version check failed: update every copy to the same version (Renovate does this in one PR)." >&2
  exit 1
fi
