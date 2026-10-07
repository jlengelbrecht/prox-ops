#!/usr/bin/env python3
"""Read-only, bounded Hindsight runnable-backlog snapshot; aggregate counts only.

"runnable" is the hindsight-api 0.9.2 PostgreSQL claim predicate evaluated in
one read-only snapshot of the public schema. It ignores worker slots, provider
availability and row locks, so it is not a guarantee that the next poll claims
those rows.
"""
import argparse
import datetime as dt
import json
import math
import os
import selectors
import subprocess
import sys
import time

SCHEMA = 'hindsight-queue-backlog/1'
DEFAULT_MAX_GROUPS = 256
HARD_MAX_GROUPS = 1024
MAX_OUTPUT = 1024 * 1024  # one aggregate row; anything larger fails closed, never truncates
MAX_COUNT = 10 ** 12
MAX_BANK_ID = 256
MAX_TYPE = 64
QUERY_DEADLINE = 15
REAP_TIMEOUT = 1
STATEMENT_TIMEOUT_MS = 10000
LOCK_TIMEOUT_MS = 1000
COUNTS = ('pending', 'payload_null_pending', 'deferred', 'due', 'runnable', 'serialization_blocked',
          'processing', 'assigned_pending', 'assigned_runnable', 'future_created_runnable')
AGES = ('runnable_oldest_task_age_seconds', 'runnable_oldest_retry_lateness_seconds')

# The only relations the live query reads. The native fixture swaps exactly
# this header for VALUES relations and runs SQL_BODY unchanged.
SQL_SOURCES = """WITH ops AS NOT MATERIALIZED (
 SELECT operation_id, bank_id, operation_type, status, task_payload, serialization_key,
        created_at, next_retry_at, worker_id
 FROM public.async_operations
), bank_ids AS NOT MATERIALIZED (
 SELECT bank_id FROM public.banks
)"""

# Mirrors claim_tasks / _claim_consolidation_* in hindsight-api 0.9.2:
# bank_serialization_sql on every type, document_serialization_sql on every
# type except consolidation, whose claim path does not apply it. worker_id is
# deliberately not part of the predicate; the claim query does not read it.
# active is MATERIALIZED so each row's correlated peer probes run once, not once per FILTER on runnable.
SQL_BODY = """, active AS MATERIALIZED (
 SELECT o.bank_id, o.operation_type, o.status, o.worker_id, o.created_at, o.next_retry_at,
        o.status = 'pending' AND o.task_payload IS NULL AS payload_null,
        o.status = 'pending' AND o.task_payload IS NOT NULL AND o.next_retry_at > now() AS deferred,
        o.status = 'pending' AND o.task_payload IS NOT NULL
          AND (o.next_retry_at IS NULL OR o.next_retry_at <= now()) AS due,
        o.status = 'pending' AND o.task_payload IS NOT NULL
          AND (o.next_retry_at IS NULL OR o.next_retry_at <= now())
          AND (o.operation_type NOT IN ('graph_maintenance', 'consolidation') OR NOT EXISTS (
            SELECT 1 FROM ops bank_peer
            WHERE bank_peer.bank_id = o.bank_id
              AND bank_peer.operation_type = o.operation_type
              AND (bank_peer.status = 'processing'
                   OR (bank_peer.status = 'pending'
                       AND bank_peer.task_payload IS NOT NULL
                       AND (bank_peer.next_retry_at IS NULL OR bank_peer.next_retry_at <= now())
                       AND (bank_peer.created_at < o.created_at
                            OR (bank_peer.created_at = o.created_at
                                AND bank_peer.operation_id < o.operation_id))))))
          AND (o.operation_type = 'consolidation' OR o.serialization_key IS NULL OR NOT EXISTS (
            SELECT 1 FROM ops doc_peer
            WHERE doc_peer.bank_id = o.bank_id
              AND doc_peer.serialization_key = o.serialization_key
              AND (doc_peer.status = 'processing'
                   OR (doc_peer.status = 'pending'
                       AND doc_peer.task_payload IS NOT NULL
                       AND (doc_peer.next_retry_at IS NULL OR doc_peer.next_retry_at <= now())
                       AND (doc_peer.created_at < o.created_at
                            OR (doc_peer.created_at = o.created_at
                                AND doc_peer.operation_id < o.operation_id)))))) AS runnable
 FROM ops o
 WHERE o.status IN ('pending', 'processing')
), grouped AS (
 SELECT bank_id, operation_type,
        count(*) FILTER (WHERE status = 'pending') AS pending,
        count(*) FILTER (WHERE payload_null) AS payload_null_pending,
        count(*) FILTER (WHERE deferred) AS deferred,
        count(*) FILTER (WHERE due) AS due,
        count(*) FILTER (WHERE runnable) AS runnable,
        count(*) FILTER (WHERE due AND NOT runnable) AS serialization_blocked,
        count(*) FILTER (WHERE status = 'processing') AS processing,
        count(*) FILTER (WHERE status = 'pending' AND worker_id IS NOT NULL) AS assigned_pending,
        count(*) FILTER (WHERE runnable AND worker_id IS NOT NULL) AS assigned_runnable,
        count(*) FILTER (WHERE runnable AND created_at > now()) AS future_created_runnable,
        CASE WHEN bool_or(runnable) THEN GREATEST(0, EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE runnable)))
        END AS runnable_oldest_task_age_seconds,
        EXTRACT(EPOCH FROM now() - min(next_retry_at) FILTER (WHERE runnable)) AS runnable_oldest_retry_lateness_seconds
 FROM active GROUP BY bank_id, operation_type
), listed AS (
 SELECT bank_id, true AS registered FROM bank_ids
 UNION
 SELECT g.bank_id, false FROM grouped g WHERE NOT EXISTS (SELECT 1 FROM bank_ids b WHERE b.bank_id = g.bank_id)
)
SELECT (CASE WHEN (SELECT count(*) FROM grouped) > {max_groups} OR (SELECT count(*) FROM listed) > {max_groups}
 THEN json_build_object('capacity_exceeded', true)
 ELSE json_build_object('capacity_exceeded', false,
  'observed_at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"+00:00"'),
  'banks', COALESCE((SELECT json_agg(json_build_object('bank', bank_id, 'registered', registered)) FROM listed), '[]'::json),
  'groups', COALESCE((SELECT json_agg(json_build_object('bank', bank_id, 'operation_type', operation_type,
    'pending', pending, 'payload_null_pending', payload_null_pending, 'deferred', deferred, 'due', due,
    'runnable', runnable, 'serialization_blocked', serialization_blocked, 'processing', processing,
    'assigned_pending', assigned_pending, 'assigned_runnable', assigned_runnable,
    'future_created_runnable', future_created_runnable, 'runnable_oldest_task_age_seconds', runnable_oldest_task_age_seconds,
    'runnable_oldest_retry_lateness_seconds', runnable_oldest_retry_lateness_seconds)) FROM grouped), '[]'::json))
END)::jsonb;"""

SEMANTICS = {
    'scope': 'public schema only; tenant or other non-default schemas are not covered',
    'runnable': 'hindsight-api 0.9.2 claim predicate in one read-only snapshot; ignores worker slots, '
                'provider availability and row locks; not a claim guarantee',
    'serialization_blocked': 'due rows held back by bank or document serialization; retain peers may be folded '
                             'into the head row, so this is not separate claim demand or a worker-capacity signal',
    'future_created_runnable': 'runnable rows whose created_at is after snapshot time (clock skew or late commit)',
    'runnable_oldest_task_age_seconds': 'snapshot time minus created_at of the oldest runnable row, floored at 0; '
                                        'task age, not continuous eligible wait',
    'runnable_oldest_retry_lateness_seconds': 'snapshot time minus the earliest next_retry_at among runnable rows; '
                                              'an upper bound, because next_retry_at survives recovery',
    'assigned_pending': 'pending rows carrying a worker_id; reported only, eligibility unchanged',
}


class BacklogUnknown(Exception):
    message = 'backlog unknown: read-only query failed'


class InvalidResult(BacklogUnknown):
    message = 'backlog unknown: invalid aggregate result'


class CapacityExceeded(BacklogUnknown):
    message = 'backlog unknown: group capacity exceeded'


class OutputBoundExceeded(BacklogUnknown):
    message = 'backlog unknown: output bound exceeded'


class CleanupIncomplete(BacklogUnknown):
    message = 'backlog unknown: read-only query failed; child cleanup incomplete'


def build_sql(max_groups):
    if type(max_groups) is not int or not 1 <= max_groups <= HARD_MAX_GROUPS:
        raise BacklogUnknown
    return SQL_SOURCES + SQL_BODY.format(max_groups=max_groups)


def psql_env():
    env = os.environ.copy()
    env['PGCONNECT_TIMEOUT'] = '5'
    env['PGOPTIONS'] = (f'-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS} '
                        f'-c lock_timeout={LOCK_TIMEOUT_MS} -c search_path=pg_catalog')
    return env


def psql_command(sql):
    # Repeated in-transaction so the bounds hold even if a service file overrides PGOPTIONS.
    preamble = (f'BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}; '
                f'SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}; SET LOCAL search_path = pg_catalog; '
                "SET LOCAL TIME ZONE 'UTC'; ")
    return ['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-c', preamble + sql + ' COMMIT;']


def _reap(process):
    """Return True only once the child is known to have exited."""
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=REAP_TIMEOUT)
    except ProcessLookupError:
        pass
    except (OSError, subprocess.TimeoutExpired):
        return False
    return process.poll() is not None


def _drain(process, deadline):
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise BacklogUnknown
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_OUTPUT:
                raise OutputBoundExceeded
    remaining = deadline - time.monotonic()
    if remaining <= 0 or process.wait(timeout=remaining) != 0:
        raise BacklogUnknown
    return bytes(data)


def run_query(sql, *, deadline_seconds=QUERY_DEADLINE):
    """One psql connection, one read-only transaction, one wall-clock deadline."""
    try:
        process = subprocess.Popen(psql_command(sql), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=psql_env())
    except OSError:
        raise BacklogUnknown from None
    try:
        return _drain(process, time.monotonic() + deadline_seconds)
    except BaseException as error:
        reaped = _reap(process)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if not reaped:
            raise CleanupIncomplete from None
        raise (OutputBoundExceeded if isinstance(error, OutputBoundExceeded) else BacklogUnknown) from None
    finally:
        process.stdout.close()


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError
    return value


def _bank_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_BANK_ID or not value.isprintable():
        raise ValueError
    return value


def _operation_type(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= MAX_TYPE or
            not all(ch.isascii() and (ch.isalnum() or ch in '_-') for ch in value)):
        raise ValueError
    return value


def _count(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise ValueError
    return value


def _age(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError
    return round(float(value), 3)


def _group(item, banks):
    if not isinstance(item, dict) or set(item) != {'bank', 'operation_type', *COUNTS, *AGES}:
        raise ValueError
    bank = _bank_id(item['bank'])
    if bank not in banks:
        raise ValueError
    group = {'operation_type': _operation_type(item['operation_type'])}
    group.update((name, _count(item[name])) for name in COUNTS)
    group.update((name, _age(item[name])) for name in AGES)
    if (group['payload_null_pending'] + group['deferred'] + group['due'] != group['pending'] or
            group['runnable'] + group['serialization_blocked'] != group['due'] or
            group['assigned_pending'] > group['pending'] or
            group['assigned_runnable'] > min(group['runnable'], group['assigned_pending']) or
            group['future_created_runnable'] > group['runnable']):
        raise ValueError
    age = group['runnable_oldest_task_age_seconds']
    if group['runnable'] == 0:
        if any(group[name] is not None for name in AGES):
            raise ValueError
    elif age is None or (group['future_created_runnable'] == group['runnable'] and age != 0):
        raise ValueError
    return bank, group


def _rollup(groups):
    totals = {name: sum(group[name] for group in groups) for name in COUNTS}
    for name in AGES:
        values = [group[name] for group in groups if group[name] is not None]
        totals[name] = max(values) if values else None
    return totals


def parse_result(data, max_groups):
    """Accept one validated aggregate document or raise a fixed error."""
    if len(data) > MAX_OUTPUT:
        raise OutputBoundExceeded
    try:
        if not data.endswith(b'\n') or data.count(b'\n') != 1:
            raise ValueError
        raw = json.loads(data.decode('utf-8'), parse_constant=_finite_float, parse_float=_finite_float)
        if not isinstance(raw, dict) or type(raw.get('capacity_exceeded')) is not bool:
            raise ValueError
        if raw['capacity_exceeded']:
            raise CapacityExceeded if set(raw) == {'capacity_exceeded'} else ValueError
        if set(raw) != {'capacity_exceeded', 'observed_at', 'banks', 'groups'}:
            raise ValueError
        observed = dt.datetime.fromisoformat(raw['observed_at'])
        if observed.utcoffset() != dt.timedelta(0):
            raise ValueError
        listed, groups = raw['banks'], raw['groups']
        if not isinstance(listed, list) or not isinstance(groups, list):
            raise ValueError
        if len(listed) > max_groups or len(groups) > max_groups:
            raise CapacityExceeded
        banks = {}
        for item in listed:
            if not isinstance(item, dict) or set(item) != {'bank', 'registered'}:
                raise ValueError
            bank = _bank_id(item['bank'])
            if bank in banks or type(item['registered']) is not bool:
                raise ValueError
            banks[bank] = {'registered': item['registered'], 'types': {}}
        for item in groups:
            bank, group = _group(item, banks)
            if group['operation_type'] in banks[bank]['types']:
                raise ValueError
            banks[bank]['types'][group['operation_type']] = group
    except CapacityExceeded:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, UnicodeDecodeError, OverflowError):
        raise InvalidResult from None
    report = []
    for bank in sorted(banks):
        types = [banks[bank]['types'][name] for name in sorted(banks[bank]['types'])]
        if not banks[bank]['registered'] and not types:
            raise InvalidResult
        report.append({'bank': bank, 'registered': banks[bank]['registered'],
                       'totals': _rollup(types), 'types': types})
    return {'schema': SCHEMA, 'observed_at': observed.isoformat(), 'semantics': SEMANTICS,
            'max_groups': max_groups, 'totals': _rollup([bank['totals'] for bank in report]), 'banks': report}


def collect(max_groups=DEFAULT_MAX_GROUPS, *, deadline_seconds=QUERY_DEADLINE):
    return parse_result(run_query(build_sql(max_groups), deadline_seconds=deadline_seconds), max_groups)


def _bound(text):
    value = int(text) if text.isascii() and text.isdecimal() else 0
    if not 1 <= value <= HARD_MAX_GROUPS:
        raise argparse.ArgumentTypeError(f'must be an integer from 1 to {HARD_MAX_GROUPS}')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-groups', type=_bound, default=DEFAULT_MAX_GROUPS,
                        help='fail closed when banks or bank/type groups exceed this bound')
    args = parser.parse_args()
    try:
        result = collect(args.max_groups)
    except BacklogUnknown as error:
        print(error.message, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
