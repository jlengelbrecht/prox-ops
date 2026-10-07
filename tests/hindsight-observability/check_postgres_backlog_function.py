#!/usr/bin/env python3
"""Native PostgreSQL permission proof of the committed runnable-backlog function DDL.

Only for a dedicated, otherwise empty fixture server: it creates its own roles and
synthetic public tables, drops only those, and refuses (never skips) otherwise.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import time
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CLI = _load('queue_backlog', 'scripts/hindsight-queue-backlog.py')
RENDERER = _load('backlog_function', 'scripts/hindsight-backlog-function.py')
OWNER, READER, OTHER = 'hindsight', 'hindsight_backlog_metrics', 'hindsight_backlog_unrelated'
CALL = 'SELECT hindsight_metrics.runnable_backlog();'
# Written out, not read from the renderer: a drifted SUPPORTED_SCHEMA_HEAD must fail this proof.
FIXTURE_HEAD = 'd1e2f3a4b5c6'
UNKNOWN_ERROR = 'HSB01: hindsight backlog unknown'
SECRETS = ('payload-secret', 'error-secret', 'synthetic-name', 'doc-a', 'w-stale', '00000000-0000')
EMPTY_SERVER = {'db': 'postgres', 'major': 16, 'superuser': True, 'databases': ['postgres', 'template0', 'template1'],
                'roles': 0, 'relations': 0, 'schemas': 0, 'reserved_user': False}

# (id, bank, type, status, has_payload, serialization_key, created minutes ago,
#  next_retry_at offset in minutes from now or None, worker_id)
ROWS = [
    (1, 'alpha', 'retain', 'pending', False, None, 60, None, None),            # payload-null parent
    (2, 'alpha', 'retain', 'pending', True, 'doc-a', 50, None, None),          # oldest of doc-a runs
    (3, 'alpha', 'retain', 'pending', True, 'doc-a', 40, None, None),          # held by row 2
    (4, 'alpha', 'retain', 'pending', True, None, 30, 10, None),               # future retry
    (5, 'alpha', 'retain', 'processing', True, 'doc-b', 70, None, 'w1'),
    (6, 'alpha', 'retain', 'pending', True, 'doc-b', 20, -5, None),            # held by a processing peer
    (7, 'alpha', 'graph_maintenance', 'pending', True, None, 25, None, None),
    (8, 'alpha', 'graph_maintenance', 'pending', True, None, 15, None, None),  # held by row 7, bank rule
    (9, 'alpha', 'consolidation', 'pending', True, 'doc-a', 35, None, None),   # no document rule applies
    (10, 'alpha', 'retain', 'completed', True, None, 80, None, 'w1'),
    (11, 'beta', 'retain', 'pending', True, None, 12, -2, 'w-stale'),          # assigned pending
    (12, 'beta', 'consolidation', 'processing', True, None, 60, None, 'w2'),
    (13, 'beta', 'consolidation', 'pending', True, None, 10, None, None),      # bank peer processing
    (14, 'gamma', 'retain', 'pending', True, None, -10, None, None),           # created after snapshot time
    (15, 'ghost', 'retain', 'pending', True, None, 5, None, None),             # bank row missing
]
BANKS = ('alpha', 'beta', 'empty', 'gamma')
# Counts must match exactly; age and lateness are lower bounds because rows
# were committed before the call, and None means None.
EXPECTED = {
    'alpha': {'retain': dict(pending=5, payload_null_pending=1, deferred=1, due=3, runnable=1,
                             serialization_blocked=2, processing=1, age=3000),
              'graph_maintenance': dict(pending=2, due=2, runnable=1, serialization_blocked=1, age=1500),
              'consolidation': dict(pending=1, due=1, runnable=1, age=2100)},
    'beta': {'retain': dict(pending=1, due=1, runnable=1, assigned_pending=1, assigned_runnable=1,
                            age=720, lateness=120),
             'consolidation': dict(pending=1, due=1, serialization_blocked=1, processing=1)},
    'empty': {},
    'gamma': {'retain': dict(pending=1, due=1, runnable=1, future_created_runnable=1, age=0)},
    'ghost': {'retain': dict(pending=1, due=1, runnable=1, age=300)},
}
FIXTURE_GROUPS = sum(len(types) for types in EXPECTED.values())

# Indexes copy the live definitions over the columns the query reads. The live
# result_metadata GIN, terminal-cleanup (updated_at) and banks internal_id/config
# indexes are left out with their columns, so plan shapes are indicative only.
BOOTSTRAP = """BEGIN;
CREATE ROLE hindsight LOGIN;
CREATE ROLE hindsight_backlog_metrics LOGIN;
CREATE ROLE hindsight_backlog_unrelated NOLOGIN;
GRANT CREATE ON DATABASE postgres TO hindsight;
CREATE TABLE public.banks (bank_id text CONSTRAINT pk_banks PRIMARY KEY, name text, mission text);
CREATE TABLE public.async_operations (
 operation_id uuid CONSTRAINT pk_async_operations PRIMARY KEY, bank_id text NOT NULL, operation_type text NOT NULL,
 status text NOT NULL, task_payload jsonb, serialization_key text, created_at timestamptz NOT NULL,
 next_retry_at timestamptz, worker_id text, error_message text);
CREATE INDEX idx_async_operations_bank_id ON public.async_operations (bank_id);
CREATE INDEX idx_async_operations_bank_status ON public.async_operations (bank_id, status);
CREATE INDEX idx_async_operations_bank_created_desc ON public.async_operations (bank_id, created_at DESC);
CREATE INDEX idx_async_operations_status ON public.async_operations (status);
CREATE INDEX idx_async_operations_status_retry ON public.async_operations (status, next_retry_at);
CREATE INDEX idx_async_operations_pending_claim ON public.async_operations (status, created_at)
 WHERE status = 'pending' AND task_payload IS NOT NULL;
CREATE INDEX idx_async_operations_serialization_key ON public.async_operations (bank_id, serialization_key)
 WHERE serialization_key IS NOT NULL AND status IN ('pending', 'processing');
CREATE INDEX idx_async_operations_worker_id ON public.async_operations (worker_id) WHERE worker_id IS NOT NULL;
CREATE TABLE public.alembic_version (version_num varchar(32) PRIMARY KEY);
INSERT INTO public.alembic_version VALUES ('{head}');
INSERT INTO public.banks VALUES {banks};
INSERT INTO public.async_operations VALUES {rows};
ALTER TABLE public.banks OWNER TO hindsight;
ALTER TABLE public.async_operations OWNER TO hindsight;
ALTER TABLE public.alembic_version OWNER TO hindsight;
COMMIT;
"""
TEARDOWN = """BEGIN;
DROP SCHEMA IF EXISTS hindsight_metrics CASCADE;
DROP TABLE IF EXISTS public.async_operations, public.banks, public.alembic_version;
DO $$BEGIN IF to_regrole('hindsight') IS NOT NULL THEN REVOKE CREATE ON DATABASE postgres FROM hindsight; END IF; END$$;
DROP ROLE IF EXISTS hindsight_backlog_unrelated, hindsight_backlog_metrics, hindsight;
COMMIT;
"""
# Anything user-created counts, not only the names this proof uses: pg_-prefixed
# names are reserved for the system, which also covers per-session temp schemas.
# The connecting role is left out of the count, so it is refused by name instead:
# with PGUSER unset libpq connects as the OS user, which may carry a fixture name.
SERVER_STATE = f"""SELECT json_build_object(
 'reserved_user', current_user IN ('{OWNER}', '{READER}', '{OTHER}'),
 'db', current_database(), 'major', current_setting('server_version_num')::int / 10000,
 'superuser', (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
 'databases', (SELECT json_agg(datname ORDER BY datname) FROM pg_database),
 'roles', (SELECT count(*) FROM pg_roles WHERE rolname !~ '^pg_' AND rolname <> current_user),
 'relations', (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'),
 'schemas', (SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_'
             AND nspname NOT IN ('information_schema', 'public')));
"""
# Reader CREATE and table SELECT are proven by behaviour in READER_DENIED instead.
CATALOG = """SELECT json_build_object(
 'owner', p.proowner::regrole::text, 'secdef', p.prosecdef, 'volatile', p.provolatile, 'config', p.proconfig,
 'returns', p.prorettype::regtype::text, 'args', p.pronargs, 'lang', l.lanname,
 'acl', (SELECT json_agg(a::text ORDER BY a::text COLLATE "C") FROM unnest(p.proacl) a),
 'schema_acl', (SELECT json_agg(a::text ORDER BY a::text COLLATE "C")
                FROM pg_namespace n, unnest(n.nspacl) a WHERE n.nspname = 'hindsight_metrics'),
 'relation_deps', (SELECT count(*) FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
                   AND d.refclassid = 'pg_class'::regclass),
 'reader_authority', (SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
                      OR NOT rolcanlogin OR rolpassword IS NOT NULL
                      FROM pg_authid WHERE rolname = 'hindsight_backlog_metrics'),
 'memberships', (SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid IN (m.member, m.roleid)
                 WHERE r.rolname = 'hindsight_backlog_metrics'),
 'unrelated_exec', has_function_privilege('hindsight_backlog_unrelated', p.oid, 'EXECUTE'))
 FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang
 WHERE p.oid = 'hindsight_metrics.runnable_backlog()'::regprocedure;
"""
EXPECTED_CATALOG = {
    'owner': OWNER, 'secdef': True, 'volatile': 's', 'config': ['search_path=pg_catalog, pg_temp'],
    'returns': 'jsonb', 'args': 0, 'lang': 'plpgsql',
    'acl': ['hindsight=X/hindsight', 'hindsight_backlog_metrics=X/hindsight'],
    'schema_acl': ['hindsight=UC/hindsight', 'hindsight_backlog_metrics=U/hindsight'], 'relation_deps': 0,
    'reader_authority': False, 'memberships': 0, 'unrelated_exec': False,
}
READER_DENIED = [
    'SELECT count(*) FROM public.async_operations;',
    'SELECT task_payload FROM public.async_operations;',
    'SELECT error_message FROM public.async_operations;',
    'SELECT bank_id FROM public.banks;',
    'SELECT version_num FROM public.alembic_version;',
    "INSERT INTO public.banks (bank_id) VALUES ('intruder');",
    "UPDATE public.banks SET mission = 'intruder';",
    'DELETE FROM public.banks;',
    "INSERT INTO public.async_operations (operation_id, bank_id, operation_type, status, created_at) "
    "VALUES (gen_random_uuid(), 'alpha', 'retain', 'pending', now());",
    "UPDATE public.async_operations SET status = 'failed';",
    'DELETE FROM public.async_operations;',
    "INSERT INTO public.alembic_version VALUES ('ffffffffffff');",
    "UPDATE public.alembic_version SET version_num = 'ffffffffffff';",
    'DELETE FROM public.alembic_version;',
    'CREATE TABLE public.intruder (i int);',
    'CREATE TABLE hindsight_metrics.intruder (i int);',
    'CREATE FUNCTION hindsight_metrics.intruder() RETURNS int LANGUAGE sql AS $$SELECT 1$$;',
    'CREATE SCHEMA intruder;',
    'SET ROLE hindsight;',
]
# The guard reads pg_class and pg_attribute unqualified. With pg_temp listed
# ahead of pg_catalog on the caller's path, those names resolve to these empty
# temp tables and the guard reports HSB01, unless the function's own SET
# search_path is in force: the call returning true counts is that proof. The
# public relations are qualified in the body; their shadows would catch a body
# that dropped the qualification, since the fixed path still ends in pg_temp.
# Functions are never resolved from pg_temp, so now() and to_regclass need none.
SHADOW = """CREATE TEMP TABLE pg_class (oid oid, relkind "char");
CREATE TEMP TABLE pg_attribute (attrelid oid, attnum int2, attisdropped bool, attname name);
CREATE TEMP TABLE async_operations (operation_id uuid, bank_id text, operation_type text, status text,
 task_payload jsonb, serialization_key text, created_at timestamptz, next_retry_at timestamptz, worker_id text);
INSERT INTO async_operations VALUES (gen_random_uuid(), 'shadow', 'retain', 'pending', '{}', NULL, now(), NULL, NULL);
CREATE TEMP TABLE banks (bank_id text);
INSERT INTO banks VALUES ('shadow');
CREATE TEMP TABLE alembic_version (version_num text);
INSERT INTO alembic_version VALUES ('ffffffffffff');
SET search_path = pg_temp, pg_catalog, public;
""" + CALL + """
-- Dropped here, not at backend exit, so teardown never races a dying session for the role.
DROP TABLE pg_temp.pg_class, pg_temp.pg_attribute, pg_temp.async_operations, pg_temp.banks, pg_temp.alembic_version;
"""
# Each runs as the fixture admin inside a rolled-back transaction. That the
# ALTER/DROP statements succeed at all proves the function does not pin them.
UNKNOWN_CASES = {
    'revision': "UPDATE public.alembic_version SET version_num = 'ffffffffffff';",
    'second revision row': "INSERT INTO public.alembic_version VALUES ('a0a0a0a0a0a0');",
    'renamed column': 'ALTER TABLE public.async_operations RENAME COLUMN worker_id TO worker_ref;',
    'dropped column': 'ALTER TABLE public.async_operations DROP COLUMN serialization_key;',
    'missing table': 'DROP TABLE public.banks;',
    'view swapped in': 'ALTER TABLE public.banks RENAME TO banks_real; '
                       'CREATE VIEW public.banks AS SELECT bank_id FROM public.banks_real;',
    # The source error carries a HINT; the function must replace it with the fixed message alone.
    'query error': 'ALTER TABLE public.async_operations ALTER COLUMN created_at TYPE text USING created_at::text;',
}
# Each is applied, the committed DDL is re-applied as the owner and must refuse, then it is undone.
# Owner changes remap the ACL grantors and the undo maps them back, which the catalog re-check confirms.
APPLY_REFUSALS = {
    'foreign object in schema': ('CREATE FUNCTION hindsight_metrics.foreign_probe() RETURNS int LANGUAGE sql '
                                 'AS $$SELECT 1$$;', 'DROP FUNCTION hindsight_metrics.foreign_probe();'),
    'schema owner drift': (f'ALTER SCHEMA hindsight_metrics OWNER TO {OTHER};',
                           f'ALTER SCHEMA hindsight_metrics OWNER TO {OWNER};'),
    'function owner drift': (f'ALTER FUNCTION hindsight_metrics.runnable_backlog() OWNER TO {OTHER};',
                             f'ALTER FUNCTION hindsight_metrics.runnable_backlog() OWNER TO {OWNER};'),
    'reader gained schema CREATE': (f'GRANT CREATE ON SCHEMA hindsight_metrics TO {READER};',
                                    f'REVOKE CREATE ON SCHEMA hindsight_metrics FROM {READER};'),
    'extra EXECUTE grant': (f'GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() TO {OTHER};',
                            f'REVOKE EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() FROM {OTHER};'),
    'reader gained authority': (f'ALTER ROLE {READER} CREATEDB;', f'ALTER ROLE {READER} NOCREATEDB;'),
    'reader is a member of a role': (f'GRANT {OWNER} TO {READER};', f'REVOKE {OWNER} FROM {READER};'),
    'a role is a member of the reader': (f'GRANT {READER} TO {OTHER};', f'REVOKE {READER} FROM {OTHER};'),
}
# PUBLIC grants are not refused but revoked: a re-apply must restore exactly the generated ACLs.
PUBLIC_GRANTS = ('GRANT USAGE ON SCHEMA hindsight_metrics TO PUBLIC; '
                 'GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() TO PUBLIC;')
# One bank, every retain row on one document: each peer probe walks the hot
# document, so this is the predicate's quadratic shape at a CI-sized count.
LOAD = {'retain': (1000, "'doc-hot'"), 'graph_maintenance': (300, 'NULL'), 'consolidation': (300, 'NULL')}


class Refused(Exception):
    pass


class Failed(AssertionError):
    pass


def fixture_env(environ):
    """The connection environment, or Refused unless it can only reach a dedicated local fixture server."""
    if environ.get('HINDSIGHT_BACKLOG_FIXTURE') != '1':
        raise Refused('set HINDSIGHT_BACKLOG_FIXTURE=1, and only against a dedicated fixture server')
    for name in ('PGSERVICE', 'PGSERVICEFILE', 'PGHOSTADDR'):
        if environ.get(name):
            raise Refused(f'{name} could redirect the connection; unset it')
    # Loopback or a Unix socket only: CI and the root's private container exec both use localhost.
    host = environ.get('PGHOST', '')
    if ',' in host or not (host in ('', 'localhost', '127.0.0.1', '::1') or host.startswith('/')):
        raise Refused('PGHOST must be one loopback host or a Unix socket directory')
    if environ.get('PGDATABASE', 'postgres') != 'postgres':
        raise Refused('PGDATABASE must be the fixture default postgres')
    if environ.get('PGUSER') in (OWNER, READER, OTHER):
        raise Refused('PGUSER must be the fixture admin, not a role this proof creates')
    env = {key: value for key, value in environ.items() if key != 'PGOPTIONS'}
    env.update(PGDATABASE='postgres', PGCONNECT_TIMEOUT='5', PGTZ='UTC')
    return env


def psql(env, sql, user=None):
    command = ['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=verbose', '-f', '-']
    run_env = env if user is None else {**env, 'PGUSER': user}
    return subprocess.run(command, input=sql, env=run_env, capture_output=True, text=True, timeout=120)


def ok(env, sql, label, user=None):
    result = psql(env, sql, user)
    if result.returncode != 0:
        # stderr comes only from synthetic fixture statements.
        raise Failed(f'{label}: {result.stderr.strip()[-400:]}')
    return result.stdout


def refused(env, sql, sqlstate, label, user=None):
    result = psql(env, sql, user)
    if result.returncode == 0 or f'ERROR:  {sqlstate}:' not in result.stderr or result.stdout.strip():
        raise Failed(f'{label}: expected SQLSTATE {sqlstate} and no output, '
                     f'got rc={result.returncode} {result.stderr.strip()[-300:]}')
    return result.stderr


def unknown(env, setup, label):
    stderr = refused(env, as_reader(setup, CALL), RENDERER.UNKNOWN_SQLSTATE, f'unknown on {label}')
    errors = [line.split('ERROR:  ', 1)[1] for line in stderr.splitlines() if 'ERROR:  ' in line]
    if errors != [UNKNOWN_ERROR] or 'DETAIL:' in stderr or 'HINT:' in stderr or any(s in stderr for s in SECRETS):
        raise Failed(f'unknown on {label}: not the fixed message alone')


def literal(text):
    return 'NULL' if text is None else "'" + text.replace("'", "''") + "'"


def row_sql(row):
    ident, bank, kind, status, payload, key, created, retry, worker = row
    return ('(' + ', '.join([
        f"'00000000-0000-0000-0000-{ident:012d}'", literal(bank), literal(kind), literal(status),
        """'{"synthetic": "payload-secret"}'""" if payload else 'NULL', literal(key),
        f"now() - interval '{created} minutes'", 'NULL' if retry is None else f"now() + interval '{retry} minutes'",
        literal(worker), "'synthetic error-secret'"]) + ')')


def bootstrap_sql():
    banks = ', '.join(f"({literal(bank)}, 'synthetic-name', 'synthetic mission')" for bank in BANKS)
    return BOOTSTRAP.format(head=FIXTURE_HEAD, banks=banks, rows=', '.join(map(row_sql, ROWS)))


def extra_groups_sql(count):
    return ("INSERT INTO public.async_operations (operation_id, bank_id, operation_type, status, task_payload, "
            f"created_at) SELECT md5('cap' || g)::uuid, 'alpha', 'cap_' || g, 'pending', '{{}}', now() "
            f"FROM generate_series(1, {count}) g;")


def load_sql():
    inserts = ["INSERT INTO public.banks (bank_id) VALUES ('load');"]
    for kind, (count, key) in LOAD.items():
        inserts.append("INSERT INTO public.async_operations (operation_id, bank_id, operation_type, status, "
                       "task_payload, serialization_key, created_at) "
                       f"SELECT md5('{kind}' || g)::uuid, 'load', '{kind}', 'pending', '{{}}', {key}, "
                       f"now() - g * interval '1 second' FROM generate_series(1, {count}) g;")
    return '\n'.join(inserts + ['ANALYZE public.async_operations;'])


def as_reader(setup, statement, timeout_ms=5000):
    return (f'BEGIN; {setup}\nSET LOCAL ROLE {READER}; SET LOCAL statement_timeout = {timeout_ms}; '
            f'{statement} ROLLBACK;')


def parsed(output):
    return CLI.parse_result(output.encode(), RENDERER.MAX_GROUPS)


def server_state(env):
    return json.loads(ok(env, SERVER_STATE, 'server inspection'))


def assert_catalog(env, label):
    if json.loads(ok(env, CATALOG, f'catalog {label}')) != EXPECTED_CATALOG:
        raise Failed(f'{label}: function ownership, ACL, attributes, dependencies or reader authority mismatch')


def assert_expected(result):
    banks = {bank['bank']: bank for bank in result['banks']}
    if {bank: item['registered'] for bank, item in banks.items()} != {bank: bank != 'ghost' for bank in EXPECTED}:
        raise Failed('bank discovery mismatch, including the new empty bank')
    for bank, types in EXPECTED.items():
        actual = {item['operation_type']: item for item in banks[bank]['types']}
        if set(actual) != set(types):
            raise Failed(f'{bank}: operation types mismatch')
        for kind, want in types.items():
            got = actual[kind]
            if any(got[name] != want.get(name, 0) for name in CLI.COUNTS):
                raise Failed(f'{bank}/{kind}: counts mismatch')
            for name, floor in (('runnable_oldest_task_age_seconds', want.get('age')),
                                ('runnable_oldest_retry_lateness_seconds', want.get('lateness'))):
                value = got[name]
                if (value is not None) if floor is None else (value is None or not floor <= value < floor + 300):
                    raise Failed(f'{bank}/{kind}: {name} out of range')


def prove(env):
    ddl = RENDERER.TARGET.read_text()
    refused(env, ddl, 'P0001', 'DDL applied by a superuser')
    ok(env, ddl, 'apply generated DDL as owner', user=OWNER)
    ok(env, ddl, 're-apply generated DDL as owner', user=OWNER)
    assert_catalog(env, 'after apply')

    for label, (change, undo) in APPLY_REFUSALS.items():
        ok(env, change, f'arrange: {label}')
        try:
            refused(env, ddl, 'P0001', f'apply with {label}', user=OWNER)
        finally:
            ok(env, undo, f'undo: {label}')
    assert_catalog(env, 'after refused applies')
    ok(env, PUBLIC_GRANTS, 'arrange: PUBLIC grants')
    ok(env, ddl, 're-apply over PUBLIC grants', user=OWNER)
    assert_catalog(env, 'after PUBLIC grants were revoked')

    for statement in READER_DENIED:
        refused(env, statement, '42501', f'reader: {statement}', user=READER)
    refused(env, f'SET ROLE {OTHER}; {CALL}', '42501', 'unrelated role without schema usage')
    refused(env, f'BEGIN; GRANT USAGE ON SCHEMA hindsight_metrics TO {OTHER}; SET LOCAL ROLE {OTHER}; {CALL} ROLLBACK;',
            '42501', 'unrelated role with schema usage but no EXECUTE')

    with mock.patch.dict(os.environ, {**env, 'PGUSER': READER}, clear=True):
        data = CLI.run_query(CALL)
    assert_expected(CLI.parse_result(data, RENDERER.MAX_GROUPS))
    if any(secret in data.decode() for secret in SECRETS):
        raise Failed('row content escaped through the function')
    with mock.patch.dict(os.environ, {**env, 'PGUSER': OWNER}, clear=True):
        same = CLI.run_query(f'SELECT (hindsight_metrics.runnable_backlog() = ({RENDERER.canonical_query(CLI)}))::text;')
    if same != b'true\n':
        raise Failed('function result differs from the canonical query in the same snapshot')

    assert_expected(parsed(ok(env, SHADOW, 'call with temp catalog and table shadows', user=READER)))

    for label, change in UNKNOWN_CASES.items():
        unknown(env, change, label)
    parsed(ok(env, as_reader('ALTER TABLE public.async_operations ALTER COLUMN worker_id TYPE varchar(128);', CALL),
              'column type change'))

    fill = RENDERER.MAX_GROUPS - FIXTURE_GROUPS
    at_bound = parsed(ok(env, as_reader(extra_groups_sql(fill), CALL), 'groups at bound'))
    if sum(len(bank['types']) for bank in at_bound['banks']) != RENDERER.MAX_GROUPS:
        raise Failed('group bound fixture miscounted')
    if ok(env, as_reader(extra_groups_sql(fill + 1), CALL), 'groups over bound') != '{"capacity_exceeded": true}\n':
        raise Failed(f'{RENDERER.MAX_GROUPS + 1} groups did not return the capacity marker')

    plan = json.loads(ok(env, f'BEGIN; {load_sql()}\nSET LOCAL ROLE {OWNER}; SET LOCAL statement_timeout = 20000; '
                              f'EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {RENDERER.canonical_query(CLI)}; ROLLBACK;',
                         'EXPLAIN ANALYZE under concentrated load'))[0]
    started = time.monotonic()
    loaded = parsed(ok(env, as_reader(load_sql(), CALL), 'function under concentrated load'))
    wall = time.monotonic() - started
    load = {item['operation_type']: item for bank in loaded['banks'] if bank['bank'] == 'load' for item in bank['types']}
    for kind, (count, _) in LOAD.items():
        if (load[kind]['runnable'], load[kind]['serialization_blocked']) != (1, count - 1):
            raise Failed(f'concentrated {kind} backlog not serialized to one runnable row')
    refused(env, as_reader(load_sql(), CALL, timeout_ms=1), '57014', 'statement timeout under load')
    print(f'concentrated load {LOAD} in one bank: canonical query {plan["Execution Time"]:.1f} ms, '
          f'planner cost {plan["Plan"]["Total Cost"]:.0f}; function call incl. load setup {wall:.2f} s '
          'under a 5 s statement_timeout (local fixture bound, not a production throughput claim)')


def main():
    try:
        env = fixture_env(os.environ)
    except Refused as error:
        raise SystemExit(f'refused: {error}') from None
    try:
        state = server_state(env)
    except Failed:
        raise SystemExit('native PostgreSQL fixture unavailable') from None
    if state != EMPTY_SERVER:
        raise SystemExit('refused: server is not an empty dedicated PostgreSQL 16 fixture '
                         '(needs superuser, default databases only, and no other roles, schemas or relations)')
    ok(env, bootstrap_sql(), 'fixture bootstrap')
    try:
        prove(env)
    finally:
        ok(env, TEARDOWN, 'fixture teardown')
        if server_state(env) != EMPTY_SERVER:
            raise SystemExit('fixture teardown left objects behind')
    print('native PostgreSQL backlog function permission fixture PASS')


if __name__ == '__main__':
    main()
