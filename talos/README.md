# Talos machine configuration

Node machine configs are rendered and applied with
[topf](https://github.com/postfinance/topf). talhelper was archived upstream
in August 2026; `talconfig.yaml`, `talenv.yaml` and `patches/` stay in place
only until the workflows and Taskfiles that still read them are moved over.
The topf layout below is the source of truth.

While both exist, keep them in step: a patch change goes into the topf layout
and the matching file under `patches/`, and a Talos or Kubernetes version
change updates `topf.yaml` and `talenv.yaml` together. `talenv.yaml` still
says v1.13.4 while the nodes run v1.13.10; a follow-up makes `topf.yaml` the
single version source and moves the Renovate rules and workflows onto it.

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

topf builds `machine.install.image` from `talosVersion`, `platform` and the
node's `schematicId`, so for these nodes it is
`factory.talos.dev/nocloud-installer/<schematic>:<talosVersion>`.

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

Kubernetes upgrades are not handled by topf; use `talosctl upgrade-k8s`.
