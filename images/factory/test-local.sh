#!/usr/bin/env bash
# Disposable, credential-free build and Orca startup fixture.
set -euo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_repo=''
archive=''
report_path=''
phase=arguments
reason=invalid_arguments
while (($#)); do
  case "$1" in
    --afk-repo) source_repo="${2:?}"; shift 2 ;;
    --archive) archive="${2:?}"; shift 2 ;;
    --report) report_path="${2:?}"; shift 2 ;;
    *) printf 'usage: %s --afk-repo PATH [--archive VERIFIED_ARCHIVE] [--report NEW_JSON_PATH]\n' "$0" >&2; exit 2 ;;
  esac
done
if [[ -n "$report_path" && ( -e "$report_path" || -L "$report_path" ) ]]; then
  printf 'test-local: report path must be new\n' >&2
  exit 2
fi
# Only fixed report fields are retained. Opaque Docker, Orca, Git, Python and
# archive command output goes to /dev/null, including on unexpected failure.
exec >/dev/null 2>&1
stage=''; scratch=''; client_cfg=''; fixture_cleanup_active=0; cleanup_failed=0
readiness_diagnostic=''; readiness_exit_status=''
release_temps() {
  local status=0
  [[ -z "$scratch" ]] || rm -rf -- "$scratch" || status=1
  [[ -z "$client_cfg" ]] || rm -rf -- "$client_cfg" || status=1
  [[ -z "$stage" ]] || rm -rf -- "$stage" || status=1
  return "$status"
}
finish() {
  local status=$?
  local initial_status=$status
  trap - EXIT
  if [[ "$fixture_cleanup_active" == 1 ]]; then
    cleanup "$status" || status=1
  else
    release_temps || { status=1; cleanup_failed=1; }
  fi
  if [[ "$cleanup_failed" == 1 ]]; then
    if [[ "$initial_status" == 0 ]]; then
      phase=cleanup; reason=owned_cleanup_failed; status=1
    else
      status=$initial_status
    fi
  fi
  if [[ "$status" == 0 ]]; then phase=complete; reason=ok; fi
  if [[ -n "$report_path" ]]; then
    /usr/bin/python3 -I - "$report_path" "$phase" "$reason" "$status" "$readiness_diagnostic" "$readiness_exit_status" <<'PY' || status=1
import ctypes, json, os, sys, tempfile
path, phase, reason, status, diagnostic, container_exit = sys.argv[1:]
fd, temporary = tempfile.mkstemp(prefix=".factory-report-", dir=os.path.dirname(os.path.abspath(path)))
try:
    with os.fdopen(fd, "w", encoding="ascii") as out:
        fd = -1
        report = {"schema": "factory-fixture-report/1", "status": "PASS" if status == "0" else "FAIL",
                  "phase": phase, "reason": reason, "observed_exit_status": int(status)}
        if phase == "supervisor_readiness" and status != "0":
            report["readiness_diagnostic"] = diagnostic or "probe_unavailable"
            if container_exit:
                report["observed_container_exit_status"] = int(container_exit)
        json.dump(report, out)
        out.write("\n")
    # Linux renameat2 makes the complete, closed file visible exactly once.
    # RENAME_NOREPLACE also refuses an existing file or symlink at path.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(temporary), -100, os.fsencode(path), 1) != 0:
        raise OSError(ctypes.get_errno(), "exclusive report publication failed")
    temporary = None
finally:
    if fd >= 0:
        os.close(fd)
    if temporary is not None:
        os.unlink(temporary)
PY
  fi
  exit "$status"
}
trap finish EXIT
phase=source_checkout; reason=invalid_source_checkout
[[ -n "$source_repo" && -e "$source_repo/.git" ]] || { printf 'test-local: --afk-repo must name a Git checkout\n' >&2; exit 2; }
phase=source_pin; reason=source_pin_failed
lock() { sed -n "s/^$1=//p" "$here/versions.lock"; }
commit=$(lock AFK_COMMIT)
tree=$(lock AFK_SOURCE_TREE)
asset=$(lock ORCA_ASSET)
url=$(lock ORCA_URL)
[[ "$commit" == f62d4efa4768a9ca985248f5ad931cc2e5c53f41 &&
   "$tree" == f2732f6fb848794974ee08fd0139c4f188c54c72 &&
   "$asset" == orca-server-linux-x64.tar.gz &&
   "$url" == "https://github.com/stablyai/orca/releases/download/v1.4.261/$asset" ]] || {
  printf 'test-local: unexpected lock values\n' >&2; exit 1;
}
# Ignore caller Git configuration, replacement refs, tracing, index overrides and
# promisor/lazy fetch settings. Only already-present objects may supply the pin.
source_git() {
  env -i PATH=/usr/bin:/bin HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 \
    GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1 \
    GIT_OPTIONAL_LOCKS=0 /usr/bin/git -C "$source_repo" "$@"
}
[[ "$(source_git rev-parse --verify "$commit^{commit}")" == "$commit" &&
   "$(source_git rev-parse --verify "$commit^{tree}")" == "$tree" ]] || {
  printf 'test-local: source commit/tree mismatch\n' >&2; exit 1;
}
# The selected paths must consist solely of regular Git blobs with no paths
# outside the three documented runtime roots.
source_git ls-tree -rz "$commit" -- bin lib adapters/runtime-scope.sh | \
  /usr/bin/python3 -I -c 'import sys
entries = [e for e in sys.stdin.buffer.read().split(b"\0") if e]
if not entries:
    raise ValueError("empty source payload")
for entry in entries:
    meta, path = entry.split(b"\t", 1)
    mode, kind, _ = meta.split(b" ")
    if mode not in (b"100644", b"100755") or kind != b"blob":
        raise ValueError("non-regular source payload")
    if not (path == b"adapters/runtime-scope.sh" or path.startswith((b"bin/", b"lib/"))):
        raise ValueError("unexpected source path")' || {
  printf 'test-local: invalid committed source payload\n' >&2; exit 1;
}
# mktemp owns this mode-0700 context for one run. Never inspect or modify the
# repository's old ignored inputs cache, even if it contains links.
phase=source_export; reason=source_export_failed
stage=$(mktemp -d /tmp/factory-fixture.XXXXXXXX)
mkdir -m 0755 -- "$stage/inputs"
install -d -m 0755 "$stage/inputs/afk"
for file in Dockerfile checksums.sha256 safe-extract.py poll-supervisor.sh poll-supervisor.py; do
  [[ -f "$here/$file" && ! -L "$here/$file" ]] || { printf 'test-local: invalid image source file: %s\n' "$file" >&2; exit 1; }
  cp -- "$here/$file" "$stage/$file"
done
# The private umask makes cp produce mode 0600 even for a public 0644 source.
# Only this installed public helper needs other-UID read access at runtime.
chmod 0644 -- "$stage/poll-supervisor.py"
source_git -c tar.umask=0022 archive --format=tar "$commit" bin lib adapters/runtime-scope.sh | tar --same-permissions --no-same-owner -xf - -C "$stage/inputs/afk"
# Docker COPY leaves the source root-owned. Require every exported directory
# to be traversable and each payload file readable by runtime UID 10001.
/usr/bin/python3 -I - "$stage/inputs/afk" <<'PY'
import pathlib, stat, sys

root = pathlib.Path(sys.argv[1])
for path in (root, *root.rglob("*")):
    mode = path.stat().st_mode
    if path.is_dir():
        if not (mode & stat.S_IROTH and mode & stat.S_IXOTH):
            raise ValueError(f"unreadable directory: {path}")
    elif path.is_file():
        if not mode & stat.S_IROTH:
            raise ValueError(f"unreadable file: {path}")
    else:
        raise ValueError(f"unexpected source type: {path}")
cli = root / "bin/agent-flow-kit"
if not (cli.is_file() and cli.stat().st_mode & stat.S_IXOTH):
    raise ValueError(f"missing executable CLI: {cli}")
PY
# Compare every exported byte and mode with the exact committed blobs. This
# also catches archive attributes that omit or substitute a selected file.
source_git ls-tree -rz "$commit" -- bin lib adapters/runtime-scope.sh | \
  /usr/bin/python3 -I -c 'import hashlib, os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1]); repo = sys.argv[2]
entries = [e for e in sys.stdin.buffer.read().split(b"\0") if e]
expected = set()
env = dict(PATH="/usr/bin:/bin", HOME="/nonexistent", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0")
for entry in entries:
    meta, path = entry.split(b"\t", 1)
    mode, kind, oid = meta.split(b" ")
    name = path.decode("utf-8", "strict"); expected.add(name)
    exported = root / name
    if not exported.is_file() or exported.is_symlink():
        raise ValueError(f"missing regular export: {name}")
    if bool(exported.stat().st_mode & 0o111) != (mode == b"100755"):
        raise ValueError(f"export mode differs: {name}")
    original = subprocess.check_output(["/usr/bin/git", "-C", repo, "cat-file", "blob", oid.decode("ascii")], env=env)
    if exported.read_bytes() != original:
        raise ValueError(f"export bytes differ: {name}")
actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
if actual != expected:
    raise ValueError("export inventory differs")' "$stage/inputs/afk" "$source_repo" || {
  printf 'test-local: exported source differs from pinned tree\n' >&2; exit 1;
}
printf 'source_commit=%s source_tree=%s\n' "$commit" "$tree"
phase=archive_acquire; reason=archive_acquire_failed
if [[ -n "$archive" ]]; then
  [[ -f "$archive" && ! -L "$archive" ]] || { printf 'test-local: archive must be a regular file\n' >&2; exit 1; }
  cp -- "$archive" "$stage/inputs/$asset.part"
else
  if ! env -i PATH="$PATH" curl --disable --fail --location --silent --show-error --max-time 180 --output "$stage/inputs/$asset.part" "$url"; then
    printf 'test-local: official archive unavailable; provide --archive PATH or permit the official public download\n' >&2
    exit 1
  fi
fi
phase=archive_verify; reason=archive_verification_failed
[[ "$(sha256sum "$stage/inputs/$asset.part" | cut -d' ' -f1)" == "$(cut -d' ' -f1 "$stage/checksums.sha256")" ]] || {
  printf 'test-local: Orca archive checksum mismatch\n' >&2; exit 1;
}
mv -- "$stage/inputs/$asset.part" "$stage/inputs/$asset"
(cd "$stage" && sha256sum -c checksums.sha256)
scratch=$(mktemp -d)
client_cfg=$(mktemp -d)
nonce=$(/usr/bin/python3 -I -c 'import secrets; print(secrets.token_hex(16))')
image_tag="factory-fixture:$nonce"
container_name="factory-fixture-$nonce"
smoke_container_name="$container_name-supervisor"
container_id=''
smoke_container_id=''
image_built=0
cleanup() {
  local status=$1 owner
  if [[ -n "${smoke_container_name:-}" ]]; then
    owner=$(dock container inspect --format '{{ index .Config.Labels "factory.fixture.owner" }}' "$smoke_container_name" 2>/dev/null) || owner=''
    if [[ "$owner" == "$container_name" ]]; then
      if ! dock rm -f "$smoke_container_name" >/dev/null 2>&1; then
        printf 'test-local: failed to remove owned supervisor container\n' >&2; status=1; cleanup_failed=1
      fi
    elif [[ -n "${smoke_container_id:-}" ]]; then
      printf 'test-local: supervisor container ownership could not be verified\n' >&2; status=1; cleanup_failed=1
    fi
  fi
  # A failed detached run can create the container before returning its ID.
  # Name alone is insufficient ownership proof: require this run's label.
  owner=$(dock container inspect --format '{{ index .Config.Labels "factory.fixture.owner" }}' "$container_name" 2>/dev/null) || owner=''
  if [[ "$owner" == "$container_name" ]]; then
    if ! dock rm -f "$container_name" >/dev/null 2>&1; then
      printf 'test-local: failed to remove owned container\n' >&2; status=1; cleanup_failed=1
    fi
  elif [[ -n "$container_id" ]]; then
    printf 'test-local: reserved container ownership could not be verified\n' >&2; status=1; cleanup_failed=1
  fi
  owner=$(dock image inspect --format '{{ index .Config.Labels "factory.fixture.owner" }}' "$image_tag" 2>/dev/null) || owner=''
  if [[ "$owner" == "$container_name" ]]; then
    if ! dock image rm "$image_tag" >/dev/null 2>&1; then
      printf 'test-local: failed to remove owned image\n' >&2; status=1; cleanup_failed=1
    fi
  elif [[ "$image_built" == 1 ]]; then
    printf 'test-local: built image ownership could not be verified\n' >&2; status=1; cleanup_failed=1
  fi
  rm -rf -- "$scratch" "$client_cfg" "$stage" || { status=1; cleanup_failed=1; }
  return "$status"
}
dock() {
  env -i PATH="$PATH" HOME="$client_cfg" XDG_CONFIG_HOME="$client_cfg" DOCKER_CONFIG="$client_cfg" DOCKER_HOST=unix:///run/docker.sock \
    docker --host unix:///run/docker.sock --config "$client_cfg" "$@"
}
fixture_cleanup_active=1
phase=archive_extract; reason=archive_extract_failed
/usr/bin/python3 -I "$stage/safe-extract.py" "$stage/inputs/$asset" "$scratch/orca-server"
test -x "$scratch/orca-server/bin/orca-ide"
printf 'orca_archive_sha256=%s archive_layout=bin/orca-ide\n' "$(sha256sum "$stage/inputs/$asset" | cut -d' ' -f1)"
phase=docker_daemon; reason=docker_daemon_unavailable
if [[ ! -S /run/docker.sock ]]; then
  printf 'test-local: explicit local Docker socket unavailable\n' >&2; exit 1
fi
dock version --format '{{.Server.Arch}}' | grep -qx amd64
phase=image_build; reason=image_build_failed
dock build --no-cache --pull --platform linux/amd64 --network default \
  --label "factory.fixture.owner=$container_name" --tag "$image_tag" "$stage" >/dev/null
image_built=1
printf 'image_id=%s\n' "$(dock image inspect --format '{{.Id}}' "$image_tag")"
run_flags=(--network none --read-only --cap-drop ALL --security-opt no-new-privileges
  --pids-limit 128 --cpus 1 --memory 1g
  --tmpfs /home/factory:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=128m
  --tmpfs /cache:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=64m
  --tmpfs /scratch:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=64m
  --tmpfs /tmp:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=32m
  --tmpfs /run:rw,noexec,nosuid,nodev,uid=10001,gid=10001,mode=0700,size=16m
  --log-driver none)
phase=afk_help; reason=afk_help_failed
dock run --rm "${run_flags[@]}" "$image_tag" /opt/afk/bin/agent-flow-kit --help >/dev/null
phase=orca_version; reason=orca_version_failed
dock run --rm "${run_flags[@]}" "$image_tag" /opt/orca-server/bin/orca-ide --version
phase=runtime_isolation; reason=runtime_isolation_failed
dock run --rm "${run_flags[@]}" "$image_tag" /bin/bash -c 'test "$(id -u):$(id -g):$HOME" = "10001:10001:/home/factory" && touch "$HOME/w" /cache/w /scratch/w && test ! -e /run/docker.sock'
phase=orca_start; reason=orca_start_failed
container_id=$(dock run -d --name "$container_name" --hostname "$container_name" \
  --env "ORCA_INSTANCE_ID=$container_name" \
  --label "factory.fixture.owner=$container_name" "${run_flags[@]}" "$image_tag" \
  /opt/orca-server/bin/orca-ide serve --bind 127.0.0.1 --port 8765 --refuse-public 2>/dev/null)
phase=orca_readiness; reason=orca_readiness_failed
ready=0
for _ in $(seq 1 40); do
  if dock exec "$container_id" python3 -c 'import socket; s=socket.socket(); s.settimeout(0.2); result=s.connect_ex(("127.0.0.1",8765)); s.close(); raise SystemExit(result != 0)' >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || { printf 'test-local: Orca readiness failed\n' >&2; exit 1; }
printf 'orca_private_tcp_ready=true\n'
stop_state() {
  /usr/bin/python3 -I -c 'import json, sys
state = json.loads(sys.argv[2])
if sys.argv[1] == "before":
    good = state.get("Running") is True and state.get("Status") == "running"
else:
    # 0/143 are accepted observed exits after stop; this does not prove TERM
    # delivery. 137 indicates forced termination and is rejected.
    good = (state.get("Running") is False and state.get("Status") == "exited"
            and type(state.get("ExitCode")) is int and state["ExitCode"] in (0, 143)
            and state.get("OOMKilled") is False and state.get("Dead") is False
            and state.get("Error") == "")
if not good:
    raise SystemExit("test-local: container stop state is not an accepted observed exit")' "$1" "$2"
}
phase=orca_stop; reason=orca_stop_failed_or_unverified
stop_state before "$(dock container inspect --format '{{json .State}}' "$container_id")"
dock stop --time 15 "$container_id" >/dev/null
stop_state after "$(dock container inspect --format '{{json .State}}' "$container_id")"
printf 'orca_observed_nonforced_stop=true\n'
# EXIT trap removes the stopped, labelled container and image exactly once.

# Shadow only the CLI in one disposable container. Verify the bind before
# launching the supervisor so a mount failure cannot invoke a real poll.
phase=supervisor_start; reason=supervisor_start_failed
fake_cli="$stage/fake-agent-flow-kit"
printf '#!/usr/bin/env bash\n# fixture-token:%s\n' "$nonce" > "$fake_cli"
cat >> "$fake_cli" <<'FAKE'
[[ "$#" == 1 && "$1" == paperclip-poll ]] || exit 86
sleep 300 & child=$!
on_term() {
  wait "$child" || [[ "$?" == 143 ]] || exit 87
  touch /scratch/fake-reaped
  for ((i=0; i<100; i++)); do
    [[ ! -e /scratch/fake-observed ]] || exit 0
    sleep 0.1
  done
  exit 88
}
trap on_term TERM
printf '%s\n' running > /scratch/fake-running
wait "$child"
FAKE
chmod 0755 "$fake_cli"
fake_mount="type=bind,source=$fake_cli,target=/opt/afk/bin/agent-flow-kit,readonly"
smoke_container_id=$(dock run -d --init --name "$smoke_container_name" \
  --label "factory.fixture.owner=$container_name" --env AFK_POLL_CADENCE_SECONDS=86400 \
  --env FACTORY_FIXTURE_DIAGNOSTICS=1 \
  "${run_flags[@]}" --mount "$fake_mount" "$image_tag" /bin/bash -c \
  'printf "fixture: entry_started\n" > /scratch/supervisor-events || exit 86
   test "$(sed -n 2p /opt/afk/bin/agent-flow-kit)" = "# fixture-token:$1" || exit 86
   printf "fixture: bind_verified\n" >> /scratch/supervisor-events || exit 86
   exec /opt/factory/poll-supervisor.sh 2>/dev/null' _ "$nonce")
phase=supervisor_readiness; reason=supervisor_readiness_failed
running=0
for _ in $(seq 1 100); do
  if dock exec "$smoke_container_id" /bin/bash -c \
    'test "$(cat /scratch/fake-running 2>/dev/null)" = running' >/dev/null 2>&1; then
    running=1; break
  fi
  sleep 0.1
done
if [[ "$running" != 1 ]]; then
  # Only fixed Docker state fields and our own supervisor events can enter
  # this closed diagnostic. The private copied event file is removed on exit.
  dock_probe() {
    /usr/bin/timeout -k 2s 5s env -i PATH="$PATH" HOME="$client_cfg" XDG_CONFIG_HOME="$client_cfg" \
      DOCKER_CONFIG="$client_cfg" DOCKER_HOST=unix:///run/docker.sock \
      docker --host unix:///run/docker.sock --config "$client_cfg" "$@"
  }
  smoke_state=$(dock_probe container inspect --format '{{json .State}}' "$smoke_container_id" 2>/dev/null) || smoke_state=''
  dock_probe cp "$smoke_container_id:/scratch/supervisor-events" "$stage/supervisor-events" >/dev/null 2>&1 || :
  diagnostic=$(/usr/bin/python3 -I - "$smoke_state" "$stage/supervisor-events" <<'PY'
import json, pathlib, sys
try:
    state = json.loads(sys.argv[1])
except (ValueError, TypeError):
    state = None
code, exit_code = "probe_unavailable", ""
if isinstance(state, dict):
    if state.get("Running") is True and state.get("Status") == "running":
        code = "container_running_marker_absent"
    elif state.get("Running") is False and state.get("Status") == "exited":
        code = "container_exited_before_marker"
        value = state.get("ExitCode")
        if type(value) is int and 0 <= value <= 255:
            exit_code = str(value)
events = pathlib.Path(sys.argv[2])
if events.is_file():
    known = ("invalid_configuration", "unsupported_linux_child_primitives", "subreaper_unavailable",
             "launch_failed=", "pass_interrupted", "cancellation_children_unreaped",
             "stop_outcome=forced_or_unreaped", "stopped")
    try:
        lines = events.read_text(encoding="ascii", errors="ignore")[:4096].splitlines()
        stage = ""
        for event in lines:
            if event == "fixture: entry_started":
                stage = "entry_started"
            elif event == "fixture: bind_verified":
                stage = "bind_verified"
            if event.startswith("poll-supervisor: "):
                value = event[len("poll-supervisor: "):]
                if value == "helper_started":
                    stage = "helper_started"
                for prefix in known:
                    if value.startswith(prefix):
                        stage = prefix.removesuffix("=")
                        break
                else:
                    continue
                break
        if stage:
            code += "_event_" + stage
    except OSError:
        pass
print(code, exit_code)
PY
  ) || diagnostic='probe_unavailable'
  read -r readiness_diagnostic readiness_exit_status <<< "$diagnostic"
  printf 'test-local: synthetic supervisor child did not start\n' >&2
  exit 1
fi
phase=supervisor_stop; reason=supervisor_stop_failed_or_unverified
stop_state before "$(dock container inspect --format '{{json .State}}' "$smoke_container_id")"
dock exec "$smoke_container_id" /bin/bash -c '
  for ((i=0; i<100; i++)); do
    if [[ -f /scratch/fake-reaped ]]; then touch /scratch/fake-observed; exit 0; fi
    sleep 0.1
  done
  exit 1' >/dev/null & observer=$!
dock stop --time 15 "$smoke_container_id" >/dev/null & stopper=$!
observer_ok=1
wait "$observer" || observer_ok=0
wait "$stopper"
[[ "$observer_ok" == 1 ]] || { printf 'test-local: synthetic child was not reaped in time\n' >&2; exit 1; }
smoke_state=$(dock container inspect --format '{{json .State}}' "$smoke_container_id")
stop_state after "$smoke_state"
/usr/bin/python3 -I -c 'import json,sys; raise SystemExit(json.loads(sys.argv[1])["ExitCode"] != 0)' "$smoke_state"
printf 'supervisor_fake_child_stop=true\n'
