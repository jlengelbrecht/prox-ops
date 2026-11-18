"""Pair two full flate renders into the list of objects that differ.

Input is `flate build all -o json` for the base and for the PR merge ref: every object
the cluster would hold on each side. Both sides are rendered in full, so nothing depends
on a tool deciding which objects a change can reach. `flate build hr -o json` for each
side says which of those objects a HelmRelease rendered. Output is one JSON document:

    {"base": <object count>, "head": <object count>, "changes": [...]}

Each change carries the whole object on both sides (`before`, `after`; null when the
object is absent on that side) and `release`, the [namespace, name] of the HelmRelease
that rendered it, or null. render.py classifies the changes.

The release labels are honoured only on an object that is in the HelmRelease render.
A manifest can carry those labels too, and a hand-written object must not be able to
claim it is the output of a release.

This runs in the render job, which holds no secrets, from the base checkout. It uses
only the standard library. Anything it cannot read is an error, never an empty result.
"""

import json
import sys

# Rendered noise that changes with every chart bump. render.py still sees
# app.kubernetes.io/version, which it needs to spot version skew.
STRIP = ("helm.sh/chart", "checksum/config")
RELEASE_NAMESPACE = "helm.toolkit.fluxcd.io/namespace"
RELEASE_NAME = "helm.toolkit.fluxcd.io/name"
# A CRD is flagged by kind alone; its schema is large and is not needed.
BODYLESS = {"CustomResourceDefinition"}


def identity(doc):
    """(API group, kind, namespace, name). Raises ValueError for anything that is not an object."""
    if not isinstance(doc, dict):
        raise ValueError("render output holds something that is not an object")
    meta = doc.get("metadata")
    kind, api = doc.get("kind"), doc.get("apiVersion")
    if not isinstance(meta, dict) or not isinstance(kind, str) or not isinstance(api, str):
        raise ValueError("render output holds an object without kind, apiVersion or metadata")
    name, namespace = meta.get("name"), meta.get("namespace")
    if not isinstance(name, str) or not name or not isinstance(namespace, (str, type(None))):
        raise ValueError("render output holds an object without a usable name")
    return api.rpartition("/")[0], kind, namespace or "", name


def _metadata(doc):
    """The object's metadata and its pod template's, where they are mappings."""
    found = [doc.get("metadata")]
    spec = doc.get("spec")
    template = spec.get("template") if isinstance(spec, dict) else None
    if isinstance(template, dict):
        found.append(template.get("metadata"))
    return [meta for meta in found if isinstance(meta, dict)]


def strip(doc):
    """Drop the STRIP keys from labels and annotations, in place."""
    for meta in _metadata(doc):
        for field in ("labels", "annotations"):
            values = meta.get(field)
            if isinstance(values, dict):
                for key in STRIP:
                    values.pop(key, None)
    return doc


def release(doc, helm):
    """[namespace, name] of the HelmRelease that rendered doc, or None.

    helm: the canonical form of every object in that side's HelmRelease render."""
    if doc is None or _canonical([doc])[0] not in helm:
        return None
    labels = (doc.get("metadata") or {}).get("labels")
    if not isinstance(labels, dict):
        return None
    namespace, name = labels.get(RELEASE_NAMESPACE), labels.get(RELEASE_NAME)
    return [namespace, name] if isinstance(namespace, str) and isinstance(name, str) else None


def _group(docs):
    if not isinstance(docs, list) or not docs:
        raise ValueError("render output is not a non-empty list of objects")
    grouped = {}
    for doc in docs:
        grouped.setdefault(identity(doc), []).append(strip(doc))
    return grouped


def _canonical(docs):
    return sorted(json.dumps(doc, sort_keys=True) for doc in docs)


def _helm(docs):
    """Canonical forms of a HelmRelease render. It may be empty; it must be a list of objects."""
    if not isinstance(docs, list):
        raise ValueError("HelmRelease render is not a list of objects")
    for doc in docs:
        identity(doc)
    return set(_canonical([strip(doc) for doc in docs]))


def pair(base, head, base_helm, head_helm):
    """Return the changes between two renders, sorted by object identity.

    base_helm, head_helm: the HelmRelease-only render of each side."""
    before_by, after_by = _group(base), _group(head)
    before_helm, after_helm = _helm(base_helm), _helm(head_helm)
    changes = []
    for key in sorted(set(before_by) | set(after_by)):
        befores, afters = before_by.get(key, []), after_by.get(key, [])
        if _canonical(befores) == _canonical(afters):
            continue
        before = befores[0] if befores else None
        after = afters[0] if afters else None
        group, kind, namespace, name = key
        change = {
            "apiVersion": (after or before)["apiVersion"], "kind": kind,
            "namespace": namespace or None, "name": name,
            # The base side speaks only for an object that is gone. An object still present
            # is attributed by what rendered it now.
            "release": release(after, after_helm) if after is not None else release(before, before_helm),
            "before": before, "after": after,
        }
        if len(befores) > 1 or len(afters) > 1:
            # Two producers render the same object and at least one copy changed. Which
            # copy the cluster ends up with is not something a field comparison can say.
            change["ambiguous"] = True
        if kind in BODYLESS:
            change["before"] = None if before is None else {}
            change["after"] = None if after is None else {}
        changes.append(change)
    return {"base": len(base), "head": len(head), "changes": changes}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 4:
        print("usage: render_pairs.py BASE.json HEAD.json BASE_HR.json HEAD_HR.json", file=sys.stderr)
        return 2
    try:
        renders = []
        for path in args:
            with open(path) as handle:
                renders.append(json.load(handle))
        result = pair(*renders)
    except Exception as err:  # fail closed: the caller records a failed render
        print(f"render pairing failed ({type(err).__name__})", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
