"""Qualify the pinned Helm image and rehearse its PostgreSQL upgrade."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
APP = Path("kubernetes/apps/ai/litellm/app")
RELEASE = APP / "helmrelease.yaml"
REPOSITORY = "ghcr.io/berriai/litellm"
STOCK_TAG = r"v[0-9]+\.[0-9]+\.[0-9]+@sha256:[a-f0-9]{64}"
OLD_REPOSITORY = "ghcr.io/jlengelbrecht/litellm-grok"
OLD_TAG = r"sha-[a-f0-9]{40}@sha256:[a-f0-9]{64}"
# Use a published PostgreSQL 16 fixture. Docker Hub pruned the deployed 16.6 image.
POSTGRES = "postgres:16@sha256:ca0bd484cb98bf4b24eb1010e73fb3fcbd6714d240fbc1a10eea5b7dbecb641d"
CONTAINER_OPTIONS = ["--rm", "--pull=never", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]
OFFLINE_SETTINGS = ("-e", "LITELLM_TELEMETRY=false", "-e", "LITELLM_LOCAL_MODEL_COST_MAP=True")


def run(*args, **options):
    return subprocess.run(args, cwd=ROOT, check=True, timeout=240, text=True, **options).stdout


def image_ref(release, *, allow_old=False):
    image = release["spec"]["values"]["image"]
    repository, tag = image["repository"], image["tag"]
    if repository == REPOSITORY and re.fullmatch(STOCK_TAG, tag):
        return f"{repository}:{tag}"
    if allow_old and repository == OLD_REPOSITORY and re.fullmatch(OLD_TAG, tag):
        return f"{repository}:{tag}"
    raise ValueError("LiteLLM image must identify an allowed stable release and exact digest")


def verify_registry(ref):
    tag, digest = ref.split("@", 1)
    found = run("docker", "buildx", "imagetools", "inspect", tag,
                "--format", "{{json .Manifest.Digest}}", capture_output=True)
    if json.loads(found) != digest:
        raise ValueError("Published LiteLLM tag does not match the configured digest")


def migrate(previous, candidate):
    suffix = uuid.uuid4().hex[:12]
    network, database = f"litellm-qualify-{suffix}", f"litellm-db-{suffix}"
    url = f"postgresql://fixture:fixture@{database}:5432/fixture"
    run("docker", "network", "create", "--internal", network)
    try:
        run("docker", "run", "--detach", "--pull=never", "--name", database, "--network", network,
            "-e", "POSTGRES_USER=fixture", "-e", "POSTGRES_PASSWORD=fixture", "-e", "POSTGRES_DB=fixture",
            POSTGRES)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready = subprocess.run(["docker", "exec", database, "pg_isready", "-h", "127.0.0.1", "-U", "fixture"],
                                   stdout=subprocess.DEVNULL, timeout=5)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("Disposable PostgreSQL never became ready")
        options = (*CONTAINER_OPTIONS, *OFFLINE_SETTINGS, "--network", network,
                   "-e", f"DATABASE_URL={url}", "-e", "DISABLE_SCHEMA_UPDATE=false")
        # This is the pinned chart's pre-install/pre-upgrade Job command.
        for image in (previous, candidate):
            run("docker", "run", *options, "--entrypoint", "python", image, "litellm/proxy/prisma_migration.py")
            applied = run("docker", "exec", database, "psql", "-U", "fixture", "-d", "fixture", "-Atc",
                          'SELECT count(*) FROM "_prisma_migrations" WHERE finished_at IS NOT NULL',
                          capture_output=True)
            if int(applied) < 1:
                raise RuntimeError("LiteLLM migration hook did not record any migrations")
        run("docker", "run", *options, "--entrypoint", "prisma", candidate, "migrate", "diff",
            "--from-url", url, "--to-schema-datamodel", "/app/schema.prisma", "--exit-code")
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", database], check=False, timeout=20)
        run("docker", "network", "rm", network)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--image-only-change", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.base):
        parser.error("--base must be a full commit SHA")
    base = yaml.safe_load(run("git", "show", f"{args.base}:{RELEASE}", capture_output=True))
    head = yaml.safe_load((ROOT / RELEASE).read_text())
    previous = image_ref(base, allow_old=True)
    candidate = image_ref(head)
    if args.image_only_change and previous != candidate:
        base_image = base["spec"]["values"]["image"]
        base_image["tag"] = head["spec"]["values"]["image"]["tag"]
        changed = run("git", "diff", "--name-only", "-z", args.base, "HEAD", capture_output=True)
        if (not previous.startswith(REPOSITORY + ":") or base != head
                or changed.split("\0") != [str(RELEASE), ""]):
            raise ValueError("Automated LiteLLM upgrades may only change the stock release tag and digest")
    verify_registry(candidate)
    for ref in dict.fromkeys((previous, candidate, POSTGRES)):
        run("docker", "pull", ref)
    run("docker", "run", *CONTAINER_OPTIONS, *OFFLINE_SETTINGS, "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=64m", "-e", "LITELLM_LOG=INFO",
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/callbacks",
        "-v", f"{ROOT / 'tests/litellm'}:/fixtures:ro", "-v", f"{ROOT / APP / 'callbacks'}:/callbacks:ro",
        "--entrypoint", "python3", candidate, "/fixtures/stock_grok_integration.py")
    migrate(previous, candidate)


if __name__ == "__main__":
    main()
