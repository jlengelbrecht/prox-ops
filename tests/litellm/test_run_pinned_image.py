"""Closed-result gate parsing; no Docker or provider access."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('run_pinned_image', Path(__file__).with_name('run_pinned_image.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class PinnedResultTests(unittest.TestCase):
    def test_missing_contract_returns_closed_failure(self):
        with patch.object(runner, 'CONTRACT', Path('/tmp/nonexistent-litellm-image-contract.json')):
            result = runner.run(Path('/tmp'))
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['reason'], 'missing_input')
        self.assertNotIn('traceback', result)

    def test_non_object_contract_returns_closed_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            contract = Path(directory) / 'image_contract.json'
            for content in ('[]', 'null'):
                with self.subTest(content=content):
                    contract.write_text(content)
                    with patch.object(runner, 'CONTRACT', contract):
                        result = runner.run(Path(directory))
                    self.assertEqual(result['status'], 'fail')
                    self.assertEqual(result['reason'], 'contract_schema')
                    self.assertEqual(result['contract_sha256'], runner.digest(contract))

    def test_closed_result_rejects_missing_and_duplicate_checks(self):
        valid = {'schema': 'litellm-copilot-image-result/1',
                 'checks': {name: 'pass' for name in runner.EXPECTED}, 'failures': {}}
        parsed, status = runner.closed_result(json.dumps(valid).encode(), 0)
        self.assertEqual(status, 'pass')
        self.assertEqual(set(parsed['checks']), runner.EXPECTED)
        missing = dict(valid, checks={'installed_import': 'pass'})
        self.assertEqual(runner.closed_result(json.dumps(missing).encode(), 0)[1], 'check_schema')
        self.assertEqual(runner.closed_result((json.dumps(valid) + '\n' + json.dumps(valid)).encode(), 0)[1], 'result_count')

    def test_failure_report_contains_only_identifiers(self):
        checks = {name: 'pass' for name in runner.EXPECTED}
        checks['session_precedence_responses'] = 'fail'
        valid = {'schema': 'litellm-copilot-image-result/1', 'checks': checks,
                 'failures': {'session_precedence_responses':
                              {'class': 'AttributeError', 'predicate': 'responses_validate_environment'}}}
        self.assertEqual(runner.closed_result(json.dumps(valid).encode(), 1)[1], 'fail')
        valid['failures']['session_precedence_responses']['class'] = 'secret value'
        self.assertEqual(runner.closed_result(json.dumps(valid).encode(), 1)[1], 'failure_value')
        self.assertEqual(runner.closed_result(b'x' * 65537, 1)[1], 'output_limit')

    def test_import_failure_reports_cause_without_child_output(self):
        result = {'schema': 'litellm-copilot-image-result/1',
                  'checks': {'installed_import': 'fail'}}
        parsed, status = runner.closed_result(json.dumps(result).encode(), 1)
        self.assertEqual(status, 'installed_import_failed')
        self.assertEqual(parsed, {'checks': {'installed_import': 'fail'}, 'failures': {}})
        child = subprocess.CompletedProcess(['docker', 'run'], 1, json.dumps(result).encode(), b'private error')
        with patch.object(runner.subprocess, 'run', return_value=child):
            report = runner.run(Path('/tmp'))
        self.assertEqual((report['status'], report['reason']), ('fail', 'installed_import_failed'))
        self.assertEqual(runner.closed_result(json.dumps(result).encode(), 0)[1], 'check_schema')
        result['extra'] = 'untrusted'
        self.assertEqual(runner.closed_result(json.dumps(result).encode(), 1)[1], 'check_schema')

    def test_timeout_removes_named_container(self):
        with patch.object(runner.subprocess, 'run', side_effect=[
            subprocess.TimeoutExpired(['docker', 'run'], 180),
            subprocess.CompletedProcess(['docker', 'rm'], 0),
        ]) as called:
            report = runner.run(Path('/tmp'))
        self.assertEqual((report['status'], report['reason']), ('fail', 'timeout'))
        run_args = called.call_args_list[0].args[0]
        name = run_args[run_args.index('--name') + 1]
        self.assertEqual(called.call_args_list[1].args[0], ['docker', 'rm', '-f', name])
        self.assertEqual(called.call_args_list[1].kwargs['timeout'], 15)
