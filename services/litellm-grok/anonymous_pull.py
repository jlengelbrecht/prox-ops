#!/usr/bin/env python3
"""Prove a published GHCR digest is pullable without Docker credentials."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


REFERENCE = re.compile(r"ghcr\.io/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}\Z")


def pull_anonymously(reference, temporary_root, runner=subprocess.run):
    """Use an empty, private Docker config and pass no inherited auth variables."""
    if REFERENCE.fullmatch(reference) is None:
        raise ValueError("expected an exact GHCR digest reference")
    with tempfile.TemporaryDirectory(prefix="grok-anonymous-", dir=temporary_root) as config:
        Path(config, "config.json").write_text(json.dumps({"auths": {}}) + "\n")
        # The hosted runner uses the default local Docker socket. No caller
        # environment, credential helper, or prior docker/login-action config
        # is available to this CLI invocation.
        env = {"PATH": os.environ.get("PATH", ""), "HOME": config,
               "DOCKER_CONFIG": config}
        runner(["docker", "--config", config, "pull", reference],
               check=True, env=env)


def main():
    """Fail publication with the owner bootstrap action when anonymous pull fails."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    parser.add_argument("--temporary-root", required=True)
    args = parser.parse_args()
    try:
        pull_anonymously(args.reference, args.temporary_root)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print("anonymous GHCR digest pull failed; owner: open the litellm-grok "
              "package settings, set visibility to Public if intended, then "
              "rerun this workflow. This run has not promoted a tag.",
              file=sys.stderr)
        return 1
    print("anonymous GHCR digest pull passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
