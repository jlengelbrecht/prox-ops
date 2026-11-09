"""GPU-aware airflow for Dell PowerEdge servers.

iDRAC has no temperature sensor for third-party GPUs such as the RTX A5000, so its
automatic fan curve cannot react when a GPU heats up. This controller closes that loop:
it reads each GPU's temperature from Prometheus (DCGM exporter), maps the GPU's
Kubernetes node to the physical server through the node's zone label, and raises that
server's airflow through iDRAC Redfish when the GPU runs hot.

Safety rules:
  * It only ever adds airflow on top of iDRAC's automatic behaviour. A slot is either
    left on Automatic or set to a custom airflow that is at least the slot's automatic
    target. A global fan offset only moves above "Off".
  * It steps down only after several calm readings in a row (hysteresis).
  * Missing or stale GPU data for a node that has a GPU counts as hot (fail-safe).
  * DRY_RUN=1 logs decisions without writing anything.
  * While it runs it owns the airflow of the GPU slots it manages (desired state): a
    manual change is reverted on the next cycle. To pin airflow by hand, scale the
    controller to 0 first.
  * iDRAC certificates are pinned by SHA-256 and redirects are never followed.

Standard library only. Configuration comes from a JSON file (CONFIG_FILE).
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

log = logging.getLogger("gpu-fan-controller")

# ---------------------------------------------------------------- decision logic (pure)

AUTOMATIC = 0  # level 0 means "leave iDRAC on its own automatic behaviour"


@dataclass
class Policy:
    """Temperature thresholds (deg C) for levels 1..n, ascending. Level i is entered at
    up[i-1] and left once the temperature has stayed below up[i-1] - hysteresis for
    `calm_cycles` readings in a row."""
    up: list[float]
    hysteresis: float = 5.0
    calm_cycles: int = 4
    failsafe_level: int = 1

    @property
    def max_level(self) -> int:
        return len(self.up)


@dataclass
class HostState:
    level: int = AUTOMATIC
    calm: int = 0


def raw_level(temp: float, policy: Policy) -> int:
    """The level a temperature asks for, ignoring hysteresis."""
    level = AUTOMATIC
    for i, threshold in enumerate(policy.up, start=1):
        if temp >= threshold:
            level = i
    return level


def next_level(state: HostState, temp: float | None, policy: Policy) -> int:
    """Decide the next level for one server and update `state`.

    `temp` is the hottest fresh GPU temperature on that server, or None when a GPU is
    known to be there but no fresh reading exists (fail-safe)."""
    if temp is None:
        state.calm = 0
        state.level = max(state.level, policy.failsafe_level)
        return state.level
    wanted = raw_level(temp, policy)
    if wanted >= state.level:
        state.level = wanted
        state.calm = 0
        return state.level
    # Hotter levels are entered at once; cooler ones only after a calm streak below the
    # current level's threshold minus the hysteresis band.
    floor = policy.up[state.level - 1] - policy.hysteresis
    if temp < floor:
        state.calm += 1
        if state.calm >= policy.calm_cycles:
            state.level -= 1
            state.calm = 0
    else:
        state.calm = 0
    return state.level


def slot_lfm_for_level(level: int, levels_lfm: list[int], auto_target: int, max_lfm: int) -> int | None:
    """Custom LFM for a slot, or None for Automatic. Never below the slot's own automatic
    target and never above its maximum."""
    if level == AUTOMATIC:
        return None
    want = levels_lfm[min(level, len(levels_lfm)) - 1]
    if max_lfm > 0:
        want = min(want, max_lfm)
    return max(want, auto_target)


OFFSETS = ["Off", "Low", "Medium", "High", "Max"]


def offset_for_level(level: int, levels_offset: list[str]) -> str:
    if level == AUTOMATIC:
        return "Off"
    return levels_offset[min(level, len(levels_offset)) - 1]


# ---------------------------------------------------------------- I/O

class AuthError(Exception):
    """The iDRAC refused the credentials (401/403)."""


class TLSPinError(Exception):
    """The iDRAC presented a certificate other than the pinned one."""


class Redfish:
    """Basic-auth Redfish client over http.client.

    * No Redfish session, so an iDRAC session timeout can never leave it stuck on 401.
    * iDRACs use self-signed certificates, so instead of CA validation each server's
      certificate is pinned by its SHA-256 fingerprint (config "tls_sha256"); the
      credentials are only sent after the fingerprint matches.
    * Redirects are never followed, so the Authorization header cannot be forwarded."""

    def __init__(self, host: str, username: str, password: str, tls_sha256: str, timeout: float = 60):
        self.host = host
        self.auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.pin = tls_sha256.replace(":", "").lower()
        self.timeout = timeout
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE  # replaced by the fingerprint check below

    def _conn(self) -> http.client.HTTPSConnection:
        return http.client.HTTPSConnection(self.host, 443, timeout=self.timeout, context=self.ctx)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        conn = self._conn()
        try:
            conn.connect()
            der = conn.sock.getpeercert(binary_form=True)
            if not self.pin or hashlib.sha256(der).hexdigest() != self.pin:
                raise TLSPinError(self.host)
            data = json.dumps(body).encode() if body is not None else None
            conn.request(method, path, body=data, headers={"Authorization": self.auth, "Accept": "application/json",
                                                           "Content-Type": "application/json"})
            r = conn.getresponse()
            raw = r.read()
            if r.status == 401 or r.status == 403:
                raise AuthError(f"HTTP {r.status}")
            if r.status >= 300:
                raise RuntimeError(f"HTTP {r.status}")
            return json.loads(raw) if raw else {}
        finally:
            conn.close()

    def get(self, path: str) -> dict:
        return self._call("GET", path)

    def patch(self, path: str, body: dict) -> dict:
        return self._call("PATCH", path, body)


ATTRS = "/redfish/v1/Managers/System.Embedded.1/Attributes"


class SlotAirflow:
    """iDRAC9: raise airflow on the PCIe slots that hold NVIDIA GPUs (PCIeSlotLFM.<n>)."""

    kind = "slot-lfm"

    def __init__(self, rf: Redfish, slots: list[int], levels_lfm: list[int]):
        self.rf, self.slots, self.levels_lfm = rf, slots, levels_lfm
        self.auto_target: dict[int, int] = {}

    def read(self) -> dict:
        return self.rf.get(ATTRS).get("Attributes", {})

    def learn_floor(self, attrs: dict, dry_run: bool) -> dict:
        """Before raising a slot, iDRAC's own automatic target must be known: it is the
        floor. A slot found in Custom mode (left by a previous run or set by hand) is put
        back on Automatic once and read again."""
        unknown = [s for s in self.slots if s not in self.auto_target]
        if not unknown:
            return attrs
        reset = {f"PCIeSlotLFM.{s}.LFMMode": "Automatic" for s in unknown
                 if attrs.get(f"PCIeSlotLFM.{s}.LFMMode") != "Automatic"}
        if reset and not dry_run:
            self.rf.patch(ATTRS, {"Attributes": reset})
            time.sleep(5)
            attrs = self.read()
        for s in unknown:
            target = attrs.get(f"PCIeSlotLFM.{s}.TargetLFM")
            if attrs.get(f"PCIeSlotLFM.{s}.LFMMode") == "Automatic" and str(target).isdigit():
                self.auto_target[s] = int(target)
        return attrs

    def apply(self, level: int, dry_run: bool) -> str:
        attrs = self.read()
        if level != AUTOMATIC:
            attrs = self.learn_floor(attrs, dry_run)
        changes: dict[str, object] = {}
        for s in self.slots:
            mode = attrs.get(f"PCIeSlotLFM.{s}.LFMMode")
            target = attrs.get(f"PCIeSlotLFM.{s}.TargetLFM")
            max_lfm = int(attrs.get(f"PCIeSlotLFM.{s}.MaxLFM") or 0)
            if mode == "Automatic" and str(target).isdigit():
                self.auto_target[s] = int(target)  # remember iDRAC's own target as the floor
            if level != AUTOMATIC and s not in self.auto_target:
                continue  # floor still unknown (dry run, or iDRAC did not report it): never guess
            lfm = slot_lfm_for_level(level, self.levels_lfm, self.auto_target.get(s, 0), max_lfm)
            if lfm is None:
                if mode != "Automatic":
                    changes[f"PCIeSlotLFM.{s}.LFMMode"] = "Automatic"
            elif mode != "Custom" or int(attrs.get(f"PCIeSlotLFM.{s}.CustomLFM") or 0) != lfm:
                changes[f"PCIeSlotLFM.{s}.LFMMode"] = "Custom"
                changes[f"PCIeSlotLFM.{s}.CustomLFM"] = lfm
        if changes and not dry_run:
            self.rf.patch(ATTRS, {"Attributes": changes})
        return json.dumps(changes, sort_keys=True) if changes else "unchanged"


class FanOffset:
    """iDRAC9 fallback: the server-wide fan speed offset (ThermalSettings.1.FanSpeedOffset)."""

    kind = "fan-offset"

    def __init__(self, rf: Redfish, levels_offset: list[str]):
        self.rf, self.levels_offset = rf, levels_offset

    def apply(self, level: int, dry_run: bool) -> str:
        want = offset_for_level(level, self.levels_offset)
        have = self.rf.get(ATTRS).get("Attributes", {}).get("ThermalSettings.1.FanSpeedOffset")
        if have == want:
            return "unchanged"
        if not dry_run:
            self.rf.patch(ATTRS, {"Attributes": {"ThermalSettings.1.FanSpeedOffset": want}})
        return json.dumps({"ThermalSettings.1.FanSpeedOffset": want})


def discover_actuator(rf: Redfish, cfg: dict):
    """Pick the actuator for one server: per-slot airflow where iDRAC supports it and the
    server holds NVIDIA GPUs, otherwise the global fan offset."""
    attrs = rf.get(ATTRS).get("Attributes", {})
    slots: list[int] = []
    if attrs.get("ThermalSettings.1.PCIeSlotLFMSupport") == "Supported":
        for m in rf.get("/redfish/v1/Chassis/System.Embedded.1/PCIeDevices").get("Members", []):
            d = rf.get(urllib.parse.urlparse(m["@odata.id"]).path)
            if "NVIDIA" in (d.get("Manufacturer") or "").upper():
                loc = ((d.get("Slot") or {}).get("Location") or {}).get("PartLocation", {})
                if loc.get("LocationType") == "Slot" and loc.get("LocationOrdinalValue"):
                    slots.append(int(loc["LocationOrdinalValue"]))
    if slots:
        return SlotAirflow(rf, sorted(set(slots)), cfg["levels_lfm"])
    if "ThermalSettings.1.FanSpeedOffset" in attrs:
        return FanOffset(rf, cfg["levels_offset"])
    raise RuntimeError("iDRAC exposes neither PCIeSlotLFM nor FanSpeedOffset over Redfish")


class Prometheus:
    def __init__(self, url: str, timeout: float = 20):
        self.url, self.timeout = url.rstrip("/"), timeout

    def query(self, q: str) -> list[dict]:
        u = f"{self.url}/api/v1/query?" + urllib.parse.urlencode({"query": q})
        with urllib.request.urlopen(u, timeout=self.timeout) as r:
            body = json.load(r)
        if body.get("status") != "success":
            raise RuntimeError("prometheus query failed")
        return body["data"]["result"]


class Kube:
    """In-cluster reads of Node objects (zone label and GPU presence)."""

    def __init__(self):
        root = "/var/run/secrets/kubernetes.io/serviceaccount"
        self.token_file = f"{root}/token"
        self.ctx = ssl.create_default_context(cafile=f"{root}/ca.crt")
        self.base = f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.environ.get('KUBERNETES_SERVICE_PORT', '443')}"

    def nodes(self) -> list[dict]:
        # Read the projected token on every request: the kubelet rotates it on disk.
        token = open(self.token_file).read().strip()
        req = urllib.request.Request(self.base + "/api/v1/nodes", headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, context=self.ctx, timeout=20) as r:
            return json.load(r)["items"]


# ---------------------------------------------------------------- metrics

@dataclass
class Metrics:
    gauges: dict[tuple[str, tuple], float] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(self, name: str, value: float, **labels: str) -> None:
        with self.lock:
            self.gauges[(name, tuple(sorted(labels.items())))] = value

    def render(self) -> str:
        with self.lock:
            out = []
            for (name, labels), v in sorted(self.gauges.items()):
                lab = ",".join(f'{k}="{val}"' for k, val in labels)
                out.append(f"{name}{{{lab}}} {v}" if lab else f"{name} {v}")
            return "\n".join(out) + "\n"


METRICS = Metrics()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = METRICS.render().encode() if self.path == "/metrics" else b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


# ---------------------------------------------------------------- main loop

def gpu_nodes_by_host(nodes: list[dict], zone_to_host: dict[str, str]) -> dict[str, list[str]]:
    """Servers -> names of their Kubernetes nodes that have an NVIDIA GPU."""
    out: dict[str, list[str]] = {}
    for n in nodes:
        labels = n["metadata"].get("labels", {})
        if labels.get("nvidia.com/gpu.present") != "true":
            continue
        host = zone_to_host.get(labels.get("topology.kubernetes.io/zone", ""))
        if host:
            out.setdefault(host, []).append(n["metadata"]["name"])
    return out


def _by_gpu(results: list[dict]) -> dict[tuple[str, str], float]:
    """(node, GPU UUID) -> value."""
    out: dict[tuple[str, str], float] = {}
    for r in results:
        m = r["metric"]
        key = (m.get("Hostname") or m.get("node") or "", m.get("UUID") or m.get("gpu") or "")
        out[key] = float(r["value"][1])
    return out


def hottest(temps: list[dict], stamps: list[dict], nodes: list[str], max_age: float, now: float,
            known: dict[str, set[str]]) -> float | None:
    """Hottest GPU temperature across `nodes`, or None (fail-safe) when any GPU lacks a
    fresh sample.

    Freshness is checked per GPU, not per node: every GPU ever seen on a node (`known`,
    updated here) must still have a sample younger than `max_age`, so one GPU going
    silent cannot be hidden by a cool neighbour. `stamps` is the result of
    timestamp(<metric>): an instant query's own value time is the evaluation time,
    not the sample time."""
    t, ts = _by_gpu(temps), _by_gpu(stamps)
    for node, uuid in t:
        if node in nodes:
            known.setdefault(node, set()).add(uuid)
    hot = None
    for node in nodes:
        gpus = known.get(node, set())
        if not gpus:
            return None  # a GPU node with no temperature ever seen
        for uuid in gpus:
            key = (node, uuid)
            if key not in t or key not in ts or now - ts[key] > max_age:
                return None
            hot = t[key] if hot is None else max(hot, t[key])
    return hot


def load_credentials(host: str) -> tuple[str, str]:
    root = os.environ.get("CREDENTIALS_DIR", "/run/secrets/idrac")
    user = open(f"{root}/{host}-username").read().strip()
    password = open(f"{root}/{host}-password").read().strip()
    return user, password


def run(cfg: dict) -> None:
    dry_run = os.environ.get("DRY_RUN", "1") == "1"
    policy = Policy(up=cfg["thresholds_c"], hysteresis=cfg.get("hysteresis_c", 5.0),
                    calm_cycles=cfg.get("calm_cycles", 4), failsafe_level=cfg.get("failsafe_level", 1))
    prom = Prometheus(cfg["prometheus_url"])
    kube = Kube()
    zone_to_host = {h["zone"]: h["name"] for h in cfg["hosts"]}
    hosts = {h["name"]: h for h in cfg["hosts"]}
    actuators: dict[str, object] = {}
    states: dict[str, HostState] = {}
    failures: dict[str, int] = {}
    known_gpus: dict[str, set[str]] = {}
    retry_at: dict[str, float] = {}
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    log.info("starting: dry_run=%s thresholds=%s hosts=%s", dry_run, policy.up, list(hosts))

    while not stop.is_set():
        now = time.time()
        try:
            gpu_hosts = gpu_nodes_by_host(kube.nodes(), zone_to_host)
            METRICS.set("gpu_fan_controller_up", 1)
        except Exception as e:  # without node data we cannot map GPUs; keep the last levels
            log.error("node list failed: %s", type(e).__name__)
            METRICS.set("gpu_fan_controller_up", 0)
            stop.wait(cfg["interval_s"])
            continue
        try:
            results = prom.query(cfg["temperature_query"])
            stamps = prom.query(cfg["timestamp_query"])
        except Exception as e:
            log.error("prometheus query failed: %s (treating GPU servers as hot)", type(e).__name__)
            results, stamps = [], []
        for host, nodes in sorted(gpu_hosts.items()):
            if host not in actuators:
                if now < retry_at.get(host, 0):
                    continue
                try:
                    user, password = load_credentials(host)
                    rf = Redfish(hosts[host]["idrac"], user, password, hosts[host].get("tls_sha256", ""))
                    actuators[host] = discover_actuator(rf, cfg)
                    failures[host] = 0
                    log.info("%s: using %s %s", host, actuators[host].kind,
                             getattr(actuators[host], "slots", ""))
                except Exception as e:
                    # Back off (30 s doubling to 1 h) so wrong credentials cannot trip iDRAC's
                    # login-failure lockout.
                    failures[host] = failures.get(host, 0) + 1
                    retry_at[host] = now + min(3600, cfg["interval_s"] * 2 ** failures[host])
                    log.error("%s: actuator discovery failed: %s (retry in %ds)", host, type(e).__name__,
                              int(retry_at[host] - now))
                    METRICS.set("gpu_fan_controller_errors", 1, host=host)
                    continue
            temp = hottest(results, stamps, nodes, cfg["max_sample_age_s"], now, known_gpus)
            state = states.setdefault(host, HostState())
            level = next_level(state, temp, policy)
            try:
                what = actuators[host].apply(level, dry_run)
                METRICS.set("gpu_fan_controller_errors", 0, host=host)
            except Exception as e:
                log.error("%s: apply failed: %s", host, type(e).__name__)
                METRICS.set("gpu_fan_controller_errors", 1, host=host)
                if isinstance(e, (AuthError, TLSPinError)):
                    actuators.pop(host, None)  # rediscover, with backoff
                    failures[host] = failures.get(host, 0) + 1
                    retry_at[host] = now + min(3600, cfg["interval_s"] * 2 ** failures[host])
                what = "error"
            METRICS.set("gpu_fan_controller_level", level, host=host)
            METRICS.set("gpu_fan_controller_gpu_temp_celsius", temp if temp is not None else -1, host=host)
            if what != "unchanged":
                log.info("%s: gpu=%s level=%s %s%s", host, temp, level, what, " (dry run)" if dry_run else "")
        METRICS.set("gpu_fan_controller_last_cycle_timestamp_seconds", now)
        stop.wait(cfg["interval_s"])
    log.info("stopping; iDRAC settings are left as they are")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    cfg = json.load(open(os.environ.get("CONFIG_FILE", "/etc/gpu-fan-controller/config.json")))
    server = HTTPServer(("0.0.0.0", int(os.environ.get("METRICS_PORT", "9100"))), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    run(cfg)


if __name__ == "__main__":
    main()
