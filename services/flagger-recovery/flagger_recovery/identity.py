"""Candidate identity resolution for the Flagger recovery pilot.

Phase is not state (canary-matrix.md F8): a Git revert to the last promoted
spec runs no analysis and leaves the Canary in ``Failed`` forever, because
Flagger refuses to start an analysis once the new template hash equals
``status.lastPromotedSpec`` (``pkg/canary/spec.go``, v1.45.0). Recovery
automation must judge a release by its recorded identity and live serving
state, never by ``status.phase`` alone.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Mapping, Optional, Sequence

_DIGEST_RE = re.compile(r"^(?:docker-pullable://|docker://)?(?P<repo>[^@]+)@sha256:(?P<hex>[0-9a-f]{64})$")
_REVISION_RE = re.compile(r"^(?P<branch>[^@]+)@sha1:(?P<sha>[0-9a-f]{40})$")
_PR_RE = re.compile(r"\(#(?P<number>\d+)\)\s*$")

class AttributionRefused(Exception):
    """Raised instead of guessing when an input to ``resolve()`` is ambiguous
    or missing (no candidate pods yet, tag-only ``imageID``, two ReplicaSets
    sharing a hash, a Canary with no ``lastAppliedSpec``)."""

@dataclasses.dataclass(frozen=True)
class ContainerImage:
    name: str
    repository: str
    tag: Optional[str]
    digest: str  # "repo@sha256:<hex>" — never the tag

@dataclasses.dataclass(frozen=True)
class CandidateIdentity:
    namespace: str
    canary_name: str
    deployment_uid: str
    template_hash: str  # Canary.status.lastAppliedSpec — Flagger's own hash of the pod
    # template. NEVER compare this to replicaset_hash: the two are computed by different
    # hashers (Flagger's vs. the Deployment controller's) and never coincide, even for the
    # same template.
    replicaset_hash: str  # the selected candidate ReplicaSet's pod-template-hash label
    images: tuple[ContainerImage, ...]
    chart_name: str
    chart_version: str
    oci_digest: str
    source_branch: str
    source_sha: str
    last_promoted_spec: Optional[str]
    is_promoted: bool
    pr_number: Optional[int]
    sources: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateIdentity":
        fields = dict(data)
        fields["images"] = tuple(ContainerImage(**image) for image in data["images"])
        fields["sources"] = dict(data["sources"])
        return cls(**fields)

def _controller_uid(obj: Mapping[str, Any]) -> Optional[str]:
    for ref in obj.get("metadata", {}).get("ownerReferences") or ():
        if ref.get("controller"):
            return ref.get("uid")
    return None

def _parse_image_id(image_id: str) -> Optional[tuple[str, str]]:
    match = _DIGEST_RE.match(image_id or "")
    if not match:
        return None
    repo = match.group("repo")
    return repo, f"{repo}@sha256:{match.group('hex')}"

def _parse_tag(image: str) -> Optional[str]:
    # Strip any "@sha256:<hex>" digest suffix first, then only treat a colon
    # as a tag separator when it falls after the final "/" — otherwise a
    # registry port (e.g. "registry:5000/repo") is mistaken for a tag.
    image_name = (image or "").split("@", 1)[0]
    colon = image_name.rfind(":")
    slash = image_name.rfind("/")
    return image_name[colon + 1 :] if colon > slash else None

def _resolve_images(candidate_pods: Sequence[Mapping[str, Any]]) -> tuple[ContainerImage, ...]:
    seen: dict[tuple[str, str], ContainerImage] = {}
    for pod in candidate_pods:
        statuses = pod.get("status", {}).get("containerStatuses") or []
        if not statuses:
            name = pod.get("metadata", {}).get("name", "<unknown>")
            raise AttributionRefused(f"candidate pod {name} has no containerStatuses yet")
        for status in statuses:
            container_name = status.get("name", "<unknown>")
            image_id = status.get("imageID", "")
            parsed = _parse_image_id(image_id)
            if parsed is None:
                raise AttributionRefused(
                    f"container {container_name!r} imageID is not a resolvable digest reference: {image_id!r}"
                )
            repository, digest_ref = parsed
            tag = _parse_tag(status.get("image", ""))
            seen[(container_name, digest_ref)] = ContainerImage(
                name=container_name, repository=repository, tag=tag, digest=digest_ref
            )
    return tuple(sorted(seen.values(), key=lambda image: (image.name, image.repository, image.digest)))

def _resolve_chart(helmrelease: Mapping[str, Any]) -> tuple[str, str]:
    history = helmrelease.get("status", {}).get("history") or []
    deployed = next((entry for entry in history if entry.get("status") == "deployed"), None)
    if deployed is None:
        raise AttributionRefused("HelmRelease status.history has no entry with status 'deployed'")
    chart_name = deployed.get("chartName")
    chart_version = deployed.get("chartVersion")
    if not chart_name or not chart_version:
        raise AttributionRefused("HelmRelease deployed history entry is missing chartName/chartVersion")
    return chart_name, chart_version

def _resolve_oci_digest(ocirepository: Mapping[str, Any]) -> str:
    digest = ocirepository.get("status", {}).get("artifact", {}).get("digest")
    if not digest:
        raise AttributionRefused("OCIRepository has no status.artifact.digest")
    return digest

def revision_sha(value: Any) -> Optional[str]:
    """The commit sha inside a Flux ``<branch>@sha1:<sha>`` revision, or ``None`` for
    anything else. The one parser of that field, so a reader that only needs the sha
    (``kube.LiveKustomization``) and the resolver below can never disagree about the
    same string — and never ``""``, which a caller comparing shas could read as a match."""
    match = _REVISION_RE.match(str(value or ""))
    return match.group("sha") if match else None

def _resolve_revision(kustomization: Mapping[str, Any]) -> tuple[str, str]:
    revision = kustomization.get("status", {}).get("lastAppliedRevision")
    match = _REVISION_RE.match(revision or "")
    if not match:
        raise AttributionRefused(
            f"Kustomization status.lastAppliedRevision is missing or malformed: {revision!r}"
        )
    return match.group("branch"), match.group("sha")

def _resolve_pr_number(commit_message: Optional[str]) -> Optional[int]:
    if not commit_message or not commit_message.strip():
        return None
    subject = commit_message.strip().splitlines()[0]
    match = _PR_RE.search(subject)
    return int(match.group("number")) if match else None

def resolve(
    *,
    canary: Mapping[str, Any],
    deployment: Mapping[str, Any],
    candidate_pods: Sequence[Mapping[str, Any]],
    helmrelease: Mapping[str, Any],
    ocirepository: Mapping[str, Any],
    kustomization: Mapping[str, Any],
    candidate_replicasets: Sequence[Mapping[str, Any]] = (),
    commit_message: Optional[str] = None,
) -> CandidateIdentity:
    """Resolve an immutable identity from live object dicts, or raise
    ``AttributionRefused``. Every returned field's provenance is recorded in
    ``sources``."""
    namespace = canary["metadata"]["namespace"]
    canary_name = canary["metadata"]["name"]
    deployment_uid = deployment["metadata"]["uid"]

    template_hash = canary.get("status", {}).get("lastAppliedSpec")
    if not template_hash:
        raise AttributionRefused("Canary status.lastAppliedSpec is missing; cannot fix a template hash")

    # Flagger's lastAppliedSpec hash and the Kubernetes pod-template-hash label are computed
    # by two different hashers and never coincide, even for the same pod template — so the
    # candidate ReplicaSet cannot be found by comparing them. Instead select the ReplicaSet
    # whose deployment.kubernetes.io/revision annotation matches the Deployment's own, which
    # is how the Deployment controller itself tracks "the ReplicaSet for my current spec".
    deployment_revision = deployment.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision")
    if not deployment_revision:
        raise AttributionRefused(
            f"Deployment {deployment_uid} has no deployment.kubernetes.io/revision annotation; "
            "cannot select a candidate ReplicaSet"
        )

    matching_replicasets = [
        rs
        for rs in candidate_replicasets
        if _controller_uid(rs) == deployment_uid
        and rs.get("metadata", {}).get("annotations", {}).get("deployment.kubernetes.io/revision") == deployment_revision
    ]
    if len(matching_replicasets) > 1:
        raise AttributionRefused(
            f"{len(matching_replicasets)} ReplicaSets owned by Deployment {deployment_uid} "
            f"carry deployment.kubernetes.io/revision {deployment_revision!r}; refusing to disambiguate the candidate"
        )
    if not matching_replicasets:
        raise AttributionRefused(
            f"no ReplicaSet owned by Deployment {deployment_uid} carries deployment.kubernetes.io/revision "
            f"{deployment_revision!r}"
        )

    candidate_rs = matching_replicasets[0]
    candidate_rs_uid = candidate_rs["metadata"]["uid"]
    replicaset_hash = candidate_rs.get("metadata", {}).get("labels", {}).get("pod-template-hash")
    if not replicaset_hash:
        raise AttributionRefused(f"candidate ReplicaSet {candidate_rs_uid} has no pod-template-hash label")

    candidate_pods = [pod for pod in candidate_pods if _controller_uid(pod) == candidate_rs_uid]
    if not candidate_pods:
        raise AttributionRefused("no candidate pods observed yet; cannot read an image digest")

    images = _resolve_images(candidate_pods)
    chart_name, chart_version = _resolve_chart(helmrelease)
    oci_digest = _resolve_oci_digest(ocirepository)
    source_branch, source_sha = _resolve_revision(kustomization)

    last_promoted_spec = canary.get("status", {}).get("lastPromotedSpec") or None
    is_promoted = last_promoted_spec is not None and last_promoted_spec == template_hash
    pr_number = _resolve_pr_number(commit_message)

    sources = {
        "namespace": "Canary.metadata.namespace",
        "canary_name": "Canary.metadata.name",
        "deployment_uid": "Deployment.metadata.uid",
        "template_hash": "Canary.status.lastAppliedSpec",
        "replicaset_hash": "ReplicaSet.metadata.labels['pod-template-hash'] "
        "(ReplicaSet selected by matching deployment.kubernetes.io/revision)",
        "images": "Pod.status.containerStatuses[].imageID",
        "chart_name": "HelmRelease.status.history[].chartName",
        "chart_version": "HelmRelease.status.history[].chartVersion",
        "oci_digest": "OCIRepository.status.artifact.digest",
        "source_branch": "Kustomization.status.lastAppliedRevision",
        "source_sha": "Kustomization.status.lastAppliedRevision",
        "last_promoted_spec": "Canary.status.lastPromotedSpec",
        "is_promoted": "derived: template_hash == last_promoted_spec",
        "pr_number": "commit_message" if commit_message else "not supplied",
    }

    return CandidateIdentity(
        namespace=namespace,
        canary_name=canary_name,
        deployment_uid=deployment_uid,
        template_hash=template_hash,
        replicaset_hash=replicaset_hash,
        images=images,
        chart_name=chart_name,
        chart_version=chart_version,
        oci_digest=oci_digest,
        source_branch=source_branch,
        source_sha=source_sha,
        last_promoted_spec=last_promoted_spec,
        is_promoted=is_promoted,
        pr_number=pr_number,
        sources=sources,
    )

def is_manual_rollback(canary_status: Mapping[str, Any], new_template_hash: str) -> bool:
    """True exactly when ``new_template_hash`` equals ``lastPromotedSpec`` (F8):
    that condition is why phase must not be used as the recovery signal."""
    return canary_status.get("lastPromotedSpec") == new_template_hash

def _main(argv: Optional[Sequence[str]] = None) -> None:
    """Read-only smoke: print the resolved identity for a live Canary. No writes."""
    import argparse
    import json

    from .kube import ApiReader, CandidateReader

    parser = argparse.ArgumentParser(description=_main.__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. http://127.0.0.1:8001 from kubectl proxy")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--canary", required=True)
    parser.add_argument("--token")
    parser.add_argument("--ca-file")
    args = parser.parse_args(argv)

    reader = CandidateReader(ApiReader(args.base_url, token=args.token, ca_file=args.ca_file))
    identity = resolve(**reader.read(args.namespace, args.canary))
    print(json.dumps(identity.to_dict(), indent=2, sort_keys=True))

if __name__ == "__main__":
    _main()
