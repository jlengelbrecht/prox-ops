#!/usr/bin/env python3
"""Native PostgreSQL proof of the runnable-backlog query over VALUES relations only, and of its session bounds."""
import importlib.util
import os
import pathlib
import subprocess
import time
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('backlog', ROOT / 'scripts/hindsight-queue-backlog.py')
BACKLOG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BACKLOG)

# (id, bank, type, status, has_payload, serialization_key, created minutes ago,
#  next_retry_at offset in minutes from now or None, worker_id)
ROWS = [
    (1, 'alpha', 'retain', 'pending', False, None, 60, None, None),          # payload-null parent
    (2, 'alpha', 'refresh_mental_model', 'failed', True, None, 59, None, None),  # historical failure
    (3, 'alpha', 'retain', 'pending', True, None, 58, 10, None),             # future retry
    (4, 'alpha', 'retain', 'pending', True, 'doc-a', 50, None, None),        # oldest of doc-a
    (5, 'alpha', 'retain', 'pending', True, 'doc-a', 50, -5, None),          # same created_at, later id
    (6, 'alpha', 'retain', 'pending', True, 'doc-a', 40, None, None),
    (7, 'alpha', 'retain', 'pending', True, 'doc-b', 45, None, None),        # doc-b busy
    (8, 'alpha', 'retain', 'processing', True, 'doc-b', 70, None, 'w1'),
    (9, 'alpha', 'retain', 'pending', True, 'doc-c', 30, -7, None),          # independent document
    (10, 'alpha', 'retain', 'pending', True, 'doc-d', 55, 10, None),         # deferred older peer
    (11, 'alpha', 'retain', 'pending', True, 'doc-d', 20, None, None),       # not blocked by it
    (12, 'alpha', 'graph_maintenance', 'processing', True, None, 90, None, 'w1'),
    (13, 'alpha', 'graph_maintenance', 'pending', True, None, 65, None, None),  # busy bank
    (14, 'alpha', 'consolidation', 'pending', True, 'doc-e', 35, None, None),  # oldest consolidation, keyed
    (15, 'alpha', 'consolidation', 'pending', True, None, 25, None, None),
    (16, 'alpha', 'retain', 'completed', True, None, 80, None, 'w1'),
    (17, 'beta', 'retain', 'pending', True, None, 15, -2, 'w-stale'),        # assigned pending
    (18, 'beta', 'retain', 'processing', True, 'doc-x', 70, None, 'w2'),
    (19, 'beta', 'consolidation', 'pending', True, 'doc-x', 10, None, None),  # no document rule
    (20, 'beta', 'retain', 'pending', True, None, 12, 0, None),              # retry due exactly now
    (21, 'beta', 'graph_maintenance', 'pending', True, None, 8, None, None),  # other bank is busy
    (22, 'ghost', 'retain', 'pending', True, None, 5, None, None),           # bank row missing
    (23, 'gamma', 'retain', 'pending', True, None, -2, None, None),          # created after snapshot time
    (24, 'gamma', 'refresh_mental_model', 'pending', True, 'doc-a', 5, None, None),  # alpha's doc-a is another bank
    (25, 'gamma', 'refresh_mental_model', 'pending', True, 'doc-a', 3, None, None),  # non-retain document peer
    (26, 'gamma', 'graph_maintenance', 'pending', True, None, 7, None, None),  # bank tie, lower id runs
    (27, 'gamma', 'graph_maintenance', 'pending', True, None, 7, None, None),
    (28, 'gamma', 'graph_maintenance', 'pending', False, None, 20, None, None),  # payload-null bank peer
    (29, 'gamma', 'graph_maintenance', 'pending', True, None, 30, 10, None),  # deferred bank peer
    (30, 'alpha', 'retain', 'pending', True, 'doc-e', 30, None, None),       # held by row 14: document rule is type-agnostic
]
BANKS = ('alpha', 'beta', 'empty', 'gamma')


def literal(text):
    return 'NULL::text' if text is None else "'" + text.replace("'", "''") + "'::text"


def values(row):
    ident, bank, kind, status, payload, key, created, retry, worker = row
    return ('(' + ', '.join([
        f"'00000000-0000-0000-0000-{ident:012d}'::uuid", literal(bank), literal(kind), literal(status),
        """'{"synthetic": true}'::jsonb""" if payload else 'NULL::jsonb', literal(key),
        f"now() - interval '{created} minutes'",
        'NULL::timestamptz' if retry is None else f"now() + interval '{retry} minutes'",
        literal(worker)]) + ')')


def sources(rows, banks):
    return ('WITH ops(operation_id, bank_id, operation_type, status, task_payload, serialization_key, '
            'created_at, next_retry_at, worker_id) AS (VALUES ' + ', '.join(map(values, rows)) +
            '), bank_ids(bank_id) AS (VALUES ' + ', '.join(f'({literal(b)})' for b in banks) + ')')


def group(pending=0, age=None, lateness=None, **counts):
    item = {**dict.fromkeys(BACKLOG.COUNTS, 0), **counts, 'pending': pending}
    return {**item, 'runnable_oldest_task_age_seconds': age, 'runnable_oldest_retry_lateness_seconds': lateness}


EXPECTED = {
    'alpha': {
        'consolidation': group(2, due=2, runnable=1, serialization_blocked=1, age=2100.0),
        'graph_maintenance': group(1, due=1, serialization_blocked=1, processing=1),
        'retain': group(10, payload_null_pending=1, deferred=2, due=7, runnable=3,
                        serialization_blocked=4, processing=1, age=3000.0, lateness=420.0),
    },
    'beta': {
        'consolidation': group(1, due=1, runnable=1, age=600.0),
        'graph_maintenance': group(1, due=1, runnable=1, age=480.0),
        'retain': group(2, due=2, runnable=2, processing=1, assigned_pending=1,
                        assigned_runnable=1, age=900.0, lateness=120.0),
    },
    'empty': {},
    'gamma': {'graph_maintenance': group(4, payload_null_pending=1, deferred=1, due=2, runnable=1,
                                         serialization_blocked=1, age=420.0),
              'refresh_mental_model': group(2, due=2, runnable=1, serialization_blocked=1, age=300.0),
              'retain': group(1, due=1, runnable=1, future_created_runnable=1, age=0.0)},
    'ghost': {'retain': group(1, due=1, runnable=1, age=300.0)},
}
CAPACITY_MARKER = b'{"capacity_exceeded": true}\n'


def verbose_psql(sql, env):
    # stderr is read only here, from synthetic statements, to tell a statement timeout from other failures.
    command = BACKLOG.psql_command(sql)
    command[1:1] = ['-v', 'VERBOSITY=verbose']
    return subprocess.run(command, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)


with mock.patch.object(BACKLOG, 'SQL_SOURCES', sources(ROWS, BANKS)):
    assert 'public.' not in BACKLOG.build_sql(BACKLOG.DEFAULT_MAX_GROUPS), 'fixture query still reads a live relation'
    try:
        result = BACKLOG.collect()
    except BACKLOG.BacklogUnknown as error:
        raise SystemExit('native PostgreSQL fixture unavailable or failed: ' + error.message) from None
    assert BACKLOG.run_query(BACKLOG.build_sql(9)) == CAPACITY_MARKER, 'group capacity did not fail closed'
with mock.patch.object(BACKLOG, 'SQL_SOURCES', sources(ROWS[:1], BANKS)):
    assert BACKLOG.run_query(BACKLOG.build_sql(3)) == CAPACITY_MARKER, 'bank capacity did not fail closed'

banks = {bank['bank']: bank for bank in result['banks']}
assert {b: v['registered'] for b, v in banks.items()} == {b: b != 'ghost' for b in EXPECTED}, 'bank discovery mismatch'
for bank, types in EXPECTED.items():
    actual = {item['operation_type']: {k: v for k, v in item.items() if k != 'operation_type'}
              for item in banks[bank]['types']}
    assert actual == types, f'{bank} per-type counts mismatch'
    assert banks[bank]['totals'] == BACKLOG._rollup(list(types.values())), f'{bank} totals mismatch'
totals = tuple(result['totals'][k] for k in ('runnable', 'payload_null_pending', 'future_created_runnable'))
assert totals == (12, 2, 1), 'overall totals mismatch'
assert 'doc-' not in str(result) and 'w-stale' not in str(result) and 'synthetic' not in str(result), 'row content leaked'

HOSTILE = '-c default_transaction_read_only=off -c statement_timeout=0 -c lock_timeout=0 -c search_path=public'
SETTINGS = "SELECT concat_ws(',', {});".format(', '.join(f"current_setting('{name}')" for name in (
    'default_transaction_read_only', 'transaction_read_only', 'statement_timeout', 'lock_timeout', 'search_path')))
with mock.patch.dict(os.environ, {'PGOPTIONS': HOSTILE}):
    product_env = BACKLOG.psql_env()
# The second env lets hostile options reach the server, as a service file can; only SET LOCAL holds the bounds.
for env, default in ((product_env, 'on'), ({**product_env, 'PGOPTIONS': HOSTILE}, 'off')):
    seen = verbose_psql(SETTINGS, env)
    assert (seen.returncode, seen.stdout) == (0, default + ',on,10s,1s,pg_catalog\n'), 'session bounds not in force'

with mock.patch.object(BACKLOG, 'STATEMENT_TIMEOUT_MS', 300):
    started = time.monotonic()
    slept = verbose_psql('SELECT pg_sleep(5);', BACKLOG.psql_env())
assert slept.returncode != 0 and '57014' in slept.stderr, 'failure was not a server statement timeout'
assert time.monotonic() - started < 4, 'statement timeout was not enforced server-side'
print('native PostgreSQL runnable backlog fixture PASS')
