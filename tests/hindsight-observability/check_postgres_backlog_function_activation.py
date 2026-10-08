#!/usr/bin/env python3
"""Native PostgreSQL proof of the committed backlog function applied after the TEMPORARY revoke and the reader cap.

Only for a dedicated, otherwise empty, TLS-enabled fixture server reached over TCP loopback: it creates its own
roles, database hindsight and synthetic tables, applies the committed revoke, the committed cap and then the
committed function DDL as the owner over verify-full TLS, drops only what it created, restores its pg_hba byte for
byte, and refuses (never skips) otherwise.
"""
import importlib.util
import json
import os
import pathlib
import secrets
import subprocess
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SQL = ROOT / 'kubernetes/apps/database/hindsight-backlog/app/runnable-backlog.sql'


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIMITS = _load('backlog_role_limits_native', 'tests/hindsight-observability/check_postgres_backlog_role_limits.py')
HARDENING, FUNCTION = LIMITS.HARDENING, LIMITS.HARDENING.NATIVE
Refused, Failed, ok, refused = LIMITS.Refused, LIMITS.Failed, LIMITS.ok, LIMITS.refused
connection, DB, OWNER, READER, APP = LIMITS.connection, LIMITS.DB, LIMITS.OWNER, LIMITS.READER, LIMITS.APP
CALL, UNKNOWN_ERROR = FUNCTION.CALL, FUNCTION.UNKNOWN_ERROR
VERIFIED = 'verify-full'
SUPERUSER_REFUSED = 'ERROR:  P0001: apply as the non-superuser hindsight role'
# The function proof's own tables, indexes and rows, created by the owner in database hindsight; the roles and the
# database grant are this proof's, from the revoke proof's bootstrap. The queue table is unlogged so the spill rows
# below cost the fixture's disk no WAL; the function's guard checks relation kind, which that leaves alone.
FIXTURE_ONLY = ('CREATE ROLE', 'GRANT CREATE ON DATABASE')
QUEUE_TABLE = 'CREATE TABLE public.async_operations ('
TABLES = ''.join(line + '\n' for line in FUNCTION.bootstrap_sql().splitlines()
                 if not line.startswith(FIXTURE_ONLY)).replace(QUEUE_TABLE, 'CREATE UNLOGGED TABLE' + QUEUE_TABLE[12:])
# Fresh reader session state: the role-default cap, TEMPORARY revoked and no SET privilege on the parameter.
READER_STATE = ("SELECT json_build_array(current_setting('temp_file_limit'), source, setting, "
                "has_database_privilege(current_database(), 'TEMPORARY'), "
                "has_parameter_privilege('temp_file_limit', 'SET')) FROM pg_settings WHERE name = 'temp_file_limit';")
READER_SESSION = ['128MB', 'user', '131072', False, False]
# Read as the fixture superuser: what the reader may do to the function, its schema and the backlog tables.
READER_PRIVILEGES = f"""SELECT json_build_array(
 has_schema_privilege('{READER}', 'hindsight_metrics', 'USAGE'),
 has_schema_privilege('{READER}', 'hindsight_metrics', 'CREATE'),
 has_function_privilege('{READER}', 'hindsight_metrics.runnable_backlog()', 'EXECUTE'),
 has_table_privilege('{READER}', 'public.async_operations', 'SELECT'),
 has_column_privilege('{READER}', 'public.async_operations', 'task_payload', 'SELECT'),
 has_table_privilege('{READER}', 'public.banks', 'SELECT'),
 has_table_privilege('{READER}', 'public.alembic_version', 'SELECT'));
"""
READER_GRANTED = [True, False, True, False, False, False, False]
# The function catalog proof, with the declared app role standing in for the unrelated one. reader_authority also
# counts a password, which the fixture reader needs for SCRAM; the cap rerun checks the reader's attributes instead.
CATALOG = FUNCTION.CATALOG.replace(FUNCTION.OTHER, APP)
EXPECTED_CATALOG = {key: value for key, value in FUNCTION.EXPECTED_CATALOG.items() if key != 'reader_authority'}
# An owner function the reader holds no grant on, beside the one it does.
UNRELATED = ("CREATE FUNCTION public.unrelated_probe() RETURNS int LANGUAGE sql STABLE AS 'SELECT 1'; "
             'REVOKE ALL ON FUNCTION public.unrelated_probe() FROM PUBLIC;')
UNRELATED_UNDO = 'DROP FUNCTION public.unrelated_probe();'
DRIFT = ("UPDATE public.alembic_version SET version_num = 'ffffffffffff';",
         f"UPDATE public.alembic_version SET version_num = '{FUNCTION.FIXTURE_HEAD}';")
# Processing rows in one bank, each carrying the same 1 kB operation type. The function's materialized active set
# keeps every one in a tuplestore, which at the 64kB work_mem floor goes to temporary files: the control stays well
# under the cap and must return, the full set needs more than the cap and must stop at it. Operation type is the one
# wide column no index covers, and the table is unlogged, so the fixture's disk holds the rows once.
SPILL_WIDTH = 1024
SPILL_TYPE = 'spill_' + 'x' * (SPILL_WIDTH - 6)
SPILL_UNDER, SPILL_OVER = 20000, 150000
SPILL_ROWS = ("INSERT INTO public.async_operations (operation_id, bank_id, operation_type, status, task_payload, "
              "created_at) SELECT md5('spill' || g)::uuid, 'spill', '{type}', 'processing', '{{}}', now() "
              'FROM generate_series({first}, {last}) g;')
SPILL_CALL = "SET work_mem = '64kB'; " + CALL
CANCEL_CALL = "SET work_mem = '64kB'; SET statement_timeout = 1; " + CALL
CAP_BYTES = 128 * 1024 * 1024
TEMP_BYTES = f"SELECT temp_bytes FROM pg_stat_database WHERE datname = '{DB}';"
CANCELED = 'ERROR:  57014: canceling statement due to statement timeout'
# What a source error would carry past the fixed unknown: the stopped spill's code and text, or a row value.
LEAKS = (SPILL_TYPE, '53400', 'temp_file_limit', 'temporary file')


def apply(env, user=OWNER, sslmode=VERIFIED):
    """The committed file's exact bytes through psql -f, as the Job runs it. Stdin, because the owned fixture mounts
    no product file beyond the revoke and the cap; user None is the fixture superuser."""
    command = ['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=verbose', '-f', '-']
    run_env = connection(env, DB, sslmode)
    if user is not None:
        run_env['PGUSER'] = user
    return subprocess.run(command, input=SQL.read_text(), env=run_env, capture_output=True, text=True, timeout=120)


def applied(env, label):
    result = apply(env)
    if result.returncode != 0 or result.stdout.strip():
        raise Failed(f'{label}: the owner apply over verify-full TLS failed')


def reader_run(env, sql):
    """A fresh reader login over verify-full TLS, authenticated by SCRAM under the production reader rules. Earlier
    reader backends are waited out first, so the role's connection limit of 2 never decides a result."""
    LIMITS.readers_gone(env)
    return FUNCTION.psql(connection(env, DB, VERIFIED), sql, READER)


def reader(env, sql, label):
    result = reader_run(env, sql)
    if result.returncode != 0:
        raise Failed(f'{label}: the reader call failed')
    return result.stdout


def reader_refused(env, sql, sqlstate, label):
    LIMITS.readers_gone(env)
    return refused(connection(env, DB, VERIFIED), sql, sqlstate, label, user=READER)


def fixed_unknown(result, label):
    """The fixed code and message alone: no source error, row value or detail, and no output."""
    errors = [line.split('ERROR:  ', 1)[1] for line in result.stderr.splitlines() if 'ERROR:  ' in line]
    if result.returncode == 0 or result.stdout.strip() or errors != [UNKNOWN_ERROR] or 'DETAIL:' in result.stderr \
            or 'HINT:' in result.stderr or any(text in result.stderr for text in FUNCTION.SECRETS + LEAKS):
        raise Failed(f'{label}: not the fixed unknown alone')


def temp_bytes(env):
    return int(ok(connection(env, DB), TEMP_BYTES, 'temporary file statistics'))


def spilled(env, sql):
    """Runs `sql` as a fresh reader; returns the result and the temporary bytes its session wrote."""
    before = temp_bytes(env)
    result = reader_run(env, sql)
    # Backend exit flushes the session's statistics before its activity entry goes.
    LIMITS.readers_gone(env)
    return result, temp_bytes(env) - before


def prove_spill_and_cancel(env):
    ok(connection(env, DB), SPILL_ROWS.format(type=SPILL_TYPE, first=1, last=SPILL_UNDER), 'spill control rows',
       user=OWNER)
    result, written = spilled(env, SPILL_CALL)
    if result.returncode != 0 or not 0 < written < CAP_BYTES:
        raise Failed('a reader call that spills under the cap did not complete, or did not spill')
    # Read without the collector parser, whose operation type bound the wide spill rows exceed on purpose.
    spill = [group for group in json.loads(result.stdout)['groups'] if group['bank'] == 'spill']
    if [(group['operation_type'], group['processing']) for group in spill] != [(SPILL_TYPE, SPILL_UNDER)]:
        raise Failed('the spill control did not count every control row')
    ok(connection(env, DB), SPILL_ROWS.format(type=SPILL_TYPE, first=SPILL_UNDER + 1, last=SPILL_OVER),
       'spill rows past the cap', user=OWNER)
    result, written = spilled(env, SPILL_CALL)
    # The cap stops the session within one 8 kB write of 128MB, and the function reports only its fixed unknown.
    if not CAP_BYTES - 2 ** 20 < written <= CAP_BYTES:
        raise Failed('the oversized reader call did not stop at the 128MB cap')
    fixed_unknown(result, 'spill past the cap')
    stderr = reader_refused(env, CANCEL_CALL, '57014', 'one millisecond budget')
    if CANCELED not in stderr or 'HSB01' in stderr:
        raise Failed('the tiny statement budget did not surface as a cancellation')


def prove(env):
    password = secrets.token_hex(16)
    ok(connection(env), f'ALTER ROLE {READER} PASSWORD {LIMITS.literal(password)};', 'fixture reader password')
    env = {**env, 'PGPASSWORD': password}
    HARDENING.applied(env, 'committed TEMPORARY revoke as the owner over TLS')
    with LIMITS.reader_hba(env):
        LIMITS.prove_reader_logins(env)
        LIMITS.applied(env, 'committed reader cap as the superuser over TLS')
        ok(connection(env, DB, VERIFIED), TABLES, 'synthetic hindsight tables', user=OWNER)
        if ok(connection(env, DB, VERIFIED), HARDENING.TLS_SESSION, 'owner TLS session', user=OWNER).strip() != 't':
            raise Failed('the owner session is not using verified TLS')
        reader_refused(env, CALL, '3F000', 'reader call before the function exists')
        if json.loads(reader(env, READER_STATE, 'fresh reader session')) != READER_SESSION:
            raise Failed('a fresh reader session lacks the role-default cap or the TEMPORARY revoke')
        before = LIMITS.snapshot(env)

        result = apply(env, None)
        if result.returncode == 0 or SUPERUSER_REFUSED not in result.stderr or result.stdout.strip():
            raise Failed('the function DDL applied by a superuser was not refused')
        applied(env, 'committed function DDL as the owner')
        after = LIMITS.snapshot(env)
        if {**after, 'schemas': None, 'routines': None, 'acl_dependencies': None} \
                != {**before, 'schemas': None, 'routines': None, 'acl_dependencies': None} \
                or after['acl_dependencies'] != before['acl_dependencies'] + 2:
            raise Failed('the function apply changed more than its schema, function and the two reader grants')
        applied(env, 'rerun of the function DDL')
        # The revoke and the cap still accept the activated state, and neither rerun changes it.
        HARDENING.applied(env, 'revoke rerun over the function')
        LIMITS.applied(env, 'cap rerun over the function grants')
        if LIMITS.snapshot(env) != after:
            raise Failed('a rerun of the function, the revoke or the cap changed the activated state')
        if {key: value for key, value in json.loads(ok(connection(env), CATALOG, 'function catalog')).items()
                if key != 'reader_authority'} != EXPECTED_CATALOG:
            raise Failed('function ownership, ACL, attributes or dependencies are not as generated')
        if json.loads(ok(connection(env), READER_PRIVILEGES, 'reader privileges')) != READER_GRANTED:
            raise Failed('the reader holds more or less than schema USAGE and function EXECUTE')

        if json.loads(reader(env, READER_STATE, 'fresh reader session after activation')) != READER_SESSION:
            raise Failed('activation changed the reader session cap or TEMPORARY')
        data = reader(env, CALL, 'fresh reader function call')
        FUNCTION.assert_expected(FUNCTION.parsed(data))
        if any(text in data for text in FUNCTION.SECRETS):
            raise Failed('row content escaped through the function')
        with mock.patch.dict(os.environ, {**connection(env, DB, VERIFIED), 'PGUSER': OWNER}, clear=True):
            same = FUNCTION.CLI.run_query(f'SELECT (hindsight_metrics.runnable_backlog() = '
                                          f'({FUNCTION.RENDERER.canonical_query(FUNCTION.CLI)}))::text;')
        if same != b'true\n':
            raise Failed('function result differs from the canonical query in the same snapshot')

        for statement in FUNCTION.READER_DENIED + LIMITS.READER_DENIED:
            reader_refused(env, statement, '42501', f'reader: {statement}')
        ok(connection(env, DB), UNRELATED, 'unrelated owner function', user=OWNER)
        try:
            reader_refused(env, 'SELECT public.unrelated_probe();', '42501', 'reader: unrelated function')
        finally:
            ok(connection(env, DB), UNRELATED_UNDO, 'drop the unrelated function', user=OWNER)
        refused(connection(env, DB, VERIFIED), CALL, '42501', 'app: backlog function', user=APP)

        ok(connection(env, DB), DRIFT[0], 'arrange: revision drift', user=OWNER)
        try:
            fixed_unknown(reader_run(env, CALL), 'revision drift')
        finally:
            ok(connection(env, DB), DRIFT[1], 'undo: revision drift', user=OWNER)
        # The five optional session defaults are not needed, and do not get in the way beside the cap.
        ok(connection(env), LIMITS.RESTORE_DEFAULTS, 'the approved reader defaults')
        try:
            LIMITS.applied(env, 'cap over the approved defaults')
            FUNCTION.assert_expected(FUNCTION.parsed(reader(env, CALL, 'reader call under the cap and defaults')))
        finally:
            ok(connection(env), f'ALTER ROLE {READER} RESET ALL;', 'drop the approved defaults')
            LIMITS.applied(env, 'cap reapplied without the defaults')
        if LIMITS.snapshot(env) != after:
            raise Failed('the reader attempts or the defaults round trip changed the activated state')

        prove_spill_and_cancel(env)
        LIMITS.readers_gone(env)
        if ok(connection(env, 'postgres'), LIMITS.READER_SESSIONS, 'reader sessions after the proof').strip() != '0':
            raise Failed('a reader session outlived the proof')


def main():
    try:
        env = HARDENING.checker_env(os.environ)
    except Refused as error:
        raise SystemExit(f'refused: {error}') from None
    if not env.get('PGSSLROOTCERT'):
        raise SystemExit('refused: PGSSLROOTCERT must name the fixture CA, so the verify-full apply can be proven')
    postgres = connection(env, 'postgres')
    try:
        state = json.loads(ok(postgres, HARDENING.SERVER_STATE, 'server inspection'))
    except Failed:
        raise SystemExit('native TLS PostgreSQL fixture unavailable') from None
    if state != HARDENING.EMPTY_SERVER or json.loads(ok(postgres, LIMITS.LEFTOVERS, 'settings inspection')) != [0, 0]:
        raise SystemExit('refused: server is not an empty dedicated PostgreSQL 16 fixture (needs superuser, default '
                         'databases only, and no other roles, schemas, relations, sessions, role settings or '
                         'parameter grants)')
    if ok(postgres, HARDENING.TLS_SESSION, 'TLS session check').strip() != 't':
        raise SystemExit('fixture session is not using TLS; the TLS guard cannot be proven')
    try:
        ok(postgres, HARDENING.BOOTSTRAP_ROLES, 'fixture roles')
        ok(postgres, f'CREATE DATABASE {DB} OWNER {OWNER};', 'fixture database')
        ok(connection(env), HARDENING.BOOTSTRAP_OWNED, 'fixture owner data', user=OWNER)
        prove(env)
    finally:
        ok(postgres, HARDENING.TEARDOWN, 'fixture teardown')
        if json.loads(ok(postgres, HARDENING.SERVER_STATE, 'server inspection')) != HARDENING.EMPTY_SERVER \
                or json.loads(ok(postgres, LIMITS.LEFTOVERS, 'settings inspection')) != [0, 0]:
            raise SystemExit('fixture teardown left objects behind')
    print('native PostgreSQL backlog function activation TLS fixture PASS')


if __name__ == '__main__':
    main()
