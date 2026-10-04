#!/usr/bin/env python3
"""Read-only, bounded aggregate Hindsight timing diagnostic; never print source rows."""
import argparse
import datetime as dt
import json
import math
import os
import re
import selectors
import time
import subprocess
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path

MAX_SAMPLES_PER_GROUP = 5000
MAX_GROUPS = 64
MAX_LINE = 4096
FIELDS = ('queue_seconds', 'claimed_wall_seconds', 'total_seconds')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
FRACTIONAL_TIMESTAMP = re.compile(
    r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.)(\d*)(Z|[+-]\d{2}:\d{2})\Z')
SQL = """WITH eligible AS MATERIALIZED (
 SELECT operation_id, bank_id, operation_type, created_at, claimed_at, completed_at
 FROM public.async_operations
 WHERE status = 'completed' AND operation_type <> 'batch_retain'
   AND completed_at >= TIMESTAMPTZ '{start}' AND completed_at < TIMESTAMPTZ '{end}'
   AND claimed_at IS NOT NULL AND created_at IS NOT NULL
   AND claimed_at >= created_at AND completed_at >= claimed_at
   AND COALESCE(retry_count, 0) = 0
), ranked AS (
 SELECT *, ROW_NUMBER() OVER (PARTITION BY bank_id, operation_type ORDER BY completed_at DESC, operation_id DESC) AS rn,
        COUNT(*) OVER (PARTITION BY bank_id, operation_type) AS eligible_count
 FROM eligible
)
SELECT json_build_object('bank_id', bank_id, 'operation_type', operation_type,
 'completed_at', completed_at, 'eligible_count', eligible_count,
 'queue_seconds', EXTRACT(EPOCH FROM claimed_at - created_at),
 'claimed_wall_seconds', EXTRACT(EPOCH FROM completed_at - claimed_at),
 'total_seconds', EXTRACT(EPOCH FROM completed_at - created_at))
FROM ranked WHERE rn <= {limit} ORDER BY bank_id, operation_type, completed_at DESC, operation_id DESC;"""


def parse_bound(value):
    try:
        if isinstance(value, str):
            match = FRACTIONAL_TIMESTAMP.fullmatch(value)
            if match:
                prefix, fraction, timezone = match.groups()
                if not 1 <= len(fraction) <= 6:
                    raise ValueError
                value = prefix + fraction.ljust(6, '0') + timezone
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() != dt.timedelta(0):
            raise ValueError
        return result.astimezone(dt.timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError('invalid UTC bound') from None


def bounds(start, end):
    first, last = parse_bound(start), parse_bound(end)
    if not dt.timedelta(minutes=30) <= last - first <= dt.timedelta(days=1):
        raise ValueError('invalid window')
    return first, last


def percentile(values, fraction):
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize(lines, start, end):
    first, last = bounds(start, end)
    groups = defaultdict(lambda: {field: [] for field in FIELDS})
    eligible = {}
    minimum = maximum = None
    count = 0
    invalid = False
    for number, line in enumerate(lines, 1):
        if invalid:
            continue
        try:
            if len(line) > MAX_LINE:
                raise ValueError('invalid timing row')
            if not line.strip():
                continue
            count += 1
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or set(row) != {'bank_id', 'operation_type', 'completed_at', 'eligible_count', *FIELDS}:
                    raise ValueError
                bank, kind = row['bank_id'], row['operation_type']
                if not all(isinstance(v, str) and IDENTIFIER.fullmatch(v) for v in (bank, kind)) or kind == 'batch_retain':
                    raise ValueError
                completed = parse_bound(row['completed_at'])
                if not first <= completed < last:
                    raise ValueError
                total = row['eligible_count']
                if type(total) is not int or total < 1:
                    raise ValueError
                key = (bank, kind)
                if key in eligible and eligible[key] != total:
                    raise ValueError
                eligible[key] = total
                if len(eligible) > MAX_GROUPS or len(groups[key][FIELDS[0]]) >= MAX_SAMPLES_PER_GROUP:
                    raise ValueError
                values = [float(row[field]) for field in FIELDS]
                if any(not math.isfinite(v) or v < 0 for v in values) or abs(values[0] + values[1] - values[2]) > .01:
                    raise ValueError
            except (ValueError, TypeError, KeyError, OverflowError):
                raise ValueError('invalid timing row') from None
            minimum = min(minimum, completed) if minimum else completed
            maximum = max(maximum, completed) if maximum else completed
            for field, value in zip(FIELDS, values):
                groups[key][field].append(value)
        except ValueError:
            invalid = True
            continue
    if invalid:
        raise ValueError('invalid timing cohort')
    result = []
    for (bank, kind), samples in sorted(groups.items()):
        sample_count = len(samples[FIELDS[0]])
        if sample_count != min(eligible[(bank, kind)], MAX_SAMPLES_PER_GROUP):
            raise ValueError('incomplete timing cohort')
        item = {'bank_id': bank, 'operation_type': kind, 'samples': sample_count,
                'eligible_rows': eligible[(bank, kind)],
                'coverage': round(sample_count / eligible[(bank, kind)], 6),
                'truncated': eligible[(bank, kind)] > sample_count}
        for field in FIELDS:
            values = sorted(samples[field])
            item[field] = {f'p{int(p * 100)}': round(percentile(values, p), 3)
                           for p in (.5, .9, .95, .99)}
        result.append(item)
    present = {x['bank_id'] for x in result}
    return {'schema': 'hindsight-timing-baseline/3',
            'cohort': {'status': 'completed', 'operation_type': 'non_batch_retain',
                       'retry_count': 'zero_recorded', 'timestamps': 'ordered_nonnull',
                       'membership': 'completed_at_in_utc_half_open_window',
                       'start_utc': first.isoformat(), 'end_utc': last.isoformat()},
            'sample_limit_per_bank_type': MAX_SAMPLES_PER_GROUP,
            'sampled_rows': count,
            'missing_reference_banks': sorted({'devb0x', 'obsidian'} - present),
            'observed_completion_range': [minimum.isoformat() if minimum else None,
                                          maximum.isoformat() if maximum else None],
            'groups': result}


def query_lines(start, end, *, deadline_seconds=25):
    """Drain psql under one wall-clock deadline, including a stalled pipe."""
    first, last = bounds(start, end)
    sql = SQL.format(start=first.isoformat(), end=last.isoformat(), limit=MAX_SAMPLES_PER_GROUP)
    command = ['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-c',
               'BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = 15000; ' + sql + ' COMMIT;']
    env = os.environ.copy()
    env['PGCONNECT_TIMEOUT'] = '5'
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env)
    deadline = time.monotonic() + deadline_seconds
    buffer = bytearray()
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError('query failed')
            if not selector.select(remaining):
                raise ValueError('query failed')
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > MAX_LINE and b'\n' not in buffer:
                raise ValueError('query failed')
            while b'\n' in buffer:
                line, _, rest = buffer.partition(b'\n')
                buffer = bytearray(rest)
                if len(line) > MAX_LINE:
                    raise ValueError('query failed')
                try:
                    yield line.decode('utf-8') + '\n'
                except UnicodeDecodeError:
                    raise ValueError('query failed') from None
        if buffer:
            if len(buffer) > MAX_LINE:
                raise ValueError('query failed')
            try:
                yield buffer.decode('utf-8')
            except UnicodeDecodeError:
                raise ValueError('query failed') from None
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining):
            raise ValueError('query failed')
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start-utc', required=True)
    parser.add_argument('--end-utc', required=True)
    parser.add_argument('--input', type=Path, help='synthetic JSONL only')
    args = parser.parse_args()
    try:
        bounds(args.start_utc, args.end_utc)
        if args.input:
            with args.input.open(encoding='utf-8') as stream:
                result = summarize(stream, args.start_utc, args.end_utc)
        else:
            with closing(query_lines(args.start_utc, args.end_utc)) as lines:
                result = summarize(lines, args.start_utc, args.end_utc)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        print('baseline unavailable: invalid input or read-only query', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
