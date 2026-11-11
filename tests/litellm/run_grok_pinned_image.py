#!/usr/bin/env python3
"""Offline, closed Grok image qualification for local builds and published digests."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PACKAGE = ROOT / "services/litellm-grok"
FIXTURE = HERE / "grok_pinned_image_integration.py"
BOOTSTRAP = HERE / "grok_pinned_image_bootstrap.py"
BASE = "ghcr.io/berriai/litellm-database:v1.108.0@sha256:80c91fd00cc039e8880474f539544b919f14cd258a22f22ce4218b864c4ff14a"
CALLBACK = ROOT / "kubernetes/apps/ai/litellm/app/callbacks/project_grok_native.py"
CALLBACK_HASH = "262b0fb497f583e22c36b656fa64a961493eab97e78053f661f780816a18b777"
CHECKS = {"installed_import", "userinfo_bearer", "same_token_subject", "fixed_transport",
          "generated_anthropic_carrier_removed", "messages_proxy_stream", "linked_continuation",
          "hook_sequence", "selected_alias", "repeated_deployment_binding", "alias_only_owner",
          "caller_override_denied", "http_duplicate_forbidden_denied", "refresh_failure_redacted",
          "prestream_fixed_502_body", "prestream_closed_response_and_logs",
          "prestream_single_provider_no_fallback", "afterstream_replacement",
          "afterstream_single_provider_no_fallback", "nonalias_error_unchanged",
          "nonalias_endpoint_original_logged_after_hook",
          "nonalias_stream_original_logged_before_hook_fault",
          "generic_denial_preserved", "generic_failure_closed", "hook_fault_fixed_terminal",
          "malformed_terminal_error_rejected"}
PACKAGE_HASHES = {"Dockerfile": "353681f1accce38b327d94687ef2aa991ec9a8217538c76585ef5388d42d70e2",
                  ".dockerignore": "7c59f2a71619924c2f4b917dcce62f522f959d61762d00cafcc9dec8dd7f2612",
                  "patch_image.py": "fdfa0804ceb8e62a5294c0d730002a530cdc4f9bf9478affb43f7c96a2924552"}
FIXTURE_HASHES = {"grok_pinned_image_integration.py": "10951732fc42d24039a75a2d5a568fe5d7cf9a9750442dcda6905d528df0c5a5",
                  "grok_pinned_image_bootstrap.py": "a79b47a1561f4093b9687eb789ee304d07b114496fad30f31238b434338b3094"}
FILES = {**{str(PACKAGE / name): expected for name, expected in PACKAGE_HASHES.items()},
         **{str(HERE / name): expected for name, expected in FIXTURE_HASHES.items()}}
PUBLISHED = re.compile(r"ghcr\.io/jlengelbrecht/litellm-grok@sha256:[a-f0-9]{64}")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_closed(command, timeout):
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.run(command, stdout=out, stderr=err, timeout=timeout, check=False)
        return process.returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def inspect(image):
    try:
        process = subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                                 timeout=15, check=False)
        if process.returncode:
            return None
        parsed = json.loads(process.stdout)
        return parsed[0] if len(parsed) == 1 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def qualify(base_id, candidate_image=None):
    receipt = {"schema": "grok-error-candidate-parent/1", "status": "fail",
               "phase": "preflight", "checks": {}, "source_hashes": {}, "callback_hash": None}
    try:
        receipt["source_hashes"] = {str(Path(name).relative_to(ROOT)): sha(Path(name))
                                    for name in FILES}
        receipt["callback_hash"] = sha(CALLBACK)
    except OSError:
        receipt["reason"] = "source_missing"
        return receipt
    if any(sha(Path(name)) != expected for name, expected in FILES.items()):
        receipt["reason"] = "source_hash_mismatch"
        return receipt
    if receipt["callback_hash"] != CALLBACK_HASH:
        receipt["reason"] = "callback_hash_mismatch"
        return receipt
    pinned = inspect(BASE)
    if base_id is None and pinned is not None:
        base_id = pinned.get("Id")
    if not isinstance(base_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", base_id):
        receipt["reason"] = "invalid_base_id"
        return receipt
    supplied = inspect(base_id)
    if pinned is None or supplied is None or pinned.get("Id") != base_id or supplied.get("Id") != base_id:
        receipt["reason"] = "cached_base_mismatch"
        return receipt
    repository = BASE.split("@", 1)[0].rsplit(":", 1)[0]
    digest = BASE.split("@", 1)[1]
    if repository + "@" + digest not in pinned.get("RepoDigests", []):
        receipt["reason"] = "base_digest_unverified"
        return receipt
    # The Docker driver resolves a bare sha256: ID as a registry name in FROM.
    # Bind a unique local tag to the verified ID for this build only.
    base_tag = "grok-error-candidate-base:" + uuid.uuid4().hex
    if inspect(base_tag) is not None:
        receipt["reason"] = "temporary_tag_collision"
        return receipt
    tag_code = run_closed(["docker", "image", "tag", base_id, base_tag], 15)
    try:
        tagged = inspect(base_tag)
        if tag_code != 0:
            receipt["reason"] = "temporary_tag_failed"
            return receipt
        if tagged is None or tagged.get("Id") != base_id:
            receipt["reason"] = "temporary_tag_mismatch"
            return receipt
        return qualify_tagged(base_id, base_tag, receipt, candidate_image)
    finally:
        tagged = inspect(base_tag)
        if tagged is None and tag_code != 0:
            pass  # Failed tag command left no temporary reference.
        elif tagged is None or tagged.get("Id") != base_id:
            receipt["status"] = "fail"
            receipt["reason"] = "temporary_tag_cleanup_unverified"
        elif run_closed(["docker", "image", "rm", "--no-prune", base_tag], 15) != 0 or inspect(base_tag) is not None:
            receipt["status"] = "fail"
            receipt["reason"] = "temporary_tag_cleanup_failed"
        elif (inspect(BASE) or {}).get("Id") != base_id or (inspect(base_id) or {}).get("Id") != base_id:
            receipt["status"] = "fail"
            receipt["reason"] = "pinned_base_missing_after_cleanup"


def qualify_tagged(base_id, base_tag, receipt, candidate_image):
    with tempfile.TemporaryDirectory(prefix="grok-error-candidate-") as temp:
        work = Path(temp)
        image_id_file = work / "image-id"
        if candidate_image is None:
            receipt["phase"] = "build"
            build = ["docker", "build", "--pull=false", "--network=none", "--no-cache",
                     "--iidfile", str(image_id_file),
                     "-f", str(PACKAGE / "Dockerfile"), str(PACKAGE)]
            if run_closed(build, 240) != 0 or not image_id_file.is_file():
                receipt["reason"] = "offline_build_failed"
                return receipt
            candidate_id = image_id_file.read_text().strip()
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", candidate_id):
                receipt["reason"] = "invalid_candidate_id"
                return receipt
            image_to_run = candidate_id
        else:
            receipt["phase"] = "published_preflight"
            if PUBLISHED.fullmatch(candidate_image) is None:
                receipt["reason"] = "invalid_published_digest"
                return receipt
            published = inspect(candidate_image)
            if published is None or candidate_image not in published.get("RepoDigests", []):
                receipt["reason"] = "published_digest_unavailable"
                return receipt
            candidate_id = published.get("Id")
            if not isinstance(candidate_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", candidate_id):
                receipt["reason"] = "invalid_candidate_id"
                return receipt
            image_to_run = candidate_image
            receipt["published_digest"] = candidate_image
        receipt["candidate_image_id"] = candidate_id
        snap = work / "input"
        out = work / "output"
        snap.mkdir(mode=0o755)
        out.mkdir(mode=0o777)
        out.chmod(0o777)
        for name in ("grok_pinned_image_integration.py", "grok_pinned_image_bootstrap.py"):
            shutil.copyfile(HERE / name, snap / name)
        shutil.copyfile(CALLBACK, snap / "project_grok_native.py")
        snap.chmod(0o755)
        receipt["phase"] = "asgi"
        name = "grok-error-candidate-" + uuid.uuid4().hex
        command = ["docker", "run", "--rm", "--pull=never", "--name", name,
                   "--network", "none", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges", "--pids-limit", "256",
                   "--memory", "2g", "--tmpfs", "/tmp:rw,nosuid,size=32m",
                   "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONPATH=/opt/grok",
                   "--mount", f"type=bind,source={snap},target=/opt/grok,readonly",
                   "--mount", f"type=bind,source={out},target=/opt/grok-result",
                   "--entrypoint", "python3", image_to_run,
                   "/opt/grok/grok_pinned_image_bootstrap.py", "/opt/grok-result/result.json"]
        code = run_closed(command, 240)
        if code == -1:
            run_closed(["docker", "rm", "-f", name], 15)
        try:
            data_path = out / "result.json"
            if data_path.stat().st_size > 65536:
                raise ValueError
            child = json.loads(data_path.read_text())
            if (not isinstance(child, dict)
                    or set(child) != {"schema", "checks", "failures", "fatal", "evidence"}
                    or child.get("schema") != "litellm-grok-image-result/2"):
                raise ValueError
            checks = child.get("checks")
            if not isinstance(checks, dict) or set(checks) != CHECKS:
                raise ValueError
            if any(value not in {"pass", "fail"} for value in checks.values()):
                raise ValueError
            receipt["checks"] = checks
            evidence = child.get("evidence", {})
            denial_statuses = evidence.get("denial_statuses", {})
            afterstream = evidence.get("afterstream", {})
            log_sites = evidence.get("afterstream_log_sites", [])
            failure_details = child.get("failures", {})
            allowed_cases = {"setup", "positive", "owner_denial", "override_denial",
                             "duplicate_denial", "refresh_denial", "prestream", "afterstream", "nonalias",
                             "generic", "hook_fault", "nonalias_endpoint_replace",
                             "nonalias_stream_hook_fault"}
            if (set(denial_statuses) != {"owner", "override", "duplicate"}
                    or any(type(value) is not int or not 100 <= value <= 599
                           for value in denial_statuses.values())
                    or set(afterstream) != {"status", "prefix", "error_event", "fixed",
                                            "closed_body", "closed_logs"}
                    or type(afterstream["status"]) is not int
                    or any(type(afterstream[key]) is not bool for key in afterstream if key != "status")
                    or not isinstance(log_sites, list) or len(log_sites) > 8
                    or any(not isinstance(site, dict)
                           or set(site) != {"source", "logger", "line", "level"}
                           or not isinstance(site["source"], str)
                           or re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", site["source"]) is None
                           or not isinstance(site["logger"], str)
                           or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", site["logger"]) is None
                           or type(site["line"]) is not int or not 0 <= site["line"] <= 20000
                           or site["level"] not in {"INFO", "WARNING", "ERROR", "CRITICAL", "other"}
                           for site in log_sites)
                    or set(failure_details) != {key for key, value in checks.items() if value == "fail"}
                    or any(not isinstance(detail, dict)
                           or set(detail) != {"class", "predicate", "case"}
                           or detail["predicate"] != key
                           or detail["case"] not in allowed_cases
                           for key, detail in failure_details.items())):
                raise ValueError
            receipt["statuses"] = evidence.get("statuses", {})
            receipt["denial_statuses"] = denial_statuses
            receipt["afterstream"] = afterstream
            receipt["afterstream_log_sites"] = log_sites
            receipt["failure_cases"] = {key: detail["case"] for key, detail in failure_details.items()}
            receipt["fatal"] = child.get("fatal") is not None
            nonalias_status = evidence.get("nonalias_status")
            nonalias_digest = evidence.get("nonalias_body_sha256")
            generic_reached = evidence.get("generic_reached")
            generic_status = evidence.get("generic_status")
            generic_digest = evidence.get("generic_body_sha256")
            if (type(nonalias_status) is not int or not 100 <= nonalias_status <= 599
                    or not isinstance(nonalias_digest, str)
                    or re.fullmatch(r"[a-f0-9]{64}", nonalias_digest) is None
                    or generic_reached is not False
                    or type(generic_status) is not int
                    or re.fullmatch(r"[a-f0-9]{64}", generic_digest) is None):
                raise ValueError
            receipt["generic_provider_reached"] = generic_reached
            receipt["generic_status"] = evidence.get("generic_status")
            receipt["generic_outbound"] = evidence.get("generic_outbound")
            receipt["generic_calls"] = evidence.get("generic_calls")
            receipt["generic_exception"] = evidence.get("generic_exception")
        except (OSError, ValueError, TypeError, AttributeError):
            receipt["reason"] = "closed_child_receipt_invalid"
            return receipt
        # Run the same synthetic ASGI fixture in baseline mode against the
        # digest-verified, unmodified cached base. The child digest is confined
        # to temporary storage; the parent receipt publishes only comparison.
        baseline_out = work / "baseline-output"
        baseline_out.mkdir(mode=0o777)
        baseline_out.chmod(0o777)
        baseline_command = command.copy()
        baseline_command[baseline_command.index(image_to_run)] = base_id
        baseline_command[baseline_command.index(f"type=bind,source={out},target=/opt/grok-result")] = (
            f"type=bind,source={baseline_out},target=/opt/grok-result")
        baseline_command[baseline_command.index(name)] = name + "-base"
        baseline_command[baseline_command.index("--entrypoint"):baseline_command.index("--entrypoint")] = [
            "--env", "GROK_BASELINE_NONALIAS=1"]
        baseline_code = run_closed(baseline_command, 240)
        if baseline_code == -1:
            run_closed(["docker", "rm", "-f", name + "-base"], 15)
        try:
            baseline_path = baseline_out / "result.json"
            if baseline_path.stat().st_size > 65536:
                raise ValueError
            baseline = json.loads(baseline_path.read_text())
            baseline_evidence = baseline["evidence"]
            receipt["baseline_checks"] = baseline.get("checks")
            receipt["baseline_fatal"] = baseline.get("fatal")
            receipt["baseline_generic_outbound"] = baseline_evidence.get("baseline_generic_outbound")
            baseline_status = baseline_evidence["nonalias_status"]
            baseline_digest = baseline_evidence["nonalias_body_sha256"]
            if (set(baseline) != {"schema", "checks", "failures", "fatal", "evidence"}
                    or baseline.get("schema") != "litellm-grok-image-result/2"
                    or baseline.get("checks") != {"baseline_nonalias_reached": "pass",
                                                   "baseline_generic_denied": "pass"}
                    or baseline.get("failures") != {}
                    or baseline.get("fatal") is not None
                    or type(baseline_status) is not int
                    or re.fullmatch(r"[a-f0-9]{64}", baseline_digest) is None
                    or baseline_evidence.get("baseline_generic_outbound") != []
                    or type(baseline_evidence.get("baseline_generic_status")) is not int
                    or re.fullmatch(r"[a-f0-9]{64}", baseline_evidence.get("baseline_generic_body_sha256", "")) is None):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            receipt["reason"] = "closed_baseline_receipt_invalid"
            return receipt
        receipt["generic_denial_paired_equal"] = (generic_status == baseline_evidence["baseline_generic_status"]
            and generic_digest == baseline_evidence["baseline_generic_body_sha256"])
        if not receipt["generic_denial_paired_equal"]:
            receipt["reason"] = "generic_denial_changed"
            return receipt
        receipt["baseline_generic_provider_reached"] = False
        receipt["nonalias_baseline_status"] = baseline_status
        receipt["nonalias_candidate_status"] = nonalias_status
        receipt["nonalias_paired_equal"] = (baseline_code == 0
            and nonalias_status == baseline_status and nonalias_digest == baseline_digest)
        if not receipt["nonalias_paired_equal"]:
            receipt["reason"] = "nonalias_paired_comparison_failed"
            return receipt
        receipt["child_exit_zero"] = code == 0
        if code != 0 or receipt["fatal"] or any(v != "pass" for v in receipt["checks"].values()):
            receipt["reason"] = "asgi_qualification_failed"
            return receipt
        receipt["status"] = "pass"
        receipt["reason"] = None
        return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-image-id", help="Verified cached base ID; defaults to the pinned reference's cached ID")
    parser.add_argument("--candidate-image", help="Already pulled published digest; skip build")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if not args.receipt.parent.is_dir():
        raise SystemExit("receipt directory must exist")
    result = qualify(args.base_image_id, args.candidate_image)
    args.receipt.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": result["status"], "phase": result["phase"],
                      "reason": result.get("reason"), "receipt": args.receipt.name}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
