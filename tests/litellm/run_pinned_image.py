"""Run the pinned public image check without exposing container output."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = Path(__file__).with_name('image_contract.json')
CALLBACK = 'kubernetes/apps/ai/litellm/app/callbacks/copilot_session_guard.py'
FIXTURE = 'tests/litellm/pinned_image_integration.py'
EXPECTED = frozenset({
    'installed_import', 'actual_registration', 'missing_import_fails',
    'session_precedence_responses', 'actual_callback_chain', 'missing_credential',
    'wrong_mode', 'missing_owner', 'spoofed_session', 'rejected_exchange',
    'mode_change_before_attach', 'change_during_exchange_keeps_session',
    'other_alias_unchanged', 'protected_backend_alias_mismatch_denied',
    'shared_auth_never_invoked', 'base_url_override_denied',
})
IDENT = re.compile(r'^[A-Za-z_][A-Za-z_0-9]{0,79}$')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def closed_result(output, exit_code):
    """Accept one exact schema result; discard every other byte of child output."""
    if len(output) > 65536:
        return None, 'output_limit'
    found = []
    for line in output.decode('utf-8', 'replace').splitlines():
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and obj.get('schema') == 'litellm-copilot-image-result/1':
            found.append(obj)
    if len(found) != 1:
        return None, 'result_count'
    obj = found[0]
    checks = obj.get('checks')
    failures = obj.get('failures', {})
    if not isinstance(checks, dict) or set(checks) != EXPECTED:
        return None, 'check_schema'
    if not isinstance(failures, dict) or set(failures) != {k for k, v in checks.items() if v == 'fail'}:
        return None, 'failure_schema'
    if any(v not in ('pass', 'fail') for v in checks.values()):
        return None, 'check_value'
    for item in failures.values():
        if (not isinstance(item, dict) or set(item) != {'class', 'predicate'}
                or any(not isinstance(v, str) or not IDENT.fullmatch(v) for v in item.values())):
            return None, 'failure_value'
    status = 'pass' if exit_code == 0 and all(v == 'pass' for v in checks.values()) else 'fail'
    if exit_code == 0 and status == 'fail':
        return None, 'exit_mismatch'
    return {'checks': checks, 'failures': failures}, status


def run(result_dir):
    sources = {}
    for name in (CALLBACK, FIXTURE):
        path = ROOT / name
        if not path.is_file():
            return {'status': 'fail', 'reason': 'missing_input', 'path': name}
        sources[name] = digest(path)
    if not CONTRACT.is_file():
        return {'status': 'fail', 'reason': 'missing_input', 'path': 'tests/litellm/image_contract.json'}
    contract_hash = digest(CONTRACT)
    try:
        contract = json.loads(CONTRACT.read_text())
    except (OSError, UnicodeError, ValueError):
        return {'status': 'fail', 'reason': 'contract_schema', 'contract_sha256': contract_hash}
    if not isinstance(contract, dict):
        return {'status': 'fail', 'reason': 'contract_schema', 'contract_sha256': contract_hash}
    image = contract.get('image')
    if (contract.get('schema') != 'litellm-copilot-image-contract/1'
            or not isinstance(image, str)
            or not re.fullmatch(r'ghcr\.io/berriai/litellm-database:v1\.108\.0@sha256:[0-9a-f]{64}', image)
            or contract.get('interfaces') != {
                'callback': 'copilot_session_guard.proxy_handler_instance',
                'deployment_hook': 'async_pre_call_deployment_hook',
                'session_kwarg': 'github_copilot_user_session',
                'responses_transform': 'GithubCopilotResponsesAPIConfig',
            }):
        return {'status': 'fail', 'reason': 'contract_schema', 'contract_sha256': contract_hash}
    with tempfile.TemporaryDirectory(prefix='litellm-copilot-image-') as temp:
        snap = Path(temp)
        guard = snap / 'guard'
        guard.mkdir()
        shutil.copyfile(ROOT / CALLBACK, guard / 'copilot_session_guard.py')
        shutil.copyfile(ROOT / FIXTURE, snap / 'pinned_image_integration.py')
        cmd = [
            'docker', 'run', '--rm', '--network', 'none', '--read-only',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--tmpfs', '/tmp:rw,nosuid,size=16m',
            '--env', 'PYTHONDONTWRITEBYTECODE=1', '--env', 'PYTHONPATH=/opt/guard',
            '--mount', f'type=bind,source={guard},target=/opt/guard,readonly',
            '--mount', f'type=bind,source={snap / "pinned_image_integration.py"},target=/opt/test/pinned_image_integration.py,readonly',
            '--entrypoint', 'python3', image, '/opt/test/pinned_image_integration.py',
        ]
        try:
            process = subprocess.run(cmd, capture_output=True, timeout=180, check=False)
        except subprocess.TimeoutExpired:
            return {'status': 'fail', 'reason': 'timeout', 'contract_sha256': contract_hash,
                    'image': image, 'source_sha256': sources}
        except OSError:
            return {'status': 'fail', 'reason': 'runtime_unavailable', 'contract_sha256': contract_hash,
                    'image': image, 'source_sha256': sources}
    result, status = closed_result(process.stdout, process.returncode)
    report = {'status': status if result else 'fail', 'reason': None if result else status,
              'contract_sha256': contract_hash, 'image': image, 'source_sha256': sources,
              'child_exit': process.returncode, 'stderr_present': bool(process.stderr)}
    if result:
        report['result'] = result
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--result-dir', required=True)
    args = parser.parse_args()
    result_dir = Path(args.result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    report = run(result_dir)
    report['schema'] = 'litellm-copilot-image-gate-report/1'
    report['finished_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
    location = result_dir / f'attempt-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex}.json'
    with location.open('x') as stream:
        json.dump(report, stream, indent=2, sort_keys=True)
        stream.write('\n')
    location.chmod(0o444)
    print(f'{location}: {report["status"]} ({report.get("reason") or "closed checks"})')
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
