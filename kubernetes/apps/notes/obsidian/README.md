# Obsidian

A persistent Obsidian desktop (LinuxServer image) on the retained 10Gi Ceph `obsidian-config` claim, with Obsidian Sync running inside the app. The desktop is at `https://obsidian.homelab0.org` behind Authentik (Admins only).

The same pod runs the Obsidian MCP gateway (`services/obsidian-mcp`) at `https://obsidian-mcp.homelab0.org/mcp`. It is internal-only.

## Connecting a coding agent

The endpoint uses streamable HTTP with a per-client bearer token. It does not redirect to a browser login. One endpoint serves every vault that is open in the desktop, and all four clients can use all of them. Call `list_vaults` to get each vault's `id`, then pass `vault` on every tool call.

| Client | Token variable in the pod | Example |
|---|---|---|
| Claude Code | `OBSIDIAN_MCP_TOKEN_CLAUDE` | `claude mcp add --transport http obsidian https://obsidian-mcp.homelab0.org/mcp --header "Authorization: Bearer $OBSIDIAN_MCP_TOKEN"` |
| Codex | `OBSIDIAN_MCP_TOKEN_CODEX` | `[mcp_servers.obsidian]` with `url = "https://obsidian-mcp.homelab0.org/mcp"` and `bearer_token_env_var = "OBSIDIAN_MCP_TOKEN"` |
| OpenCode | `OBSIDIAN_MCP_TOKEN_OPENCODE` | a `remote` MCP entry with the URL and an `Authorization` header |
| Antigravity | `OBSIDIAN_MCP_TOKEN_ANTIGRAVITY` | an HTTP MCP server entry with the URL and an `Authorization` header |

Each token is stored SOPS-encrypted in `app/secret-mcp.sops.yaml`. Keep a client's token in that machine's own secret store or environment. Never put it in a repository, a shell history or a chat. To rotate one token, re-encrypt only that key and let Flux reconcile. Reloader then restarts the pod so the gateway picks up the new value.

Client machines need neither the Obsidian app nor SSH.

## Tools

- **Vaults:** `list_vaults` (open vaults with their ids), `list_all_vaults` (every vault the desktop knows, open or closed), `open_vault`, `create_vault`. A new vault opens in its own window and appears in `list_vaults` within a few seconds. Connecting it to Obsidian Sync is a one-time step in the desktop.
- **Notes:** `list_entries` (direct children of a folder), `read_note`, `read_note_with_images` (text plus embedded images in reading order, 10 per page), `search_notes`, `create_note` (creates missing folders), `append_note` (requires the current `revision`), `mutation_receipt` (reconciles a create or append whose response was lost), `read_embedded_image` (local raster images only).

Destructive and administrative tools (delete, overwrite, plugin and settings changes) are coming. They will require owner approval through the `/owner/` route on `obsidian.homelab0.org`.

## How it works

- **Everything goes through the app.** A private plugin (`obsidian-private-bridge`) runs in every vault window and performs reads and writes through Obsidian's own API, so Sync sees normal edits. When a vault window opens, the plugin registers itself under `/run/obsidian-bridge/<vault id>/` with a fresh credential. It deregisters when the window closes. The gateway mounts no vault content and talks to each plugin over a per-vault abstract Unix socket.
- **Bootstrap.** The `prepare-mcp-runtime` init container creates the private runtime directory. It installs the bundled plugin into every vault listed in the desktop's `obsidian.json` that already has Obsidian settings, and it never creates or initializes a vault itself. Vaults created through `create_vault` get the plugin from the window that created them. If an older copy of the plugin is installed (exactly its two files), bootstrap replaces it with the image's copy. Anything else in that directory, such as extra files, nested directories or symlinks, stops startup for review. Other plugins, Sync and Hindsight settings are left alone.
- **Network.** Cilium admits port 8000 only from the internal Envoy gateway. The policy applies to the whole pod, not to each container, so the gateway shares the desktop's egress (DNS, Obsidian Sync, GitHub, the internal gateway) plus the Authentik server on port 9000, which it uses for owner checks.

## Adding a vault

Create it in the desktop, or have an agent call `create_vault`. Either way it appears in `list_vaults` once its window is open, and no change in this repository is needed.

## Updating the gateway

Renovate tracks `ghcr.io/jlengelbrecht/obsidian-mcp` by digest. When a new image ships different plugin bytes, the next pod start refreshes the installed plugin, and the init log records it.
