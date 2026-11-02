#!/usr/bin/env bash
# Fail a PR that changes the desired Talos or Kubernetes version to one the
# cluster cannot safely move to. Patch updates of both auto-merge, so this is
# what stands between a Renovate PR and tuppr rolling it out.
#
# Checks, comparing talos/topf.yaml on the base branch with the PR:
#   - neither version goes down (tuppr would downgrade every node)
#   - Kubernetes moves at most one minor (Kubernetes cannot skip a minor)
#   - Talos's own compatibility tables, taken from the target Talos release,
#     allow upgrading from the current Talos and running the target Kubernetes
#   - the Kubernetes release is at least MIN_K8S_AGE_DAYS old; the kubelet
#     image comes from ghcr.io, which carries no release date, so Renovate's
#     minimumReleaseAge cannot soak it
#   - the kubelet image tuppr will pull exists
#
# Usage: scripts/check-upgrade-path.sh <base-ref> [repo-root]
set -euo pipefail

BASE="${1:?usage: check-upgrade-path.sh <base-ref> [repo-root]}"
ROOT="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MIN_K8S_AGE_DAYS="${MIN_K8S_AGE_DAYS:-3}"
cd "$ROOT"

# version <talosVersion|kubernetesVersion> <file contents>: X.Y.Z without the v,
# or nothing if the key is missing, malformed or appears more than once.
version() {
  [[ "$(grep -c "^$1:" <<<"$2")" == 1 ]] || return 0
  sed -nE "s/^$1:[[:space:]]*\"?v([0-9]+\.[0-9]+\.[0-9]+)\"?[[:space:]]*(#.*)?$/\1/p" <<<"$2"
}

base_topf="$(git show "${BASE}:talos/topf.yaml")"
head_topf="$(cat talos/topf.yaml)"
from_talos="$(version talosVersion "$base_topf")"
to_talos="$(version talosVersion "$head_topf")"
from_k8s="$(version kubernetesVersion "$base_topf")"
to_k8s="$(version kubernetesVersion "$head_topf")"

for v in from_talos to_talos from_k8s to_k8s; do
  [[ -n "${!v}" ]] || { echo "::error file=talos/topf.yaml::Could not read ${v//_/ } from talos/topf.yaml"; exit 1; }
done

if [[ "$from_talos" == "$to_talos" && "$from_k8s" == "$to_k8s" ]]; then
  echo "No Talos or Kubernetes version change (Talos ${to_talos}, Kubernetes ${to_k8s})."
  exit 0
fi
echo "Talos ${from_talos} -> ${to_talos}, Kubernetes ${from_k8s} -> ${to_k8s}"

fail=0
error() { echo "::error file=talos/topf.yaml::$*"; fail=1; }

# older <a> <b>: true when version a sorts before version b.
older() { [[ "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]]; }

older "$to_talos" "$from_talos" && error "Talos goes down from ${from_talos} to ${to_talos}"
older "$to_k8s" "$from_k8s" && error "Kubernetes goes down from ${from_k8s} to ${to_k8s}"

from_minor="$(cut -d. -f2 <<<"$from_k8s")"
to_minor="$(cut -d. -f2 <<<"$to_k8s")"
if [[ "$(cut -d. -f1 <<<"$from_k8s")" != "$(cut -d. -f1 <<<"$to_k8s")" ]] || (( to_minor - from_minor > 1 )); then
  error "Kubernetes ${from_k8s} -> ${to_k8s} skips a minor; upgrade one minor per PR"
fi

# Build the checker against the target release's machinery module, so the
# compatibility tables are the ones that ship with the Talos being installed.
# The downloaded module code never sees the GitHub token.
(
  unset GH_TOKEN
  cd scripts/talos-compat
  go get "github.com/siderolabs/talos/pkg/machinery@v${to_talos}" >/dev/null
  go build -o "${RUNNER_TEMP:-/tmp}/talos-compat" .
)
env -u GH_TOKEN "${RUNNER_TEMP:-/tmp}/talos-compat" "v${from_talos}" "v${to_talos}" "$to_k8s" || error "Talos compatibility check failed (see above)"

if [[ "$from_k8s" != "$to_k8s" ]]; then
  auth=()
  [[ -n "${GH_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer ${GH_TOKEN}")
  published="$(curl -fsSL "${auth[@]}" "https://api.github.com/repos/kubernetes/kubernetes/releases/tags/v${to_k8s}" | jq -r '.published_at // empty')" || true
  if [[ -z "$published" ]]; then
    error "No kubernetes/kubernetes release v${to_k8s} found"
  else
    age_days=$(( ($(date -u +%s) - $(date -u -d "$published" +%s)) / 86400 ))
    if (( age_days < MIN_K8S_AGE_DAYS )); then
      error "Kubernetes v${to_k8s} was released ${age_days} day(s) ago; wait until it is ${MIN_K8S_AGE_DAYS} days old (re-run this check then)"
    else
      echo "Kubernetes v${to_k8s} released ${age_days} days ago"
    fi
  fi

  docker manifest inspect "ghcr.io/siderolabs/kubelet:v${to_k8s}" >/dev/null 2>&1 \
    || error "ghcr.io/siderolabs/kubelet:v${to_k8s} does not exist"
fi

if [[ "$fail" != 0 ]]; then
  echo "Upgrade path check failed." >&2
  exit 1
fi
echo "Upgrade path is supported."
