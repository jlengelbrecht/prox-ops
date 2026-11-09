"""Tests for the GPU fan controller's decision logic and actuators (no network)."""
import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "controller", ROOT / "kubernetes/apps/observability/gpu-fan-controller/app/controller.py")
c = importlib.util.module_from_spec(SPEC)
sys.modules["controller"] = c  # dataclasses look the module up while the class is built
SPEC.loader.exec_module(c)

POLICY = c.Policy(up=[70.0, 78.0], hysteresis=5.0, calm_cycles=3, failsafe_level=1)


class Levels(unittest.TestCase):
    def test_raw_level(self):
        self.assertEqual(c.raw_level(40, POLICY), 0)
        self.assertEqual(c.raw_level(70, POLICY), 1)
        self.assertEqual(c.raw_level(77.9, POLICY), 1)
        self.assertEqual(c.raw_level(84, POLICY), 2)

    def test_steps_up_at_once(self):
        s = c.HostState()
        self.assertEqual(c.next_level(s, 79, POLICY), 2)

    def test_steps_down_only_after_calm_streak(self):
        s = c.HostState(level=2)
        self.assertEqual(c.next_level(s, 74, POLICY), 2)   # inside the band (78-5=73): not calm
        self.assertEqual(c.next_level(s, 70, POLICY), 2)   # calm 1
        self.assertEqual(c.next_level(s, 70, POLICY), 2)   # calm 2
        self.assertEqual(c.next_level(s, 70, POLICY), 1)   # calm 3 -> one level down

    def test_calm_streak_resets(self):
        s = c.HostState(level=1)
        c.next_level(s, 60, POLICY)
        c.next_level(s, 60, POLICY)
        c.next_level(s, 66, POLICY)                         # 70-5=65: not calm, resets
        self.assertEqual(c.next_level(s, 60, POLICY), 1)
        self.assertEqual(s.calm, 1)

    def test_missing_data_is_failsafe(self):
        s = c.HostState()
        self.assertEqual(c.next_level(s, None, POLICY), 1)
        s = c.HostState(level=2)
        self.assertEqual(c.next_level(s, None, POLICY), 2)  # never lowers on missing data

    def test_slot_lfm_never_below_automatic_or_above_max(self):
        self.assertIsNone(c.slot_lfm_for_level(0, [300, 390], 186, 390))
        self.assertEqual(c.slot_lfm_for_level(1, [300, 390], 186, 390), 300)
        self.assertEqual(c.slot_lfm_for_level(1, [300, 390], 340, 390), 340)  # auto target wins
        self.assertEqual(c.slot_lfm_for_level(2, [300, 500], 186, 390), 390)  # capped at max

    def test_offset_levels(self):
        self.assertEqual(c.offset_for_level(0, ["Low", "High"]), "Off")
        self.assertEqual(c.offset_for_level(2, ["Low", "High"]), "High")
        self.assertEqual(c.offset_for_level(5, ["Low", "High"]), "High")


class Mapping(unittest.TestCase):
    def node(self, name, zone, gpu):
        labels = {"topology.kubernetes.io/zone": zone}
        if gpu:
            labels["nvidia.com/gpu.present"] = "true"
        return {"metadata": {"name": name, "labels": labels}}

    def test_gpu_nodes_by_host(self):
        nodes = [self.node("k8s-work-7", "odin", True), self.node("k8s-work-4", "heimdall", True),
                 self.node("k8s-work-1", "baldar", False), self.node("k8s-work-9", "mars", True)]
        got = c.gpu_nodes_by_host(nodes, {"odin": "odin", "heimdall": "heimdall"})
        self.assertEqual(got, {"odin": ["k8s-work-7"], "heimdall": ["k8s-work-4"]})

    def g(self, node, uuid, value, at=1000.0):
        return {"metric": {"Hostname": node, "UUID": uuid}, "value": [at, str(value)]}

    def test_hottest_and_staleness(self):
        now, known = 1000.0, {}
        temps = [self.g("k8s-work-7", "GPU-a", 71), self.g("k8s-work-7", "GPU-b", 64)]
        fresh = [self.g("k8s-work-7", "GPU-a", 990), self.g("k8s-work-7", "GPU-b", 990)]
        self.assertEqual(c.hottest(temps, fresh, ["k8s-work-7"], 120, now, known), 71.0)
        old = [self.g("k8s-work-7", "GPU-a", 700), self.g("k8s-work-7", "GPU-b", 990)]
        self.assertIsNone(c.hottest(temps, old, ["k8s-work-7"], 120, now, known))   # one stale GPU
        self.assertIsNone(c.hottest(temps, [], ["k8s-work-7"], 120, now, known))    # no timestamps
        self.assertIsNone(c.hottest([], fresh, ["k8s-work-7"], 120, now, {}))       # nothing ever seen

    def test_silent_gpu_is_not_masked_by_a_cool_neighbour(self):
        now, known = 1000.0, {}
        both_t = [self.g("k8s-work-7", "GPU-a", 85), self.g("k8s-work-7", "GPU-b", 40)]
        both_s = [self.g("k8s-work-7", "GPU-a", 990), self.g("k8s-work-7", "GPU-b", 990)]
        self.assertEqual(c.hottest(both_t, both_s, ["k8s-work-7"], 120, now, known), 85.0)
        # GPU-a's series disappears; GPU-b stays cool and fresh.
        only_b_t, only_b_s = [self.g("k8s-work-7", "GPU-b", 40)], [self.g("k8s-work-7", "GPU-b", 995)]
        self.assertIsNone(c.hottest(only_b_t, only_b_s, ["k8s-work-7"], 120, now, known))


class FakeRedfish:
    def __init__(self, attrs):
        self.attrs = attrs
        self.patches = []

    def get(self, path):
        return {"Attributes": dict(self.attrs)}

    on_automatic_target = None

    def patch(self, path, body):
        self.patches.append(body["Attributes"])
        self.attrs.update(body["Attributes"])
        if self.on_automatic_target and body["Attributes"].get("PCIeSlotLFM.4.LFMMode") == "Automatic":
            self.attrs["PCIeSlotLFM.4.TargetLFM"] = self.on_automatic_target
        return {}


class Actuators(unittest.TestCase):
    def slot_attrs(self, mode="Automatic", target="186", custom=0):
        return {"PCIeSlotLFM.4.LFMMode": mode, "PCIeSlotLFM.4.TargetLFM": target,
                "PCIeSlotLFM.4.MaxLFM": 390, "PCIeSlotLFM.4.CustomLFM": custom}

    def test_slot_raise_and_return_to_automatic(self):
        rf = FakeRedfish(self.slot_attrs())
        a = c.SlotAirflow(rf, [4], [300, 390])
        self.assertEqual(a.apply(0, False), "unchanged")
        a.apply(1, False)
        self.assertEqual(rf.patches[-1], {"PCIeSlotLFM.4.LFMMode": "Custom", "PCIeSlotLFM.4.CustomLFM": 300})
        self.assertEqual(a.apply(1, False), "unchanged")
        a.apply(0, False)
        self.assertEqual(rf.patches[-1], {"PCIeSlotLFM.4.LFMMode": "Automatic"})

    def test_dry_run_writes_nothing(self):
        rf = FakeRedfish(self.slot_attrs())
        out = c.SlotAirflow(rf, [4], [300, 390]).apply(2, True)
        self.assertEqual(rf.patches, [])
        self.assertEqual(json.loads(out)["PCIeSlotLFM.4.CustomLFM"], 390)

    def test_custom_slot_learns_floor_before_raising(self):
        # Found in Custom (previous run or by hand): reset to Automatic once, read the
        # automatic target, then raise no lower than it.
        rf = FakeRedfish(self.slot_attrs(mode="Custom", target="250", custom=200))
        rf.on_automatic_target = "340"
        a = c.SlotAirflow(rf, [4], [300, 390])
        with mock.patch.object(c.time, "sleep"):
            a.apply(1, False)
        self.assertEqual(rf.patches[0], {"PCIeSlotLFM.4.LFMMode": "Automatic"})
        self.assertEqual(rf.patches[-1], {"PCIeSlotLFM.4.LFMMode": "Custom", "PCIeSlotLFM.4.CustomLFM": 340})

    def test_unknown_floor_in_dry_run_never_guesses(self):
        rf = FakeRedfish(self.slot_attrs(mode="Custom", target="250", custom=200))
        out = c.SlotAirflow(rf, [4], [300, 390]).apply(1, True)
        self.assertEqual(rf.patches, [])
        self.assertEqual(out, "unchanged")

    def test_fan_offset(self):
        rf = FakeRedfish({"ThermalSettings.1.FanSpeedOffset": "Off"})
        a = c.FanOffset(rf, ["Low", "High"])
        a.apply(2, False)
        self.assertEqual(rf.patches[-1], {"ThermalSettings.1.FanSpeedOffset": "High"})
        a.apply(0, False)
        self.assertEqual(rf.patches[-1], {"ThermalSettings.1.FanSpeedOffset": "Off"})


if __name__ == "__main__":
    unittest.main()


class Transport(unittest.TestCase):
    """The real client: certificate pinning and no redirects, against a local TLS server."""

    @classmethod
    def setUpClass(cls):
        import http.server, ssl, subprocess, tempfile, threading
        cls.dir = tempfile.mkdtemp()
        key, crt = f"{cls.dir}/k.pem", f"{cls.dir}/c.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                        "-days", "1", "-subj", "/CN=localhost"], check=True, capture_output=True)

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://example.invalid/")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            def log_message(self, *a):
                pass

        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, key)
        cls.srv.socket = ctx.wrap_socket(cls.srv.socket, server_side=True)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        import hashlib
        der = ssl.PEM_cert_to_DER_cert(open(crt).read())
        cls.pin = hashlib.sha256(der).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def client(self, pin):
        rf = c.Redfish("127.0.0.1", "u", "p", pin, timeout=5)
        port = self.srv.server_address[1]
        orig = c.http.client.HTTPSConnection
        rf._conn = lambda: orig("127.0.0.1", port, timeout=5, context=rf.ctx)
        return rf

    def test_pinned_certificate_is_accepted(self):
        self.assertEqual(self.client(self.pin).get("/x"), {"ok": True})

    def test_wrong_certificate_is_refused(self):
        with self.assertRaises(c.TLSPinError):
            self.client("00" * 32).get("/x")

    def test_redirect_is_not_followed(self):
        with self.assertRaises(RuntimeError):
            self.client(self.pin).get("/redirect")
