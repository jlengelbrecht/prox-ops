# netboot.xyz

LAN PXE server. The UDM SE stays the only DHCP server and points clients at
`10.20.67.28` (TFTP UDP 69, HTTP TCP 8080). Boot delivery needs no login and
does not depend on Authentik.

## Management UI

- URL: `https://netboot.homelab0.org`, on the `envoy-internal` gateway only
  (internal DNS and the gateway's TLS certificate; no Cloudflare record, no
  external route).
- Access: Authentik forward auth (`securitypolicy.yaml`, `failOpen: false`)
  on every path, including Socket.IO (`/socket.io/`), which carries the
  dashboard, menu editor and asset downloads. Only members of
  `authentik Admins` pass (`app-netbootxyz` in the Authentik blueprints).
  Everyone else is sent to sign-in or denied. Header authentication is off
  for this provider: app passwords and API tokens do not open the editor,
  only an interactive browser session does.
- The Socket.IO WebSocket upgrade is accepted only from
  `https://netboot.homelab0.org` (exact `Origin` match in `httproute.yaml`);
  an upgrade from any other origin, or with none, gets a gateway 500, so a
  page on another `homelab0.org` host cannot drive the editor with an admin's
  session.
- `/outpost.goauthentik.io/` on the same hostname is the Authentik
  login/callback route. It reaches Authentik only, never the pod.
- The pod's editor port 3000 accepts traffic from the `envoy-internal`
  gateway pods only (`networkpolicy.yaml`). It is not on the load balancer.
- If Authentik is down, the UI is unavailable; PXE boot keeps working.
- An open editor tab keeps its authenticated WebSocket until the tab closes,
  so removing someone from `authentik Admins` takes full effect on their next
  connection.
- Break-glass access without Authentik, for cluster admins only:
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
