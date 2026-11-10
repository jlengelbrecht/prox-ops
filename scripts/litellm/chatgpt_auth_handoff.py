#!/usr/bin/env python3
"""Offline-reviewed, explicit ChatGPT credential generation handoff.

Only --live permits kubectl. Child output and exceptions are never printed.
"""

import argparse
import base64
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import time

ROOT = "/var/lib/litellm/chatgpt"
GEN = "chatgpt-gen-20261110-01"
IMAGE = "ghcr.io/berriai/litellm-database:v1.108.0@sha256:80c91fd00cc039e8880474f539544b919f14cd258a22f22ce4218b864c4ff14a"
IMAGE_DIGEST = IMAGE.split("@sha256:")[1]
BASE = ["kubectl", "--context", "agent-codex-admin", "-n", "ai"]
MAX = 65536
FIELDS = {"access_token", "refresh_token", "id_token", "account_id", "expires_at"}


def cancelled(_signum, _frame):
    raise KeyboardInterrupt


@contextmanager
def spawn_guard():
    """Defer termination until the owned child handle has been assigned."""
    old = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


def reap(proc):
    if proc is None:
        return
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=2)
    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()
    proc.stdout.close()


def valid_record(data):
    if not isinstance(data, dict) or set(data) != FIELDS:
        return False
    if any(not isinstance(data.get(k), str) or not data[k] or len(data[k]) > MAX
           for k in FIELDS - {"expires_at"}):
        return False
    expiry = data["expires_at"]
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return False
    if not math.isfinite(expiry) or expiry <= time.time() + 300:
        return False
    try:
        parts = data["id_token"].split(".")
        if len(parts) != 3 or not all(parts):
            return False
        payload = parts[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return (isinstance(claims, dict) and
                isinstance(claims.get("chatgpt_account_id"), str) and
                bool(claims["chatgpt_account_id"]) and
                claims["chatgpt_account_id"] == data["account_id"])
    except (IndexError, ValueError, TypeError, UnicodeError):
        return False


def parse_record(raw):
    if not raw or len(raw) > MAX:
        raise ValueError("SCHEMA_INVALID")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("SCHEMA_INVALID") from None
    if not valid_record(data):
        raise ValueError("SCHEMA_INVALID")
    return data


def source_bytes(path):
    """Read a private, task-owned regular file on tmpfs without following links."""
    p = Path(path).absolute()
    if p == Path("/") or any(stat.S_ISLNK(os.lstat(a).st_mode) for a in [p, *p.parents]):
        raise ValueError("SOURCE_UNSAFE")
    parent = os.lstat(p.parent)
    if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError("SOURCE_UNSAFE")
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        mount = before.split()[4].replace("\\040", " ")
        if str(p) == mount or str(p).startswith(mount.rstrip("/") + "/"):
            mounts.append((len(mount), after.split()[0]))
    if not mounts or max(mounts)[1] != "tmpfs":
        raise ValueError("SOURCE_UNSAFE")
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) != 0o600 or s.st_size > MAX:
            raise ValueError("SOURCE_UNSAFE")
        raw = os.read(fd, MAX + 1)
        parse_record(raw)
        return raw
    finally:
        os.close(fd)


def remote_program(stage):
    # The remote interpreter imports only the standard library. No arbitrary path or
    # command comes from the CLI; stdout is a single fixed protocol marker.
    body = '''
import base64,ctypes,json,math,os,stat,sys,time
R="/var/lib/litellm/chatgpt"; G="chatgpt-gen-20261110-01"; M=65536
def good(b):
    if not b or len(b)>M: return False
    try:
        d=json.loads(b)
        if set(d)!={"access_token","refresh_token","id_token","account_id","expires_at"}: return False
        if any(not isinstance(d[k],str) or not d[k] or len(d[k])>M for k in ("access_token","refresh_token","id_token","account_id")): return False
        e=d["expires_at"]
        if isinstance(e,bool) or not isinstance(e,(int,float)) or not math.isfinite(e) or e<=time.time()+300: return False
        parts=d["id_token"].split(".")
        if len(parts)!=3 or not all(parts): return False
        p=parts[1]; c=json.loads(base64.urlsafe_b64decode(p+"="*(-len(p)%4)))
        return isinstance(c,dict) and isinstance(c.get("chatgpt_account_id"),str) and bool(c["chatgpt_account_id"]) and c["chatgpt_account_id"]==d["account_id"]
    except Exception: return False
def root():
    path=""
    for part in R.split("/")[1:]:
        path+="/"+part
        if stat.S_ISLNK(os.lstat(path).st_mode): raise ValueError()
    s=os.lstat(R)
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)&0o022: raise ValueError()
    f=os.open(R,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    if os.fstat(f).st_ino!=s.st_ino: os.close(f); raise ValueError()
    return f
def present(f,name,kind):
    s=os.stat(name,dir_fd=f,follow_symlinks=False)
    if s.st_uid!=os.geteuid() or stat.S_IMODE(s.st_mode)!=(0o600 if kind=="file" else 0o700): raise ValueError()
    if not (stat.S_ISREG(s.st_mode) if kind=="file" else stat.S_ISDIR(s.st_mode)): raise ValueError()
try:
    f=root()
    try:
        present(f,"auth.json","file"); present(f,"xai","dir")
        T="."+G+".staging"
        try: os.stat(G,dir_fd=f,follow_symlinks=False); raise FileExistsError()
        except FileNotFoundError: pass
        try: os.stat(T,dir_fd=f,follow_symlinks=False); raise FileExistsError()
        except FileNotFoundError: pass
        if not os.access(R,os.W_OK|os.X_OK): raise ValueError()
        if STAGE:
            print("READY",flush=True)
            b=sys.stdin.buffer.read(M+1)
            if not good(b): raise ValueError()
            mask=os.umask(0o077)
            try: os.mkdir(T,0o700,dir_fd=f)
            finally: os.umask(mask)
            g=os.open(T,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=f)
            try:
                os.fchmod(g,0o700)
                h=os.open("auth.json",os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=g)
                try:
                    os.fchmod(h,0o600)
                    with os.fdopen(h,"wb",closefd=False) as w: w.write(b); w.flush(); os.fsync(h)
                finally: os.close(h)
                h=os.open("auth.json",os.O_RDONLY|os.O_NOFOLLOW,dir_fd=g)
                try:
                    present(g,"auth.json","file")
                    if os.read(h,M+1)!=b or not good(b): raise ValueError()
                finally: os.close(h)
                os.fsync(g)
            finally: os.close(g)
            os.fsync(f)
            libc=ctypes.CDLL(None,use_errno=True)
            if not hasattr(libc,"renameat2"): raise ValueError()
            if libc.renameat2(f,T.encode(),f,G.encode(),1)!=0:
                e=ctypes.get_errno()
                if e==17: raise FileExistsError()
                raise OSError(e,"")
            # A lost receipt or failed parent sync after publication is uncertain.
            published=True
            os.fsync(f)
            print("STAGED",flush=True)
        else: print("SAFE",flush=True)
    finally: os.close(f)
except FileExistsError:
    print("GENERATION_EXISTS",flush=True)
except Exception:
    print("DESTINATION_UNSAFE" if not STAGE else ("STAGE_UNCERTAIN" if globals().get("published") else "STAGE_FAILED"),flush=True)
'''
    return "STAGE=" + ("True" if stage else "False") + "\n" + body


def exchange(proc, payload, timeout, limit):
    """Bounded nonblocking pipe exchange; caller always reaps the child."""
    deadline = time.monotonic() + timeout
    output = bytearray()
    sent = 0
    out = proc.stdout.fileno()
    os.set_blocking(out, False)
    inp = proc.stdin.fileno() if proc.stdin is not None else None
    if inp is not None:
        os.set_blocking(inp, False)
    while out is not None or inp is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        ready_r, ready_w, _ = select.select([out] if out is not None else [],
                                            [inp] if inp is not None else [], [], remaining)
        if out in ready_r:
            chunk = os.read(out, min(4096, limit + 1 - len(output)))
            if chunk:
                output.extend(chunk)
                if len(output) > limit:
                    raise ValueError()
            else:
                out = None
        if inp in ready_w:
            if sent == len(payload):
                proc.stdin.close(); inp = None
            else:
                sent += os.write(inp, payload[sent:sent + 4096])
    remaining = deadline - time.monotonic()
    if remaining <= 0 or proc.wait(timeout=remaining) != 0:
        raise ValueError()
    return bytes(output)


def call(args, timeout):
    proc = None
    try:
        with spawn_guard():
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        return json.loads(exchange(proc, b"", timeout, 1024 * 1024))
    except (OSError, subprocess.SubprocessError, ValueError, TimeoutError):
        raise ValueError("PREFLIGHT_FAILED") from None
    finally:
        reap(proc)


def metadata():
    if not __debug__:
        raise ValueError("PREFLIGHT_FAILED")
    dep = call(BASE + ["get", "deployment", "litellm", "-o", "json"], 10)
    pods = call(BASE + ["get", "pods", "-o", "json"], 10)["items"]
    sets = call(BASE + ["get", "replicaset", "-o", "json"], 10)["items"]
    try:
        dmeta = dep["metadata"]
        assert dmeta["name"] == "litellm" and dmeta["namespace"] == "ai"
        assert dep["spec"]["replicas"] == 1 and dep["spec"]["strategy"]["type"] == "Recreate"
        duid = dmeta["uid"]
        owned_sets = {s["metadata"]["uid"] for s in sets if any(
            o.get("uid") == duid and o.get("kind") == "Deployment" and o.get("controller")
            for o in s["metadata"].get("ownerReferences", []))}
        active = [p for p in pods if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        owned = [p for p in active if any(o.get("uid") in owned_sets and o.get("kind") == "ReplicaSet" and o.get("controller")
                 for o in p["metadata"].get("ownerReferences", []))]
        assert len(owned) == 1
        p = owned[0]; spec = p["spec"]
        assert p["status"]["phase"] == "Running" and all(x.get("status") == "True" for x in p["status"].get("conditions", []) if x["type"] == "Ready")
        assert any(x["type"] == "Ready" and x["status"] == "True" for x in p["status"]["conditions"])
        assert len(spec["containers"]) == 1 and len(p["status"]["containerStatuses"]) == 1
        c = spec["containers"][0]; cs = p["status"]["containerStatuses"][0]
        assert c["name"] == cs["name"] == "litellm" and cs["ready"] is True
        assert c["image"] == IMAGE and cs["imageID"].endswith("sha256:" + IMAGE_DIGEST)
        volumes = {v["name"] for v in spec["volumes"] if v.get("persistentVolumeClaim", {}).get("claimName") == "litellm-data"}
        assert len(volumes) == 1
        assert any(m["name"] in volumes and m["mountPath"] == ROOT and not m.get("readOnly", False) for m in c["volumeMounts"])
        env = {e["name"]: e.get("value") for e in c["env"]}
        assert env["CHATGPT_TOKEN_DIR"] == ROOT and env["XAI_OAUTH_TOKEN_DIR"] == ROOT + "/xai" and "CHATGPT_AUTH_FILE" not in env
        for other in active:
            claims = {v["name"] for v in other["spec"].get("volumes", []) if v.get("persistentVolumeClaim", {}).get("claimName") == "litellm-data"}
            writers = (other["spec"].get("containers", []) + other["spec"].get("initContainers", [])
                       + other["spec"].get("ephemeralContainers", []))
            assert not any(m["name"] in claims and not m.get("readOnly", False)
                           for oc in writers if not (other["metadata"]["uid"] == p["metadata"]["uid"] and oc.get("name") == "litellm" and oc in other["spec"].get("containers", []))
                           for m in oc.get("volumeMounts", []))
        return p["metadata"]["name"], p["metadata"]["uid"], c["name"]
    except (KeyError, TypeError, AssertionError):
        raise ValueError("PREFLIGHT_FAILED") from None


def uid_still(pod, uid):
    p = call(BASE + ["get", "pod", pod, "-o", "json"], 10)
    if p.get("metadata", {}).get("uid") != uid:
        raise ValueError("POD_IDENTITY_CHANGED")


def marker(proc, timeout):
    deadline = time.monotonic() + timeout
    line = bytearray()
    while len(line) < 32 and not line.endswith(b"\n"):
        ready, _, _ = select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))
        if not ready:
            raise ValueError("STAGE_FAILED")
        part = os.read(proc.stdout.fileno(), 1)
        if not part:
            break
        line.extend(part)
    if bytes(line) not in (b"READY\n", b"SAFE\n", b"STAGED\n", b"GENERATION_EXISTS\n", b"DESTINATION_UNSAFE\n", b"STAGE_FAILED\n"):
        raise ValueError("STAGE_FAILED")
    return line.strip().decode()


def remote(pod, container, stage, uid, raw, timeout):
    args = BASE + ["exec"] + (["-i"] if stage else []) + [pod, "-c", container, "--", "python", "-u", "-c", remote_program(stage)]
    proc = None
    try:
        with spawn_guard():
            proc = subprocess.Popen(args, stdin=subprocess.PIPE if stage else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        first = marker(proc, timeout)
        if stage:
            if first != "READY": raise ValueError(first if first == "GENERATION_EXISTS" else "STAGE_UNCERTAIN")
            uid_still(pod, uid)
            output = exchange(proc, raw, timeout, 32)
            if output != b"STAGED\n" or proc.returncode != 0:
                raise ValueError("STAGE_UNCERTAIN")
        elif first != "SAFE":
            raise ValueError(first if first in ("GENERATION_EXISTS", "DESTINATION_UNSAFE") else "PREFLIGHT_FAILED")
        elif exchange(proc, b"", timeout, 1):
            raise ValueError("PREFLIGHT_FAILED")
        return "STAGED" if stage else "SAFE"
    except (OSError, subprocess.SubprocessError, BrokenPipeError, TimeoutError):
        raise ValueError("STAGE_UNCERTAIN" if stage else "PREFLIGHT_FAILED") from None
    except ValueError as exc:
        if stage and str(exc) != "GENERATION_EXISTS":
            raise ValueError("STAGE_UNCERTAIN") from None
        raise
    finally:
        reap(proc)


def main():
    parser = argparse.ArgumentParser(description="Bounded ChatGPT generation handoff")
    parser.add_argument("command", choices=("preflight", "stage"))
    parser.add_argument("--live", action="store_true", help="explicitly permit fixed kubectl calls")
    parser.add_argument("--source", help="private flat record on task-owned tmpfs; stage only")
    parser.add_argument("--timeout", type=int, default=30, help="per exec phase, 10..300 seconds")
    a = parser.parse_args()
    if not a.live or not 10 <= a.timeout <= 300 or (a.command == "stage") != bool(a.source):
        print("PREFLIGHT_FAILED"); return 1
    try:
        raw = source_bytes(a.source) if a.command == "stage" else None
        pod, uid, container = metadata()
        remote(pod, container, False, uid, None, a.timeout)
        uid_still(pod, uid)
        if a.command == "stage":
            receipt = remote(pod, container, True, uid, raw, a.timeout)
            if receipt != "STAGED":
                raise ValueError("STAGE_UNCERTAIN")
            try:
                if metadata() != (pod, uid, container):
                    raise ValueError()
            except ValueError:
                raise ValueError("STAGE_UNCERTAIN") from None
            print("STAGED")
        else: print("CUTOVER_PENDING")
        return 0
    except KeyboardInterrupt:
        print("STAGE_UNCERTAIN" if a.command == "stage" else "PREFLIGHT_FAILED")
        return 1
    except (ValueError, OSError) as exc:
        status = str(exc)
        if status == "SOURCE_UNSAFE":
            status = "PREFLIGHT_FAILED"
        if status not in {"SCHEMA_INVALID", "PREFLIGHT_FAILED", "POD_IDENTITY_CHANGED", "DESTINATION_UNSAFE", "GENERATION_EXISTS", "STAGE_FAILED", "STAGE_UNCERTAIN"}:
            status = "STAGE_UNCERTAIN" if a.command == "stage" else "PREFLIGHT_FAILED"
        print(status)
        return 1
    except Exception:
        print("STAGE_UNCERTAIN" if a.command == "stage" else "PREFLIGHT_FAILED")
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGHUP, cancelled)
    sys.exit(main())
