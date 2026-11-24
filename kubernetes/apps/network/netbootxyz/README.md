# netboot.xyz

LAN PXE server. The UDM SE stays the only DHCP server and points clients at
`10.20.67.28` (TFTP UDP 69, HTTP TCP 8080). Boot delivery needs no login.

## Management UI

- The web app on port 3000 has no login of its own, so nothing routes to it:
  it is not on the load balancer, and `networkpolicy.yaml` admits no pod or
  LAN host to it.
- Access is for cluster admins only, through the pod's loopback:
  `kubectl -n network port-forward svc/netbootxyz-admin 3000:3000`.

## Menus: what is Git-managed and what the UI may change

- Git-managed, restored on every start: `custom/custom.ipxe` (the Talos entry)
  and `local-vars.ipxe`, from `app/configmap.yaml`. `entrypoint.sh` installs
  them read-only (mode 0444, owner `nbxyz`), so the UI cannot save these two
  files: change Talos in Git. Readiness still requires the served copies to
  equal the ConfigMap.
- If a UI-local override named `local-vars.ipxe` exists under
  `/config/menus/local`, the UI's save step fails on it, and on every later
  save, until you remove it with Revert in the UI. Boot delivery is not
  affected.
- The UI's menu-version upgrade changes `/config/menuversion.txt`, which
  makes the pod unready (PXE stops) until the next restart refetches the
  pinned `MENU_VERSION`. Do not use it; bump the version in Git.
- UI-local overrides: other menu edits saved by the UI live under
  `/config/menus/local` on the `netbootxyz-config` volume. They survive
  restarts and menu-version upgrades (`entrypoint.sh` keeps `local/` and the
  Secure Boot files when it refetches a release). They are not in Git, so
  record any you rely on here or in the runbook.

## Repair

On every start `entrypoint.sh` checks that the served release is the complete
pinned `MENU_VERSION` (and the Secure Boot files the pinned `IPXE_SB_VERSION`).
Anything incomplete or out of date is fetched again, `local/` excepted; a
failed fetch exits so the restart retries. To repair a pod that is unready,
delete it and let the new one refetch.

## Storage

| Volume | Claim | Size | Holds |
|---|---|---|---|
| `/config` | `netbootxyz-config` | 2Gi | menus, loaders, web-app settings |
| `/assets` | `netbootxyz-assets` | 50Gi | installer images downloaded in the UI |

Both use `ceph-block-retain` and carry `kustomize.toolkit.fluxcd.io/prune:
disabled`: a restart, removing the app from Git or a bad commit does not
delete them. Remove an installer from the UI when it is no longer needed;
nothing prunes stored assets automatically. nginx serves `/assets` on the boot
port, so downloaded installers are reachable by any LAN host or pod without
login.

The UI builds every installer download URL on `github.com` (the host is fixed
in the web app, not taken from the browser) and writes the file straight to
its final name. If the pod stops during a download, a
partial file can remain; delete it in the UI and download it again.

The first rollout with the asset volume replaces an `emptyDir`. Check
`/assets` in the running pod first and copy out anything present before the
pod is replaced; at planning time it was empty.
