# Factory image foundation: local fixture only

This packages the pinned Debian amd64 base, Orca Linux server v1.4.261 and
the source CLI at AFK commit `f62d4efa4768a9ca985248f5ad931cc2e5c53f41`.
The source candidate is not a release approval. The payload is the source CLI,
libraries and runtime-scope adapter needed for read-only help and state commands.
No harness binaries, credentials, Paperclip configuration or factory-stage
route/stamp/validator binding are packaged. The local fixture starts the poll
supervisor only with a synthetic CLI mounted over its polling command.

Run the executable helper directly from the repository root, with the **exact
candidate Git checkout**:

```sh
./images/factory/test-local.sh --afk-repo /path/to/agent-flow-kit-checkout
```

This is the supported clean entry. Linux executes its GNU `/usr/bin/env -S -i`
shebang before `/bin/bash`, with only `PATH=/usr/bin:/bin`,
`HOME=/nonexistent`, `LANG=C`, and `LC_ALL=C`. The executable bit is part of
the source contract. Keep optional `--archive PATH` and `--report NEW_JSON_PATH`
after `--afk-repo PATH`; their paths are still caller-selected inputs subject
to the checks below. The trusted controller must launch this executable as an
argv vector from a trusted checkout and working directory, with a fresh
environment before its own interpreter and any timeout process start. It must
control the absolute `/usr/bin/env`, `/bin/bash`, Git, tar, curl, Docker and
Python host tools and their loader/configuration boundary. `bash
images/factory/test-local.sh` bypasses the clean shebang and is unsupported
from a contaminated parent. This entry cannot undo code or loader execution
that already happened in the parent or before `/usr/bin/env` clears its
environment. The controller should pass only the four listed variables to
the helper and any timeout wrapper.

For a closed diagnostic result, pass `--report /tmp/unique-fixture-result.json`
using a new path in a private directory. The helper writes a mode-0600 JSON
object once, with `status`, `phase`, a fixed `reason` code and
`observed_exit_status`. It writes and closes a private temporary file, then
publishes it with Linux `renameat2(RENAME_NOREPLACE)`; an existing file or
symlink is refused. Command output is
discarded, including errors from Docker, Orca, Git, Python and extraction;
the report contains no command output or exception text. A failed run remains
a failure even when cleanup succeeds. If the report cannot be created, the
helper exits nonzero. A controller must check the helper's exit status is zero,
the complete report exactly says `PASS` / `complete` / `ok` / exit status zero,
and the current source and input identities and hashes before accepting a run.
A report file alone is not success evidence. Normal write or close failure
cannot publish a partial `PASS`; an uncatchable kill, kernel stall, or failed
temporary-file removal can leave an uncertain residual and requires controller
rejection.

If the synthetic supervisor never creates its readiness marker, the report
also includes a fixed `readiness_diagnostic` code derived from Docker's
running/exited state and recognized fixture startup stages or supervisor event
codes. `entry_started` means the image shell command ran, `bind_verified`
means its fake CLI token check passed, and `helper_started` means Python
entered the supervisor's main function. A later fixed failure code takes
precedence; the event file records allowlisted codes such as `launch_failed`
and `stop_outcome` without exception text or values. The smoke redirects
interpreter and wrapper stderr to `/dev/null`;
only these fixed codes enter its private tmpfs event file. An
`observed_container_exit_status` appears only when Docker reports an exited
container with an integer code. Unknown Docker state yields
`probe_unavailable`; unavailable event data leaves only the container-state
code. No Docker error text, child output, path, or environment
value is copied into the report. These fields narrow a failed run but do not
establish a cause by exit code alone. If cleanup also fails, the earlier readiness phase and exit status
remain in the report; a cleanup failure after otherwise successful checks is
reported as a cleanup failure. The controller still requires helper exit 0
and the complete current `PASS` report.

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

The receiving GNU tar runs as `/usr/bin/tar` under a fresh environment, so
`TAR_OPTIONS` cannot add checkpoint actions or extraction flags. Git uses
`/usr/bin/git` with cleared configuration and replacement settings. The
download uses `/usr/bin/curl --disable` under a fresh environment; Docker uses
`/usr/bin/docker`, the explicit Unix socket and a private configuration.
The clean entry fixes `PATH` for all remaining host commands and excludes
caller shell functions and startup/configuration variables before Bash starts.

The private context runs with `umask 077`. Its producer explicitly makes only
the copied public `poll-supervisor.py` mode 0644, and the Dockerfile sets that
installed helper to mode 0644. The image runs as UID 10001, which must be able
to read the root-owned helper through `/opt/factory/`. No blanket permission
change is made to other staged files or private data. Earlier physical smoke reports exited
before readiness with code 2; they did not establish that file mode as the
actual cause, so a new image fixture is still required.

The helper uses only the explicit local Unix Docker socket and an empty private
Docker configuration. It builds without cache export or push. The disposable
container has no external network or host socket, no capabilities, read-only
root, fixed UID/GID 10001, bounded tmpfs HOME/cache/scratch, CPU/memory/PID
limits and no persisted logs. Cleanup requires the fixture ownership label,
including after a partial detached start. It checks CLI help, versions, writes, private
loopback TCP readiness, then verifies a running state before stop and an exited
state after stop. Exit 0 or 143 is accepted only without OOM, forced kill, or
Docker error; exit 137 is rejected. This verifies an observed nonforced process
exit after `docker stop`; the state alone cannot prove TERM delivery because the
process may exit between the two observations. It cannot prove application
state flush or persistent recovery. The fixture then destroys the instance. It never prints
Orca pairing data. Any server identity it generates lives only in tmpfs.

After the Orca check, the same image runs one synthetic supervisor smoke under
Docker's init process and the same UID, resource and isolation flags. A
read-only bind shadows the AFK CLI; a token check verifies that exact fake
before the wrapper starts. The fake starts a sleeping child, records its
startup and actual shell wait/reap in container tmpfs, and never contacts
Paperclip or a model.
The controller and Docker daemon must see the same absolute `/tmp` fixture path
for this bind mount. A remote daemon or one with a different filesystem namespace
cannot run this smoke with that path.
The helper checks the markers, stops through PID 1 with a 15-second deadline,
and requires an exit code of 0 without forced kill or Docker error. It removes
the labelled smoke container with the same owned-resource cleanup.

The offline contract suite launches each exclusive fake inside its own owner
process. That owner is the subreaper for the fake tree, holds verified pidfds,
signals and reaps its descendants, and requires a kernel `waitpid` child-table
barrier before `wait` or `communicate` returns completed evidence. `poll` can
report the root's intermediate exit status. A live descendant after root exit
causes bounded forced retirement and a nonpassing result; its output is not
published as completed evidence. The test runner uses bounded pipes to request fake operations;
unrelated runner children never enter the owner's child table. Control failure
is nonpassing and triggers bounded owner cleanup. The nonexclusive ancestry
tests retain their separate, explicit foreign-child fixtures.

`poll-supervisor.sh` is a thin entrypoint to the Linux Python helper for a future
authorized controller. The helper uses `PR_SET_CHILD_SUBREAPER`, pidfds and
`waitpid` to keep one local `paperclip-poll` child tree active at a time. It
discards child stdout/stderr and emits pass status, timestamp and stop status.
Cadence defaults to 90 seconds and accepts decimal values from 1 to 86400
seconds; the upper bound is a technical
limit, not a measured capacity SLA. Cadence is measured from each
pass start; a long pass finishes before another begins. The kernel's empty
child table, after adopting and reaping descendants, is the completion barrier;
`/proc` child listings are used only to find signal targets. Cancellation
retains each selected parent's pidfd while opening, verifying, and signalling
its children, and checks that the parent is still live during selection. A
child whose parent has exited is selected after subreaper adoption on a later
scan; a numeric descendant PID alone is never treated as ownership proof.
A handled TERM/INT blocks later CLI launches. During the final
launch handoff, the parent masks TERM/INT until the close-on-exec startup pipe
closes or the pre-exec child is retired. Pipe EOF alone does not prove exec:
an uncatchable pre-exec signal also closes it. A signaled child is reported as
an interrupted pass unless its changed live executable was observed in `/proc`;
normal exits use the child path's explicit denial/failure markers to distinguish
launch failures from CLI exit statuses. A signal delivered in the short startup
window remains pending: exec may occur before the parent handles the
signal, then the parent handles cancellation and drains the child tree. The
startup window is bounded to two seconds; timeout or startup I/O failure
enters the bounded owned-tree TERM/KILL/reap path before a pending stop is
handled. The signal handler does not
wait on a child-held lock. Cancellation gives owned children
three monotonic seconds for TERM. Each retained owned task gets TERM once
during that grace while newly discovered or adopted children are still scanned;
the helper then sends KILL to those still present and
waits up to two more seconds for reaping. It reports a forced stop when an
owned task sent KILL is reaped with a KILL exit status. If a parent reaps that
task first, its retained KILL delivery remains an uncertain nonpassing stop.
A pass has no ordinary
deadline. An uncertain or unreaped stop fails. This covers the wrapper's local descendant tree, including
reparented children; jobs separately managed by Orca outside this tree are not
owned by this helper. It does not prove application flush, persistence, recovery,
or native worker fencing. It is **not** enabled by
this image's default command. The local fixture does not connect to Paperclip.

The Debian base digest and Orca archive are pinned, but apt indices and
packages are not; rebuilds are not bit-for-bit reproducible. UID/GID and HOME
are fixture choices pending storage identity QA. Native pairing/PAM, durable
CSI/fencing, native harness login and model execution remain unproved.
