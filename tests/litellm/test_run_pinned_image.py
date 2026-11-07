"""Closed-result gate parsing; no Docker or provider access."""
import importlib.util
import json
from pathlib import Path
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
