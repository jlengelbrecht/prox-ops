# Obsidian

A persistent Obsidian desktop (LinuxServer image) on the retained 10Gi Ceph `obsidian-config` claim, with Obsidian Sync running inside the app. The desktop is at `https://obsidian.homelab0.org` behind Authentik (Admins only).

The same pod runs the Obsidian MCP gateway (`services/obsidian-mcp`) at `https://obsidian-mcp.homelab0.org/mcp`. It is internal-only.

## Connecting a coding agent

The endpoint uses streamable HTTP with a per-client bearer token. It does not redirect to a browser login. One endpoint serves every enrolled vault. Call `list_vaults`, then pass `vault` on every tool call.

| Client | Token variable in the pod | Example |
|---|---|---|
| Claude Code | `OBSIDIAN_MCP_TOKEN_CLAUDE` | `claude mcp add --transport http obsidian https://obsidian-mcp.homelab0.org/mcp --header "Authorization: Bearer $OBSIDIAN_MCP_TOKEN"` |
| Codex | `OBSIDIAN_MCP_TOKEN_CODEX` | `[mcp_servers.obsidian]` with `url = "https://obsidian-mcp.homelab0.org/mcp"` and `bearer_token_env_var = "OBSIDIAN_MCP_TOKEN"` |
| OpenCode | `OBSIDIAN_MCP_TOKEN_OPENCODE` | a `remote` MCP entry with the URL and an `Authorization` header |
| Antigravity | `OBSIDIAN_MCP_TOKEN_ANTIGRAVITY` | an HTTP MCP server entry with the URL and an `Authorization` header |

Each token is stored SOPS-encrypted in `app/secret-mcp.sops.yaml`. Keep a client's token in that machine's own secret store or environment. Never put it in a repository, a shell history or a chat. To rotate one token, re-encrypt only that key and let Flux reconcile.

Client machines need neither the Obsidian app nor SSH.

## Tools

`list_vaults`, `list_entries` (direct children of a folder), `read_note`, `search_notes`, `create_note`, `append_note` (requires the current `revision`), `mutation_receipt` (reconciles a create or append whose response was lost), and `read_embedded_image` (local raster images only).

There are no move, delete, plugin or settings tools yet. Those are destructive or administrative, and they will require owner approval through the `/owner/` route on `obsidian.homelab0.org`.

## How it works

- **Writes go through the app.** A private plugin (`obsidian-private-bridge`) performs every write through Obsidian's own API, so Sync sees normal edits. The gateway mounts each vault read-only and talks to the plugin over a per-vault abstract Unix socket, authenticated with a credential that is regenerated on every pod start.
- **Bootstrap.** The `prepare-mcp-runtime` init container reads the `obsidian-mcp-registry-v1` ConfigMap and creates the private runtime under `/run/obsidian-bridge`. It installs the bundled plugin only into enrolled vaults that already exist, and it never creates a vault. If the installed plugin bytes differ from the image, startup fails so an owner can review the change. Other plugins, Sync and Hindsight settings are left alone.
- **Network.** Cilium admits port 8000 only from the internal Envoy gateway. The gateway's only egress is the Authentik server on port 9000, used for owner checks.

## Adding a vault

1. Create the vault in the desktop and let Sync download it.
2. Add it to a new version of the registry ConfigMap.
3. Add a same-path `subPath` mount for both containers, and add client grants.
4. Roll the pod.

The MCP URL does not change.

## Updating the gateway

Renovate tracks `ghcr.io/jlengelbrecht/obsidian-mcp` by digest. If an image changes the bundled plugin bytes, bootstrap fails on purpose. Review the plugin diff, then remove the old plugin copy from each enrolled vault so bootstrap reinstalls it.
