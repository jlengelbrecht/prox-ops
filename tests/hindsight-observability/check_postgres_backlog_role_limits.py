#!/usr/bin/env python3
"""Native PostgreSQL proof of the committed backlog reader temp_file_limit cap, over TLS.

Only for a dedicated, otherwise empty, TLS-enabled fixture server reached over TCP loopback: it creates its own
roles and database hindsight, applies the committed TEMPORARY revoke and then the committed cap, drops only what it
created, restores its pg_hba byte for byte, and refuses (never skips) otherwise.
"""
import contextlib
import importlib.util
import json
import os
import pathlib
import secrets
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
SQL = ROOT / 'kubernetes/apps/database/hindsight-backlog/role-limits/role-limits.sql'
_SPEC = importlib.util.spec_from_file_location(
    'backlog_hardening_native', ROOT / 'tests/hindsight-observability/check_postgres_backlog_hardening.py')
HARDENING = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(HARDENING)
Refused, Failed, ok, refused, literal = (HARDENING.Refused, HARDENING.Failed, HARDENING.ok, HARDENING.refused,
                                         HARDENING.NATIVE.literal)
connection, DB, OWNER, READER, APP = (HARDENING.connection, HARDENING.DB, HARDENING.OWNER, HARDENING.READER,
                                      HARDENING.APP)
CAP = 'temp_file_limit=128MB'
# The approved reader session defaults in their stored text and the order they are written.
DEFAULTS = ['default_transaction_read_only=on', 'statement_timeout=5s', 'lock_timeout=1s',
            'idle_in_transaction_session_timeout=5s', 'idle_session_timeout=30s']
SET_DEFAULTS = ''.join("ALTER ROLE {} SET {} = '{}';".format(READER, *entry.split('=')) for entry in DEFAULTS)
# Run as the fixture superuser: puts the reader back to the approved defaults alone.
RESTORE_DEFAULTS = f'ALTER ROLE {READER} RESET ALL;' + SET_DEFAULTS
# Literal, for reader attempts only: past the read-only default, a refusal can only be the privilege check.
WRITABLE = '-c default_transaction_read_only=off'
# Settings of other roles the apply must leave byte-identical.
OWN_SETTINGS = f"ALTER ROLE {APP} SET work_mem = '8MB'; ALTER ROLE {OWNER} SET lock_timeout = '10s';"
# Ahead of the image's own rules, its trust catch-all included, so these two decide every reader login while
# every other role still authenticates as before.
READER_HBA = [f'hostssl {DB} {READER} all scram-sha-256', f'host all {READER} all reject']
# Ahead of both: it matches the reader first, which the presence-and-order guard cannot see past.
PERMISSIVE_HBA = f'host all {READER} all trust'
HBA_REJECTED, SCRAM_FAILED = 'pg_hba.conf rejects connection', 'password authentication failed'
# PostgreSQL 16 system_user is the authentication method and identity, NULL under trust.
LOGIN = 'SELECT system_user, ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid();'
READER_SESSIONS = f"SELECT count(*) FROM pg_stat_activity WHERE usename = '{READER}';"
# The backlog function's grants as its owner applies them, over a stub body: the guard reads only the catalog.
METRICS = f"""BEGIN;
CREATE SCHEMA hindsight_metrics;
CREATE FUNCTION hindsight_metrics.runnable_backlog() RETURNS int LANGUAGE sql STABLE AS 'SELECT 1';
REVOKE ALL ON SCHEMA hindsight_metrics FROM PUBLIC;
REVOKE ALL ON FUNCTION hindsight_metrics.runnable_backlog() FROM PUBLIC;
GRANT USAGE ON SCHEMA hindsight_metrics TO {READER};
GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() TO {READER};
COMMIT;
"""
METRICS_CALL = 'SELECT hindsight_metrics.runnable_backlog();'
METRICS_UNDO = 'BEGIN; DROP FUNCTION hindsight_metrics.runnable_backlog(); DROP SCHEMA hindsight_metrics; COMMIT;'
LEFTOVERS = 'SELECT json_build_array((SELECT count(*) FROM pg_db_role_setting), (SELECT count(*) FROM pg_parameter_acl));'
# Added to the revoke proof's catalog snapshot: every role setting and parameter privilege.
SETTINGS_SNAPSHOT = f"""SELECT json_build_object(
 'settings', (SELECT json_agg(x ORDER BY x COLLATE "C") FROM (
              SELECT coalesce(d.datname, '*') || '/' || coalesce(r.rolname, '*') || '=' || s.setconfig::text AS x
              FROM pg_db_role_setting s LEFT JOIN pg_database d ON d.oid = s.setdatabase
              LEFT JOIN pg_roles r ON r.oid = s.setrole) t),
 'parameter_acl', (SELECT json_agg(parname || '=' || coalesce(paracl::text, 'NULL') ORDER BY parname) FROM pg_parameter_acl),
 'reader_set', has_parameter_privilege('{READER}', 'temp_file_limit', 'SET'));
"""
READER_STATE = ("SELECT json_build_array(current_setting('temp_file_limit'), source, setting, "
                "has_database_privilege(current_database(), 'TEMPORARY'), current_setting('default_transaction_read_only'), "
                "current_setting('statement_timeout'), current_setting('lock_timeout'), "
                "current_setting('idle_in_transaction_session_timeout'), current_setting('idle_session_timeout')) "
                "FROM pg_settings WHERE name = 'temp_file_limit';")
READER_SESSION = ['128MB', 'user', '131072', False, 'on', '5s', '1s', '5s', '30s']
# Direct RESET of a superuser parameter is refused like SET; ALTER DATABASE needs ownership the reader lacks.
READER_DENIED = ['SET temp_file_limit = -1;', 'BEGIN; SET LOCAL temp_file_limit = -1; COMMIT;',
                 "SELECT set_config('temp_file_limit', '-1', false);", 'RESET temp_file_limit;',
                 f'ALTER ROLE {READER} SET temp_file_limit = -1;', f'ALTER ROLE {READER} RESET temp_file_limit;',
                 f'ALTER ROLE {READER} IN DATABASE {DB} SET temp_file_limit = -1;',
                 f'ALTER DATABASE {DB} SET temp_file_limit = -1;']
STARTUP_DENIED = 'permission denied to set parameter "temp_file_limit"'
# A constant pad is folded at plan time and an unread subquery column is dropped, so a padded sort spills only
# its integers. A materialized CTE keeps every row it produces in a tuplestore, here 1 kB of per-row text that
# cannot be folded, which at the 64kB work_mem floor goes to temporary files: the small one stays well under the
# cap, the large one needs more than twice 128MB and must stop at it.
SPILL_WIDTH = 1024
SPILL = ("SET work_mem = '64kB'; SET statement_timeout = '30s'; WITH wide AS MATERIALIZED (SELECT g, "
         "repeat(md5(g::text), 32) AS s FROM generate_series(1, {rows}) g) "
         f"SELECT count(*) FROM wide WHERE length(s) = {SPILL_WIDTH};")
SPILL_STOPPED = 'temporary file size exceeds temp_file_limit (131072kB)'
SPILL_UNDER, SPILL_OVER = 20000, 300000

SUPER = (None, DB, 'require')
REVOKE_MISSING, HBA_MISSING = 'does not carry the TEMPORARY revoke', 'pg_hba lacks the reader TLS rule'
SESSION_REFUSAL = 'a hindsight_backlog_metrics session is connected'
SETTINGS, DRIFT = 'carries settings this file does not accept', 'attributes or memberships drifted'
PER_DATABASE = 'a per-database setting exists'
PRIVILEGES, CREATE = 'holds privileges this file does not expect', 'can create objects in a schema'


def setting(target, name, value):
    """Arrange and undo of one stored setting on `target` (ALTER ROLE <role> [IN DATABASE] or ALTER DATABASE)."""
    return f"ALTER {target} SET {name} = '{value}';", f'ALTER {target} RESET {name};'


# label: (arrange, undo, (user, database, sslmode) for the apply, the guard message it must raise)
# Arrange and undo run as the fixture superuser in database hindsight.
PRE_APPLY_REFUSALS = {
    'non-superuser owner': (None, None, (OWNER, DB, 'require'), 'apply as a superuser session'),
    'non-TLS session': (None, None, (None, DB, 'disable'), 'this session is not using TLS'),
    'wrong database': (None, None, (None, 'postgres', 'require'), 'run in database hindsight'),
    'unknown reader setting': (*setting(f'ROLE {READER}', 'work_mem', '64kB'), SUPER, SETTINGS),
    'different reader cap': (*setting(f'ROLE {READER}', 'temp_file_limit', '1MB'), SUPER, SETTINGS),
    'one approved default alone': (*setting(f'ROLE {READER}', 'statement_timeout', '5s'), SUPER, SETTINGS),
    'SET grant on the parameter': (f'GRANT SET ON PARAMETER temp_file_limit TO {READER};',
                                   f'REVOKE SET ON PARAMETER temp_file_limit FROM {READER};', SUPER,
                                   'a privilege on temp_file_limit has been granted'),
    'reader per-database setting': (*setting(f'ROLE {READER} IN DATABASE {DB}', 'temp_file_limit', '128MB'), SUPER,
                                    PER_DATABASE),
    'owner per-database setting': (*setting(f'ROLE {OWNER} IN DATABASE {DB}', 'work_mem', '8MB'), SUPER, PER_DATABASE),
    'database-wide cap': (*setting(f'DATABASE {DB}', 'temp_file_limit', '1GB'), SUPER,
                          'a temp_file_limit default for every role'),
    'reader INHERIT': (f'ALTER ROLE {READER} INHERIT;', f'ALTER ROLE {READER} NOINHERIT;', SUPER, DRIFT),
    'reader connection limit': (f'ALTER ROLE {READER} CONNECTION LIMIT 3;', f'ALTER ROLE {READER} CONNECTION LIMIT 2;',
                                SUPER, DRIFT),
    'reader member of the owner': (f'GRANT {OWNER} TO {READER};', f'REVOKE {OWNER} FROM {READER};', SUPER, DRIFT),
    'a role is a member of the reader': (f'GRANT {READER} TO {APP};', f'REVOKE {READER} FROM {APP};', SUPER, DRIFT),
    'one unexpected reader grant': (f'CREATE SCHEMA lone AUTHORIZATION {OWNER}; GRANT USAGE ON SCHEMA lone TO {READER};',
                                    'DROP SCHEMA lone;', SUPER, PRIVILEGES),
    # Owner objects and the accepted privileges, two dependencies, but not the function and its schema.
    'two owner grants on other objects': (
        f"CREATE SCHEMA stand_in AUTHORIZATION {OWNER}; "
        "CREATE FUNCTION stand_in.probe() RETURNS int LANGUAGE sql STABLE AS 'SELECT 1'; "
        f'ALTER FUNCTION stand_in.probe() OWNER TO {OWNER}; GRANT USAGE ON SCHEMA stand_in TO {READER}; '
        f'GRANT EXECUTE ON FUNCTION stand_in.probe() TO {READER};',
        'DROP FUNCTION stand_in.probe(); DROP SCHEMA stand_in;', SUPER, PRIVILEGES),
    'PUBLIC CREATE on schema public': ('GRANT CREATE ON SCHEMA public TO PUBLIC;',
                                       'REVOKE CREATE ON SCHEMA public FROM PUBLIC;', SUPER, CREATE),
}
# Over the applied cap with METRICS in place, which each undo restores exactly. A superuser's grant or revoke
# acts as the object owner, so grantors stay the owner throughout.
METRICS_REFUSALS = {
    'reader CREATE on the function schema': (f'GRANT CREATE ON SCHEMA hindsight_metrics TO {READER};',
                                             f'REVOKE CREATE ON SCHEMA hindsight_metrics FROM {READER};', SUPER,
                                             PRIVILEGES),
    'PUBLIC CREATE on the function schema': ('GRANT CREATE ON SCHEMA hindsight_metrics TO PUBLIC;',
                                             'REVOKE CREATE ON SCHEMA hindsight_metrics FROM PUBLIC;', SUPER, CREATE),
    'schema USAGE with grant option': (f'GRANT USAGE ON SCHEMA hindsight_metrics TO {READER} WITH GRANT OPTION;',
                                       f'REVOKE GRANT OPTION FOR USAGE ON SCHEMA hindsight_metrics FROM {READER};',
                                       SUPER, PRIVILEGES),
    'function EXECUTE with grant option': (
        f'GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() TO {READER} WITH GRANT OPTION;',
        f'REVOKE GRANT OPTION FOR EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() FROM {READER};',
        SUPER, PRIVILEGES),
    'another function in the schema granted': (
        "CREATE FUNCTION hindsight_metrics.other() RETURNS int LANGUAGE sql STABLE AS 'SELECT 2'; "
        f'ALTER FUNCTION hindsight_metrics.other() OWNER TO {OWNER}; '
        f'GRANT EXECUTE ON FUNCTION hindsight_metrics.other() TO {READER};',
        'DROP FUNCTION hindsight_metrics.other();', SUPER, PRIVILEGES),
    # Still two dependencies, on the right schema and name, but not the zero-argument function.
    'same name with an argument in its place': (
        f'REVOKE EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() FROM {READER}; '
        "CREATE FUNCTION hindsight_metrics.runnable_backlog(int) RETURNS int LANGUAGE sql STABLE AS 'SELECT 1'; "
        f'ALTER FUNCTION hindsight_metrics.runnable_backlog(int) OWNER TO {OWNER}; '
        f'GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog(int) TO {READER};',
        'DROP FUNCTION hindsight_metrics.runnable_backlog(int); '
        f'GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() TO {READER};', SUPER, PRIVILEGES),
    'function schema owned by another role': (f'ALTER SCHEMA hindsight_metrics OWNER TO {APP};',
                                              f'ALTER SCHEMA hindsight_metrics OWNER TO {OWNER};', SUPER, PRIVILEGES),
}
# Over the completed cap and defaults, which the undo restores in place; the refused value stays meanwhile.
POST_APPLY_REFUSALS = {
    'cap lowered by hand': (f"ALTER ROLE {READER} SET temp_file_limit = '1MB';",
                            f"ALTER ROLE {READER} SET temp_file_limit = '128MB';", SUPER, SETTINGS),
    'approved default changed': (f"ALTER ROLE {READER} SET statement_timeout = '10s';",
                                 f"ALTER ROLE {READER} SET statement_timeout = '5s';", SUPER, SETTINGS),
    'unknown setting beside the defaults': (*setting(f'ROLE {READER}', 'work_mem', '64kB'), SUPER, SETTINGS),
}


def apply(env, user=None, database=DB, sslmode='require'):
    """The committed file itself, through psql -f, as the Job runs it; user None is the fixture superuser."""
    command = ['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=verbose',
               '-f', str(SQL)]
    run_env = connection(env, database, sslmode)
    if user is not None:
        run_env['PGUSER'] = user
    return subprocess.run(command, env=run_env, capture_output=True, text=True, timeout=120)


def snapshot(env):
    return {**HARDENING.snapshot(env), **json.loads(ok(connection(env), SETTINGS_SNAPSHOT, 'settings snapshot'))}


def with_reader(state, entries):
    """`state` with the reader's role default replaced by exactly `entries`, in stored order."""
    others = [entry for entry in state['settings'] or [] if not entry.startswith(f'*/{READER}=')]
    return {**state, 'settings': sorted(others + ([f"*/{READER}={{{','.join(entries)}}}"] if entries else []))}


def writable(env):
    return {**connection(env), 'PGOPTIONS': WRITABLE}


def applied(env, label):
    HARDENING.settle(env)
    result = apply(env)
    if result.returncode != 0 or result.stdout.strip():
        raise Failed(f'{label}: {result.stderr.strip()[-400:]}')


def refusal(env, label, arrange, undo, run_as, message, held=OWNER):
    before = snapshot(env)
    if arrange:
        ok(connection(env), arrange, f'arrange: {label}')
    try:
        arranged = snapshot(env)
        HARDENING.settle(env, held)
        result = apply(env, *run_as)
        if result.returncode == 0 or 'ERROR:  P0001:' not in result.stderr or message not in result.stderr \
                or result.stdout.strip():
            raise Failed(f'{label}: expected guard P0001 "{message}" and no output, '
                         f'got rc={result.returncode} {result.stderr.strip()[-300:]}')
        if snapshot(env) != arranged:
            raise Failed(f'{label}: the refused apply changed the catalog instead of rolling back')
    finally:
        if undo:
            ok(connection(env), undo, f'undo: {label}')
    if snapshot(env) != before:
        raise Failed(f'{label}: undo did not restore the catalog')


def hba_text(env):
    # psql -A -t ends its output with one newline of its own.
    return ok(connection(env, 'postgres'), "SELECT pg_read_file(current_setting('hba_file'));", 'read pg_hba')[:-1]


def write_hba(env, lines):
    """Server-side rewrite of the fixture's pg_hba, one line each, then a reload that every new session sees."""
    postgres = connection(env, 'postgres')
    path = ok(postgres, 'SHOW hba_file;', 'pg_hba path').strip()
    loaded = ok(postgres, 'SELECT pg_conf_load_time();', 'configuration load time').strip()
    rows = ', '.join(map(literal, lines))
    # Control-character delimiter and quote, so CSV output never quotes or splits a line; a blank line goes out
    # as NULL, which CSV writes unquoted, where an empty string would be written as two quote characters.
    ok(postgres, f"COPY (SELECT nullif(line, '') FROM unnest(ARRAY[{rows}]::text[]) WITH ORDINALITY u(line, n) "
       f"ORDER BY n) TO {literal(path)} (FORMAT csv, DELIMITER E'\\x01', QUOTE E'\\x02'); SELECT pg_reload_conf();",
       'write pg_hba')
    # A new backend inherits the postmaster's load time, so a later one means new logins use the new rules.
    deadline = time.monotonic() + 10
    while ok(postgres, f'SELECT pg_conf_load_time() > {literal(loaded)}::timestamptz;', 'pg_hba reload').strip() != 't':
        if time.monotonic() > deadline:
            raise Failed('the fixture did not reload pg_hba')
        time.sleep(0.1)


@contextlib.contextmanager
def reader_hba(env):
    """The production reader rules ahead of the fixture's own for the duration, then its original bytes back."""
    original = hba_text(env)
    if not original.endswith('\n'):
        raise Failed('the fixture pg_hba does not end in a newline, so it cannot be restored byte for byte')
    lines = original.split('\n')[:-1]
    write_hba(env, READER_HBA + lines)
    try:
        yield lines
    finally:
        write_hba(env, lines)
    if hba_text(env) != original:
        raise Failed('the fixture pg_hba was not restored byte for byte')


def reader_login(env, database=DB, sslmode='require', password=None):
    run_env = {**connection(env, database, sslmode), 'PGUSER': READER}
    if password is not None:
        run_env['PGPASSWORD'] = password
    return subprocess.run(['psql', '-X', '-q', '-A', '-t', '-w', '-c', LOGIN], env=run_env, capture_output=True,
                          text=True, timeout=60)


def readers_gone(env):
    """Wait for reader backends in any database to exit; the apply refuses while one is connected."""
    deadline = time.monotonic() + 10
    while ok(connection(env, 'postgres'), READER_SESSIONS, 'reader session probe').strip() != '0':
        if time.monotonic() > deadline:
            raise Failed('an earlier reader session is still connected')
        time.sleep(0.1)


def prove_reader_logins(env):
    """With the reader rules first: SCRAM over TLS to hindsight only, every other reader login rejected."""
    login = reader_login(env)
    if login.returncode != 0 or login.stdout.strip() != f'scram-sha-256:{READER}|t':
        raise Failed(f'a TLS reader login to {DB} did not authenticate by SCRAM: {login.stderr.strip()[-300:]}')
    for label, kwargs, message in (('wrong password', {'password': secrets.token_hex(16)}, SCRAM_FAILED),
                                   ('non-TLS', {'sslmode': 'disable'}, HBA_REJECTED),
                                   ('other database over TLS', {'database': 'postgres'}, HBA_REJECTED)):
        result = reader_login(env, **kwargs)
        if result.returncode == 0 or message not in result.stderr or result.stdout.strip():
            raise Failed(f'reader {label} login: expected "{message}", got rc={result.returncode} '
                         f'{result.stderr.strip()[-300:]}')
    readers_gone(env)


def prove_hba_evidence_is_limited(env, lines, after):
    """An earlier rule admitting the reader decides its logins, and the guard still passes: it shows the rules are
    present and ordered, never that they confine the reader."""
    write_hba(env, [PERMISSIVE_HBA] + READER_HBA + lines)
    try:
        login = reader_login(env, 'postgres', 'disable')
        if login.returncode != 0 or login.stdout.strip() != '|f':
            raise Failed('the earlier permissive rule did not admit a non-TLS reader login without a password')
        readers_gone(env)
        applied(env, 'rerun with an earlier rule admitting the reader')
        if snapshot(env) != after:
            raise Failed('the rerun with an earlier permissive rule was not a no-op')
    finally:
        write_hba(env, READER_HBA + lines)


def reader_cap(env, label, sql='SHOW temp_file_limit;'):
    if ok(connection(env), sql, f'reader: {label}', user=READER).strip().splitlines()[-1:] != ['128MB']:
        raise Failed(f'reader: {label} does not leave the cap in force')


def prove_runtime(env, after):
    if json.loads(ok(connection(env), READER_STATE, 'reader session state', user=READER)) != READER_SESSION:
        raise Failed('a fresh reader session lacks the cap as its role default, the approved defaults, '
                     'or the TEMPORARY revoke')
    for user in (OWNER, APP):
        if ok(connection(env), 'SHOW temp_file_limit;', f'{user} temp_file_limit', user=user).strip() != '-1':
            raise Failed(f'{user}: the cap reached a role it was not written for')
    for statement in READER_DENIED:
        refused(writable(env), statement, '42501', f'reader: {statement}', user=READER)
    startup = subprocess.run(['psql', '-X', '-q', '-A', '-t', '-w', '-c', 'SHOW temp_file_limit;'],
                             env={**connection(env), 'PGUSER': READER, 'PGOPTIONS': '-c temp_file_limit=-1'},
                             capture_output=True, text=True, timeout=60)
    if startup.returncode == 0 or STARTUP_DENIED not in startup.stderr or startup.stdout.strip():
        raise Failed('a startup PGOPTIONS override of the cap was not refused')
    reader_cap(env, 'RESET ALL', 'RESET ALL; SHOW temp_file_limit;')
    if snapshot(env) != after:
        raise Failed('a refused or reset reader attempt changed a setting or a privilege')
    # The reader may drop its own user-settable defaults, never the superuser-context cap.
    ok(writable(env), f'ALTER ROLE {READER} RESET ALL;', 'reader: ALTER ROLE RESET ALL', user=READER)
    if snapshot(env) != with_reader(after, [CAP]):
        raise Failed('the reader ALTER ROLE RESET ALL did not leave exactly the stored cap')
    reader_cap(env, 'session after ALTER ROLE RESET ALL')
    # The live order: the cap first, the defaults written after it; a rerun then changes nothing.
    ok(connection(env), SET_DEFAULTS, 'approved defaults after the cap')
    applied(env, 'rerun over the cap followed by the defaults')
    if snapshot(env) != with_reader(after, [CAP] + DEFAULTS):
        raise Failed('the rerun over the cap followed by the defaults was not a no-op')
    ok(connection(env), RESTORE_DEFAULTS, 'restore the approved defaults')
    applied(env, 'reapply the cap over the restored defaults')
    if snapshot(env) != after:
        raise Failed('restoring the defaults and reapplying did not return to the applied state')
    if ok(connection(env), SPILL.format(rows=SPILL_UNDER), 'spill under the cap', user=READER).strip() \
            != str(SPILL_UNDER):
        raise Failed('a spill under the cap did not complete')
    if SPILL_STOPPED not in refused(connection(env), SPILL.format(rows=SPILL_OVER), '53400', 'spill past the cap',
                                    user=READER):
        raise Failed('the oversized spill did not stop at the 128MB cap')


def prove(env):
    # A throwaway password of the fixture's own reader, so its rule really runs SCRAM; trust ignores it elsewhere.
    password = secrets.token_hex(16)
    ok(connection(env), f'ALTER ROLE {READER} PASSWORD {literal(password)};', 'fixture reader password')
    env = {**env, 'PGPASSWORD': password}
    ok(connection(env), OWN_SETTINGS, 'settings of other roles')
    start = snapshot(env)
    refusal(env, 'TEMPORARY revoke not applied', None, None, SUPER, REVOKE_MISSING)
    HARDENING.applied(env, 'committed TEMPORARY revoke as the owner over TLS')
    refusal(env, 'reader rules missing from pg_hba', None, None, SUPER, HBA_MISSING)
    with reader_hba(env) as lines:
        prove_reader_logins(env)
        before = snapshot(env)
        if before != {**start, 'datacl': before['datacl']} or before['settings'] is None:
            raise Failed('the revoke changed role settings, or the fixture lacks settings of other roles')
        for label, case in PRE_APPLY_REFUSALS.items():
            refusal(env, label, *case)
        with HARDENING.held_session(env, READER):
            refusal(env, 'held reader session', None, None, SUPER, SESSION_REFUSAL, held=READER)
        if snapshot(env) != before:
            raise Failed('refused applies left the catalog changed')

        applied(env, 'apply over no reader settings as the superuser over TLS')
        if snapshot(env) != with_reader(before, [CAP]):
            raise Failed('the apply over no reader settings changed more than the reader role default')
        applied(env, 'rerun over the cap alone')
        if snapshot(env) != with_reader(before, [CAP]):
            raise Failed('the rerun over the cap alone was not a no-op')
        ok(connection(env), RESTORE_DEFAULTS, 'the approved reader defaults')
        defaults = snapshot(env)
        if defaults != with_reader(before, DEFAULTS):
            raise Failed('the fixture reader defaults are not the approved five alone')
        applied(env, 'apply over the approved defaults')
        after = snapshot(env)
        if after != with_reader(before, DEFAULTS + [CAP]):
            raise Failed('the apply did not keep the approved defaults byte-identical and add only the cap')
        applied(env, 'rerun over the completed cap')
        if snapshot(env) != after:
            raise Failed('the rerun over the completed cap was not a no-op')
        prove_runtime(env, after)
        if snapshot(env) != after:
            raise Failed('the rerun or the reader attempts changed a setting or a privilege')

        ok(connection(env), METRICS, 'the function grants as its owner applies them', user=OWNER)
        granted = snapshot(env)
        if granted['acl_dependencies'] != after['acl_dependencies'] + 2:
            raise Failed('the function grants did not record two reader object ACL dependencies')
        if ok(connection(env), METRICS_CALL, 'reader function call', user=READER).strip() != '1':
            raise Failed('the reader cannot call the function its grants name')
        applied(env, 'rerun with exactly the function grants to the reader')
        if snapshot(env) != granted:
            raise Failed('the rerun with the function grants was not a no-op')
        for label, case in METRICS_REFUSALS.items():
            refusal(env, label, *case)
        ok(connection(env), METRICS_UNDO, 'drop the function and its schema', user=OWNER)
        if snapshot(env) != after:
            raise Failed('dropping the function grants did not return to the applied state')

        prove_hba_evidence_is_limited(env, lines, after)
        for label, case in POST_APPLY_REFUSALS.items():
            refusal(env, label, *case)
        final = snapshot(env)
        if final != after or final['parameter_acl'] is not None or final['reader_set']:
            raise Failed('the catalog after every case is not the applied cap alone')


def main():
    try:
        env = HARDENING.checker_env(os.environ)
    except Refused as error:
        raise SystemExit(f'refused: {error}') from None
    postgres = connection(env, 'postgres')
    try:
        state = json.loads(ok(postgres, HARDENING.SERVER_STATE, 'server inspection'))
    except Failed:
        raise SystemExit('native TLS PostgreSQL fixture unavailable') from None
    if state != HARDENING.EMPTY_SERVER or json.loads(ok(postgres, LEFTOVERS, 'settings inspection')) != [0, 0]:
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
                or json.loads(ok(postgres, LEFTOVERS, 'settings inspection')) != [0, 0]:
            raise SystemExit('fixture teardown left objects behind')
    print('native PostgreSQL backlog reader temp_file_limit TLS fixture PASS')


if __name__ == '__main__':
    main()
