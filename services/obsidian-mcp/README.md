# App-backed Obsidian MCP gateway

The container starts `python /app/gateway.py`. It exposes authenticated Streamable HTTP at `/mcp`, a content-free `/health`, and an owner browser route at `/owner/{pending_id}`. The image installs the hash-pinned `requirements.txt` runtime and contains an immutable bridge plugin at `/opt/obsidian-mcp/bridge-plugin/` (`main.js`, `manifest.json`, and `README.md`). A bootstrap init container can copy that bundle into the app's managed plugin directory before Obsidian starts. The image does not contain the legacy filesystem writer; `/app/server.py` is the source compatibility module containing only the gateway's token validator and two protocol constants.

## Runtime configuration

Set `OBSIDIAN_GATEWAY_REGISTRY` to an absolute JSON path on a **read-only mount**. The configured parent directory, resolved target parent, and target must all have read-only filesystem flags. The runtime rejects a writable backing filesystem even if its file mode is `0444`. The registry contains no bearer or bridge credentials. Its exact top-level keys are `vaults`, `clients`, and `owner`:

```json
{
  "vaults": {
    "iam": {
      "app_name": "IAM Team",
      "app_root": "/vaults/iam",
      "socket_path": "/run/obsidian-bridge/iam/iam.sock",
      "credential_file": "/run/obsidian-bridge/iam/credential",
      "owner_ids": ["owner-uid"]
    },
    "homelab": {
      "app_name": "Homelab",
      "app_root": "/vaults/homelab",
      "socket_path": "/run/obsidian-bridge/homelab/homelab.sock",
      "credential_file": "/run/obsidian-bridge/homelab/credential",
      "owner_ids": ["owner-uid"]
    }
  },
  "clients": {
    "codex": {"iam": ["list_notes", "read_note", "search", "create_note", "append_note", "read_media"]},
    "claude": {"homelab": ["list_notes", "read_note", "search", "create_note", "append_note", "read_media"]},
    "opencode": {"iam": ["list_notes", "read_note", "search", "create_note", "append_note", "read_media"]},
    "antigravity": {"homelab": ["list_notes", "read_note", "search", "create_note", "append_note", "read_media"]}
  },
  "owner": {
    "check_url": "http://authentik.security.svc.cluster.local:9000/outpost.goauthentik.io/auth/nginx",
    "origin": "https://approval.example.test",
    "owner_ids": ["owner-uid"]
  }
}
```

The example names and addresses are placeholders. Use the reviewed internal Authentik outpost address and fixed HTTPS approval origin. Set distinct, URL-safe, unpadded credentials generated from at least 32 random bytes as `OBSIDIAN_MCP_TOKEN_CODEX`, `OBSIDIAN_MCP_TOKEN_CLAUDE`, `OBSIDIAN_MCP_TOKEN_OPENCODE`, and `OBSIDIAN_MCP_TOKEN_ANTIGRAVITY`. Supply them through a Secret or equivalent private environment injection. Do not put them in the registry, image, logs, or client repository configuration. There is no synthetic backend environment option.

Each `app_root` must be an existing canonical absolute path. Each `socket_path` names the **abstract Unix address seed** used by the private bridge; no socket file exists at that path. Mount one shared ephemeral bridge registry directory, owned by UID 1000 and mode `0700`, at `/run/obsidian-bridge`. Within it, each vault has its own UID 1000, mode `0700` directory and a distinct UID 1000, mode `0600` `credential` file. The app bridge plugin and gateway must see the same shared directory and credential. The plugin's bootstrap registry is separate; follow [bridge-plugin/README.md](bridge-plugin/README.md) for its exact version-one instance and per-vault enrollment contract. Mount app roots at the exact registry paths; the gateway needs only read access to the roots for identity checks. It does not write note files. Run the image as UID/GID 1000 with a read-only root filesystem and private network access to the Authentik outpost. Do not mount a host Docker socket or Kubernetes credentials.

The private ingress must terminate TLS, restrict access to LAN/VPN, and route `/mcp` and `/owner/` to the gateway. It must strip caller-supplied `X-authentik-*` headers. The owner route performs a fresh server-side browser-cookie check against the fixed outpost URL for each GET and POST. Owner GET cannot mutate; POST requires the one-use CSRF nonce and exact HTTPS Origin. Browser credentials are distinct from MCP bearer credentials. Set a 1 MiB ingress request-body limit in addition to the gateway's own bound.

## Public capability and limits

The production image advertises `list_vaults`, `list_entries`, `read_note`, `search_notes`, `create_note`, `append_note`, and `read_embedded_image` only when the selected running app reports the corresponding capability and the client has that vault/verb grant. Every content tool requires an explicit vault ID. Create and append go through the app bridge and coordinator; there is no filesystem mutation path. Image responses include a native MCP image block with decoded and validated PNG bytes plus note/path context. Remote images, SVG, and malformed media are rejected. No PDF, audio, or video perception is offered. The current plugin does not implement destructive organization actions, so `prepare_action` and `commit_action` are absent from the production tool list. The owner approval path remains in the gateway for later supported adapters; synthetic guarded tests do not establish live app destructive capability.

`/health` is intentionally content-free and reports only `{"status":"ok"}` when the gateway responds. It does not prove that every Obsidian app is available. Tool discovery probes live bridge capabilities and omits unavailable operations; calls fail closed on app disconnect. The synthetic container smoke verifies the packaged HTTP gateway and private peer protocol, not a real Obsidian application, Sync round trip, native client, or owner browser session.

## Build, verify, and publish

```sh
python3 services/obsidian-mcp/tests/verify_requirements.py
docker build -t obsidian-mcp:app-gateway-verification services/obsidian-mcp
python3 services/obsidian-mcp/tests/verify_requirements.py --image obsidian-mcp:app-gateway-verification
```

`requirements.txt` pins every runtime distribution and allowed artifact hash; `requirements-dev.txt` contains the same runtime pins plus the test extra. Both were exported from the same trusted lock set. `verify_requirements.py` checks the manifests, creates a disposable environment, installs with pip's `--require-hashes`, and runs the Python and Node bridge tests. Its `--image` mode uses the same pinned test environment for the packaged two-vault HTTP smoke. The public source does not require or include `uv.lock`.

The pull-request workflow runs tests, builds the runtime, and smokes it on an existing `ubuntu-latest` runner. A successful protected-main job builds a digest-only GHCR candidate. `publish_image.py` pulls and smokes that exact `ghcr.io/jlengelbrecht/obsidian-mcp@sha256:...` reference before it writes the commit or `main` manifest tags. Publication jobs are serialized; a stale main build cannot advance the alias after a newer successfully verified job. Independent external registry writers are outside that guarantee. A later deployment change must pin the **tested digest**, for example `ghcr.io/jlengelbrecht/obsidian-mcp@sha256:<verified-digest>`; the mutable `:main` tag is only a tracking channel. This preparation does not publish or deploy an image. Before any publication, perform a fresh prepublication secret scan and obtain required owner dispositions.
