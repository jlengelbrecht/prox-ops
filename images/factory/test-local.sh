#!/usr/bin/env bash
# Disposable, credential-free build and Orca startup fixture.
set -euo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
source_repo=''
archive=''
while (($#)); do
  case "$1" in
    --afk-repo) source_repo="${2:?}"; shift 2 ;;
    --archive) archive="${2:?}"; shift 2 ;;
    *) printf 'usage: %s --afk-repo PATH [--archive VERIFIED_ARCHIVE]\n' "$0" >&2; exit 2 ;;
  esac
done
[[ -n "$source_repo" && -e "$source_repo/.git" ]] || { printf 'test-local: --afk-repo must name a Git checkout\n' >&2; exit 2; }
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
  python3 -c 'import sys
entries = [e for e in sys.stdin.buffer.read().split(b"\0") if e]
assert entries
for entry in entries:
    meta, path = entry.split(b"\t", 1)
    mode, kind, _ = meta.split(b" ")
    assert mode in (b"100644", b"100755") and kind == b"blob"
    assert path == b"adapters/runtime-scope.sh" or path.startswith((b"bin/", b"lib/"))' || {
  printf 'test-local: invalid committed source payload\n' >&2; exit 1;
}
# mktemp owns this mode-0700 context for one run. Never inspect or modify the
# repository's old ignored inputs cache, even if it contains links.
stage=$(mktemp -d /tmp/factory-fixture.XXXXXXXX)
trap 'rm -rf -- "$stage"' EXIT
mkdir -m 0755 -- "$stage/inputs"
install -d -m 0755 "$stage/inputs/afk"
for file in Dockerfile checksums.sha256 safe-extract.py poll-supervisor.sh; do
  [[ -f "$here/$file" && ! -L "$here/$file" ]] || { printf 'test-local: invalid image source file: %s\n' "$file" >&2; exit 1; }
  cp -- "$here/$file" "$stage/$file"
done
source_git archive --format=tar "$commit" bin lib adapters/runtime-scope.sh | tar --same-permissions --no-same-owner -xf - -C "$stage/inputs/afk"
# Docker COPY leaves the source root-owned. Require every exported directory
# to be traversable and each payload file readable by runtime UID 10001.
python3 - "$stage/inputs/afk" <<'PY'
import pathlib, stat, sys

root = pathlib.Path(sys.argv[1])
for path in (root, *root.rglob("*")):
    mode = path.stat().st_mode
    if path.is_dir():
        assert mode & stat.S_IROTH and mode & stat.S_IXOTH, path
    elif path.is_file():
        assert mode & stat.S_IROTH, path
    else:
        raise AssertionError(path)
cli = root / "bin/agent-flow-kit"
assert cli.is_file() and cli.stat().st_mode & stat.S_IXOTH, cli
PY
# Compare every exported byte and mode with the exact committed blobs. This
# also catches archive attributes that omit or substitute a selected file.
source_git ls-tree -rz "$commit" -- bin lib adapters/runtime-scope.sh | \
  python3 -c 'import hashlib, os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1]); repo = sys.argv[2]
entries = [e for e in sys.stdin.buffer.read().split(b"\0") if e]
expected = set()
env = dict(PATH="/usr/bin:/bin", HOME="/nonexistent", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_OPTIONAL_LOCKS="0")
for entry in entries:
    meta, path = entry.split(b"\t", 1)
    mode, kind, oid = meta.split(b" ")
    name = path.decode("utf-8", "strict"); expected.add(name)
    exported = root / name
    assert exported.is_file() and not exported.is_symlink()
    assert bool(exported.stat().st_mode & 0o111) == (mode == b"100755")
    original = subprocess.check_output(["/usr/bin/git", "-C", repo, "cat-file", "blob", oid.decode("ascii")], env=env)
    assert exported.read_bytes() == original
actual = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
assert actual == expected' "$stage/inputs/afk" "$source_repo" || {
  printf 'test-local: exported source differs from pinned tree\n' >&2; exit 1;
}
printf 'source_commit=%s source_tree=%s\n' "$commit" "$tree"
if [[ -n "$archive" ]]; then
  [[ -f "$archive" && ! -L "$archive" ]] || { printf 'test-local: archive must be a regular file\n' >&2; exit 1; }
  cp -- "$archive" "$stage/inputs/$asset.part"
else
  if ! env -i PATH="$PATH" curl --fail --location --silent --show-error --max-time 180 --output "$stage/inputs/$asset.part" "$url"; then
    printf 'test-local: official archive unavailable; provide --archive PATH or permit the official public download\n' >&2
    exit 1
  fi
fi
[[ "$(sha256sum "$stage/inputs/$asset.part" | cut -d' ' -f1)" == "$(cut -d' ' -f1 "$stage/checksums.sha256")" ]] || {
  printf 'test-local: Orca archive checksum mismatch\n' >&2; exit 1;
}
mv -- "$stage/inputs/$asset.part" "$stage/inputs/$asset"
(cd "$stage" && sha256sum -c checksums.sha256)
scratch=$(mktemp -d)
client_cfg=$(mktemp -d)
image_tag="factory-fixture:$(date +%s)-$$"
container_name="factory-fixture-$(date +%s)-$$"
container_id=''
image_built=0
cleanup() {
  local status=$? owner
  # A failed detached run can create the container before returning its ID.
  # Name alone is insufficient ownership proof: require this run's label.
  owner=$(dock container inspect --format '{{ index .Config.Labels "factory.fixture.owner" }}' "$container_name" 2>/dev/null) || owner=''
  if [[ "$owner" == "$container_name" ]]; then
    if ! dock rm -f "$container_name" >/dev/null 2>&1; then
      printf 'test-local: failed to remove owned container\n' >&2; status=1
    fi
  elif [[ -n "$container_id" ]]; then
    printf 'test-local: reserved container ownership could not be verified\n' >&2; status=1
  fi
  owner=$(dock image inspect --format '{{ index .Config.Labels "factory.fixture.owner" }}' "$image_tag" 2>/dev/null) || owner=''
  if [[ "$owner" == "$container_name" ]]; then
    if ! dock image rm "$image_tag" >/dev/null 2>&1; then
      printf 'test-local: failed to remove owned image\n' >&2; status=1
    fi
  elif [[ "$image_built" == 1 ]]; then
    printf 'test-local: built image ownership could not be verified\n' >&2; status=1
  fi
  rm -rf -- "$scratch" "$client_cfg" "$stage"
  return "$status"
}
dock() {
  env -i PATH="$PATH" HOME="$client_cfg" XDG_CONFIG_HOME="$client_cfg" DOCKER_CONFIG="$client_cfg" DOCKER_HOST=unix:///run/docker.sock \
    docker --host unix:///run/docker.sock --config "$client_cfg" "$@"
}
trap cleanup EXIT
python3 "$stage/safe-extract.py" "$stage/inputs/$asset" "$scratch/orca-server"
test -x "$scratch/orca-server/bin/orca-ide"
printf 'orca_archive_sha256=%s archive_layout=bin/orca-ide\n' "$(sha256sum "$stage/inputs/$asset" | cut -d' ' -f1)"
if [[ ! -S /run/docker.sock ]]; then
  printf 'test-local: explicit local Docker socket unavailable\n' >&2; exit 1
fi
dock version --format '{{.Server.Arch}}' | grep -qx amd64
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
dock run --rm "${run_flags[@]}" "$image_tag" /opt/afk/bin/agent-flow-kit --help >/dev/null
dock run --rm "${run_flags[@]}" "$image_tag" /opt/orca-server/bin/orca-ide --version
dock run --rm "${run_flags[@]}" "$image_tag" /bin/bash -c 'test "$(id -u):$(id -g):$HOME" = "10001:10001:/home/factory" && touch "$HOME/w" /cache/w /scratch/w && test ! -e /run/docker.sock'
container_id=$(dock run -d --name "$container_name" --hostname "$container_name" \
  --env "ORCA_INSTANCE_ID=$container_name" \
  --label "factory.fixture.owner=$container_name" "${run_flags[@]}" "$image_tag" \
  /opt/orca-server/bin/orca-ide serve --bind 127.0.0.1 --port 8765 --refuse-public 2>/dev/null)
ready=0
for _ in $(seq 1 40); do
  if dock exec "$container_id" python3 -c 'import socket; s=socket.socket(); s.settimeout(0.2); result=s.connect_ex(("127.0.0.1",8765)); s.close(); raise SystemExit(result != 0)' >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 1
done
[[ "$ready" == 1 ]] || { printf 'test-local: Orca readiness failed\n' >&2; exit 1; }
printf 'orca_private_tcp_ready=true\n'
dock stop --time 15 "$container_id" >/dev/null
printf 'orca_graceful_stop=true\n'
# EXIT trap removes the stopped, labelled container and image exactly once.
