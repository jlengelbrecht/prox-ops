# Factory image foundation: local fixture only

This packages the pinned Debian amd64 base, Orca Linux server v1.4.261 and
the source CLI at AFK commit `f62d4efa4768a9ca985248f5ad931cc2e5c53f41`.
The source candidate is not a release approval. The payload is the source CLI,
libraries and runtime-scope adapter needed for read-only help and state commands.
No harness binaries, credentials, Paperclip configuration or factory-stage
route/stamp/validator binding are packaged. The local fixture starts the poll
supervisor only with a synthetic CLI mounted over its polling command.

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
loopback TCP readiness, then verifies a running state before stop and an exited
state after stop. Exit 0 (handled TERM) or 143 (direct SIGTERM termination) is
accepted only without OOM, forced kill, or Docker error; exit 137 is rejected.
This verifies process termination, not application state flush or persistent
recovery. The fixture then destroys the instance. It never prints
Orca pairing data. Any server identity it generates lives only in tmpfs.

After the Orca check, the same image runs one synthetic supervisor smoke under
Docker's init process and the same UID, resource and isolation flags. A
read-only bind shadows the AFK CLI; a token check verifies that exact fake
before the wrapper starts. The fake starts a sleeping child, records its
startup and reap in container tmpfs, and never contacts Paperclip or a model.
The helper checks the markers, stops through PID 1 with a 15-second deadline,
and requires an exit code of 0 without forced kill or Docker error. It removes
the labelled smoke container with the same owned-resource cleanup.

`poll-supervisor.sh` is a separate foreground wrapper for a future authorized
controller. It keeps one `paperclip-poll` process group active at a time, discards child
stdout/stderr, and emits only pass exit status, timestamp and stop status.
Cadence defaults to 90 seconds and accepts decimal values from 1 to 86400
seconds; the upper bound avoids Bash integer overflow and is a technical
limit, not a measured capacity SLA. Cadence is measured from each
pass start; a long pass finishes before another begins. After the CLI exits,
the group leader waits for all active members of its process group before the
wrapper starts another pass. It forwards TERM/INT to individually reverified
members of its owned process group and checks every known active group member,
including descendants after the leader exits. An ordinary pass has no
deadline. Cancellation deadlines use the kernel monotonic uptime clock: it
gives TERM three seconds, then sends KILL to
individually reverified owned members that remain; it allows two more seconds
to confirm the group has no active members and to reap the leader. If group
ownership or emptiness cannot be verified, or KILL was needed, stop fails.
This bounds the wrapper's stop wait; it does not prove
application flush, persistence, or recovery. Workers that deliberately leave
the owned process group are outside this lifecycle contract; this wrapper is
not universal native worker fencing. It is **not** enabled by
this image's default command. The local fixture does not connect to Paperclip.

The Debian base digest and Orca archive are pinned, but apt indices and
packages are not; rebuilds are not bit-for-bit reproducible. UID/GID and HOME
are fixture choices pending storage identity QA. Native pairing/PAM, durable
CSI/fencing, native harness login and model execution remain unproved.
