# Factory image foundation: local fixture only

This packages the pinned Debian amd64 base, Orca Linux server v1.4.261 and
the source CLI at AFK commit `f62d4efa4768a9ca985248f5ad931cc2e5c53f41`.
The source candidate is not a release approval. The payload is the source CLI,
libraries and runtime-scope adapter needed for read-only help and state commands.
No harness binaries, credentials, Paperclip configuration or factory-stage
route/stamp/validator binding are packaged. Never start the poll supervisor in
the local fixture.

Run from the repository root, with the **exact candidate Git checkout**:

```sh
bash images/factory/test-local.sh --afk-repo /path/to/agent-flow-kit-checkout
```

If the sandbox cannot download the official archive, fetch
`https://github.com/stablyai/orca/releases/download/v1.4.261/orca-server-linux-x64.tar.gz`
through an authorized public network path and pass `--archive PATH`. The helper
checks SHA-256 before extraction and the Dockerfile checks it again. Source is
exported with `git archive` from the exact commit in a cleared Git environment,
with replacement objects and lazy fetch disabled. Every exported file is
compared with its committed blob before the build; current checkout edits never
enter the image. Each run creates a fresh mode-0700 context under `/tmp`, copies
only the named image files, and exports the pinned source there. A supplied
archive is copied to a new temporary file, hashed, then renamed within that
private context. The helper never reads or changes an existing
`images/factory/inputs/` cache. The context is removed on exit; host profiles
and credentials do not enter it.

The helper uses only the explicit local Unix Docker socket and an empty private
Docker configuration. It builds without cache export or push. The disposable
container has no external network or host socket, no capabilities, read-only
root, fixed UID/GID 10001, bounded tmpfs HOME/cache/scratch, CPU/memory/PID
limits and no persisted logs. Cleanup requires the fixture ownership label,
including after a partial detached start. It checks CLI help, versions, writes, private
loopback TCP readiness, then stops and destroys the instance. It never prints
Orca pairing data. Any server identity it generates lives only in tmpfs.

`poll-supervisor.sh` is a separate foreground wrapper for a future authorized
controller. It runs one documented `paperclip-poll` at a time, discards child
stdout/stderr, and emits only pass exit status, timestamp and stop status.
Cadence defaults to 90 seconds measured from each
pass start; a long pass finishes before another begins. It forwards TERM/INT
to its owned process group and waits for the leader. It is **not** enabled by
this image's default command. The local fixture does not connect to Paperclip.

The Debian base digest and Orca archive are pinned, but apt indices and
packages are not; rebuilds are not bit-for-bit reproducible. UID/GID and HOME
are fixture choices pending storage identity QA. Native pairing/PAM, durable
CSI/fencing, native harness login and model execution remain unproved.
