# CachyOS RX 7900 XTX edge worker

The `cachyos-7900xtx` placement from the EPIC-035 model catalog: llama.cpp on
ROCm serving Qwen3.6-27B, behind llama-swap, behind TLS and a bearer token, on
the LAN. This directory is everything needed to stand it up and everything
learned doing it.

It is deliberately *not* wired into the cluster. Making this placement
selectable — the `AgentgatewayBackend`, the groups, the health policy, the
`ExternalSecret`, the CNP, and the catalog's `selectable: true` — is 35.8.

Contract: [`edge/EDGE-WORKER-CONTRACT.md`](../EDGE-WORKER-CONTRACT.md).
Checker: [`edge/conformance.sh`](../conformance.sh).

## Shape

```
                    LAN (cluster VLAN)
                          │
                          │  https://<edge-hostname>:8443
                          │  bearer token, dedicated edge CA
                          ▼
  ┌──────────────────────────────────────── container: edge-llama-swap ──┐
  │  edge-supervisor.sh  (PID 1)                                          │
  │    └── llama-swap        :8443   TLS terminator, API-key enforcement, │
  │          │                       model lifecycle                      │
  │          └── llama-server 127.0.0.1:10001   never published           │
  │                                             ROCm / gfx1100            │
  └───────────────────────────────────────────────────────────────────────┘
     supervisor reads interactive-claim,        both read rocm-smi
     writes phase                               and llama-swap /running
  ┌─────┴───────────────────┐        ┌─────┴───────────────────────────────┐
  │ edge-interactive-guard  │        │ edge-heartbeat                      │
  │ (systemctl --user)      │        │ (systemctl --user)                  │
  │ writes interactive-claim│        │ reads claim + phase; posts to 35.9  │
  └─────────────────────────┘        └─────────────────────────────────────┘

  interactive-claim and phase live in ONE host directory,
  ~/.local/state/edge-cachyos-state, bind-mounted into the container at
  /edge/state — all three processes open the same files
```

Three processes, three jobs, and the split is not arbitrary:

- **llama-swap is the only thing on the LAN.** Ruling R3. It is the piece that
  can terminate TLS and require an API key, which is what ruling R4 demands of
  anything cluster traffic reaches.
- **llama-server is a child of it**, bound to loopback inside the container's
  network namespace, with no published port. It is unreachable from the LAN and
  unreachable from this host too — only from inside the container.
- **The guard and the heartbeat run on the host**, outside the container, and
  keep running when it is down. That is the point: `INTERACTIVE` and `OFFLINE`
  have to be distinguishable, and a heartbeat producer that stopped with the
  endpoint would collapse "withdrawn on purpose" and "died" into one signal.
- **All three share one directory on the host**, and it has to be one. See
  [Shared state directory](#shared-state-directory).

## Shared state directory

Two files carry the whole interactive-priority mechanism: `interactive-claim`,
written by the guard, and `phase`, written by the supervisor. The guard and the
heartbeat are `systemctl --user` units on the host; the supervisor is PID 1
inside a container. They must all open the same directory or the mechanism does
nothing while every log line still looks right — the guard says `CLAIM`, and the
endpoint stays up.

So the directory is a **host path**, `~/.local/state/edge-cachyos-state`, and
the container bind-mounts it:

| Who | How it gets there |
|---|---|
| `edge-interactive-guard.service` | `StateDirectory=edge-cachyos-state`, `EDGE_STATE_DIR=%S/edge-cachyos-state` |
| `edge-heartbeat.service` | the same, plus `ReadOnlyPaths=` — it only reads |
| the container | bind mount `${EDGE_STATE_HOST_DIR}` → `/edge/state`, `create_host_path: false` |

`StateDirectory=` is what makes it deterministic. systemd creates the directory
before it builds the unit's sandbox, so `ReadWritePaths=`/`ReadOnlyPaths=` have
something to bind and both units start on a fresh boot with nothing having run
first. A unit that instead named a directory some other process was expected to
create would not start at all until that process had — and the process in
question is the guard, which is the one that needs the directory.

It is deliberately **not** under `%t` (`/run/user/<uid>`). That is a tmpfs
logind mounts when the user manager starts, while docker starts the container
independently and can get there first; a bind mount taken before the tmpfs
appears stays attached to what was underneath it, and the container and the
host daemons end up in two different directories — with no error
anywhere. `%S` is an ordinary directory that exists before either.

The one thing to keep in step is `EDGE_STATE_HOST_DIR` in `.env`, because
Compose cannot read a systemd specifier. Derive it, do not type it:

```sh
echo "EDGE_STATE_HOST_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/edge-cachyos-state"
```

and step 7 below checks the two sides really did land on the same directory.

## Files

| Path | What it is |
|---|---|
| `Dockerfile` | llama.cpp's ROCm server image + the llama-swap release binary |
| `docker-compose.yaml` | the LAN-facing service, its mounts and its published port |
| `llama-swap.yaml` | model definition, TTL, API-key enforcement |
| `env.example` | template for the untracked `.env` |
| `model-id-map.json` | runtime identity → catalog `model_id` (contract §1) |
| `scripts/issue-edge-pki.sh` | dedicated edge CA + host leaf |
| `scripts/edge-supervisor.sh` | container PID 1; withdraws the listener on demand |
| `scripts/edge-interactive-guard.sh` | detects desktop GPU use; claims the GPU |
| `scripts/edge-heartbeat.sh` | builds and posts the contract heartbeat |
| `scripts/edge-common.sh` | shared GPU sampling and llama-swap calls |
| `systemd/*.service` | user units for the two host-side daemons |
| `testing/gpu-load.cpp` | a competing GPU workload, for drilling the guard |
| `testing/interactive-drill.sh` | times detection, withdrawal and recovery |

## Bring-up

Everything below runs as your normal user. Nothing here needs `sudo`, and that
is a design constraint rather than a coincidence — see
[ufw and publication](#ufw-and-publication).

### 1. Fetch the model

The same artifact the cluster's `kserve-a5000` placement runs, from the same
source, because 35.7 compares the two and a different quant would make that
comparison meaningless.

```sh
docker volume create edge-model-cache
docker run --rm --user 0:0 -v edge-model-cache:/models \
  --entrypoint curl curlimages/curl:8.10.1 \
  -sSL --retry 8 --retry-delay 5 --retry-all-errors -C - --create-dirs \
  -o /models/unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf \
  https://huggingface.co/unsloth/Qwen3.6-27B-MTP-GGUF/resolve/main/Qwen3.6-27B-UD-Q4_K_XL.gguf
```

Verify it before trusting it — 17.9 GB over the internet is exactly the sort of
thing that arrives truncated:

```sh
docker run --rm -v edge-model-cache:/models alpine:3.20 \
  sha256sum /models/unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf
# 4085665ee36d82a672a238a43f0e5643f2f0e39f2d7bd5d373f0ef10ecf53095
```

That hash is the Hugging Face LFS object id for the file, so a match also
proves the artifact is the one the repo advertises.

### 2. Issue the PKI

From this directory:

```sh
docker volume create edge-pki
docker run --rm -v edge-pki:/pki -v "$PWD":/work alpine:3.20 sh -c \
  'apk add --no-cache bash openssl >/dev/null &&
   bash /work/scripts/issue-edge-pki.sh \
     --hostname cachyos-7900xtx.homelab0.org --out /pki'
```

Or run `scripts/issue-edge-pki.sh` directly if the host has `openssl`. See
[TLS and PKI](#tls-and-pki-provisional) for what it produces and why.

### 3. Seed the bearer credential

One file, mode 0600, whose copy of record is a 1Password item — the same shape
`agentgateway-keys` already uses for the client-facing keys, so 35.8's
`ExternalSecret` reads the same item.

```sh
docker volume create edge-secrets
op read 'op://<vault>/edge-cachyos-7900xtx/credential' |
  docker run --rm -i --user 0:0 -v edge-secrets:/secrets alpine:3.20 sh -c \
    'umask 077; tr -d "\r\n" > /secrets/api-key'
```

To mint one first: `printf 'sk-%s\n' "$(head -c 48 /dev/urandom | base64)"`.

### 4. Configure

```sh
cp env.example .env && chmod 600 .env
$EDITOR .env                    # address, hostname, measured VRAM baseline

# EDGE_STATE_HOST_DIR must be the directory the systemd units use. Derive it
# rather than typing it — the container mount is `create_host_path: false`, so
# if this is wrong the container fails to start instead of running against an
# empty directory nothing else can see.
sed -i "s|^EDGE_STATE_HOST_DIR=.*|EDGE_STATE_HOST_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/edge-cachyos-state|" .env
```

### 5. Install the host-side daemons

```sh
mkdir -p ~/.config/edge-cachyos ~/.config/systemd/user

# Copied as-is. Neither the state directory nor the credential path is in it —
# each means something different inside and outside the container, so the units
# and docker-compose state their own. That is also why they must not be in
# .env: EnvironmentFile= overrides Environment= whatever the order in a unit.
install -m 600 .env ~/.config/edge-cachyos/edge.env

# A host copy of the same bearer credential the container mounts. llama-swap
# requires it on /running, so without it the guard cannot count loaded models
# and loses its compute-client rule; the units point EDGE_API_KEY_FILE here.
( umask 077
  op read 'op://<vault>/edge-cachyos-7900xtx/credential' |
    tr -d '\r\n' > ~/.config/edge-cachyos/api-key )

# And a host copy of the edge CA. The daemons verify the certificate chain like
# everything else here, and /pki exists only inside the container. This is the
# path EDGE_CA_CERT points at in .env.
docker run --rm -v edge-pki:/pki:ro alpine:3.20 cat /pki/edge-ca.crt \
  > ~/.config/edge-cachyos/edge-ca.crt

cp systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now edge-interactive-guard.service edge-heartbeat.service
loginctl enable-linger "$USER"   # so they survive logout
```

`StateDirectory=edge-cachyos-state` means the units create the shared directory
themselves, before their sandbox is built. Nothing here needs a `mkdir`, and
nothing breaks after a reboot. The name carries the `-state` suffix on purpose:
when a user unit's state directory is missing and a *configuration* directory of
the same name exists — and `~/.config/edge-cachyos` is created at the top of
this step —
systemd reads that as a pre-v254 migration and replaces the state directory with
a symlink to the configuration one, which would put the claim file, the phase
file and a container bind mount on top of the credential.

The unit files assume the repo is at `~/repos/prox-ops`; adjust `ExecStart` if
it is not.

### 6. Start the container

The daemons come first because the units are what create the shared state
directory, and the container bind-mounts it with `create_host_path: false`.

```sh
docker compose up -d --build
docker compose logs -f
```

### 7. Verify

The two halves of the shared directory, first — this is the thing that has no
error message of its own when it is wrong:

```sh
scripts/edge-interactive-guard.sh claim
docker compose exec llama-swap ls -l /edge/state   # the claim must be here
scripts/edge-interactive-guard.sh release
```

Then the contract checker:

```sh
EDGE_API_KEY=$(op read 'op://<vault>/edge-cachyos-7900xtx/credential') \
../conformance.sh \
  --endpoint https://cachyos-7900xtx.homelab0.org:8443 \
  --ca-cert /path/to/edge-ca.crt \
  --model qwen36-27b --alias-model qwen3.6-27b \
  --timeout 300
```

Pass the credential through `EDGE_API_KEY` rather than `--api-key`: on a shared
host a credential on the command line is readable from `/proc`.

There is no LAN DNS record for the edge hostname yet (34.19b is unstarted), so
until there is, add `--resolve cachyos-7900xtx.homelab0.org:8443:<edge-lan-ip>`.
Do not reach for the certificate-validation bypass instead — the whole point of
`--ca-cert` is that verification stays on.

## TLS and PKI (provisional)

**This procedure is provisional, and saying so is part of the deliverable.**
There is no automated non-Kubernetes PKI flow in this estate. cert-manager
cannot mount a `Certificate` onto a host it does not schedule, so issuance and
renewal here are operator actions run from `scripts/issue-edge-pki.sh`. That
gap does not block the contract — the contract explicitly allows it at 35.6 —
but it does mean the host certificate expires on a date somebody has to
remember. It is a 397-day leaf; put it in a calendar, or replace this section
with a real flow before the second renewal.

What the script produces and why:

- **A real CA** (`basicConstraints: CA:TRUE, pathlen:0`), not a self-signed
  leaf. The 35.4 spike measured agentgateway rejecting a self-signed leaf used
  as its own trust anchor: `invalid peer certificate:
  Other(OtherError(CaUsedAsEndEntity))`. A two-level chain is the only shape
  that works.
- **A leaf whose only SAN is the edge hostname.** agentgateway verifies the SAN
  against the `host` field of the backend, which is why the backend uses a name
  and not an address, and why the script refuses an IP for `--hostname`.
- **A dedicated key.** Never the Kubernetes wildcard private key. This one is
  specific to this host and is revocable without touching anything else.
- **ECDSA P-256** throughout: supported by Go's TLS stack (llama-swap) and by
  rustls (agentgateway), and smaller than RSA for no loss.

`edge-ca.crt` is public material and is what goes to the cluster — 35.8
references it from `policies.tls.caCertificateRefs`, and `conformance.sh` takes
it with `--ca-cert`. Everything else in that directory is secret and lives
outside Git, in a docker volume or a root-owned host directory.

If the PKI lives in a docker volume, that is also the only file you need out of
it, and it is safe to copy anywhere:

```sh
docker run --rm -v edge-pki:/pki:ro alpine:3.20 cat /pki/edge-ca.crt > edge-ca.crt
```

Renewal without disturbing the cluster: `issue-edge-pki.sh --leaf-only --force`
reissues the host certificate against the existing CA, so the ConfigMap holding
the anchor never changes and no Git commit is needed to rotate.

## ufw and publication

The single most important operational fact about this host, from the 35.4
spike: **ufw drops inbound TCP to a plain host process, and lets
docker-published ports through.** Docker's DNAT rules sit in `PREROUTING` and
`DOCKER-USER` and never traverse ufw's `INPUT` chain. A host-process llama-swap
bound to the LAN interface would answer perfectly on this machine and be
invisible from the cluster — and an unreachable backend costs agentgateway a
fixed 10 s connect deadline per request to discover, which looks like a hung
model rather than a firewall.

So the endpoint is published by docker, on one address and one port, and no ufw
rule was added or is needed. The alternative — a host process plus an explicit
`ufw allow from <cluster-vlan> to any port 8443` — works too, needs `sudo`, and
buys nothing here.

One consequence worth knowing when testing: a container on the default docker
bridge *cannot* reach the published port on this host's LAN address (it hits
ufw's `INPUT` on the way in and is dropped). Test from the host, or with
`--network host`. This does not affect the cluster, which arrives on the
physical interface where the DNAT applies — 35.4 measured that path at ~1 ms
connect and ~3 ms first byte from five different workers.

## Interactive priority

Contract §4 for this host: *the machine's interactive desktop work outranks AI;
AI work is expected to yield, not fight for the GPU.* §1.2 adds that the node
announces its own state and stops accepting work — it does not ask permission.

### Mechanism

`edge-interactive-guard.sh` samples the GPU. When it decides the desktop wants
the card, it writes one file: `interactive-claim`, in the
[shared state directory](#shared-state-directory).
`edge-supervisor.sh`, PID 1 inside the container, sees it and sends llama-swap
`SIGTERM`, which closes the listener immediately and drains in-flight requests
for up to 40 s. `edge-heartbeat.sh` sees the same file and publishes `DRAINING`
and then `INTERACTIVE`. When the claim goes away, the supervisor starts
llama-swap again and the node is back.

Two design choices worth defending:

**Why the listener closes rather than answering an error.** The contract's
data-plane withdrawal signal is a 503 or a wire failure, because those are the
only things agentgateway's passive health treats as a failure. llama-swap's
other withdrawal shapes — dropping the model from the config, or a null profile
pin — answer `404`, which a gateway reads as a working endpoint returning a
client error, so it would keep sending traffic to a node that has already gone.
A closed listener gives connection-refused, which 35.5 measured as an instant
client-visible failure and failover rather than the 10 s a dropped packet costs.

**Why a file rather than `docker stop`.** Stopping the container is the obvious
way to close the listener, and it would mean the guard — a process that runs
whenever the desktop is in use — needs the docker socket. Anything that can
drive docker can start a privileged container. Watching a file keeps the guard
unprivileged; the supervisor, already inside the blast radius, does the part
that needs authority.

### What the guard actually measures

`rocm-smi`'s KFD process table reports VRAM held by *compute* clients, so:

- `used − compute` is VRAM held by **graphics** clients: the compositor at
  idle, plus whatever a game or video editor adds. Above the measured idle
  baseline by more than `EDGE_INTERACTIVE_VRAM_MB` → claim.
- **more compute clients than llama-swap has models loaded** → someone else's
  HIP/OpenCL process → claim. This rule needs no size estimate, so it does not
  drift when the model, its quant or its context size changes.
- **the card busy with nothing of ours on it at all** → claim.

When the loaded-model query does not answer, that reading is **unknown**, not
zero, and the rule that needs it is skipped for that sample. Zero would mean
"nothing of ours is on the card", which is the opposite of what a failed query
tells you: our own `llama-server` would be counted as a foreign compute client
and a healthy node would take itself out of service for the whole release
hold-down over a few seconds of telemetry trouble. The other two rules keep
working on their own terms — and so does the release path, which matters,
because a withdrawn node has no llama-swap to answer the query at all.

The only calibrated number is the idle compositor footprint
(`EDGE_DESKTOP_BASELINE_MB`), measured on this host at ~1.1 GB. Re-measure it
after a desktop-environment change:

```sh
rocm-smi --showmeminfo vram --showpids --json   # with the model unloaded
```

### Limits, both real

- **A loaded Qwen3.6-27B leaves roughly 0.9–1.5 GB of the card free** — about
  1.5 GB immediately after loading, settling to ~900 MB once requests have run.
  A desktop client therefore has around a gigabyte to allocate before it fails
  outright, which is less headroom than the trip threshold plus the drain
  wants. Detection is fast but it is not instantaneous, so for the first
  several seconds of a game launch the desktop is competing with a model that
  has not finished yielding.
- **Interactive work that is neither VRAM- nor compute-hungry is invisible.**
  Scrolling a browser will never trip this.

Both are why `edge-interactive-guard.sh claim` — called from a game launcher, a
compositor rule, or by hand *before* the GPU is wanted — is the primary
mechanism on this host, and the detector is the backstop rather than the other
way round. A manual claim is sticky: the detector will not release a claim it
did not make.

### Measured latency

`testing/interactive-drill.sh` starts a competing GPU workload
(`testing/gpu-load.cpp` — see its header for both build recipes) and times the
whole path: detection, withdrawal at the socket, and recovery. With the
defaults in `env.example` — 2 s sampling, 3 samples to trip, 30 to release —
this host measured **7 s to detect, 8 s until the endpoint refused
connections**, and recovery in the same second the hold-down expired. It
measures the socket rather than the state file on purpose: the contract
requires that an `INTERACTIVE` node actually stops accepting work, not just
that it says so.

Re-run the drill after changing any threshold. The latency numbers are the
acceptance criterion; the thresholds are just how they were reached.

## Model lifecycle

llama-swap owns all three parts of it:

- **On demand.** No model is loaded at startup. The first request naming a
  model starts `llama-server` for it. Measured cold start here is about 8 s
  end to end, of which 5.7 s is loading 17.9 GB from local NVMe — an order of
  magnitude below the cluster's ~120–180 s from CephFS, and the largest single
  difference between the two placements.
- **Idle unload.** `ttl: 1800` in `llama-swap.yaml`. Thirty minutes matches the
  `scale-to-zero-pod-retention-period: 30m` on the cluster's copy of the same
  model, so the two placements have the same warm/cold duty cycle and 35.7 is
  not measuring a difference this repo invented.
- **Cache.** The GGUF lives in a volume or directory mounted read-only at
  `/models`. Unloading frees VRAM, not the artifact, so a reload pays the load
  and not the download — which is exactly the middle cost tier the contract's
  `cached_models` field exists to express.

Manual control, for when the operator wants the GPU back without withdrawing
the node: `POST /api/models/unload` (authenticated).

## Heartbeat and the model-id translation

`scripts/edge-heartbeat.sh` emits the contract §1 payload. There is no router
to receive it yet, so with `EDGE_ROUTER_URL` unset it prints the payload and
exits.

The part that is not deferrable is the translation. The contract is explicit:
`active_model` and `cached_models` carry the **catalog `model_id`**
(`qwen36-27b`), never the `upstream_model_id` (`qwen3.6-27b`), and *translation
is the edge producer's job* — the router does not guess, keeps no alias table
and does no fuzzy matching. A heartbeat reporting the upstream id joins against
nothing in the catalog, so a warm node looks cold and the work goes somewhere
that has to load from scratch: the exact cost distinction those two fields
exist to preserve.

`model-id-map.json` holds both directions the producer needs:

- `runtime_to_catalog` — what llama-swap calls a model → what the catalog does.
- `artifact_to_catalog` — a GGUF path under `$EDGE_MODEL_DIR` → catalog id.
  `cached_models` is derived from the **filesystem**, not from llama-swap's
  configuration, because a model can be configured and its artifact absent, and
  reporting that as cached promises the router a load time it cannot deliver.

An id in neither table is **dropped with a warning**, never guessed at and
never passed through. `scripts/edge-heartbeat.sh --self-test` asserts exactly
that, plus that the map is one-directional and that every catalog id it can
produce has capabilities and a context length to advertise.

Two other fields deserve a note:

- **`runtime.endpoint` is observational metadata only.** It is not service
  discovery, it never becomes a routing target, and it must never trigger
  gateway reconfiguration. Whoever holds an edge credential must not be able to
  redirect a placement's traffic by advertising an address of their choosing.
  agentgateway backend hostnames stay Git-managed.
- **`cluster_reachable` is reported `false` when it cannot be demonstrated.**
  Eligibility implications are one-way: under-claiming makes this placement
  less attractive, while a cheerful `true` from a host that cannot reach the
  cluster sends the router work that will never land.

## Conformance

`edge/conformance.sh` is the checker and **must not be edited to make this host
pass**. A check this host cannot pass is a finding about the host, or a defect
in the contract, and either way it gets reported rather than legislated away.

Run it with `--ca-cert` against the dedicated edge CA. Not against the system
trust store (the edge CA is not in it, and it should not be), and not with the
certificate-validation bypass, which downgrades `tls_validation` to `SKIP` and
tests a shape ruling R4 says must never ship.

Results from this host are in the story's Dev Agent Record.

## Evidence baseline for 35.7

Pinned identifiers, so 35.7 measures what it thinks it is measuring:

| | |
|---|---|
| llama.cpp image | `ghcr.io/ggml-org/llama.cpp:server-rocm-b9917` |
| image digest | `sha256:a56e8917cc07c37b5730fc98ebac1a39a30ba0acb5ccadf52478f2798d736728` |
| llama.cpp build | `b9917`, commit `4a7ee3126` (server `system_fingerprint`) |
| ROCm userspace | 7.2.1 (`rocm/dev-ubuntu-24.04:7.2.1-complete` base), ROCM-SMI-LIB 7.8.0 |
| GPU | AMD Radeon RX 7900 XTX, `gfx1100`, 24,560 MiB |
| llama-swap | v250, commit `60226b6` |
| model | `unsloth/Qwen3.6-27B-MTP-GGUF` / `Qwen3.6-27B-UD-Q4_K_XL.gguf` |
| model sha256 | `4085665ee36d82a672a238a43f0e5643f2f0e39f2d7bd5d373f0ef10ecf53095` |

The cluster's placement runs `server-cuda-b9917` — the same build number, so
the only intended difference between the two is the backend.

**`--spec-type draft-mtp` works on the ROCm build.** This was the open question
35.7 needed answered and the answer is yes, with measured draft acceptance
around 0.91–0.95 on short prompts. The invocation is identical to the cluster's
apart from the bind address:

```
--n-gpu-layers 999 --ctx-size 65536 --parallel 1
--spec-type draft-mtp --spec-draft-n-max 2 --jinja --metrics
```

Timing samples, VRAM figures and the one ROCm-specific warning worth carrying
into 35.7 are in the story's Dev Agent Record.

## Operations

```sh
docker compose ps                              # is it up
docker compose logs -f llama-swap              # supervisor + llama-swap
scripts/edge-interactive-guard.sh status       # what the guard currently sees
scripts/edge-heartbeat.sh --once | jq .        # what the router would be told
scripts/edge-heartbeat.sh --self-test          # is the id translation still right
```

The daemons read their settings from the environment, so run these with the
same settings the units use — the env file, plus the two paths the units state
themselves:

```sh
set -a; . ~/.config/edge-cachyos/edge.env; set +a
export EDGE_API_KEY_FILE=~/.config/edge-cachyos/api-key
```

`EDGE_STATE_DIR` needs no export: `scripts/edge-common.sh` defaults to the same
`${XDG_STATE_HOME:-$HOME/.local/state}/edge-cachyos-state` the units resolve
`%S/edge-cachyos-state` to.

- **Rotate the token**: replace the `api-key` file in the secrets volume and
  the host copy at `~/.config/edge-cachyos/api-key`, then restart the container
  and the two user units. Those two files and the 1Password item are the only
  copies.
- **Rotate the host certificate**: `issue-edge-pki.sh --leaf-only --force`,
  then restart the container. The CA and therefore the cluster side are
  untouched.
- **Upgrade llama.cpp**: bump the tag in `Dockerfile`, rebuild, restart. Keep
  it on the same build number as the cluster's CUDA image while 35.7 is open,
  or the comparison stops being one.
- **Take the node out of service** without stopping anything:
  `scripts/edge-interactive-guard.sh claim`. Put it back with `release`. A
  manual claim outranks the detector and is never released by it.

## What this directory deliberately does not do

- It does not make `cachyos-7900xtx` catalog-selectable. The catalog still
  carries `selectable: false` and `status: planned`, and changing that is 35.8's
  call, after the backend, the credential and the CA exist cluster-side.
- It does not touch anything under `kubernetes/`, and it never calls the AI
  gateway or wakes a KServe predictor.
- It does not ship a cluster-side `AgentgatewayBackend`, `ExternalSecret`,
  `ConfigMap` for the CA, DNS record, or `CiliumNetworkPolicy`. All 35.8.
