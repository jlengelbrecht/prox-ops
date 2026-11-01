# Talos machine configuration

Node machine configs are rendered and applied with
[topf](https://github.com/postfinance/topf). talhelper was archived upstream
in August 2026; `talconfig.yaml`, `talenv.yaml` and `patches/` stay in place
only until the Taskfiles that still read them are moved over.
The topf layout below is the source of truth.

topf applies the config to every node. The talhelper files under `patches/`
are frozen at the Talos 1.13 layout and are no longer applied; they stay only
for the Taskfiles until those are removed. Do not
use them to configure a node, and do not port the 1.14 typed documents into
them.

## Versions and upgrades

`topf.yaml` holds the desired Talos (`talosVersion`) and Kubernetes
(`kubernetesVersion`) versions. Copies of the Talos version also live in
`talenv.yaml`, the tuppr `TalosUpgrade`
(`kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml`),
`terraform/variables.tf` and `terraform/terraform.tfvars`; `talenv.yaml` also
copies the Kubernetes version.

- Renovate bumps every copy in one PR: one for Talos, one for Kubernetes.
  Neither auto-merges.
- `scripts/check-talos-versions.sh` runs on every PR (the required "Talos and
  Kubernetes versions agree" check) and fails if a copy differs from
  `topf.yaml`. Run it locally before editing a version by hand.
- tuppr is the only upgrader. It moves nodes labelled
  `tuppr.home-operations.com/enabled=true` to the `TalosUpgrade` version, in
  either direction.

## Layout

```text
talos/
├── topf.yaml            # cluster, versions, platform, schematics, nodes + per-node data
├── talsecret.sops.yaml  # Talos secrets bundle (SOPS/age), read by topf via secretsPath
├── all/                 # every node: install disk, hostname, cluster network, links, DNS, NTP, kubelet, sysctls
├── control-plane/       # control plane: cluster components, admission, Talos API access, taint, API VIP
├── worker/              # workers: IoT (VLAN 62) and DMZ (VLAN 81) interfaces
└── node/<host>/         # single node: GPU modules/labels (k8s-work-4, k8s-work-10), YubiHSM udev rule (k8s-work-12)
```

Patches are strategic merges, applied `all/` -> role -> `node/<host>/`, in
lexicographic order within a directory. Files ending in `.yaml.tpl` are Go
templates and can use `.Node.Host`, `.Node.IP` and `.Node.Data.*` from
`topf.yaml`.

Talos 1.14 moved install, Kubernetes, kubelet and resolver settings out of the
v1alpha1 document into typed documents (`UnattendedInstallConfig`,
`KubeNodeConfig`, `KubeNetworkConfig`, `ResolverConfig` and others) and rejects
configs that set both forms. Patches that differ by version branch on
`semverCompare ">=1.14.0-0" (coalesce .Node.RuntimeData.TalosVersion .TalosVersion)`.
That is the version topf picks for the config contract: the node's running
version when it can reach the node, otherwise `talosVersion`. An offline
render switches documents when `talosVersion` changes; `topf apply` switches a
node only once it is running 1.14.

topf builds the installer image from `talosVersion`, `platform` and the
node's `schematicId` (`machine.install.image` before 1.14,
`UnattendedInstallConfig` from 1.14), so for these nodes it is
`factory.talos.dev/metal-installer/<schematic>:<talosVersion>`. The nodes run
the `metal` platform (see `/proc/cmdline`), even though the VM templates start
from the nocloud image; keep `platform: metal` so upgrades don't switch them.

Only `/var` is writable on Talos. Never add `machine.files` entries outside
`/var`. A recursive grep of `talos/` for file paths under `/etc` must come
back empty.

## Commands

Run from `talos/`. `sops` and the age key must be available to decrypt the
secrets bundle, and `vals` once secrets move to `ref+op://` references.

```sh
topf render -o ./rendered   # offline; output contains secrets and is git-ignored
topf apply --dry-run        # diff against live nodes (needs Talos API access)
topf apply --nodes-filter '^k8s-work-1$'
```

Kubernetes upgrades are not handled by topf. tuppr's `KubernetesUpgrade`
(`kubernetes/apps/system-upgrade/tuppr/upgrades/kubernetesupgrade.yaml`) runs
them when its version changes; it carries a third copy of the Kubernetes
version, and Renovate proposes one minor version per PR.
