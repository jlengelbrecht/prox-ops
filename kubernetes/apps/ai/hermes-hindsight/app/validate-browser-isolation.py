#!/usr/bin/env python3
"""Semantic check of the rendered Hermes browser isolation boundary.

Reads rendered Kubernetes objects (not the Helm values) and relates them the
way Cilium, the kubelet and External Secrets will: the browser pod must carry
the authored isolation label and be selected by its own policy and by the
Hermes egress rule; its ingress must admit only Hermes on the CDP port; its
egress must reach only cluster DNS and public IPv4 web ports; the pod must
hold no service-account token, credential or persistent storage beyond the
generated browser token; Hermes must read the CDP URL from that generated
Secret and nothing else from it. Cilium leaves an endpoint unrestricted in a
direction until some policy selects it, so an unselected pod fails open; that
is the case this check exists to catch.

Inputs are the rendered objects of the app tree, for example:

  flux-local build ks hermes-hindsight -A --path kubernetes/flux/cluster > ks.yaml
  flux-local build hr hermes -n ai --path kubernetes/flux/cluster > hermes.yaml
  flux-local build hr hermes-browser -n ai --path kubernetes/flux/cluster > browser.yaml
  flux-local build hr hindsight -n ai --path kubernetes/flux/cluster > hindsight.yaml
  python3 validate-browser-isolation.py ks.yaml hermes.yaml browser.yaml hindsight.yaml
  python3 validate-browser-isolation.py --self-test ks.yaml hermes.yaml browser.yaml hindsight.yaml

Every rendered workload of the tree should be included so the selector checks
can prove nothing else is selected.

--self-test applies a bounded set of bad outcomes to the same render in
memory (dropped label, widened ingress, internal egress, literal credential,
privileged pod, persistent volume, env-dumping log level, no-op probe,
credential moved to a sidecar, ...) and requires the check to reject each
one. Exit 0 when every check passes, 1 otherwise. Needs PyYAML. Not run by
CI; run it locally against a fresh render whenever these files change.
"""

import copy
import ipaddress
import re
import shlex
import sys
from urllib.parse import parse_qs, urlsplit

import yaml

ISOLATION_KEY = "prox-ops.io/isolation"
NS_KEY = "io.kubernetes.pod.namespace"
DNS_SELECTOR = {NS_KEY: "kube-system", "k8s-app": "kube-dns"}
# Address blocks a public-web rule must carve out: the pod, service and node
# networks live in 10/8, plus every block that is never a public destination
# (the same 15 carve-outs as the Hermes public-web rule).
MUST_EXCLUDE = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
                "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
                "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
                "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
                "224.0.0.0/4", "240.0.0.0/4"]
WEB_PORTS = {("80", "TCP"), ("443", "TCP")}
# The chart-named application containers that own the generated credential:
# Browserless reads TOKEN, Hermes reads the composed URL. A sidecar or init
# container must not hold either.
BROWSER_APP_CONTAINER = "app"
HERMES_APP_CONTAINER = "hermes-agent"
# The only writable paths of the browser pod: mountPath -> (volume name,
# emptyDir medium, sizeLimit). Everything else stays on the read-only root.
WRITABLE_MOUNTS = {"/tmp": ("tmp", None, "2Gi"),
                   "/dev/shm": ("shm", "Memory", "512Mi"),
                   "/home/blessuser": ("home", None, "256Mi")}
# Startup must prove a Chromium launch (/json/version starts a browser);
# readiness and liveness only ask Browserless whether it is up.
PROBE_ROUTES = {"startupProbe": "/json/version", "readinessProbe": "/active", "livenessProbe": "/active"}
# The whole curl argument list of a probe, pinned to the shipped form: one
# transfer of the route with -f and the stdin config and nothing else. curl
# reports the last transfer's result, so a second URL (or --next) after a
# failing health request returns success, and --help/--version/--no-fail/-K
# <file> short-circuit or override the intended request.
CURL_MAX_TIME = re.compile(r"^[0-9]+(\.[0-9]+)?$")
PROBE_CURL = ("curl", "-fsS", "-o", "/dev/null", "--max-time", CURL_MAX_TIME, "-K", "-")
CURL_CONFIG_HEADER = re.compile(r'^header\s*=\s*"Authorization: Bearer %s"(\\n)?$')
GENERATED_VALUE = re.compile(r"^\{\{\s*\.password\s*\}\}$")
BEARER_LITERAL = re.compile(r"Bearer\s+(?!\$TOKEN\b|\$\{TOKEN\}|\"\$TOKEN\"|%s)\S")
TOKEN_QUERY_LITERAL = re.compile(r"token=(?!\$TOKEN\b|\$\{TOKEN\}|\{\{)")


def load(paths):
    docs = []
    for p in paths:
        with open(p) as fh:
            docs += [d for d in yaml.safe_load_all(fh) if isinstance(d, dict) and d.get("kind")]
    return docs


def ns(obj, default):
    return obj.get("metadata", {}).get("namespace") or default


def name(obj):
    return obj.get("metadata", {}).get("name", "?")


def selects(selector, labels, pod_ns):
    """Cilium/k8s matchLabels semantics; matchExpressions are refused (None)."""
    if not isinstance(selector, dict) or "matchExpressions" in selector:
        return None
    ml = selector.get("matchLabels")
    if not isinstance(ml, dict) or not ml:
        return None
    for k, v in ml.items():
        k = k.split(":", 1)[1] if k.startswith(("k8s:", "any:")) else k
        actual = pod_ns if k == NS_KEY else labels.get(k)
        if actual != v:
            return False
    return True


def ports_of(rule):
    out = set()
    for tp in rule.get("toPorts") or []:
        if tp.get("rules"):
            out.add(("L7", "L7"))
        for p in tp.get("ports") or []:
            out.add((str(p.get("port")), p.get("protocol", "ANY")))
    return out


def debug_enabled(setting, namespace):
    """The `debug` npm module's DEBUG matcher: comma/space separated patterns,
    `*` wildcard, leading `-` excludes; an excluded namespace never logs."""
    names, skips = [], []
    for pat in re.split(r"[\s,]+", setting.strip()):
        if pat:
            rx = re.compile("^" + re.escape(pat.lstrip("-")).replace(r"\*", ".*?") + "$")
            (skips if pat.startswith("-") else names).append(rx)
    return not any(r.match(namespace) for r in skips) and any(r.match(namespace) for r in names)


def probe_errors(probe, p, pod_port):
    """The probe must be `sh -c` running exactly `printf <curl header line>
    "$TOKEN" | curl -f ... -K - http://127.0.0.1:<port><route>` and nothing
    else: the token reaches curl only through its stdin config, -f turns
    401/500 into a probe failure, the route is the one that proves launch
    (startup) or health (the rest), no other executable, command separator
    or operator stands in for curl or hides its exit status, and the curl
    argument list is exactly the shipped one so no second URL or extra
    option changes which transfer's result the probe reports."""
    want = f"http://127.0.0.1:{pod_port}{PROBE_ROUTES[probe]}"
    cmd = (p.get("exec") or {}).get("command") or []
    if set(p) & {"httpGet", "tcpSocket", "grpc"} or len(cmd) != 3 or cmd[:2] != ["sh", "-c"] or not isinstance(cmd[2], str):
        return [f"{probe} must be an exec `sh -c` pipeline that authenticates GET {want}"]
    # sh ends a command at an unquoted newline just as at `;`; the shipped
    # script has only the continuation right after `|` and the block scalar's
    # trailing newline.
    if "\n" in re.sub(r"\|[ \t]*\n+", "| ", cmd[2]).strip():
        return [f"{probe} script must be a single pipeline; a further line runs another command in place of curl's exit status"]
    lex = shlex.shlex(cmd[2], posix=True, punctuation_chars=True)
    lex.whitespace_split, lex.commenters = True, ""
    try:
        words = list(lex)
    except ValueError:
        return [f"{probe} command is not parseable shell"]
    # Unquoted `();<>|&` come out as their own tokens; the only one allowed
    # is the single `|` between the two stages.
    stages, cur, ops = [], [], []
    for w in words:
        if w and not w.strip(lex.punctuation_chars):
            ops.append(w)
            stages.append(cur)
            cur = []
        else:
            cur.append(w)
    stages.append(cur)
    if ops not in ([], ["|"]):
        return [f"{probe} must be `printf <header> \"$TOKEN\" | curl ...` and nothing else; shell operator(s) {ops} could run another command or hide curl's exit status"]
    if not all(stages):
        return [f"{probe} must be `printf <header> \"$TOKEN\" | curl ...`, got an empty pipeline stage"]
    errs = []
    fmt, curl = stages if len(stages) == 2 else ([], stages[0])
    if curl[0] != "curl":
        errs.append(f"{probe} must run curl against Browserless, not {curl[0]}")
    if len(fmt) != 3 or fmt[0] != "printf" or not CURL_CONFIG_HEADER.match(fmt[1]) or fmt[2] not in ("$TOKEN", "${TOKEN}"):
        errs.append(f"{probe} must pipe `header = \"Authorization: Bearer %s\"` formatted with $TOKEN into curl")
    if not any(curl[i] in ("-K", "--config") and curl[i + 1] == "-" for i in range(len(curl) - 1)):
        errs.append(f"{probe} curl must read its config from stdin (-K -)")
    if not any(a == "--fail" or (a.startswith("-") and not a.startswith("--") and "f" in a[1:]) for a in curl[1:]):
        errs.append(f"{probe} curl must use -f so an HTTP error fails the probe")
    if any("TOKEN" in a for a in curl):
        errs.append(f"{probe} passes the token on the curl command line instead of stdin")
    urls = [a for a in curl if a.startswith(("http://", "https://", "ws://", "wss://"))]
    if urls != [want]:
        errs.append(f"{probe} must request exactly {want}, found {urls}")
    # Every argument, in order: an added URL, --next, --help, --version, a
    # second -K or a negated -f would make curl report something other than
    # this one authenticated transfer.
    expect = PROBE_CURL + (want,)
    if len(curl) != len(expect) or not all(e.fullmatch(a) if isinstance(e, re.Pattern) else a == e for a, e in zip(curl, expect)):
        errs.append(f"{probe} curl arguments must be exactly `curl -fsS -o /dev/null --max-time <seconds> -K - {want}`, got {shlex.join(curl)}")
    return errs


class Check:
    def __init__(self, docs, default_ns):
        self.docs, self.ns, self.errors = docs, default_ns, []

    def fail(self, msg):
        self.errors.append(msg)

    def kind(self, k):
        return [d for d in self.docs if d.get("kind") == k]

    def pods(self):
        """(owner object, pod namespace, pod labels, pod spec) for every workload."""
        out = []
        for d in self.kind("Deployment") + self.kind("StatefulSet") + self.kind("DaemonSet"):
            t = d.get("spec", {}).get("template", {})
            out.append((d, ns(d, self.ns), t.get("metadata", {}).get("labels") or {}, t.get("spec") or {}))
        return out

    def run(self):
        pods = self.pods()
        browser = [p for p in pods if p[2].get(ISOLATION_KEY)]
        if len(browser) != 1:
            return self.fail(f"expected exactly one pod template labelled {ISOLATION_KEY}, found {len(browser)}")
        b_obj, b_ns, b_labels, b_spec = browser[0]
        others = [p for p in pods if p[0] is not b_obj]
        svc_name, svc_port, pod_port = self.service(b_obj, b_ns, b_labels, b_spec, others)
        secret = self.browser_pod(b_obj, b_labels, b_spec, pod_port)
        hermes = self.browser_policy(b_ns, b_labels, others, pod_port)
        if hermes:
            self.hermes_side(hermes, b_ns, b_labels, others, pod_port, secret)
        self.secret_plumbing(secret, b_ns, svc_name, svc_port)
        return None

    # -- Service -------------------------------------------------------------------------
    def service(self, b_obj, b_ns, b_labels, b_spec, others):
        svcs = [s for s in self.kind("Service") if ns(s, self.ns) == b_ns
                and s.get("spec", {}).get("selector")
                and all(b_labels.get(k) == v for k, v in s["spec"]["selector"].items())]
        if len(svcs) != 1:
            self.fail(f"expected exactly one Service selecting the browser pod, found {len(svcs)}")
            return None, None, None
        svc = svcs[0]
        sel = svc["spec"]["selector"]
        # The URL carries the browser token, so the Service must resolve to the
        # isolation-labelled pod alone: pin it by the authored label or by the
        # browser controller's own pod selector, and select no other workload.
        for o in others:
            if o[1] == b_ns and all(o[2].get(k) == v for k, v in sel.items()):
                self.fail(f"browser Service also selects {o[0]['kind']} {name(o[0])}")
        ctl = (b_obj.get("spec", {}).get("selector") or {}).get("matchLabels") or {}
        if ISOLATION_KEY not in sel and not (ctl and all(sel.get(k) == v for k, v in ctl.items())):
            self.fail(f"browser Service selector must pin {ISOLATION_KEY} or the browser controller labels, not {sel}")
        if svc["spec"].get("type", "ClusterIP") != "ClusterIP":
            self.fail("browser Service must be ClusterIP")
        ports = svc["spec"].get("ports") or []
        if len(ports) != 1:
            self.fail("browser Service must expose exactly one port")
            return name(svc), None, None
        if ports[0].get("protocol", "TCP") != "TCP":
            self.fail(f"browser Service must carry the WebSocket over TCP, not {ports[0].get('protocol')}")
        tgt = ports[0].get("targetPort", ports[0].get("port"))
        if not isinstance(tgt, int):
            cports = {p.get("name"): p.get("containerPort") for c in b_spec.get("containers", []) for p in c.get("ports", [])}
            tgt = cports.get(tgt)
        if not isinstance(tgt, int):
            self.fail("browser Service targetPort does not resolve to a container port")
        for r in self.kind("HTTPRoute") + self.kind("Ingress"):
            if name(svc) in yaml.safe_dump(r.get("spec", {})):
                self.fail(f"{r['kind']} {name(r)} references the browser Service")
        return name(svc), str(ports[0].get("port")), str(tgt)

    # -- browser pod hardening ---------------------------------------------------------------
    def browser_pod(self, b_obj, b_labels, spec, pod_port):
        f = self.fail
        if spec.get("automountServiceAccountToken") is not False:
            f("browser pod must set automountServiceAccountToken: false")
        for k in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(k):
                f(f"browser pod must not set {k}")
        psc = spec.get("securityContext") or {}
        if psc.get("runAsNonRoot") is not True or not psc.get("runAsUser"):
            f("browser pod must run as a non-root uid")
        if (psc.get("seccompProfile") or {}).get("type") in (None, "Unconfined"):
            f("browser pod must set a seccomp profile")
        volumes = {v.get("name"): v for v in spec.get("volumes") or []}
        if set(volumes) != set(n for n, _, _ in WRITABLE_MOUNTS.values()):
            f(f"browser volumes must be exactly {sorted(n for n, _, _ in WRITABLE_MOUNTS.values())}, found {sorted(volumes)}")
        for path, (vname, medium, size) in WRITABLE_MOUNTS.items():
            v = volumes.get(vname) or {}
            ed = v.get("emptyDir") or {}
            if [k for k in v if k != "name"] != ["emptyDir"] or ed.get("medium") != medium or ed.get("sizeLimit") != size:
                f(f"browser volume {vname} for {path} must be an emptyDir with medium {medium} and sizeLimit {size}")
        if spec.get("initContainers"):
            f("browser pod must not run init containers")
        secret = None
        for c in spec.get("containers") or []:
            # Every writable path is one of the three bounded emptyDirs; the app
            # container mounts all of them, nothing mounts anything else.
            mounts = {m.get("mountPath"): m for m in c.get("volumeMounts") or []}
            for path, m in mounts.items():
                want = WRITABLE_MOUNTS.get(path)
                if not want or m.get("name") != want[0] or m.get("readOnly") or m.get("subPath"):
                    f(f"container {c['name']} mount {path} is not one of the bounded writable mounts {sorted(WRITABLE_MOUNTS)}")
            if c["name"] == BROWSER_APP_CONTAINER and set(mounts) != set(WRITABLE_MOUNTS):
                f(f"container {c['name']} must mount exactly {sorted(WRITABLE_MOUNTS)}, found {sorted(mounts)}")
            # Browserless dumps its launch options, process environment
            # included, on the debug log level; the shipped DEBUG setting
            # must disable that namespace and keep the error level.
            debug = {e.get("name"): e.get("value") for e in c.get("env") or []}.get("DEBUG")
            if debug is None or debug_enabled(debug, "browserless.io:ChromiumCDPWebSocketRoute:debug"):
                f(f"container {c['name']} DEBUG must exclude the debug namespace that logs the launch environment")
            elif not debug_enabled(debug, "browserless.io:server:error"):
                f(f"container {c['name']} DEBUG must keep the error namespace")
            csc = c.get("securityContext") or {}
            if csc.get("privileged") or csc.get("allowPrivilegeEscalation") is not False:
                f(f"container {c['name']} must not allow privilege escalation")
            if csc.get("readOnlyRootFilesystem") is not True:
                f(f"container {c['name']} must have a read-only root filesystem")
            # A container may override the pod identity and seccomp settings;
            # it must not weaken them.
            if csc.get("runAsNonRoot") is False or csc.get("runAsUser") == 0 or csc.get("runAsGroup") == 0:
                f(f"container {c['name']} must not override the pod's non-root identity")
            if "seccompProfile" in csc and (csc.get("seccompProfile") or {}).get("type") in (None, "Unconfined"):
                f(f"container {c['name']} must not override the pod seccomp profile with Unconfined")
            caps = csc.get("capabilities") or {}
            if "ALL" not in (caps.get("drop") or []) or caps.get("add"):
                f(f"container {c['name']} must drop ALL capabilities and add none")
            if c.get("envFrom"):
                f(f"container {c['name']} must not use envFrom")
            for e in c.get("env") or []:
                ref = (e.get("valueFrom") or {}).get("secretKeyRef")
                if e.get("name") == "TOKEN":
                    if c["name"] != BROWSER_APP_CONTAINER:
                        f(f"container {c['name']} must not read TOKEN; only container {BROWSER_APP_CONTAINER} authenticates Browserless")
                    else:
                        if not ref or ref.get("optional") or ref.get("key") != "TOKEN" or "value" in e:
                            f("TOKEN must be a required secretKeyRef to key TOKEN")
                        secret = ref and ref.get("name")
                elif ref or (e.get("valueFrom") or {}):
                    f(f"container {c['name']} env {e.get('name')} must not come from a Secret or field reference")
                elif re.search(r"token|secret|password|key", e.get("name", ""), re.I):
                    f(f"container {c['name']} env {e.get('name')} looks like a literal credential")
            res = c.get("resources") or {}
            for section in ("requests", "limits"):
                for r in ("memory", "ephemeral-storage"):
                    if not (res.get(section) or {}).get(r):
                        f(f"container {c['name']} must declare {section}.{r}")
            for probe in PROBE_ROUTES:
                p = c.get(probe)
                if not p:
                    f(f"container {c['name']} must define {probe}")
                    continue
                text = yaml.safe_dump(p)
                if "httpHeaders" in text or BEARER_LITERAL.search(text) or TOKEN_QUERY_LITERAL.search(text):
                    f(f"{probe} carries a literal credential")
                cmd = (p.get("exec") or {}).get("command") or []
                if any(a.startswith("-") and "x" in a for a in cmd[:-1]) or "set -x" in text:
                    f(f"{probe} enables shell tracing")
                if c["name"] == BROWSER_APP_CONTAINER:
                    for err in probe_errors(probe, p, pod_port):
                        f(err)
        if not secret:
            f(f"browser container {BROWSER_APP_CONTAINER} must read TOKEN from a Secret")
        reload = (b_obj.get("metadata", {}).get("annotations") or {}).get("secret.reloader.stakater.com/reload", "")
        if secret and secret not in [s.strip() for s in reload.split(",")]:
            f("browser Deployment must carry a Reloader annotation for the token Secret")
        return secret

    # -- browser policy ----------------------------------------------------------------------
    def browser_policy(self, b_ns, b_labels, others, pod_port):
        f = self.fail
        cnps = [c for c in self.kind("CiliumNetworkPolicy") if ns(c, self.ns) == b_ns]
        selecting = [c for c in cnps if selects(c["spec"].get("endpointSelector"), b_labels, b_ns) is not False]
        if len(selecting) != 1:
            return f(f"expected exactly one policy to select the browser pod, found {len(selecting)}")
        cnp = selecting[0]
        sel = cnp["spec"]["endpointSelector"]
        if selects(sel, b_labels, b_ns) is not True:
            return f(f"policy {name(cnp)} endpointSelector must select the browser pod by matchLabels")
        ml = sel["matchLabels"]
        if ISOLATION_KEY not in ml or any(k.startswith("app.kubernetes.io/") for k in ml):
            f(f"policy {name(cnp)} must select {ISOLATION_KEY}, not chart-generated labels")
        for o in others:
            if selects(sel, o[2], o[1]):
                f(f"policy {name(cnp)} also selects {o[0]['kind']} {name(o[0])}")
        hermes = None
        ingress = cnp["spec"].get("ingress")
        if not ingress:
            f(f"policy {name(cnp)} must declare its ingress")
        # One rule with one source: Hermes, and nothing beside it.
        elif len(ingress) != 1 or len(ingress[0].get("fromEndpoints") or []) != 1:
            f(f"policy {name(cnp)} ingress must be exactly one rule with one Hermes source, found "
              f"{len(ingress)} rule(s) with {[len(r.get('fromEndpoints') or []) for r in ingress]} source(s)")
        for rule in ingress or []:
            if set(rule) - {"fromEndpoints", "toPorts"}:
                f(f"policy {name(cnp)} ingress rule uses {sorted(set(rule) - {'toPorts'})}; only fromEndpoints is allowed")
            if not rule.get("fromEndpoints"):
                f(f"policy {name(cnp)} ingress rule admits every source")
            for s in rule.get("fromEndpoints") or []:
                if NS_KEY not in (s.get("matchLabels") or {}):
                    f(f"policy {name(cnp)} ingress selector must pin the source namespace")
                matched = [o for o in others if selects(s, o[2], o[1])]
                if len(matched) != 1:
                    f(f"policy {name(cnp)} ingress selector must select exactly one workload, found {len(matched)}")
                elif hermes is None:
                    hermes = matched[0]
            if ports_of(rule) != {(pod_port, "TCP")}:
                f(f"policy {name(cnp)} ingress must allow only TCP {pod_port}, found {sorted(ports_of(rule))}")
        egress = cnp["spec"].get("egress")
        if not egress:
            f(f"policy {name(cnp)} must declare its egress")
        seen = set()
        for rule in egress or []:
            keys = set(rule) - {"toPorts"}
            if keys == {"toEndpoints"} and [s.get("matchLabels") for s in rule["toEndpoints"]] == [DNS_SELECTOR]:
                if ports_of(rule) != {("53", "UDP"), ("53", "TCP")}:
                    f(f"policy {name(cnp)} DNS egress must allow only port 53")
                seen.add("dns")
            elif keys == {"toCIDRSet"} and len(rule["toCIDRSet"]) == 1 and rule["toCIDRSet"][0].get("cidr") == "0.0.0.0/0":
                exc = [ipaddress.ip_network(x) for x in rule["toCIDRSet"][0].get("except") or []]
                for must in MUST_EXCLUDE:
                    if not any(ipaddress.ip_network(must).subnet_of(e) for e in exc):
                        f(f"policy {name(cnp)} public-web egress does not carve out {must}")
                if ports_of(rule) != WEB_PORTS:
                    f(f"policy {name(cnp)} public-web egress must allow exactly TCP 80 and TCP 443, found {sorted(ports_of(rule))}")
                seen.add("web")
            else:
                f(f"policy {name(cnp)} egress rule {sorted(keys)} opens a lane other than DNS and public web")
        if seen != {"dns", "web"}:
            f(f"policy {name(cnp)} egress must contain exactly the DNS and public-web lanes")
        return hermes

    # -- Hermes side -------------------------------------------------------------------------
    def hermes_side(self, hermes, b_ns, b_labels, others, pod_port, secret):
        f = self.fail
        h_obj, h_ns, h_labels, h_spec = hermes
        h_cnps = [c for c in self.kind("CiliumNetworkPolicy") if ns(c, self.ns) == h_ns
                  and selects(c["spec"].get("endpointSelector"), h_labels, h_ns)]
        rules = [r for c in h_cnps for r in c["spec"].get("egress") or []
                 if any(selects(s, b_labels, b_ns) for s in r.get("toEndpoints") or [])]
        if len(rules) != 1:
            f(f"expected exactly one Hermes egress rule selecting the browser pod, found {len(rules)}")
        for r in rules:
            # Cilium destination fields are additive: a toEntities/toCIDRSet/
            # toFQDNs/toServices sibling would widen this rule beyond the browser.
            if set(r) != {"toEndpoints", "toPorts"}:
                f(f"Hermes egress to the browser must contain only toEndpoints and toPorts, found {sorted(r)}")
            for s in r["toEndpoints"]:
                ml = s.get("matchLabels") or {}
                if NS_KEY not in ml or ISOLATION_KEY not in ml:
                    f(f"Hermes egress to the browser must select {NS_KEY} and {ISOLATION_KEY}")
                for o in others:
                    if selects(s, o[2], o[1]):
                        f(f"Hermes egress to the browser also selects {o[0]['kind']} {name(o[0])}")
            if ports_of(r) != {(pod_port, "TCP")}:
                f(f"Hermes egress to the browser must allow only TCP {pod_port}")
        found = 0
        # Only the Hermes application container consumes the URL; a sidecar
        # or (native-sidecar) init container must not read the Secret at all.
        for c in (h_spec.get("containers") or []) + (h_spec.get("initContainers") or []):
            for src in c.get("envFrom") or []:
                if (src.get("secretRef") or {}).get("name") == secret:
                    f(f"Hermes container {c.get('name')} must not import the whole browser token Secret")
            for e in c.get("env") or []:
                ref = (e.get("valueFrom") or {}).get("secretKeyRef") or {}
                if e.get("name") == "BROWSER_CDP_URL" and c.get("name") == HERMES_APP_CONTAINER:
                    found += 1
                    if ref.get("name") != secret or ref.get("key") != "BROWSER_CDP_URL" or ref.get("optional") or "value" in e:
                        f("Hermes BROWSER_CDP_URL must be a required secretKeyRef to the browser token Secret")
                elif (secret and ref.get("name") == secret) or e.get("name") == "BROWSER_CDP_URL":
                    f(f"Hermes container {c.get('name')} env {e.get('name')} reads the browser token Secret; only {HERMES_APP_CONTAINER} may")
        if found != 1:
            f(f"Hermes container {HERMES_APP_CONTAINER} must read BROWSER_CDP_URL exactly once, found {found}")
        reload = (h_obj.get("metadata", {}).get("annotations") or {}).get("secret.reloader.stakater.com/reload", "")
        if secret not in [s.strip() for s in reload.split(",")]:
            f("Hermes Deployment must carry a Reloader annotation for the token Secret")

    # -- generated Secret --------------------------------------------------------------------
    def secret_plumbing(self, secret, b_ns, svc_name, svc_port):
        f = self.fail
        for s in self.kind("Secret"):
            keys = set(s.get("data") or {}) | set(s.get("stringData") or {})
            if name(s) == secret or keys & {"TOKEN", "BROWSER_CDP_URL"}:
                f(f"Secret {name(s)} is declared in the render; the token must only be generated in-cluster")
        ess = [e for e in self.kind("ExternalSecret") if ns(e, self.ns) == b_ns
               and (e.get("spec", {}).get("target") or {}).get("name") == secret]
        if len(ess) != 1:
            return f(f"expected exactly one ExternalSecret targeting {secret}, found {len(ess)}")
        spec = ess[0]["spec"]
        if spec.get("refreshPolicy") != "CreatedOnce":
            f("browser token ExternalSecret must use refreshPolicy CreatedOnce")
        if spec.get("secretStoreRef") or spec.get("data"):
            f("browser token must come from a generator, not a secret store")
        if spec["target"].get("creationPolicy") != "Owner":
            f("browser token ExternalSecret must own its Secret")
        refs = [(d.get("sourceRef") or {}).get("generatorRef") for d in spec.get("dataFrom") or []]
        if len(refs) != 1 or not refs[0] or refs[0].get("kind") != "Password":
            f("browser token must come from exactly one Password generator")
        else:
            gens = [g for g in self.kind("Password") if ns(g, self.ns) == b_ns and name(g) == refs[0].get("name")]
            if len(gens) != 1:
                f(f"Password generator {refs[0].get('name')} is not rendered")
            else:
                g = gens[0]["spec"]
                if g.get("symbols") != 0 or (g.get("length") or 0) < 32 or "allowRepeat" not in g:
                    f("Password generator must produce an alphanumeric value of at least 32 characters")
        tpl = spec["target"].get("template") or {}
        data = tpl.get("data") or {}
        if tpl.get("mergePolicy", "Replace") != "Replace" or set(data) != {"TOKEN", "BROWSER_CDP_URL"}:
            f("browser token Secret must hold exactly TOKEN and BROWSER_CDP_URL")
        if not GENERATED_VALUE.match(str(data.get("TOKEN", "")).strip()):
            f("TOKEN must be exactly the generated password")
        url = urlsplit(str(data.get("BROWSER_CDP_URL", "")))
        host = f"{svc_name}.{b_ns}.svc.cluster.local" if svc_name else None
        # The Service is plaintext, so the scheme is exactly ws; the token is
        # the only credential and it travels in the query, never as userinfo.
        if url.scheme != "ws" or url.hostname != host or str(url.port) != str(svc_port) or url.path != "/":
            f(f"BROWSER_CDP_URL must be the direct plaintext WebSocket root ws://{host}:{svc_port}/, found scheme {url.scheme!r}")
        if url.username is not None or url.password is not None:
            f("BROWSER_CDP_URL must not carry userinfo")
        token = parse_qs(url.query).get("token", [""])
        if len(token) != 1 or not GENERATED_VALUE.match(token[0].strip()) or set(parse_qs(url.query)) != {"token"}:
            f("BROWSER_CDP_URL must carry only the generated password as its token query")


def validate(docs, default_ns):
    c = Check(docs, default_ns)
    c.run()
    return c.errors


def mutations(docs):
    """Bounded bad outcomes; each must be rejected. Yields (label, mutated docs)."""
    def get(kind, nm):
        return next(d for d in docs if d["kind"] == kind and name(d) == nm)
    browser = get("Deployment", "hermes-browser")
    hermes = get("Deployment", "hermes")
    cnp = get("CiliumNetworkPolicy", "hermes-browser")
    hcnp = get("CiliumNetworkPolicy", "hermes")
    es = get("ExternalSecret", "hermes-browser-token")
    bpod = browser["spec"]["template"]
    hpod = hermes["spec"]["template"]
    bctr = bpod["spec"]["containers"][0]
    hctr = hpod["spec"]["containers"][0]
    to_browser = next(r for r in hcnp["spec"]["egress"] if any(ISOLATION_KEY in (s.get("matchLabels") or {}) for s in r.get("toEndpoints") or []))
    web = next(r for r in cnp["spec"]["egress"] if "toCIDRSet" in r)
    svc = get("Service", "hermes-browser")
    ingress = cnp["spec"]["ingress"][0]
    web_port = lambda port: next(p for p in web["toPorts"][0]["ports"] if str(p["port"]) == port)
    env = lambda ctr, nm: next(e for e in ctr["env"] if e["name"] == nm)
    mount = lambda path: next(m for m in bctr["volumeMounts"] if m["mountPath"] == path)
    volume = lambda nm: next(v for v in bpod["spec"]["volumes"] if v["name"] == nm)
    probe_cmd = "printf 'header = \"Authorization: Bearer %s\"\\n' \"$TOKEN\" |\n  curl {} -o /dev/null --max-time 3 -K - http://127.0.0.1:3000{}\n"
    cdp_url = lambda u: es["spec"]["target"]["template"]["data"].__setitem__("BROWSER_CDP_URL", u)
    curl_append = lambda p, extra: bctr[p]["exec"]["command"].__setitem__(2, bctr[p]["exec"]["command"][2].rstrip("\n") + extra + "\n")

    def other_workload(labels):
        """A second in-namespace workload for selector-overlap cases (both
        render paths, whether or not Hindsight is part of the input)."""
        docs.append({"kind": "Deployment", "metadata": {"name": "other", "namespace": "ai"},
                     "spec": {"template": {"metadata": {"labels": labels}, "spec": {"containers": [{"name": "other"}]}}}})

    def sidecar(pod, ctr, moved_env):
        """Clone the app container as a sidecar and move one env entry to it."""
        side = copy.deepcopy(ctr)
        side["name"] = "sidecar"
        if moved_env:
            ctr["env"].remove(env(ctr, moved_env))
        pod["spec"]["containers"].append(side)

    cases = [
        ("pod label removed", lambda: bpod["metadata"]["labels"].pop(ISOLATION_KEY)),
        ("policy selector typo", lambda: cnp["spec"]["endpointSelector"]["matchLabels"].__setitem__(ISOLATION_KEY, "hermes-browsr")),
        ("policy selects chart label", lambda: cnp["spec"]["endpointSelector"]["matchLabels"].__setitem__("app.kubernetes.io/name", "hermes-browser")),
        ("policy selector by expression", lambda: cnp["spec"].__setitem__("endpointSelector", {"matchExpressions": [{"key": ISOLATION_KEY, "operator": "Exists"}]})),
        ("Hermes pod gains the label", lambda: hpod["metadata"]["labels"].__setitem__(ISOLATION_KEY, "hermes-browser")),
        ("ingress from everyone", lambda: cnp["spec"]["ingress"][0].__setitem__("fromEndpoints", [])),
        ("ingress from all entities", lambda: cnp["spec"]["ingress"][0].__setitem__("fromEntities", ["all"])),
        ("ingress selector without namespace", lambda: cnp["spec"]["ingress"][0]["fromEndpoints"][0]["matchLabels"].pop(NS_KEY)),
        ("ingress extra port", lambda: cnp["spec"]["ingress"][0]["toPorts"][0]["ports"].append({"port": "22", "protocol": "TCP"})),
        ("egress to Hindsight", lambda: cnp["spec"]["egress"].append({"toEndpoints": [{"matchLabels": {NS_KEY: "ai", "app.kubernetes.io/name": "hindsight"}}]})),
        ("egress to gateway entity", lambda: cnp["spec"]["egress"].append({"toEntities": ["cluster"]})),
        ("egress FQDN lane", lambda: cnp["spec"]["egress"].append({"toFQDNs": [{"matchName": "api.minimax.io"}]})),
        ("private range not carved out", lambda: web["toCIDRSet"][0]["except"].remove("10.0.0.0/8")),
        ("public-web extra port", lambda: web["toPorts"][0]["ports"].append({"port": "8888", "protocol": "TCP"})),
        ("DNS extra port", lambda: cnp["spec"]["egress"][0]["toPorts"][0]["ports"].append({"port": "5353", "protocol": "UDP"})),
        ("second additive policy", lambda: docs.append({"kind": "CiliumNetworkPolicy", "metadata": {"name": "extra", "namespace": "ai"}, "spec": {"endpointSelector": {"matchLabels": {ISOLATION_KEY: "hermes-browser"}}, "egress": [{"toEntities": ["all"]}]}})),
        ("Hermes egress rule removed", lambda: hcnp["spec"]["egress"].remove(to_browser)),
        ("Hermes egress rule widened", lambda: to_browser["toEndpoints"][0]["matchLabels"].pop(ISOLATION_KEY)),
        ("Hermes egress extra port", lambda: to_browser["toPorts"][0]["ports"].append({"port": "9222", "protocol": "TCP"})),
        ("service-account token mounted", lambda: bpod["spec"].__setitem__("automountServiceAccountToken", True)),
        ("host network", lambda: bpod["spec"].__setitem__("hostNetwork", True)),
        ("privileged container", lambda: bctr["securityContext"].__setitem__("privileged", True)),
        ("writable root filesystem", lambda: bctr["securityContext"].__setitem__("readOnlyRootFilesystem", False)),
        ("capability added", lambda: bctr["securityContext"]["capabilities"].__setitem__("add", ["SYS_ADMIN"])),
        ("seccomp unconfined", lambda: bpod["spec"]["securityContext"].__setitem__("seccompProfile", {"type": "Unconfined"})),
        ("Hermes PVC mounted", lambda: bpod["spec"]["volumes"].append({"name": "data", "persistentVolumeClaim": {"claimName": "hermes-data"}})),
        ("hostPath volume", lambda: bpod["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/"}})),
        ("unbounded emptyDir", lambda: bpod["spec"]["volumes"][0]["emptyDir"].pop("sizeLimit")),
        ("Hindsight secret in env", lambda: bctr["env"].append({"name": "HINDSIGHT_API_TENANT_API_KEY", "valueFrom": {"secretKeyRef": {"name": "hindsight-secrets", "key": "HINDSIGHT_API_TENANT_API_KEY"}}})),
        ("whole Secret imported", lambda: bctr.__setitem__("envFrom", [{"secretRef": {"name": "hermes-browser-token"}}])),
        ("literal TOKEN", lambda: [e for e in bctr["env"] if e["name"] == "TOKEN"][0].__setitem__("value", "abc") or [e for e in bctr["env"] if e["name"] == "TOKEN"][0].pop("valueFrom")),
        ("optional TOKEN", lambda: [e for e in bctr["env"] if e["name"] == "TOKEN"][0]["valueFrom"]["secretKeyRef"].__setitem__("optional", True)),
        ("DEBUG unset (vendor default logs env)", lambda: bctr["env"].remove([e for e in bctr["env"] if e["name"] == "DEBUG"][0])),
        ("DEBUG everything", lambda: [e for e in bctr["env"] if e["name"] == "DEBUG"][0].__setitem__("value", "*")),
        ("DEBUG debug level kept", lambda: [e for e in bctr["env"] if e["name"] == "DEBUG"][0].__setitem__("value", "browserless*,-*:trace")),
        ("DEBUG diagnostics off", lambda: [e for e in bctr["env"] if e["name"] == "DEBUG"][0].__setitem__("value", "-*")),
        ("memory request dropped", lambda: bctr["resources"]["requests"].pop("memory")),
        ("storage limit dropped", lambda: bctr["resources"]["limits"].pop("ephemeral-storage")),
        ("probe with literal bearer", lambda: bctr["readinessProbe"]["exec"]["command"].__setitem__(2, "curl -H 'Authorization: Bearer abc123' http://127.0.0.1:3000/active")),
        ("probe with header literal", lambda: bctr["livenessProbe"].__setitem__("httpGet", {"path": "/active", "port": 3000, "httpHeaders": [{"name": "Authorization", "value": "Bearer x"}]})),
        ("probe with shell tracing", lambda: bctr["startupProbe"]["exec"]["command"].__setitem__(1, "-xc")),
        ("browser Reloader annotation removed", lambda: browser["metadata"]["annotations"].pop("secret.reloader.stakater.com/reload")),
        ("Hermes BROWSER_CDP_URL literal", lambda: [e for e in hctr["env"] if e["name"] == "BROWSER_CDP_URL"][0].update({"value": "ws://x:3000/?token=abc"})),
        ("Hermes reads TOKEN", lambda: hctr["env"].append({"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "hermes-browser-token", "key": "TOKEN"}}})),
        ("Hermes Reloader annotation removed", lambda: hermes["metadata"]["annotations"].pop("secret.reloader.stakater.com/reload")),
        ("token rotates every refresh", lambda: es["spec"].__setitem__("refreshPolicy", "Periodic")),
        ("token from a secret store", lambda: es["spec"].__setitem__("secretStoreRef", {"kind": "ClusterSecretStore", "name": "onepassword-connect"})),
        ("literal token in template", lambda: es["spec"]["target"]["template"]["data"].__setitem__("TOKEN", "abc123")),
        ("CDP URL to another host", lambda: es["spec"]["target"]["template"]["data"].__setitem__("BROWSER_CDP_URL", "ws://hindsight-api.ai.svc.cluster.local:3000/?token={{ .password }}")),
        ("CDP URL via HTTP discovery", lambda: es["spec"]["target"]["template"]["data"].__setitem__("BROWSER_CDP_URL", "http://hermes-browser.ai.svc.cluster.local:3000/?token={{ .password }}")),
        ("extra Secret key", lambda: es["spec"]["target"]["template"]["data"].__setitem__("password", "{{ .password }}")),
        ("weak generator", lambda: get("Password", "hermes-browser-token")["spec"].__setitem__("length", 8)),
        ("symbols in token", lambda: get("Password", "hermes-browser-token")["spec"].__setitem__("symbols", 4)),
        ("Secret manifest in Git", lambda: docs.append({"kind": "Secret", "metadata": {"name": "hermes-browser-token", "namespace": "ai"}, "stringData": {"TOKEN": "abc"}})),
        ("second Service", lambda: docs.append({"kind": "Service", "metadata": {"name": "hermes-browser-2", "namespace": "ai"}, "spec": {"selector": {ISOLATION_KEY: "hermes-browser"}, "ports": [{"port": 3000}]}})),
        ("HTTPRoute to the browser", lambda: docs.append({"kind": "HTTPRoute", "metadata": {"name": "b", "namespace": "ai"}, "spec": {"rules": [{"backendRefs": [{"name": "hermes-browser", "port": 3000}]}]}})),
        # ingress: exactly one Hermes source
        ("second ingress source (another workload)", lambda: (other_workload({"app.kubernetes.io/name": "other"}), ingress["fromEndpoints"].append({"matchLabels": {NS_KEY: "ai", "app.kubernetes.io/name": "other"}}))),
        ("second ingress rule (another workload)", lambda: (other_workload({"app.kubernetes.io/name": "other"}), cnp["spec"]["ingress"].append({"fromEndpoints": [{"matchLabels": {NS_KEY: "ai", "app.kubernetes.io/name": "other"}}], "toPorts": copy.deepcopy(ingress["toPorts"])}))),
        # Hermes browser rule: no additive destination field
        ("Hermes browser rule adds toEntities", lambda: to_browser.__setitem__("toEntities", ["world"])),
        ("Hermes browser rule adds toCIDRSet", lambda: to_browser.__setitem__("toCIDRSet", [{"cidr": "0.0.0.0/0"}])),
        ("Hermes browser rule adds toFQDNs", lambda: to_browser.__setitem__("toFQDNs", [{"matchPattern": "*"}])),
        ("Hermes browser rule adds toServices", lambda: to_browser.__setitem__("toServices", [{"k8sService": {"serviceName": "hindsight-api", "namespace": "ai"}}])),
        # Service: exclusive, pinned to the browser identity, TCP
        ("Service also selects another workload", lambda: (other_workload({"shared": "x"}), bpod["metadata"]["labels"].__setitem__("shared", "x"), svc["spec"].__setitem__("selector", {"shared": "x"}))),
        ("Service selector weaker than the browser identity", lambda: (bpod["metadata"]["labels"].__setitem__("shared", "x"), svc["spec"].__setitem__("selector", {"shared": "x"}))),
        ("Service over UDP", lambda: svc["spec"]["ports"][0].__setitem__("protocol", "UDP")),
        # container-level overrides of the pod hardening
        ("container runs as root", lambda: bctr["securityContext"].__setitem__("runAsUser", 0)),
        ("container disables runAsNonRoot", lambda: bctr["securityContext"].__setitem__("runAsNonRoot", False)),
        ("container seccomp unconfined", lambda: bctr["securityContext"].__setitem__("seccompProfile", {"type": "Unconfined"})),
        # writable volume and mount map
        ("/tmp mount moved onto the application path", lambda: mount("/tmp").__setitem__("mountPath", "/usr/src/app")),
        ("/dev/shm on disk", lambda: volume("shm")["emptyDir"].pop("medium")),
        ("/tmp size limit raised", lambda: volume("tmp")["emptyDir"].__setitem__("sizeLimit", "20Gi")),
        ("extra writable mount", lambda: (bpod["spec"]["volumes"].append({"name": "cache", "emptyDir": {"sizeLimit": "1Gi"}}), bctr["volumeMounts"].append({"name": "cache", "mountPath": "/var/cache"}))),
        ("writable mount made read-only", lambda: mount("/tmp").__setitem__("readOnly", True)),
        # probes: authenticated launch and health routes
        ("no-op probes", lambda: [bctr[p].__setitem__("exec", {"command": ["sh", "-c", "true"]}) for p in PROBE_ROUTES]),
        ("startup probe on the launch-insensitive route", lambda: bctr["startupProbe"]["exec"]["command"].__setitem__(2, probe_cmd.format("-fsS", "/active"))),
        ("readiness probe on an unrelated route", lambda: bctr["readinessProbe"]["exec"]["command"].__setitem__(2, probe_cmd.format("-fsS", "/"))),
        ("probe without the token", lambda: bctr["livenessProbe"]["exec"]["command"].__setitem__(2, "curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:3000/active")),
        ("probe without curl -f", lambda: bctr["readinessProbe"]["exec"]["command"].__setitem__(2, probe_cmd.format("-sS", "/active"))),
        ("token on the curl command line", lambda: bctr["livenessProbe"]["exec"]["command"].__setitem__(2, "curl -fsS -o /dev/null -H \"Authorization: Bearer $TOKEN\" http://127.0.0.1:3000/active")),
        ("unauthenticated httpGet probe", lambda: (bctr["livenessProbe"].pop("exec"), bctr["livenessProbe"].__setitem__("httpGet", {"path": "/active", "port": 3000}))),
        # public IPv4 carve-outs (10.0.0.0/8 is covered above)
        *[(f"reserved range not carved out: {x}", (lambda x: lambda: web["toCIDRSet"][0]["except"].remove(x))(x)) for x in MUST_EXCLUDE if x != "10.0.0.0/8"],
        # direct WebSocket URL: plaintext, no userinfo
        ("CDP URL over TLS", lambda: cdp_url("wss://hermes-browser.ai.svc.cluster.local:3000/?token={{ .password }}")),
        ("CDP URL with userinfo", lambda: cdp_url("ws://user:pass@hermes-browser.ai.svc.cluster.local:3000/?token={{ .password }}")),
        # Secret values bound to the application containers
        ("browser TOKEN moved to a sidecar", lambda: sidecar(bpod, bctr, "TOKEN")),
        ("browser sidecar also reads TOKEN", lambda: sidecar(bpod, bctr, None)),
        ("Hermes BROWSER_CDP_URL moved to a sidecar", lambda: sidecar(hpod, hctr, "BROWSER_CDP_URL")),
        ("Hermes init container reads BROWSER_CDP_URL", lambda: hpod["spec"].setdefault("initContainers", []).append({"name": "side", "env": [copy.deepcopy(env(hctr, "BROWSER_CDP_URL"))]})),
        # public web: both ports
        ("public-web without TCP 443", lambda: web["toPorts"][0]["ports"].remove(web_port("443"))),
        ("public-web without TCP 80", lambda: web["toPorts"][0]["ports"].remove(web_port("80"))),
        # probes: the shell really runs curl and nothing hides its exit status
        ("probe executable replaced by true", lambda: [bctr[p]["exec"]["command"].__setitem__(0, "true") for p in PROBE_ROUTES]),
        ("probe script followed by `; true`", lambda: [bctr[p]["exec"]["command"].__setitem__(2, bctr[p]["exec"]["command"][2].rstrip("\n") + " ; true\n") for p in PROBE_ROUTES]),
        ("probe script followed by `|| true`", lambda: [bctr[p]["exec"]["command"].__setitem__(2, bctr[p]["exec"]["command"][2].rstrip("\n") + " || true\n") for p in PROBE_ROUTES]),
        ("probe script followed by true on a new line", lambda: [bctr[p]["exec"]["command"].__setitem__(2, bctr[p]["exec"]["command"][2] + "true\n") for p in PROBE_ROUTES]),
        ("probe curl piped into true without spaces", lambda: [bctr[p]["exec"]["command"].__setitem__(2, bctr[p]["exec"]["command"][2].replace("curl -fsS", "curl -fsS|true", 1)) for p in PROBE_ROUTES]),
        # probes: curl reports exactly the one authenticated transfer
        ("probe curl given a second URL", lambda: [curl_append(p, " file:///dev/null") for p in PROBE_ROUTES]),
        ("probe curl given --next and a second URL", lambda: [curl_append(p, " --next file:///dev/null") for p in PROBE_ROUTES]),
        ("probe curl given --no-fail after -f", lambda: [curl_append(p, " --no-fail") for p in PROBE_ROUTES]),
        ("probe curl given --help", lambda: [curl_append(p, " --help") for p in PROBE_ROUTES]),
        ("probe curl given --version", lambda: [curl_append(p, " --version") for p in PROBE_ROUTES]),
        ("probe curl given a second config file", lambda: [curl_append(p, " -K /dev/null") for p in PROBE_ROUTES]),
    ]
    return cases


def main(argv):
    self_test = "--self-test" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    docs = load(paths)
    errors = validate(docs, "ai")
    if errors:
        print("FAIL")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PASS: browser isolation boundary holds across {len(docs)} rendered objects")
    if not self_test:
        return 0
    bad = 0
    for i in range(len(mutations(copy.deepcopy(docs)))):
        fresh = copy.deepcopy(docs)
        label, mutate = mutations(fresh)[i]
        mutate()
        errs = validate(fresh, "ai")
        status = "rejected" if errs else "ACCEPTED"
        bad += not errs
        print(f"  {status}: {label}" + (f" -> {errs[0]}" if errs else ""))
    if bad:
        print(f"FAIL: {bad} bad outcome(s) were not rejected")
        return 1
    print("PASS: every bad outcome was rejected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
