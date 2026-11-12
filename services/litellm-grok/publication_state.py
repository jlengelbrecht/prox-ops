#!/usr/bin/env python3
"""Fail-closed decisions for an already published GHCR image."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SOURCE_PATHS = (
    "services/litellm-grok",
    "tests/litellm/grok_pinned_image_integration.py",
    "tests/litellm/grok_pinned_image_bootstrap.py",
    "tests/litellm/run_grok_pinned_image.py",
    "tests/litellm/test_grok_image_package.py",
    "tests/litellm/test_grok_publication.py",
    "kubernetes/apps/ai/litellm/app/callbacks/project_grok_native.py",
    ".github/workflows/litellm-grok.yaml",
)


class Denied(Exception):
    pass


def _load(path):
    """Read required JSON evidence, denying missing or malformed input."""
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise Denied("missing or invalid publication evidence") from None


def qualified(image, digest, inspection, receipt, revision, source):
    """Require the exact pulled digest, source labels, and passing runner receipt."""
    ref = f"{image}@{digest}"
    if DIGEST.fullmatch(digest) is None or SHA.fullmatch(revision) is None:
        raise Denied("invalid digest or revision")
    if (not isinstance(inspection, list) or len(inspection) != 1
            or not isinstance(inspection[0], dict) or not isinstance(receipt, dict)):
        raise Denied("invalid image inspection")
    info = inspection[0]
    config = info.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        raise Denied("missing image source labels")
    refs = info.get("RepoDigests")
    if (not isinstance(refs, list) or ref not in refs
            or labels.get("org.opencontainers.image.revision") != revision
            or labels.get("org.opencontainers.image.source") != source):
        raise Denied("published digest source provenance mismatch")
    checks = receipt.get("checks")
    if (receipt.get("status") != "pass"
            or receipt.get("published_digest") != ref
            or receipt.get("reason") is not None
            or receipt.get("child_exit_zero") is not True
            or receipt.get("nonalias_paired_equal") is not True
            or receipt.get("generic_denial_paired_equal") is not True
            or not isinstance(checks, dict) or len(checks) != 35
            or any(value != "pass" for value in checks.values())):
        raise Denied("published digest is not qualified")
    return ref


def decide(state, image, candidate_digest, candidate_inspection, candidate_receipt,
           revision, source, existing_inspection=None, existing_receipt=None):
    """Choose create or reuse; rebuilding the same source need not reproduce a digest."""
    qualified(image, candidate_digest, candidate_inspection, candidate_receipt,
              revision, source)
    if state == {"state": "absent", "digest": None}:
        return {"action": "create", "digest": candidate_digest}
    if (not isinstance(state, dict) or set(state) != {"state", "digest"}
            or state.get("state") != "present"
            or not isinstance(state.get("digest"), str)
            or DIGEST.fullmatch(state["digest"]) is None):
        raise Denied("ambiguous commit tag state")
    existing = state["digest"]
    qualified(image, existing, existing_inspection, existing_receipt, revision, source)
    return {"action": "reuse", "digest": existing}


def create_ready(decision, current_state, candidate_digest):
    """Stop if a tag appeared after the qualified create decision."""
    if (DIGEST.fullmatch(candidate_digest) is None
            or decision != {"action": "create", "digest": candidate_digest}
            or current_state != {"state": "absent", "digest": None}):
        raise Denied("commit tag changed before create; do not overwrite")


def source_fresh(revision, runner=subprocess.run):
    """Distinguish changed source from a failed git comparison."""
    if SHA.fullmatch(revision) is None:
        raise Denied("invalid source revision")
    outcome = runner(["git", "diff", "--quiet", revision, "FETCH_HEAD", "--",
                      *SOURCE_PATHS], check=False)
    if outcome.returncode not in (0, 1):
        raise Denied("source freshness comparison failed")
    return outcome.returncode == 0


def main():
    """Dispatch publication decisions and source-freshness checks for CI."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("decide")
    for name in ("state-file", "image", "candidate-digest", "candidate-inspection",
                 "candidate-receipt", "revision", "source", "output"):
        plan.add_argument("--" + name, required=True)
    plan.add_argument("--existing-inspection")
    plan.add_argument("--existing-receipt")
    fresh = sub.add_parser("fresh")
    fresh.add_argument("--revision", required=True)
    ready = sub.add_parser("create-ready")
    ready.add_argument("--decision-file", required=True)
    ready.add_argument("--state-file", required=True)
    ready.add_argument("--candidate-digest", required=True)
    args = parser.parse_args()
    try:
        if args.command == "fresh":
            return 0 if source_fresh(args.revision) else 10
        if args.command == "create-ready":
            create_ready(_load(args.decision_file), _load(args.state_file),
                         args.candidate_digest)
            return 0
        decision = decide(_load(args.state_file), args.image, args.candidate_digest,
                          _load(args.candidate_inspection), _load(args.candidate_receipt),
                          args.revision, args.source,
                          _load(args.existing_inspection) if args.existing_inspection else None,
                          _load(args.existing_receipt) if args.existing_receipt else None)
        with open(args.output, "x", encoding="utf-8") as result:
            json.dump(decision, result)
            result.write("\n")
        return 0
    except Denied as exc:
        print(f"publication denied: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
