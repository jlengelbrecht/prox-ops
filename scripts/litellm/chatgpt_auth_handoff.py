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
import re
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
    account_id = data["account_id"]
    if len(account_id) > 256 or not account_id.isascii() or any(c.isspace() for c in account_id):
        return False
    expiry = data["expires_at"]
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
        return False
    if not math.isfinite(expiry) or expiry <= time.time() + 300:
        return False
    try:
        accounts = []
        for field in ("access_token", "id_token"):
            parts = data[field].split(".")
            if field == "access_token" and len(parts) == 1:
                continue
            if len(parts) != 3 or any(not p or len(p) > 32768 or re.fullmatch(r"[A-Za-z0-9_-]+", p) is None for p in parts):
                return False
            payload = parts[1]
            if len(payload) > 32768:
                return False
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            if len(raw) > 16384:
                return False
            claims = json.loads(raw)
            if not isinstance(claims, dict):
                return False
            nested = claims.get("https://api.openai.com/auth")
            if "https://api.openai.com/auth" in claims and not isinstance(nested, dict):
                return False
            if nested is not None and "chatgpt_account_id" in nested:
                accounts.append(nested["chatgpt_account_id"])
            if field == "id_token" and "chatgpt_account_id" in claims:
                accounts.append(claims["chatgpt_account_id"])
        return bool(accounts) and all(
            isinstance(account, str) and account == data["account_id"]
            for account in accounts)
    except (ValueError, TypeError, UnicodeError, base64.binascii.Error):
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
    p = Path(path)
    if ".." in p.parts:
        raise ValueError("SOURCE_UNSAFE")
    p = p.absolute()
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
import base64,ctypes,json,math,os,re,stat,sys,time
R="/var/lib/litellm/chatgpt"; G="chatgpt-gen-20261110-01"; M=65536
def good(b):
    if not b or len(b)>M: return False
    try:
        d=json.loads(b)
        if set(d)!={"access_token","refresh_token","id_token","account_id","expires_at"}: return False
        if any(not isinstance(d[k],str) or not d[k] or len(d[k])>M for k in ("access_token","refresh_token","id_token","account_id")): return False
        a=d["account_id"]
        if len(a)>256 or not a.isascii() or any(ch.isspace() for ch in a): return False
        e=d["expires_at"]
        if isinstance(e,bool) or not isinstance(e,(int,float)) or not math.isfinite(e) or e<=time.time()+300: return False
        accounts=[]
        for field in ("access_token","id_token"):
            parts=d[field].split(".")
            if field=="access_token" and len(parts)==1: continue
            if len(parts)!=3 or any(not p or len(p)>32768 or re.fullmatch(r"[A-Za-z0-9_-]+",p) is None for p in parts): return False
            p=parts[1]
            if len(p)>32768: return False
            raw=base64.urlsafe_b64decode(p+"="*(-len(p)%4))
            if len(raw)>16384: return False
            c=json.loads(raw)
            if not isinstance(c,dict): return False
            nested=c.get("https://api.openai.com/auth")
            if "https://api.openai.com/auth" in c and not isinstance(nested,dict): return False
            if nested is not None and "chatgpt_account_id" in nested: accounts.append(nested["chatgpt_account_id"])
            if field=="id_token" and "chatgpt_account_id" in c: accounts.append(c["chatgpt_account_id"])
        return bool(accounts) and all(isinstance(a,str) and a==d["account_id"] for a in accounts)
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


def repair_program():
    """Fixed, content-blind chmod program for the one approved runtime artifact."""
    return '''
import os,stat,sys
R="/var/lib/litellm/chatgpt"; G="chatgpt-gen-20261110-01"
changed=False
fds=[]
def opened(parent,name):
    before=os.stat(name,dir_fd=parent,follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode): raise ValueError()
    fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
    fds.append(fd)
    after=os.fstat(fd)
    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino): raise ValueError()
    return fd
def same(parent,name,fd):
    a=os.stat(name,dir_fd=parent,follow_symlinks=False)
    b=os.fstat(fd)
    if (a.st_dev,a.st_ino,a.st_mode&0o170000)!=(b.st_dev,b.st_ino,b.st_mode&0o170000): raise ValueError()
    return b
try:
    if os.geteuid()!=0 or os.getegid()!=0: raise ValueError()
    parent=os.open("/",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW); fds.append(parent)
    for part in ("var","lib","litellm"):
        parent=opened(parent,part)
        ancestor=os.fstat(parent)
        if ancestor.st_uid!=0 or stat.S_IMODE(ancestor.st_mode)&0o022: raise ValueError()
    root=opened(parent,"chatgpt")
    rs=same(parent,"chatgpt",root)
    if rs.st_uid!=0 or rs.st_gid!=65534 or stat.S_IMODE(rs.st_mode) not in (0o2775,0o700): raise ValueError()
    before=os.stat("auth.json",dir_fd=root,follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1: raise ValueError()
    auth=os.open("auth.json",os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root); fds.append(auth)
    a=same(root,"auth.json",auth)
    if a.st_uid!=0 or a.st_gid!=65534 or a.st_nlink!=1 or stat.S_IMODE(a.st_mode) not in (0o644,0o600): raise ValueError()
    xai=opened(root,"xai")
    x=os.fstat(xai)
    if x.st_uid!=0 or x.st_gid!=65534 or stat.S_IMODE(x.st_mode)!=0o700: raise ValueError()
    for name in (G,"."+G+".staging"):
        try: os.stat(name,dir_fd=root,follow_symlinks=False); raise ValueError()
        except FileNotFoundError: pass
    # Resolve every target through its pinned parent immediately before changing it.
    same(parent,"chatgpt",root); same(root,"auth.json",auth); same(root,"xai",xai)
    print("READY",flush=True)
    if sys.stdin.buffer.read(1)!=b"1": raise ValueError()
    rs=same(parent,"chatgpt",root); a=same(root,"auth.json",auth); x=same(root,"xai",xai)
    if rs.st_uid!=0 or rs.st_gid!=65534 or stat.S_IMODE(rs.st_mode) not in (0o2775,0o700): raise ValueError()
    if a.st_uid!=0 or a.st_gid!=65534 or a.st_nlink!=1 or stat.S_IMODE(a.st_mode) not in (0o644,0o600): raise ValueError()
    if x.st_uid!=0 or x.st_gid!=65534 or stat.S_IMODE(x.st_mode)!=0o700: raise ValueError()
    for name in (G,"."+G+".staging"):
        try: os.stat(name,dir_fd=root,follow_symlinks=False); raise ValueError()
        except FileNotFoundError: pass
    if stat.S_IMODE(rs.st_mode)!=0o700:
        changed=True
        os.fchmod(root,0o700)
    a=same(root,"auth.json",auth)
    if a.st_uid!=0 or a.st_gid!=65534 or a.st_nlink!=1 or stat.S_IMODE(a.st_mode) not in (0o644,0o600): raise ValueError()
    if stat.S_IMODE(a.st_mode)!=0o600:
        changed=True
        os.fchmod(auth,0o600)
    rs=same(parent,"chatgpt",root); a=same(root,"auth.json",auth); x=same(root,"xai",xai)
    if (rs.st_uid,rs.st_gid,stat.S_IMODE(rs.st_mode))!=(0,65534,0o700): raise ValueError()
    if (a.st_uid,a.st_gid,a.st_nlink,stat.S_IMODE(a.st_mode))!=(0,65534,1,0o600): raise ValueError()
    if (x.st_uid,x.st_gid,stat.S_IMODE(x.st_mode))!=(0,65534,0o700): raise ValueError()
    print("REPAIRED",flush=True)
except Exception:
    print("REPAIR_UNCERTAIN" if changed else "REPAIR_REFUSED",flush=True)
finally:
    for fd in reversed(fds):
        try: os.close(fd)
        except OSError: pass
'''


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


def metadata(timeout=30):
    if not __debug__:
        raise ValueError("PREFLIGHT_FAILED")
    deadline = time.monotonic() + timeout
    def read(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("PREFLIGHT_FAILED")
        return call(BASE + args, remaining)
    dep = read(["get", "deployment", "litellm", "-o", "json"])
    pods = read(["get", "pods", "-o", "json"])["items"]
    try:
        dmeta = dep["metadata"]
        assert dmeta["name"] == "litellm" and dmeta["namespace"] == "ai"
        assert dep["spec"]["replicas"] == 1 and dep["spec"]["strategy"]["type"] == "Recreate"
        duid = dmeta["uid"]
        active = [p for p in pods if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
        # Resolve only ReplicaSets named by active pods. Historical namespace-wide
        # lists can exceed the fixed capture cap; pod enumeration stays unfiltered
        # so unrelated PVC writers remain visible below.
        sets = {}
        owned = []
        for candidate in active:
            for owner in candidate["metadata"].get("ownerReferences", []):
                if owner.get("kind") != "ReplicaSet" or owner.get("controller") is not True:
                    continue
                name = owner.get("name")
                if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
                    raise ValueError("PREFLIGHT_FAILED")
                if name not in sets:
                    sets[name] = read(["get", "replicaset", name, "-o", "json"])
                smeta = sets[name]["metadata"]
                assert smeta["name"] == name and smeta["namespace"] == "ai"
                target_set = any(
                    o.get("uid") == duid and o.get("kind") == "Deployment" and o.get("controller") is True
                    for o in smeta.get("ownerReferences", []))
                if target_set and smeta["uid"] != owner.get("uid"):
                    raise ValueError("PREFLIGHT_FAILED")
                if target_set:
                    owned.append(candidate)
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


def uid_still(pod, uid, timeout=30):
    p = call(BASE + ["get", "pod", pod, "-o", "json"], timeout)
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
    if bytes(line) not in (b"READY\n", b"SAFE\n", b"STAGED\n", b"GENERATION_EXISTS\n", b"DESTINATION_UNSAFE\n", b"STAGE_FAILED\n", b"REPAIRED\n", b"REPAIR_REFUSED\n", b"REPAIR_UNCERTAIN\n"):
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
            uid_still(pod, uid, timeout)
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


def remote_repair(pod, container, uid, timeout):
    args = BASE + ["exec", "-i", pod, "-c", container, "--", "python", "-u", "-c", repair_program()]
    proc = None
    try:
        with spawn_guard():
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        first = marker(proc, timeout)
        if first in ("REPAIR_REFUSED", "REPAIR_UNCERTAIN"):
            if exchange(proc, b"", timeout, 1) or proc.returncode != 0:
                raise ValueError("REPAIR_UNCERTAIN")
            raise ValueError(first)
        if first != "READY":
            raise ValueError("REPAIR_UNCERTAIN")
        try:
            if metadata(timeout) != (pod, uid, container):
                raise ValueError("PREFLIGHT_FAILED")
            uid_still(pod, uid, timeout)
        except ValueError:
            raise ValueError("REPAIR_REFUSED") from None
        output = exchange(proc, b"1", timeout, 32)
        if output == b"REPAIRED\n" and proc.returncode == 0:
            return "REPAIRED"
        if output == b"REPAIR_REFUSED\n" and proc.returncode == 0:
            raise ValueError("REPAIR_REFUSED")
        raise ValueError("REPAIR_UNCERTAIN")
    except (OSError, subprocess.SubprocessError, TimeoutError, KeyboardInterrupt):
        raise ValueError("REPAIR_UNCERTAIN") from None
    except ValueError as exc:
        if str(exc) == "REPAIR_REFUSED":
            raise
        raise ValueError("REPAIR_UNCERTAIN") from None
    finally:
        reap(proc)


def main():
    parser = argparse.ArgumentParser(description="Bounded ChatGPT generation handoff")
    parser.add_argument("command", choices=("preflight", "stage", "harden"))
    parser.add_argument("--live", action="store_true", help="explicitly permit fixed kubectl calls")
    parser.add_argument("--source", help="private flat record on task-owned tmpfs; stage only")
    parser.add_argument("--timeout", type=int, default=30, help="per exec phase, 10..300 seconds")
    a = parser.parse_args()
    if not a.live or not 10 <= a.timeout <= 300 or (a.command == "stage") != bool(a.source):
        print("PREFLIGHT_FAILED"); return 1
    try:
        raw = source_bytes(a.source) if a.command == "stage" else None
        pod, uid, container = metadata(a.timeout)
        if a.command == "harden":
            uid_still(pod, uid, a.timeout)
            remote_repair(pod, container, uid, a.timeout)
            try:
                if metadata(a.timeout) != (pod, uid, container):
                    raise ValueError()
                uid_still(pod, uid, a.timeout)
                remote(pod, container, False, uid, None, a.timeout)
            except (ValueError, OSError):
                raise ValueError("REPAIR_UNCERTAIN") from None
            print("REPAIRED")
            return 0
        remote(pod, container, False, uid, None, a.timeout)
        uid_still(pod, uid, a.timeout)
        if a.command == "stage":
            receipt = remote(pod, container, True, uid, raw, a.timeout)
            if receipt != "STAGED":
                raise ValueError("STAGE_UNCERTAIN")
            try:
                if metadata(a.timeout) != (pod, uid, container):
                    raise ValueError()
            except ValueError:
                raise ValueError("STAGE_UNCERTAIN") from None
            print("STAGED")
        else: print("CUTOVER_PENDING")
        return 0
    except KeyboardInterrupt:
        print("STAGE_UNCERTAIN" if a.command == "stage" else "REPAIR_UNCERTAIN" if a.command == "harden" else "PREFLIGHT_FAILED")
        return 1
    except (ValueError, OSError) as exc:
        status = str(exc)
        if status == "SOURCE_UNSAFE":
            status = "PREFLIGHT_FAILED"
        if a.command == "harden":
            status = status if status in {"REPAIR_REFUSED", "REPAIR_UNCERTAIN"} else "REPAIR_REFUSED"
        elif status not in {"SCHEMA_INVALID", "PREFLIGHT_FAILED", "POD_IDENTITY_CHANGED", "DESTINATION_UNSAFE", "GENERATION_EXISTS", "STAGE_FAILED", "STAGE_UNCERTAIN"}:
            status = "STAGE_UNCERTAIN" if a.command == "stage" else "PREFLIGHT_FAILED"
        print(status)
        return 1
    except Exception:
        print("STAGE_UNCERTAIN" if a.command == "stage" else "REPAIR_UNCERTAIN" if a.command == "harden" else "PREFLIGHT_FAILED")
        return 1


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGHUP, cancelled)
    sys.exit(main())
