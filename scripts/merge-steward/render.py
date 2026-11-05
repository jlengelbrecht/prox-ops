"""Render stage: classify the rendered object diff of a PR against an allowlist.

Input is `flux-local diff {helmrelease,kustomization} --output json --unified 100000`
for the PR merge ref against its base. The huge context makes each object's diff carry
the whole object, so the before and after documents can be rebuilt and compared field by
field. Only these changes may pass:

- a pod-template container image on a Deployment, DaemonSet, StatefulSet or ReplicaSet:
  same repository, a non-major tag change (or a new digest on the same tag), and no
  version skew against an unchanged app.kubernetes.io/version label
- ConfigMap `data`
- HelmRelease `spec.chart.spec.version`, non-major
- HelmRelease `spec.values`, but only when flux-local rendered that release, so the
  effect of the values is classified object by object
- OCIRepository `spec.ref.tag`, non-major
- on the one OCIRepository a policy release exception names: `spec.ref.tag` and
  `spec.ref.digest`, where a 0.x minor and a prerelease also pass
- the value of a version label: `app.kubernetes.io/version`, `app.kubernetes.io/managed-by`
  or `helm.sh/chart` on the object, `app.kubernetes.io/version` on a pod template.
  Annotations never pass: external-dns hostnames and Cilium LB IPs live there.

Everything else is needs-human: any other field, any other kind, and any added or
removed object. A parse problem or a flux-local failure is needs-human too.
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

from classify import HOLD, NEEDS, PASS, Stage
from fold import clean

WORKLOADS = {"Deployment", "DaemonSet", "StatefulSet", "ReplicaSet"}
# Reason names for kinds and fields that have their own label; anything else is "changed".
KIND_FLAGS = {
    "CustomResourceDefinition": "CRD", "PersistentVolumeClaim": "PVC",
    "MutatingWebhookConfiguration": "webhook", "ValidatingWebhookConfiguration": "webhook",
    "ValidatingAdmissionPolicy": "webhook", "ValidatingAdmissionPolicyBinding": "webhook",
    "ClusterRole": "RBAC", "ClusterRoleBinding": "RBAC", "Role": "RBAC", "RoleBinding": "RBAC",
    "ServiceAccount": "RBAC",
    "NetworkPolicy": "network policy", "CiliumNetworkPolicy": "network policy",
    "CiliumClusterwideNetworkPolicy": "network policy", "CiliumEgressGatewayPolicy": "network policy",
    "CiliumLoadBalancerIPPool": "network policy", "CiliumL2AnnouncementPolicy": "network policy",
    "ExternalSecret": "external secret", "ClusterExternalSecret": "external secret",
    "SecretStore": "external secret", "ClusterSecretStore": "external secret",
    "Gateway": "routing", "HTTPRoute": "routing", "Namespace": "namespace",
    "Job": "Job template", "CronJob": "CronJob template",
}
# Pod-spec fields that widen what a pod can reach on its node or in the API.
PRIVILEGED = {"hostNetwork", "hostPID", "hostIPC", "hostUsers", "securityContext", "privileged", "capabilities",
              "hostPath", "hostPort", "allowPrivilegeEscalation", "procMount"}
SERVICE_ACCOUNT = {"serviceAccountName", "serviceAccount", "automountServiceAccountToken"}
# Lower index = reported first when several flags apply.
PRIORITY = ["render", "CRD", "webhook", "removed", "added", "privileged pod spec", "service account", "RBAC",
            "network policy", "external secret", "CNPG cluster", "PVC", "Job template", "CronJob template",
            "StatefulSet", "major", "version", "version skew", "image", "replicas", "routing", "namespace", "values",
            "changed"]
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_RELEASE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(-)?")
_RELEASE_FIELDS = (("spec", "ref", "tag"), ("spec", "ref", "digest"))
_MISSING = object()
STALE = "the PR merge ref was stale; re-run the render (pull_request_target run)"
# Labels whose value may change silently. Other labels feed Service and policy selectors.
OBJECT_LABELS = {"app.kubernetes.io/version", "app.kubernetes.io/managed-by", "helm.sh/chart"}
TEMPLATE_LABELS = {"app.kubernetes.io/version"}


def rebuild(diff):
    """Return (before, after) documents; None for a side that is empty (added or removed)."""
    before, after, in_hunk = [], [], False
    for line in diff.splitlines():
        if line.startswith("@@"):
            m = _HUNK.match(line)
            if not m or not (m.group(1) in ("0", "1") and m.group(3) in ("0", "1")):
                raise ValueError("diff does not carry the whole object")
            if in_hunk:
                raise ValueError("diff has more than one hunk")
            in_hunk = True
        elif not in_hunk or line == "":
            continue
        elif line[0] == " ":
            before.append(line[1:]), after.append(line[1:])
        elif line[0] == "-":
            before.append(line[1:])
        elif line[0] == "+":
            after.append(line[1:])
        else:
            raise ValueError("unexpected diff line")
    if not in_hunk:
        raise ValueError("diff has no hunk")
    load = lambda lines: yaml.safe_load("\n".join(lines)) if lines else None  # noqa: E731
    return load(before), load(after)


def changes(before, after, path=()):
    """Yield (path, old, new) for every leaf that differs. A missing side is _MISSING;
    lists of different lengths count as one change at the list."""
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            yield from changes(before.get(key, _MISSING), after.get(key, _MISSING), path + (key,))
    elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        for i, (b, a) in enumerate(zip(before, after)):
            yield from changes(b, a, path + (i,))
    elif before != after:
        yield path, before, after


def _version(tag):
    m = _SEMVER.match(str(tag or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _major_change(old, new):
    """None when both parse and the change is not major (a 0.x minor counts as major),
    else a reason."""
    a, b = _version(old), _version(new)
    if not a or not b:
        return "version"  # not a version we can compare
    if a[0] != b[0] or (a[0] == 0 and a[1] != b[1]):
        return "major"
    return None


def _split_image(image):
    image = str(image)
    name, _, digest = image.partition("@")
    if ":" in name.rsplit("/", 1)[-1]:
        repo, tag = name.rsplit(":", 1)
    else:
        repo, tag = name, ""
    return repo, tag, digest


def _app_version(doc):
    return (((doc or {}).get("metadata") or {}).get("labels") or {}).get("app.kubernetes.io/version")


def _image_flag(before, after, old, new):
    old_repo, old_tag, old_digest = _split_image(old)
    new_repo, new_tag, new_digest = _split_image(new)
    if old_repo != new_repo:
        return "image"
    if old_digest and not new_digest:
        return "image"  # the digest pin was dropped
    if old_tag == new_tag:
        # Same tag: a new digest on both sides, or a digest added.
        return None if new_digest and old_digest != new_digest else "image"
    flag = _major_change(old_tag, new_tag)
    if flag:
        return flag
    # The PR moves the image away from an unchanged chart version label that it did match.
    label = _app_version(after) if _app_version(before) == _app_version(after) else None
    if label and _version(label) == _version(old_tag) and _version(label) != _version(new_tag):
        return "version skew"
    return None


def _metadata_churn(path, old, new):
    """The value of an allowlisted version label changed (never an annotation)."""
    if old is _MISSING or new is _MISSING:
        return False
    if len(path) == 3 and path[:2] == ("metadata", "labels"):
        return path[2] in OBJECT_LABELS
    if len(path) == 5 and path[:4] == ("spec", "template", "metadata", "labels"):
        return path[4] in TEMPLATE_LABELS
    return False


def _keys(value):
    """Every mapping key inside value, at any depth."""
    if isinstance(value, dict):
        return {str(k) for k in value} | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


def _pod_flag(path, new):
    """privileged pod spec or service account when the change touches one of those fields."""
    keys = {str(k) for k in path} | _keys(new)
    if keys & PRIVILEGED:
        return "privileged pod spec"
    if keys & SERVICE_ACCOUNT:
        return "service account"
    return None


def _release_exception(before, after, exceptions):
    """The exception naming this exact object, on both sides of the diff, or None."""
    for rule in exceptions:
        want = rule["oci_repository"]
        if all((doc.get("metadata") or {}).get("namespace") == want["namespace"]
               and (doc.get("metadata") or {}).get("name") == want["name"]
               and (doc.get("spec") or {}).get("url") == want["url"] for doc in (before, after)):
            return rule
    return None


def _release_order(tag):
    """Sort key for a release tag: a prerelease comes before its final release."""
    m = _RELEASE.match(str(tag))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) is None) if m else None


def _release_flag(rule, path, old, new):
    """Tag and digest of an excepted release: well formed, same major, and not going backwards."""
    if path[-1] == "digest":
        return None if all(_DIGEST.fullmatch(str(v)) for v in (old, new)) else "changed"
    if not all(re.fullmatch(rule["tag_pattern"], str(v)) for v in (old, new)):
        return "version"
    a, b = _release_order(old), _release_order(new)
    if not a or not b:
        return "version"
    if a[0] != b[0]:
        return "major"
    return "version" if b < a else None


def field_flag(kind, path, old, new, before, after, rendered, exceptions=()):
    """None when the change is on the allowlist, else the reason flag."""
    if _metadata_churn(path, old, new):
        return None
    if kind in WORKLOADS:
        if (len(path) == 6 and path[:4] == ("spec", "template", "spec", "containers") or
                len(path) == 6 and path[:4] == ("spec", "template", "spec", "initContainers")) and path[5] == "image":
            if old is _MISSING or new is _MISSING:
                return "image"
            return _image_flag(before, after, old, new)
        if path[:3] == ("spec", "template", "spec") and len(path) > 3:
            return _pod_flag(path, new) or ("StatefulSet" if kind == "StatefulSet" else "changed")
        if path[:2] == ("spec", "replicas"):
            return "replicas"
        return "StatefulSet" if kind == "StatefulSet" else "changed"
    if kind == "ConfigMap" and path[:1] == ("data",):
        return None
    if kind == "HelmRelease" and path == ("spec", "chart", "spec", "version"):
        return _major_change(old, new)
    if kind == "HelmRelease" and path[:2] == ("spec", "values"):
        return None if rendered else "values"
    if kind == "OCIRepository" and path in _RELEASE_FIELDS:
        rule = _release_exception(before, after, exceptions)
        if rule:
            return _release_flag(rule, path, old, new)
    if kind == "OCIRepository" and path == ("spec", "ref", "tag"):
        return _major_change(old, new)
    if kind == "Cluster" and str((after or {}).get("apiVersion", "")).startswith("postgresql.cnpg.io/"):
        return "CNPG cluster"
    return KIND_FLAGS.get(kind, "changed")


def object_flags(kind, before, after, rendered=False, exceptions=()):
    """Flags for one object. rendered: flux-local rendered this HelmRelease's output."""
    if before is None and after is None:
        return []
    if before is None:
        return ["added"]
    if after is None:
        return ["removed"]
    flags = []
    for path, old, new in changes(before, after):
        flag = field_flag(kind, path, old, new, before, after, rendered, exceptions)
        if flag:
            flags.append(flag if flag != "changed" else f"changed {'.'.join(map(str, path))[:60]}")
    return flags


def _rank(flag):
    flag = "changed" if flag.startswith("changed ") else flag
    return PRIORITY.index(flag) if flag in PRIORITY else len(PRIORITY)


def classify(*reports, exceptions=()):
    """reports: parsed flux-local JSON lists. Returns (Stage, Counter of changed kinds).

    exceptions: the policy's release_exceptions. Without them no release exception applies.
    """
    found, counts = _scan(reports, exceptions)
    if not found:
        changed = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no rendered changes"
        return Stage("render", PASS, changed), counts
    flag, obj = min(found, key=lambda f: _rank(f[0]))
    where = f"{obj['kind']} {obj.get('namespace') or '-'}/{obj['name']}"
    others = len({(f, o["kind"], o["name"]) for f, o in found}) - 1
    more = f" (+{others} more)" if others else ""
    return Stage("render", NEEDS, f"{flag}: {where}{more}"), counts


def _scan(reports, exceptions=()):
    """Returns (flags found as (flag, object) pairs, Counter of changed kinds)."""
    found, counts = [], Counter()
    # HelmReleases whose rendered output flux-local diffed (the helmrelease report).
    rendered = {(r.get("namespace"), r.get("name")) for report in reports for r in report
                if r.get("kind") == "HelmRelease" and r.get("diffs")}
    for report in reports:
        for release in report:
            for obj in release["diffs"]:
                kind = obj["kind"]
                counts[kind] += 1
                if kind == "CustomResourceDefinition":
                    found.append(("CRD", obj))  # flagged by kind; the body is not needed
                    continue
                before, after = rebuild(obj["diff"])
                was_rendered = (obj.get("namespace"), obj["name"]) in rendered
                found += [(flag, obj) for flag in object_flags(kind, before, after, was_rendered, exceptions)]
    return found, counts


def from_artifact(directory, head_sha, exceptions=()):
    """Classify the render job's artifact for head_sha.

    Returns None when the artifact belongs to another commit (a newer push superseded
    it), so the caller leaves the verdict to the newer run. Any other problem is
    needs-human.
    """
    try:
        meta = json.loads((Path(directory) / "meta.json").read_text())
    except (OSError, ValueError):
        return Stage("render", NEEDS, "render evidence unavailable")
    if meta.get("head_sha") != head_sha:
        return None
    if meta.get("stale") is True:
        # GitHub had not rebuilt the merge ref for this head within the render job's wait.
        return Stage("render", HOLD, STALE)
    if meta.get("failed") != 0:
        return Stage("render", NEEDS, "render failed (flux-local exited non-zero)")
    try:
        # flux-local writes an empty file, not an empty list, when nothing rendered differently.
        texts = [(Path(directory) / f"{name}.json").read_text() for name in ("helmrelease", "kustomization")]
        reports = [json.loads(text) if text.strip() else [] for text in texts]
        return classify(*reports, exceptions=exceptions)[0]
    except Exception as err:  # fail closed
        return Stage("render", NEEDS, f"render failed ({type(err).__name__})")


def object_lines(directory, exceptions=(), limit=40):
    """One line per flag found in the artifact, for the AI review's context. Empty on any problem."""
    try:
        texts = [(Path(directory) / f"{name}.json").read_text() for name in ("helmrelease", "kustomization")]
        found, counts = _scan([json.loads(text) if text.strip() else [] for text in texts], exceptions)
    except Exception:  # the review goes on without it; the verdict comes from the render stage
        return []
    lines = [f"flagged: {flag}: {obj['kind']} {obj.get('namespace') or '-'}/{obj['name']}" for flag, obj in found]
    return lines[:limit] + [f"changed: {n} {k}" for k, n in sorted(counts.items())]


def main(argv=None):
    """Classify flux-local JSON reports locally and print the result."""
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", help="flux-local JSON files")
    parser.add_argument("--failed", action="store_true", help="flux-local exited non-zero")
    args = parser.parse_args(argv)
    try:
        if args.failed:
            raise RuntimeError("flux-local diff failed")
        reports = []
        for path in args.reports:
            with open(path) as handle:
                reports.append(json.load(handle))
        stage, counts = classify(*reports)
    except Exception as err:  # fail closed
        stage, counts = Stage("render", NEEDS, f"render failed ({type(err).__name__})"), Counter()
    print(json.dumps({"verdict": stage.verdict, "reason": clean(stage.reason), "counts": dict(counts)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
