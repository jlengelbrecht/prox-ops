#!/usr/bin/env python3
"""Classify protected JSON API pod logs into fixed UTC window counts only."""
import argparse
import datetime as dt
import json
import math
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
WORKER = re.compile(rf'^Task {UUID} (scheduled for retry at|deferred until|timed out:|failed:) .*$')
CATEGORIES = ('worker_retry', 'worker_deferred', 'worker_timeout', 'worker_failed',
              'provider_429_evidence', 'auth_refresh_error')
MAX_LINE = 65536
CAPTURE_TIMEOUT = 15
MIN_P95_OBSERVATIONS = 100  # at least five observations in the upper 5% tail
WORKER_LOGGER = 'hindsight_api.worker.poller'
CODEX_LLM_LOGGER = 'hindsight_api.engine.providers.codex_llm'
CODEX_AUTH_LOGGER = 'hindsight_api.engine.providers.codex_auth'


def bounded_lines(stream):
    """Never materialize a source entry larger than MAX_LINE bytes."""
    while True:
        chunk = stream.readline(MAX_LINE + 1)
        if not chunk:
            return
        if len(chunk) > MAX_LINE:
            while chunk and not chunk.endswith(b'\n'):
                chunk = stream.readline(MAX_LINE + 1)
            yield None
            continue
        try:
            yield chunk.decode('utf-8')
        except UnicodeDecodeError:
            yield None


def bound(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
            raise ValueError
        return parsed.astimezone(dt.timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError('invalid UTC bound') from None


def counts(lines, start, end):
    first, last = bound(start), bound(end)
    if first >= last or last - first > dt.timedelta(days=1):
        raise ValueError('invalid window')
    result = dict.fromkeys(CATEGORIES, 0)
    result.update(source_events=0, malformed_lines=0, in_window_lines=0,
                  source_lines=0, first_timestamp=None, last_timestamp=None)
    for line in lines:
        result['source_lines'] += 1
        if line is None or len(line.encode('utf-8')) > MAX_LINE:
            result['malformed_lines'] += 1
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError
            stamp = bound(event.get('timestamp'))
            message = event.get('message')
            logger = event.get('logger')
            if not isinstance(message, str) or not isinstance(logger, str):
                raise ValueError
        except (ValueError, TypeError):
            result['malformed_lines'] += 1
            continue
        stamp_text = stamp.isoformat()
        if result['first_timestamp'] is None or stamp_text < result['first_timestamp']:
            result['first_timestamp'] = stamp_text
        if result['last_timestamp'] is None or stamp_text > result['last_timestamp']:
            result['last_timestamp'] = stamp_text
        if not first <= stamp < last:
            continue
        result['in_window_lines'] += 1
        match = WORKER.fullmatch(message)
        if match and logger == WORKER_LOGGER:
            category = {'scheduled for retry at': 'worker_retry', 'deferred until': 'worker_deferred',
                        'timed out:': 'worker_timeout', 'failed:': 'worker_failed'}[match.group(1)]
            result[category] += 1
            result['source_events'] += 1
            # The arbitrary exception suffix identifies no provider. Only
            # the dedicated Codex logger below supplies provider provenance.
        if logger == CODEX_LLM_LOGGER and message.startswith('Codex LLM quota exhausted for '):
            result['provider_429_evidence'] += 1
        if logger in (CODEX_LLM_LOGGER, CODEX_AUTH_LOGGER) and (
            message.startswith('Codex refresh_token is permanently invalid;') or
            message.startswith('Codex auth error (HTTP 401):') or
            message.startswith('Codex auth error (HTTP 403):') or
            message.startswith('Codex auth file unreadable when loading refresh_token:')
        ):
            result['auth_refresh_error'] += 1
    return {'schema': 'hindsight-source-counts/1', 'start_utc': first.isoformat(),
            'end_utc': last.isoformat(), **result}


def _count(value):
    if type(value) is not int or value < 0:
        raise ValueError('incomplete comparison')
    return value


def _completion(value):
    # Prometheus increase() extrapolates observed counter changes. Its finite
    # estimate is not an exact event count and must never be rounded to one.
    return _p95(value)


def _p95(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('incomplete comparison')
    return value


def _map(sample, key, expected, validate):
    value = sample.get(key)
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError('incomplete comparison')
    return {name: validate(item) for name, item in value.items()}


def _window(sample):
    if not isinstance(sample, dict):
        raise ValueError('incomplete comparison')
    start, end = bound(sample.get('start_utc')), bound(sample.get('end_utc'))
    if start >= end or end - start > dt.timedelta(days=1):
        raise ValueError('incomplete comparison')
    return start, end


def trial_regressions(reference_a, reference_b, trial, expected_banks):
    """Do not authorize a cap decision without protected source receipts.

    No complete provider/outcome producer exists in this rollout. Caller-set
    booleans, even with plausible metric values, cannot certify those sources.
    The candidate arithmetic is retained below for a later receipt-backed API.
    """
    raise ValueError('incomplete comparison')


def _candidate_regressions(reference_a, reference_b, trial, expected_banks):
    """Evaluate only synthetic candidate evidence; never authorize a trial."""
    if not isinstance(expected_banks, (set, frozenset)) or not expected_banks or not all(
            isinstance(bank, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', bank) for bank in expected_banks):
        raise ValueError('incomplete comparison')
    count_keys = ('provider_429', 'auth_refresh_error', 'worker_timeout', 'readiness_failure')
    coverage_keys = ('source_coverage', 'scrape_coverage', 'lifecycle_complete',
                     'provider_evidence_complete', 'outcome_evidence_complete', 'db_wait_coverage')
    samples = (reference_a, reference_b, trial)
    try:
        windows = [_window(sample) for sample in samples]
        duration = windows[0][1] - windows[0][0]
        if any(end - start != duration for start, end in windows) or any(
                windows[i][1] != windows[i + 1][0] for i in (0, 1)):
            raise ValueError('incomplete comparison')
        llm_keys = set(reference_a['llm_p95'])
        if not llm_keys or not all(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', key) for key in llm_keys):
            raise ValueError('incomplete comparison')
        evidence = []
        for sample in samples:
            if any(sample.get(key) is not True for key in coverage_keys):
                raise ValueError('incomplete comparison')
            item = {key: _count(sample.get(key)) for key in count_keys}
            item['llm_p95'] = _map(sample, 'llm_p95', llm_keys, _p95)
            for key in ('recall_p95', 'reflect_p95'):
                item[key] = _map(sample, key, expected_banks, _p95)
            for metric, scopes in (('llm', llm_keys), ('recall', expected_banks),
                                   ('reflect', expected_banks)):
                observations = _map(sample, metric + '_observations', scopes, _count)
                if any(n < MIN_P95_OBSERVATIONS for n in observations.values()):
                    raise ValueError('incomplete comparison')
            item['completions'] = _map(sample, 'completions', expected_banks, _completion)
            item['completion_error'] = _map(sample, 'completion_error', expected_banks, _completion)
            measures = _map(sample, 'completion_measure', expected_banks, lambda value: value)
            for bank in expected_banks:
                if measures[bank] == 'raw_delta':
                    if type(item['completions'][bank]) is not int or item['completion_error'][bank] != 0:
                        raise ValueError('incomplete comparison')
                elif measures[bank] == 'prometheus_estimate':
                    if item['completion_error'][bank] <= 0:
                        raise ValueError('incomplete comparison')
                else:
                    raise ValueError('incomplete comparison')
            item['pending'] = _map(sample, 'pending', expected_banks, _count)
            if type(sample.get('db_wait_five_minutes')) is not bool:
                raise ValueError('incomplete comparison')
            item['db_wait_five_minutes'] = sample['db_wait_five_minutes']
            evidence.append(item)
        a, b, t = evidence
        if any(a['completions'][bank] == 0 or b['completions'][bank] == 0
               for bank in expected_banks):
            raise ValueError('incomplete comparison')
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('incomplete comparison') from None
    reasons = []
    for key in count_keys:
        ceiling = max(a[key], b[key])
        if t[key] > 0 and ceiling == 0 or ceiling > 0 and t[key] >= 2 * ceiling:
            reasons.append(key)
    for key in ('llm_p95', 'recall_p95', 'reflect_p95'):
        for scope in sorted(t[key]):
            if t[key][scope] > 1.5 * max(a[key][scope], b[key][scope]):
                reasons.append(key + ':' + scope)
    for bank in sorted(expected_banks):
        reference_lower = min(a['completions'][bank] - a['completion_error'][bank],
                              b['completions'][bank] - b['completion_error'][bank])
        reference_upper = min(a['completions'][bank] + a['completion_error'][bank],
                              b['completions'][bank] + b['completion_error'][bank])
        trial_lower = max(0, t['completions'][bank] - t['completion_error'][bank])
        trial_upper = t['completions'][bank] + t['completion_error'][bank]
        if reference_lower <= 0:
            raise ValueError('incomplete comparison')
        if t['pending'][bank] > b['pending'][bank]:
            if trial_upper < .5 * reference_lower:
                reasons.append('throughput:' + bank)
            elif trial_lower < .5 * reference_upper:
                raise ValueError('incomplete comparison')
    if t['db_wait_five_minutes']:
        reasons.append('db_wait_five_minutes')
    return reasons


IDENTITY = ('pod_uid', 'container_id', 'log_source')


def identity(source):
    if not isinstance(source, dict) or not all(
            isinstance(source.get(key), str) and source[key] for key in IDENTITY):
        raise ValueError('source coverage unknown')
    if (not re.fullmatch(UUID, source['pod_uid']) or
            not re.fullmatch(r'(?:containerd|docker|cri-o)://[0-9a-f]{64}', source['container_id']) or
            source['log_source'] not in ('current', 'previous')):
        raise ValueError('source coverage unknown')
    return tuple(source[key] for key in IDENTITY)


def observed_source(pod, log_source):
    if not re.fullmatch(r'hindsight-api-[a-z0-9-]{1,63}', pod):
        raise ValueError('source coverage unknown')
    run = subprocess.run(['kubectl', 'get', 'pod', pod, '-n', 'ai', '-o', 'json'],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, timeout=15, check=False)
    if run.returncode or len(run.stdout) > 1024 * 1024:
        raise ValueError('source coverage unknown')
    try:
        item = json.loads(run.stdout)
        uid = item['metadata']['uid']
        statuses = [x for x in item['status']['containerStatuses'] if x['name'] == 'api']
        if len(statuses) != 1:
            raise ValueError
        status = statuses[0]
        container = (status['containerID'] if log_source == 'current' else
                     status['lastState']['terminated']['containerID'])
        observed = {'pod_uid': uid, 'container_id': container, 'log_source': log_source}
        identity(observed)
        return observed
    except (KeyError, TypeError, ValueError):
        raise ValueError('source coverage unknown') from None


def capture_source(pod, key, start, end):
    """Bind the streamed bytes to kubectl's physical source on both sides."""
    before = observed_source(pod, key[2])
    if identity(before) != key:
        raise ValueError('source coverage unknown')
    command = ['kubectl', 'logs', '-n', 'ai', 'pod/' + pod, '-c', 'api',
               '--since-time=' + bound(start).isoformat(), '--timestamps=false']
    if key[2] == 'previous':
        command.append('--previous')
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + CAPTURE_TIMEOUT
    timer = threading.Timer(CAPTURE_TIMEOUT, process.kill)
    timer.daemon = True
    timer.start()
    try:
        result = counts(bounded_lines(process.stdout), start, end)
        status = process.wait(timeout=max(0, deadline - time.monotonic()))
        if time.monotonic() >= deadline:
            raise ValueError('source coverage unknown')
    except BaseException as exc:
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        if isinstance(exc, subprocess.TimeoutExpired):
            raise ValueError('source coverage unknown') from None
        raise
    finally:
        timer.cancel()
        process.stdout.close()
    if status != 0:
        raise ValueError('source coverage unknown')
    after = observed_source(pod, key[2])
    if after != before:
        raise ValueError('source coverage unknown')
    result.update(zip(IDENTITY, key))
    result.update(capture_before=before, capture_after=after, producer_exit_code=status)
    return result


def validate_coverage(manifest, start, end):
    """Require independent inventory, lifecycle, rotation and retention proof."""
    first, last = bound(start), bound(end)
    if first >= last or not isinstance(manifest, dict) or any(
            manifest.get(key) is not True for key in
            ('complete', 'inventory_complete', 'lifecycle_complete')):
        raise ValueError('source coverage unknown')
    sources = manifest.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('source coverage unknown')
    seen, intervals = set(), []
    for source in sources:
        key = identity(source)
        physical = key[:2]
        if physical in seen:
            raise ValueError('source coverage unknown')
        seen.add(physical)
        if any(source.get(flag) is not True for flag in
               ('lifecycle_verified', 'rotation_verified', 'retention_verified')):
            raise ValueError('source coverage unknown')
        if type(source.get('producer_exit_code')) is not int or source['producer_exit_code'] != 0:
            raise ValueError('source coverage unknown')
        if type(source.get('source_lines')) is not int or source['source_lines'] < 0:
            raise ValueError('source coverage unknown')
        begin, finish = bound(source.get('interval_start')), bound(source.get('interval_end'))
        life_begin = bound(source.get('lifecycle_start'))
        life_end = bound(source.get('lifecycle_end'))
        # A retrospective --since-time read may include later lines. Its
        # independently verified interval may extend past this comparison
        # window, while the interval intersection must still cover the window.
        if not (begin < finish and life_begin < life_end and
                begin < last and finish > first and
                life_begin <= begin < finish <= life_end):
            raise ValueError('source coverage unknown')
        required_begin, required_end = max(first, life_begin), min(last, life_end)
        if required_begin >= required_end or begin > required_begin or finish < required_end:
            raise ValueError('source coverage unknown')
        stamps = (source.get('first_timestamp'), source.get('last_timestamp'))
        if source['source_lines'] == 0:
            if stamps != (None, None):
                raise ValueError('source coverage unknown')
        elif not (isinstance(stamps[0], str) and isinstance(stamps[1], str) and
                  begin <= bound(stamps[0]) <= bound(stamps[1]) < finish):
            raise ValueError('source coverage unknown')
        intervals.append((life_begin, life_end))
    cursor = first
    for begin, finish in sorted(intervals):
        if begin > cursor:
            raise ValueError('source coverage unknown')
        cursor = max(cursor, finish)
    if cursor < last:
        raise ValueError('source coverage unknown')
    return {identity(source): source for source in sources}


def aggregate_receipts(manifest, receipts, start, end):
    """Count only when every inventoried source has one verified receipt."""
    sources = validate_coverage(manifest, start, end)
    if not isinstance(receipts, list) or len(receipts) != len(sources):
        raise ValueError('source coverage unknown')
    actual = {}
    for receipt in receipts:
        key = identity(receipt)
        if key not in sources or key in actual or receipt.get('schema') != 'hindsight-source-counts/1':
            raise ValueError('source coverage unknown')
        expected_fields = set(IDENTITY) | {'schema', 'start_utc', 'end_utc', *CATEGORIES,
            'source_events', 'malformed_lines', 'in_window_lines', 'source_lines',
            'first_timestamp', 'last_timestamp', 'capture_before', 'capture_after',
            'producer_exit_code'}
        if set(receipt) != expected_fields:
            raise ValueError('source coverage unknown')
        expected_identity = dict(zip(IDENTITY, key))
        if (receipt['capture_before'] != expected_identity or
                receipt['capture_after'] != expected_identity or
                type(receipt['producer_exit_code']) is not int or
                receipt['producer_exit_code'] != 0):
            raise ValueError('source coverage unknown')
        if receipt.get('start_utc') != bound(start).isoformat() or receipt.get('end_utc') != bound(end).isoformat():
            raise ValueError('source coverage unknown')
        if (receipt.get('source_lines') != sources[key]['source_lines'] or
                type(receipt.get('malformed_lines')) is not int or receipt['malformed_lines'] != 0):
            raise ValueError('source coverage unknown')
        if any(type(receipt.get(name)) is not int or receipt[name] < 0 for name in
               (*CATEGORIES, 'source_events', 'in_window_lines', 'source_lines')):
            raise ValueError('source coverage unknown')
        if not (receipt['in_window_lines'] <= receipt['source_lines'] and
                receipt['source_events'] <= receipt['in_window_lines'] and
                receipt['source_events'] == sum(receipt[name] for name in CATEGORIES[:4]) and
                receipt['provider_429_evidence'] <= receipt['in_window_lines'] and
                receipt['auth_refresh_error'] <= receipt['in_window_lines']):
            raise ValueError('source coverage unknown')
        for field in ('first_timestamp', 'last_timestamp'):
            expected = sources[key][field]
            if receipt.get(field) != (bound(expected).isoformat() if expected else None):
                raise ValueError('source coverage unknown')
        actual[key] = receipt
    result = {name: sum(receipt[name] for receipt in actual.values())
              for name in (*CATEGORIES, 'source_events', 'in_window_lines', 'source_lines')}
    return {'schema': 'hindsight-source-aggregate/1', 'start_utc': bound(start).isoformat(),
            'end_utc': bound(end).isoformat(), 'verified_sources': len(actual), **result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-utc', required=True)
    parser.add_argument('--end-utc', required=True)
    parser.add_argument('--coverage-file', type=Path, required=True)
    parser.add_argument('--capture-pod')
    parser.add_argument('--pod-uid')
    parser.add_argument('--container-id')
    parser.add_argument('--log-source', choices=('current', 'previous'))
    parser.add_argument('--receipts-dir', type=Path)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.coverage_file.read_text(encoding='utf-8'))
        sources = validate_coverage(manifest, args.start_utc, args.end_utc)
        if args.receipts_dir:
            if any((args.pod_uid, args.container_id, args.log_source, args.capture_pod)):
                raise ValueError('source coverage unknown')
            files = sorted(args.receipts_dir.glob('*.json'))
            result = aggregate_receipts(manifest, [json.loads(p.read_text()) for p in files],
                                        args.start_utc, args.end_utc)
        else:
            key = (args.pod_uid, args.container_id, args.log_source)
            if key not in sources or not args.capture_pod:
                raise ValueError('source coverage unknown')
            result = capture_source(args.capture_pod, key, args.start_utc, args.end_utc)
            if (result['source_lines'] != sources[key]['source_lines'] or
                    result['malformed_lines'] != 0):
                raise ValueError('source coverage unknown')
            for field in ('first_timestamp', 'last_timestamp'):
                expected = sources[key][field]
                if result[field] != (bound(expected).isoformat() if expected else None):
                    raise ValueError('source coverage unknown')
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        print('source coverage unknown', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
