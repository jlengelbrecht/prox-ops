"""Evaluate actual rules and dashboard PromQL with pinned native promtool."""
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import copy

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
RULE = yaml.safe_load((ROOT / 'kubernetes/apps/observability/kube-prometheus-stack/app/alerts/hindsight-alerts.yaml').read_text())
DASH = yaml.safe_load((ROOT / 'kubernetes/apps/observability/kube-prometheus-stack/app/dashboards/hindsight-dashboard.yaml').read_text())
PANELS = json.loads(DASH['data']['hindsight-ingestion.json'])['panels']
P95 = next(p['targets'][0]['expr'] for p in PANELS if p['title'].startswith('LLM request p95'))
PROMTOOL = os.environ.get('PROMTOOL_BIN') or shutil.which('promtool') or str(ROOT / '_bmad-output/implementation-artifacts/tooling/promtool')
if not pathlib.Path(PROMTOOL).is_file():
    raise SystemExit('native promtool required')

RULES = {r['alert']: r for r in RULE['spec']['groups'][0]['rules']}
NAMES = tuple(name for name in RULES if name != 'HindsightMetricSeriesChurn')
CHURN = 'HindsightMetricSeriesChurn'
UP = {'series': 'up{namespace="ai",service="hindsight-api"}', 'values': '1+0x25'}
PENDING = {'series': 'hindsight_async_operations{namespace="ai",service="hindsight-api",bank_id="newbank",operation_type="retain",status="pending"}', 'values': '1+0x25'}
PARENT = {'series': 'hindsight_async_operations{namespace="ai",bank_id="newbank",operation_type="batch_retain",status="failed"}', 'values': '1+0x25'}
PROGRESS = {'series': 'hindsight_operation_operations_total{namespace="ai",service="hindsight-api",bank_id="newbank",operation="retain",source="worker"}', 'values': '0+1x25'}
ZERO = dict(PROGRESS, values='0+0x25')
OTHER_PROGRESS = dict(PROGRESS, series=PROGRESS['series'].replace('service="hindsight-api"', 'service="unrelated"'))
WAIT = {'series': 'hindsight_db_pool_waiting{namespace="ai",bank_id="newbank"}', 'values': '1+0x25'}
WAIT_ZERO = dict(WAIT, values='0+0x25')
GAUGE = {'series': 'hindsight_db_pool_waiting{namespace="ai",service="hindsight-api"}', 'values': '0+0x25'}
DOWN = dict(UP, values='0+0x25')


def series_group(count, prefix='steady', values='1+0x25'):
    return [{'series': f'hindsight_llm_calls_total{{namespace="ai",service="hindsight-api",scope="{prefix}{i}"}}',
             'values': values} for i in range(count)]


def expected(name, labels):
    rule = RULES[name]
    return {'exp_labels': {**labels, **rule.get('labels', {})}, 'exp_annotations': rule['annotations']}


def checks(at, firing=None):
    firing = firing or {}
    return [{'eval_time': at, 'alertname': name,
             'exp_alerts': [expected(name, labels) for labels in firing.get(name, [])]}
            for name in NAMES]


cases = [
    ('parent_only', [UP, PARENT], checks('20m')),
    ('progress', [UP, PENDING, PROGRESS], checks('20m')),
    ('other_service_does_not_suppress', [UP, PENDING, OTHER_PROGRESS], checks('20m', {
        'HindsightRetainPendingWithoutCompletion': [{'bank_id': 'newbank'}]})),
    ('new_bank_counter_absent_early', [UP, PENDING], checks('1m') + checks('9m') + checks('11m') + checks('19m')),
    ('new_bank_counter_absent_sustained', [UP, PENDING], checks('20m', {
        'HindsightRetainPendingWithoutCompletion': [{'bank_id': 'newbank'}]})),
    ('deferred_only_status_early', [UP, PENDING], checks('9m')),
    ('deferred_only_status_sustained', [UP, PENDING], checks('20m', {
        'HindsightRetainPendingWithoutCompletion': [{'bank_id': 'newbank'}]})),
    ('zero_completions', [UP, PENDING, ZERO], checks('20m', {
        'HindsightRetainPendingWithoutCompletion': [{'bank_id': 'newbank'}]})),
    ('db_wait_below_threshold', [UP, WAIT_ZERO], checks('20m')),
    ('db_wait_before_five_minutes', [UP, WAIT], checks('4m')),
    ('db_wait_after_five_minutes', [UP, WAIT], checks('7m', {
        'HindsightDBPoolWaiting': [{'namespace': 'ai', 'bank_id': 'newbank'}]})),
    ('scrape_loss', [], checks('7m', {'HindsightMetricsAbsent': [
        {'namespace': 'ai', 'service': 'hindsight-api'}]})),
    ('scrape_down', [DOWN], checks('7m', {'HindsightMetricsAbsent': [
        {'namespace': 'ai', 'service': 'hindsight-api'}]})),
    ('native_gauge_missing', [UP], checks('7m', {'HindsightNativeGaugeAbsent': [
        {'namespace': 'ai', 'service': 'hindsight-api'}]})),
    ('genuinely_quiet', [UP, GAUGE], checks('7m')),
    ('steady_below_budget', [UP, GAUGE, *series_group(3999)], checks('20m')),
    ('steady_over_budget', [UP, GAUGE, *series_group(4000)], checks('20m', {
        'HindsightMetricSeriesGrowth': [{}]})),
]

with tempfile.TemporaryDirectory() as directory:
    path = pathlib.Path(directory)
    rules_path = path / 'rules.yaml'
    group = copy.deepcopy(RULE['spec']['groups'][0])
    group['rules'] = [rule for rule in group['rules'] if rule['alert'] != CHURN]
    rules_path.write_text(yaml.safe_dump({'groups': [group]}))
    histogram = [{'series': f'hindsight_llm_duration_seconds_bucket{{namespace="ai",scope="retain",le="{le}"}}',
                  'values': values} for le, values in [('1', '0+1x25'), ('2', '0+2x25'), ('+Inf', '0+2x25')]]
    tests = [{'name': name, 'interval': '1m',
              'input_series': series if name in ('scrape_loss', 'scrape_down', 'native_gauge_missing', 'genuinely_quiet',
                                                 'steady_below_budget', 'steady_over_budget')
              else [*series, GAUGE], 'alert_rule_test': alerts}
             for name, series, alerts in cases]
    dashboard_series = [UP, *histogram,
        {'series': 'hindsight_async_operations{namespace="ai",bank_id="devb0x",operation_type="retain",status="pending"}', 'values': '2+0x25'},
        {'series': 'hindsight_async_operations{namespace="ai",bank_id="obsidian",operation_type="retain",status="pending"}', 'values': '3+0x25'},
        {'series': 'hindsight_async_operations{namespace="ai",bank_id="devb0x",operation_type="retain",status="failed"}', 'values': '4+0x25'},
        {'series': 'hindsight_async_operations{namespace="ai",bank_id="obsidian",operation_type="retain",status="failed"}', 'values': '1+0x25'},
        {'series': 'hindsight_async_operations{namespace="ai",bank_id="devb0x",operation_type="batch_retain",status="failed"}', 'values': '7+0x25'},
        {'series': 'hindsight_operation_operations_total{namespace="ai",bank_id="devb0x",operation="retain",source="worker"}', 'values': '0+1x25'},
        {'series': 'hindsight_operation_operations_total{namespace="ai",bank_id="obsidian",operation="retain",source="worker"}', 'values': '0+2x25'},
        {'series': 'hindsight_llm_calls_total{namespace="ai",scope="retain",success="true"}', 'values': '0+1x25'},
        {'series': 'hindsight_llm_calls_total{namespace="ai",scope="retain",success="false"}', 'values': '0+2x25'},
        {'series': 'hindsight_http_requests_total{namespace="ai",status_code="429"}', 'values': '0+1x25'},
        {'series': 'hindsight_db_pool_waiting{namespace="ai"}', 'values': '2+0x25'},
        *[{'series': f'hindsight_db_pool_acquire_wait_seconds_bucket{{namespace="ai",le="{le}"}}',
           'values': values} for le, values in [('1', '0+1x25'), ('2', '0+2x25'), ('+Inf', '0+2x25')]],
    ]
    expected_panels = {
        1: [('{bank_id="devb0x"}', 2), ('{bank_id="obsidian"}', 3)],
        2: [('{bank_id="devb0x"}', 4), ('{bank_id="obsidian"}', 1)],
        3: [('{bank_id="devb0x"}', 7)],
        4: [('{bank_id="devb0x"}', 1/60), ('{bank_id="obsidian"}', 2/60)],
        5: [('{scope="retain",success="false"}', 2/60), ('{scope="retain",success="true"}', 1/60)],
        6: [('{scope="retain"}', 1.9)],
        7: [('{}', 1/60)],
        8: [('{__name__="hindsight_db_pool_waiting",namespace="ai"}', 2)],
        9: [('{}', 1.9)],
        10: [('{__name__="up",namespace="ai",service="hindsight-api"}', 1)],
    }
    assert {p['id'] for p in PANELS} == set(expected_panels), 'new dashboard panel needs a result fixture'
    tests.append({'name': 'dashboard_all_actual_expressions', 'interval': '1m',
                  'input_series': dashboard_series,
                  'promql_expr_test': [{'expr': panel['targets'][0]['expr'], 'eval_time': '10m',
                                        'exp_samples': [{'labels': labels, 'value': value}
                                                        for labels, value in expected_panels[panel['id']]]}
                                       for panel in PANELS]})
    fixture = path / 'fixture.yaml'
    fixture.write_text(yaml.safe_dump({'rule_files': [str(rules_path)], 'evaluation_interval': '1m', 'tests': tests}))
    subprocess.run([PROMTOOL, 'test', 'rules', str(fixture)], check=True)
    subprocess.run([PROMTOOL, 'check', 'rules', str(rules_path)], check=True)

    churn_rule = copy.deepcopy(RULES[CHURN])
    assert churn_rule['expr'].endswith('> 6000') and churn_rule['for'] == '10m'
    churn_rule['expr'] = churn_rule['expr'].removesuffix('6000') + '2'
    churn_rules = path / 'churn-rules.yaml'
    churn_rules.write_text(yaml.safe_dump({'groups': [{**group, 'rules': [churn_rule]}]}))
    same_labels = {'series': 'hindsight_llm_calls_total{namespace="ai",service="hindsight-api",bank_id="first"}',
                   'values': '1+0x25'}
    other_name = {'series': 'hindsight_operation_operations_total{namespace="ai",service="hindsight-api",bank_id="first"}',
                  'values': '1 _x25'}
    transient_bank = {'series': 'hindsight_llm_calls_total{namespace="ai",service="hindsight-api",bank_id="later"}',
                      'values': '_ 1 _x24'}
    expired_bank = dict(transient_bank, values='1 _x200')
    off_grid_bank = dict(transient_bank, values='_x181 1 _x20')
    outside_family = dict(transient_bank, series=transient_bank['series'].replace('hindsight_llm_calls_total', 'other_llm_calls_total'))
    outside_service = dict(transient_bank, series=transient_bank['series'].replace('service="hindsight-api"', 'service="other"'))
    steady_for_boundary = [dict(same_labels, values='1+0x201'),
                           dict(other_name, values='1+0x201')]
    churn_fixture = path / 'churn-fixture.yaml'
    churn_fixture.write_text(yaml.safe_dump({
        'rule_files': [str(churn_rules)], 'evaluation_interval': '10s',
        'tests': [
            {'name': 'two_metric_names_at_threshold', 'interval': '1m',
             'input_series': [same_labels, other_name],
             'alert_rule_test': [{'eval_time': '20m', 'alertname': CHURN, 'exp_alerts': []}]},
            {'name': 'short_lived_distinct_bank_above_threshold', 'interval': '1m',
             'input_series': [same_labels, other_name, transient_bank],
             'alert_rule_test': [{'eval_time': '20m', 'alertname': CHURN,
                                  'exp_alerts': [expected(CHURN, {})]}]},
            {'name': 'sample_at_cutoff_is_expired', 'interval': '10s',
             'input_series': [*steady_for_boundary, expired_bank],
             'promql_expr_test': [{'expr': churn_rule['expr'], 'eval_time': '30m',
                                   'exp_samples': []}]},
            {'name': 'off_grid_sample_inside_window_counts', 'interval': '10s',
             'input_series': [*steady_for_boundary, off_grid_bank],
             'promql_expr_test': [{'expr': churn_rule['expr'], 'eval_time': '30m20s',
                                   'exp_samples': [{'labels': '{}', 'value': 3}]}]},
            {'name': 'expired_sample_is_not_lookback_carried', 'interval': '10s',
             'input_series': [*steady_for_boundary, expired_bank],
             'promql_expr_test': [{'expr': churn_rule['expr'], 'eval_time': '30m20s',
                                   'exp_samples': []}]},
            {'name': 'whole_family_with_scoped_selectors', 'interval': '1m',
             'input_series': [same_labels, other_name, transient_bank, outside_family, outside_service],
             'promql_expr_test': [{'expr': churn_rule['expr'], 'eval_time': '20m',
                                   'exp_samples': [{'labels': '{}', 'value': 3}]}]},
        ]}))
    subprocess.run([PROMTOOL, 'test', 'rules', str(churn_fixture)], check=True)
    subprocess.run([PROMTOOL, 'check', 'rules', str(churn_rules)], check=True)
    assert RULES[CHURN]['expr'] == ('count(last_over_time({__name__=~"hindsight_.*",'
                                    'namespace="ai",service="hindsight-api"}[30m])) > 6000')
