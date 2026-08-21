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

```text
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
| `scripts/install.sh` | idempotent install/refresh into `~/.local/libexec/edge-cachyos/`; reports `ACTIVATION REQUIRED` if guard/heartbeat/container are already running (see [Activating an install](#activating-an-install)) |
| `systemd/*.service` | user units for the two host-side daemons |
| `testing/gpu-load.cpp` | a competing GPU workload, for drilling the guard |
| `testing/interactive-drill.sh` | times detection, withdrawal and recovery |
| `testing/lease-drill.sh` | proves the guard-ready lease fails closed at the socket (reboot race, container restart, guard crash, guard restart) |

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

# EDGE_LIBEXEC_DIR must be the directory scripts/install.sh installs into
# (step 5 below), for the same reason: the container mounts it with
# `create_host_path: false`, so a mismatch fails loudly instead of running the
# container against an empty directory. install.sh always installs to
# ~/.local/libexec/edge-cachyos -- fixed, not derived from XDG_LIBEXEC_HOME,
# because the units' ExecStart= hardcodes the same fixed path and systemd
# specifiers cannot read arbitrary environment variables.
sed -i "s|^EDGE_LIBEXEC_DIR=.*|EDGE_LIBEXEC_DIR=$HOME/.local/libexec/edge-cachyos|" .env
```

`ROCM_SMI` must stay an absolute path (the default, `/opt/rocm/bin/rocm-smi`,
is correct on this host). It must not be edited down to a bare command name
— PATH resolution works from an interactive shell but not from the systemd
user manager at boot, which is how the guard ran blind through an entire
cold boot before this was caught; see ["A blind guard is not a live
guard"](#a-blind-guard-is-not-a-live-guard-cycle-5). `scripts/install.sh`
(next step) refuses to install if this does not point at a real, executable
file.

### 5. Install the host-side daemons

```sh
mkdir -p ~/.config/edge-cachyos

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

# Validates ROCM_SMI in the edge.env just installed above (exits 1 if it is
# not an existing, executable path — see "A blind guard is not a live
# guard"), then copies scripts/, model-id-map.json and README.md to
# ~/.local/libexec/edge-cachyos/ and the two unit files to
# ~/.config/systemd/user/, then daemon-reloads. See "Layout independence"
# below for why the units point there instead of at this checkout, and
# re-run this after every pull that touches scripts/, systemd/ or
# model-id-map.json — it is idempotent.
scripts/install.sh

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

### Layout independence

The units shipped before this fix hardcoded `ExecStart=%h/repos/prox-ops/...`,
so they only ever worked on the one checkout that path happened to match —
anywhere else, `203/EXEC`. `ExecStart=` does **not** expand environment
variables in the binary path (`ExecStart=$SOMEVAR/script.sh` is silently
wrong, not merely unsupported), so a fixed, checkout-independent path is the
only shape that works without a wrapper.

`scripts/install.sh` is that fixed point: it copies the runtime scripts,
`model-id-map.json`, `llama-swap.yaml` and this README to
`~/.local/libexec/edge-cachyos/` (mirroring this directory's own layout, so
`edge-heartbeat.sh`'s default model-map lookup keeps resolving unmodified),
and copies the two unit files — which point `ExecStart=` and `Documentation=`
at that libexec copy, never at this checkout — to `~/.config/systemd/user/`.
Run it once after cloning, and again after every pull that touches
`scripts/`, `systemd/`, `model-id-map.json` or `llama-swap.yaml`; it is
idempotent and does not touch `.env`, secrets, or PKI.

**The container consumes the same installed copy, not this checkout.**
`docker-compose.yaml` bind-mounts `${EDGE_LIBEXEC_DIR}/scripts` and
`${EDGE_LIBEXEC_DIR}/llama-swap.yaml` — never `./scripts` or
`./llama-swap.yaml` relative to wherever `docker compose` is invoked from.
That distinction is not cosmetic: a relative mount ties the running container
to whatever directory happened to be current when it was brought up, which is
how a live node ended up bound to a disposable Orca worktree — and, once that
worktree was deleted, one `docker restart` away from failing to start, because
its `/edge/scripts` was already empty. `EDGE_LIBEXEC_DIR` (see `env.example`)
must be the same path `install.sh` installs to, and both compose bind mounts
declare `create_host_path: false`, so a wrong or not-yet-installed value fails
the container loudly instead of docker silently creating an empty directory
the entrypoint then cannot find itself in. Running `scripts/install.sh` after
a pull is what updates a deployed node — for the host daemons *and* for the
container: `docker compose restart` (or the next `docker restart`/reboot)
picks up the refreshed files without a rebuild, because they are bind-mounted,
not baked into the image.

### Activating an install

`scripts/install.sh` only ever rewrites files on disk — it never restarts
anything itself, on purpose, because turning an idempotent file install into a
runtime operation would be a much bigger thing to run casually. That means a
successful install and a live fix are two different states, and none of this
directory's three runtime consumers collapses them for you:

- `edge-interactive-guard.sh` and `edge-heartbeat.sh` are `systemctl --user`
  units that hold their script in memory from process start.
- `edge-supervisor.sh` is PID 1 **inside the container**; it holds its own
  copy the same way, from whenever that container was last started or
  recreated.

None of the three watches its file for changes or reloads on `SIGHUP` — each
keeps running whatever it loaded until it is explicitly restarted or
recreated. So after a pull that touches `scripts/`, `systemd/`,
`model-id-map.json` or `llama-swap.yaml`, re-running `scripts/install.sh`
refreshes `~/.local/libexec/edge-cachyos/` and `~/.config/systemd/user/`, and
then prints one of two things:

```text
INSTALL COMPLETE      runtime files updated on disk
ACTIVATION REQUIRED   running processes must be restarted/recreated
```

If the guard, the heartbeat, or the container were already running when
install ran, the second line lists exactly which ones and is not
optional — those processes are still executing what they had loaded
*before* this install ran, including a safety fix, until the canonical
activation procedure below is followed:

```sh
systemctl --user restart edge-interactive-guard.service edge-heartbeat.service
docker compose up -d --force-recreate   # or: docker compose restart
```

This is the fix for the failure mode this story hit during its own review
cycle: an operator ran `install.sh`, saw it report success, and kept testing
against the still-running, unpatched supervisor — which made a real fix look
like a false failure. `install.sh` reporting `INSTALL COMPLETE` was never a
claim that a running safety fix is active; it is a claim about the files on
disk only. `ACTIVATION REQUIRED` is what closes that gap without making the
installer decide, on its own, to restart a serving node.

On a fresh host with nothing running yet, `install.sh` instead prints the
normal next step — enabling the two units for the first time (step 5 below).

**Migrating off the checkout-path drop-in.** Before this fix, the only thing
that made the hardcoded `ExecStart=` work on a checkout other than
`~/repos/prox-ops` was an untracked, host-local systemd drop-in —
`~/.config/systemd/user/edge-*.service.d/10-repo-path.conf` — silently
overriding `ExecStart=` back to wherever this host's checkout actually is. If
this host has one, remove it explicitly; leaving it in place is not harmless,
because a drop-in always wins over the unit file underneath it, so the fixed
`ExecStart=` above would silently never take effect:

```sh
rm -f ~/.config/systemd/user/edge-heartbeat.service.d/10-repo-path.conf \
      ~/.config/systemd/user/edge-interactive-guard.service.d/10-repo-path.conf
rmdir --ignore-fail-on-non-empty \
      ~/.config/systemd/user/edge-heartbeat.service.d \
      ~/.config/systemd/user/edge-interactive-guard.service.d 2>/dev/null || true

scripts/install.sh
systemctl --user daemon-reload
systemctl --user restart edge-interactive-guard.service edge-heartbeat.service
```

Verify the drop-in is gone and the fixed path is what actually runs:

```sh
systemctl --user cat edge-heartbeat.service | grep '^ExecStart='
# expect: ExecStart=/home/<user>/.local/libexec/edge-cachyos/scripts/edge-heartbeat.sh ...
```

### 6. Start the container

The daemons come first because the units are what create the shared state
directory, and step 5's `scripts/install.sh` is what populates
`EDGE_LIBEXEC_DIR` — both are bind-mounted with `create_host_path: false`, so
the container refuses to start against a missing or empty one rather than
silently running with no scripts and no llama-swap config. Run `docker
compose` from **this directory** (`edge/cachyos/`), where `.env` lives — the
command below omits `-f`, so Compose resolves `docker-compose.yaml` by
searching the current directory and its parents, and only finds it if you are
here. There is no other supported invocation directory.

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

### Guard-ready lease (fail-closed)

`interactive-claim` answers "does the desktop want the GPU back." It cannot
answer a different question that turned out to matter just as much: "is the
guard that would notice even alive." A guard that has crashed or hung writes
no claim, so the endpoint that infers safety purely from claim-presence reads
a dead detector identically to a quiet desktop — and that is exactly what
happened in production. After a reboot, docker's `restart: unless-stopped`
policy brought the container back before `graphical-session.target` had
started `edge-interactive-guard.service`; nothing was checking that the guard
had ever run, so the endpoint served with no interlock watching it at all.

The fix is a second file, `guard-lease`, in the same
[shared state directory](#shared-state-directory). `edge-interactive-guard.sh`
writes the current UTC epoch second to it at the end of every pass of its
`watch` loop — a **positive, continuously renewed** assertion, not a one-shot
flag a dead process's last write would keep "proving" forever. `edge-supervisor.sh`
enforces it **at the listener**, independent of `interactive-claim` entirely:

- no lease file, an unparseable one, or one older than `EDGE_GUARD_LEASE_TTL`
  (default 6s) → the listener is closed, exactly like an interactive claim;
- a fresh lease and no claim → serving is permitted;
- the lease goes stale (guard crashes or hangs) → the listener is withdrawn
  again, on the supervisor's own poll, with no dependency on the guard
  process, systemd, or anything cluster-side.

**This makes "starting closed" the actual default, not an aspiration.** On
container boot the supervisor checks the lease before ever starting
`llama-swap` — so a container that comes up before its guard (the reboot
race, and a plain `docker restart` too) stays withdrawn until a fresh lease
appears, however that container came to be running. No Kubernetes,
agentgateway, heartbeat, or network round-trip is consulted anywhere in this
path: every check is a local file read against `EDGE_STATE_DIR`, which is why
this still fails closed on a node that is entirely offline.

**The TTL and the residual exposure, stated plainly.** `EDGE_GUARD_LEASE_TTL`
defaults to 6s — three times the guard's own default 2s sampling interval, so
one slow `rocm-smi` call does not trip a false withdrawal, while a genuinely
dead or hung guard is still caught inside two of its own sampling periods.
The supervisor polls every `EDGE_POLL_INTERVAL` (default 1s). Worst case, a
guard dies the instant after renewing, so its lease reads as fresh for the
entire TTL before going stale, and the supervisor takes up to one more poll
to notice: **`EDGE_GUARD_LEASE_TTL + EDGE_POLL_INTERVAL` = 7s by default**
between guard death and listener withdrawal. That 7s window is the residual
exposure this design accepts, named rather than hidden — a shorter
interval/TTL shrinks it at the cost of more sensitivity to a single slow GPU
sample. `testing/lease-drill.sh` measures the real number on this host; see
its output in the story's Dev Agent Record for the values actually observed.

`EDGE_GUARD_LEASE_TTL` is read by the container (`docker-compose.yaml`, with
the same default) and must agree with the interval the host guard actually
runs at — the two are not derived from each other automatically, so changing
`EDGE_GUARD_INTERVAL` without reviewing this value can silently widen or
narrow the window above.

### A blind guard is not a live guard (cycle 5)

The first cold boot this story ran proved the mechanism above — units
activated and service answered before login, with no manual fix-up — but it
also surfaced a defect the mechanism did not cover: `rocm-smi` lives at
`/opt/rocm/bin/rocm-smi`, the systemd **user manager's** PATH at boot is
`/usr/local/bin:/usr/bin`, and the shipped default (`ROCM_SMI=rocm-smi`) is a
bare name resolved through PATH. Every sample failed, silently, from the
moment the unit started — invisible to every earlier check in this story,
because every earlier guard restart was `systemctl --user restart` from an
interactive shell, which inherits a login PATH that does include
`/opt/rocm/bin`. Only a real cold boot exposed it.

The guard kept renewing its lease throughout, because the original design
renewed on every loop pass regardless of whether the sample it just took
meant anything — the loop was alive, so the lease said "safe", while the
guard could not see the GPU at all. **Fail-closed on process liveness,
fail-open on detection** — the exact failure mode this story exists to
remove, one layer down.

Two changes close it, both scoped to the guard; `edge-supervisor.sh`'s
enforcement did not need to change, since it already treats "no fresh
lease" as one undifferentiated fact:

- **`ROCM_SMI` must be an absolute path.** `env.example` defaults it to
  `/opt/rocm/bin/rocm-smi`, and `scripts/install.sh` reads the value from
  the deployed `~/.config/edge-cachyos/edge.env` and refuses to install —
  `exit 1`, before touching the units — if that path does not exist or is
  not executable. A bad or missing sensor binary is a deployment failure now,
  not a warning buried in `journalctl`.
- **A lease renewal means "the guard just completed a valid GPU sample"**,
  not "the process looped". `edge-interactive-guard.sh`'s `cmd_watch` calls
  `renew_lease` only from the branch where `sample()` succeeded; a failed,
  malformed, or missing sample skips it entirely. There is deliberately no
  second failure-count timer — `EDGE_GUARD_LEASE_TTL` is still the only
  grace period, so one transient failure does not withdraw service, but
  sampling broken for longer than the TTL lets the existing lease expire and
  the supervisor withdraws exactly as it would for a hung or crashed guard.
  Running is not authorization; only seeing is.

```text
valid sample                  -> lease refreshed
one/transient failed sample   -> lease NOT refreshed; previous lease valid only
                                  for its remaining TTL
sampling broken > TTL         -> lease expires -> supervisor withdraws llama-swap
                                  -> client cannot obtain inference
valid sampling returns        -> fresh lease -> autonomous recovery
```

`testing/lease-drill.sh`'s sensor-blindness scenario reproduces this without
waiting for a real cold boot: it points a running guard's `ROCM_SMI` at a
path that does not exist, proves the client-observed outage and phase
`withdrawn` within ~TTL, then restores it and proves autonomous recovery to
an authenticated 200.

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

This drill exercises the VRAM/compute detector and `interactive-claim` only.
The [guard-ready lease](#guard-ready-lease-fail-closed) — the fail-closed
interlock that also has to survive the guard itself crashing or a reboot
racing the container — is a different failure mode with its own proof:
`testing/lease-drill.sh`. It cannot be exercised from a checkout with no
`docker`/`systemctl` access; run it on the host with both units installed and
the container up. See its own `--help` and the story's Dev Agent Record for
what it proved and what, if anything, still needs a human to run it.

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

**`capabilities` and `max_context` describe what this deployment can serve**,
and are derived from the models it is configured with — the catalog ids in
`model-id-map.json`'s `runtime_to_catalog`, with their facts from the same file.
Deliberately not from `cached_models`: with the shipped default the model store
is a docker volume this daemon cannot read, so a cache-derived answer advertised
a warm, serving node as capable of nothing with a zero-length context window,
which a router can only read as "never pick me". An unobservable cache is not an
incapable node. It stays an advertisement rather than an authority — the router
intersects it with the real catalog under R14, so facts that drifted here narrow
placement instead of widening it.

**`cached_models` stays a filesystem observation, and stays pessimistic.** It is
positive evidence that an artifact is on local storage, so with the volume-backed
default it is reported empty even while the model is warm. That combination is
honest rather than contradictory: `active_model` says the model is loaded now,
`cached_models` says nothing can be proven about what is on disk. Neither is
inferred from the other, and neither is inferred from configuration — claiming
disk-cache knowledge from `model-id-map.json` would promise the router a load
time nobody measured.

```json
{"active_model": "qwen36-27b", "cached_models": [],
 "capabilities": ["chat", "tools"], "max_context": 65536}
```

Making the cache observable for an *unloaded* model — so the router can tell a
cold-but-cached node from a cold-and-empty one — is a readiness and economics
follow-up, worth doing before 35.11. It needs either the model store on a
host-readable path or a cache observer that can see inside the volume, and it is
deliberately not built here.

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

```sh
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
