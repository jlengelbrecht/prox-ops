#!/usr/bin/env python3
"""Native PostgreSQL proof of the committed hindsight database TEMP revoke, over TLS.

Only for a dedicated, otherwise empty, TLS-enabled fixture server reached over TCP
loopback: it creates its own roles and database hindsight, drops only those, and
refuses (never skips) otherwise.
"""
import contextlib
import importlib.util
import json
import os
import pathlib
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
SQL = ROOT / 'kubernetes/apps/database/hindsight-backlog/db-acl/db-acl.sql'


def _load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NATIVE = _load('backlog_function_native', 'tests/hindsight-observability/check_postgres_backlog_function.py')
Refused, Failed, ok, refused = NATIVE.Refused, NATIVE.Failed, NATIVE.ok, NATIVE.refused
DB, OWNER, READER = 'hindsight', 'hindsight', 'hindsight_backlog_metrics'
# A declared non-owner role (CNPG's app) and one outside the declared roster.
APP, UNDECLARED = 'app', 'hindsight_backlog_undeclared'
FIXTURE_ROLES = (OWNER, READER, APP, UNDECLARED)
COMPLETED_ACL = ['=c/hindsight', 'hindsight=CTc/hindsight']
EMPTY_SERVER = {'db': 'postgres', 'major': 16, 'superuser': True, 'databases': ['postgres', 'template0', 'template1'],
                'roles': 0, 'relations': 0, 'schemas': 0, 'clients': 0, 'reserved_user': False}
RESERVED = ', '.join(f"'{role}'" for role in FIXTURE_ROLES)

SERVER_STATE = f"""SELECT json_build_object(
 'reserved_user', current_user IN ({RESERVED}),
 'db', current_database(), 'major', current_setting('server_version_num')::int / 10000,
 'superuser', (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
 'databases', (SELECT json_agg(datname ORDER BY datname) FROM pg_database),
 'roles', (SELECT count(*) FROM pg_roles WHERE rolname !~ '^pg_' AND rolname <> current_user),
 'relations', (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'),
 'schemas', (SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_'
             AND nspname NOT IN ('information_schema', 'public')),
 'clients', (SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'
             AND pid <> pg_backend_pid()));
"""
TLS_SESSION = 'SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid();'
BOOTSTRAP_ROLES = f"""BEGIN;
CREATE ROLE {OWNER} LOGIN;
CREATE ROLE {READER} LOGIN NOINHERIT CONNECTION LIMIT 2;
CREATE ROLE {APP} LOGIN;
COMMIT;
"""
# Run as the owner once database hindsight exists: owner data and non-default ACLs
# that the revoke must leave alone, with no grant that would pin a fixture role.
BOOTSTRAP_OWNED = """BEGIN;
CREATE SCHEMA owned;
CREATE TABLE owned.probe (i int PRIMARY KEY, t text NOT NULL);
INSERT INTO owned.probe SELECT g, md5(g::text) FROM generate_series(1, 50) g;
GRANT USAGE ON SCHEMA owned TO PUBLIC;
GRANT SELECT ON owned.probe TO PUBLIC;
COMMIT;
"""
# DROP DATABASE cannot run in a transaction; FORCE ends any session a failed case left behind.
TEARDOWN = f"""DROP DATABASE IF EXISTS {DB} WITH (FORCE);
DROP ROLE IF EXISTS {UNDECLARED}, {APP}, {READER}, {OWNER};
"""
# Read as the fixture superuser inside database hindsight. Database hindsight's
# own ACL is reported sorted and apart, so the apply may change exactly that.
SNAPSHOT = f"""SELECT json_build_object(
 'datacl', (SELECT json_agg(a::text ORDER BY a::text COLLATE "C") FROM pg_database d, unnest(d.datacl) a
            WHERE d.datname = '{DB}'),
 'databases', (SELECT json_agg(datname || ':' || datdba::regrole::text || ':'
               || CASE WHEN datname = '{DB}' THEN '' ELSE coalesce(datacl::text, 'NULL') END
               ORDER BY datname COLLATE "C") FROM pg_database),
 'roles', (SELECT json_agg(concat_ws(',', rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
           rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil) ORDER BY rolname COLLATE "C") FROM pg_roles),
 'memberships', (SELECT count(*) FROM pg_auth_members),
 'schemas', (SELECT json_agg(nspname || ':' || nspowner::regrole::text || ':' || coalesce(nspacl::text, 'NULL')
             ORDER BY nspname COLLATE "C") FROM pg_namespace WHERE nspname !~ '^pg_'),
 'relations', (SELECT json_agg(c.oid::regclass::text || ':' || c.relowner::regrole::text || ':'
               || coalesce(c.relacl::text, 'NULL') ORDER BY c.oid::regclass::text COLLATE "C")
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'),
 'routines', (SELECT json_agg(p.oid::regprocedure::text || ':' || p.proowner::regrole::text || ':'
              || coalesce(p.proacl::text, 'NULL') ORDER BY p.oid::regprocedure::text COLLATE "C")
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'),
 'large_objects', (SELECT json_agg(oid::text || ':' || lomowner::regrole::text ORDER BY oid)
                   FROM pg_largeobject_metadata),
 'data', (SELECT md5(string_agg(i || ':' || t, ',' ORDER BY i)) FROM owned.probe),
 'default_acls', (SELECT json_agg(defaclacl::text ORDER BY defaclacl::text COLLATE "C") FROM pg_default_acl),
 'acl_dependencies', (SELECT count(*) FROM pg_shdepend WHERE deptype = 'a'));
"""
PRIVILEGES = ("SELECT json_build_array(has_database_privilege(current_database(), 'CONNECT'), "
              "has_database_privilege(current_database(), 'TEMPORARY'), "
              "has_database_privilege(current_database(), 'CREATE'));")
OWNER_WORK = """CREATE TEMP TABLE owner_temp AS SELECT * FROM owned.probe;
BEGIN; INSERT INTO owned.probe VALUES (0, 'rolled back'); ROLLBACK;
CREATE TABLE owned.scratch (i int); DROP TABLE owned.scratch;
DROP TABLE owner_temp;
"""
TEMP_TABLE = 'CREATE TEMP TABLE denied_temp (i int);'
# Proves CNPG's ensure: absent would still work: no database ACL dependency pins either role.
DROP_ROLES = f'BEGIN; DROP ROLE {APP}; DROP ROLE {READER}; ROLLBACK;'
# A generic owner routine granted to the reader alone, standing in for the later
# metrics function: object grants are not database ACL, so a rerun leaves them be.
STAND_IN = f"""BEGIN;
CREATE SCHEMA stand_in;
CREATE FUNCTION stand_in.probe() RETURNS int LANGUAGE sql STABLE AS 'SELECT 1';
REVOKE EXECUTE ON FUNCTION stand_in.probe() FROM PUBLIC;
GRANT USAGE ON SCHEMA stand_in TO {READER};
GRANT EXECUTE ON FUNCTION stand_in.probe() TO {READER};
COMMIT;
"""
STAND_IN_UNDO = f"""BEGIN;
REVOKE EXECUTE ON FUNCTION stand_in.probe() FROM {READER};
REVOKE USAGE ON SCHEMA stand_in FROM {READER};
DROP FUNCTION stand_in.probe();
DROP SCHEMA stand_in;
COMMIT;
"""
STAND_IN_CALL = 'SELECT stand_in.probe();'
SESSION_REFUSAL = 'another non-superuser session is connected'
# The held client creates a temp table first, the state the session guard exists for.
HELD_TEMP = 'CREATE TEMP TABLE held_temp (i int);'

APPLY = (OWNER, DB, 'require')
# label: (arrange, undo, (user, database, sslmode) for the apply, the guard message it must raise)
# Arrange and undo run as the fixture superuser in database hindsight.
PRE_APPLY_REFUSALS = {
    'non-TLS session': (None, None, (OWNER, DB, 'disable'), 'this session is not using TLS'),
    'superuser caller': (None, None, (None, DB, 'require'), 'apply as a non-superuser session'),
    # The right role name is not enough: the guard must refuse by attribute when
    # the owner itself is a superuser. Only the fixture's own owner is altered.
    'owner promoted to superuser': (f'ALTER ROLE {OWNER} SUPERUSER;', f'ALTER ROLE {OWNER} NOSUPERUSER;', APPLY,
                                    'apply as a non-superuser session'),
    'wrong database': (None, None, (OWNER, 'postgres', 'require'), 'run in database hindsight'),
    'undeclared role': (f'CREATE ROLE {UNDECLARED} NOLOGIN;', f'DROP ROLE {UNDECLARED};', APPLY,
                        'a role outside the declared roster'),
    'schema owned by a declared non-owner role': (f'CREATE SCHEMA app_owned AUTHORIZATION {APP};',
                                                  'DROP SCHEMA app_owned;', APPLY, 'is owned by a role other'),
    # Only the standard public schema may be owned by pg_database_owner.
    'other schema owned by pg_database_owner': ('CREATE SCHEMA builtin_owned AUTHORIZATION pg_database_owner;',
                                                'DROP SCHEMA builtin_owned;', APPLY, 'is owned by a role other'),
    'reader INHERIT': (f'ALTER ROLE {READER} INHERIT;', f'ALTER ROLE {READER} NOINHERIT;', APPLY,
                       'attributes or memberships drifted'),
    'reader connection limit': (f'ALTER ROLE {READER} CONNECTION LIMIT 3;', f'ALTER ROLE {READER} CONNECTION LIMIT 2;',
                                APPLY, 'attributes or memberships drifted'),
    'reader member of the owner': (f'GRANT {OWNER} TO {READER};', f'REVOKE {OWNER} FROM {READER};', APPLY,
                                   'attributes or memberships drifted'),
    'default grant to PUBLIC': (f'ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} GRANT SELECT ON TABLES TO PUBLIC;',
                                f'ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} REVOKE SELECT ON TABLES FROM PUBLIC;',
                                APPLY, 'a default privilege grants to PUBLIC'),
    # A predefined role is pinned, so pg_shdepend holds no row for this owner.
    'large object owned by a predefined role': ('SELECT lo_create(424242); '
                                                'ALTER LARGE OBJECT 424242 OWNER TO pg_read_all_data;',
                                                'SELECT lo_unlink(424242);', APPLY, 'is owned by a role other'),
}
# Run over the completed ACL, which their undo restores exactly; from the NULL
# default it would leave a materialised ACL behind instead.
POST_APPLY_REFUSALS = {
    'explicit database grant': (f'GRANT CONNECT ON DATABASE {DB} TO {APP};',
                                f'REVOKE CONNECT ON DATABASE {DB} FROM {APP};', APPLY, 'holds an explicit privilege'),
    'non-default database ACL': (f'REVOKE CONNECT ON DATABASE {DB} FROM PUBLIC;',
                                 f'GRANT CONNECT ON DATABASE {DB} TO PUBLIC;', APPLY, 'an ACL this file did not produce'),
}


def checker_env(environ):
    """fixture_env, also refusing this proof's extra role names as the caller."""
    env = NATIVE.fixture_env(environ)
    if environ.get('PGUSER') in FIXTURE_ROLES:
        raise Refused('PGUSER must be the fixture admin, not a role this proof creates')
    return env


def connection(env, database=DB, sslmode='require'):
    # Set on every call, never taken from the caller: a non-TLS run must not pass by default.
    return {**env, 'PGDATABASE': database, 'PGSSLMODE': sslmode}


def apply(env, user=OWNER, database=DB, sslmode='require'):
    """The committed file itself, through psql -f, as a Job would run it."""
    command = ['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=verbose',
               '-f', str(SQL)]
    run_env = connection(env, database, sslmode)
    if user is not None:
        run_env['PGUSER'] = user
    return subprocess.run(command, env=run_env, capture_output=True, text=True, timeout=120)


def settle(env, held=OWNER):
    """Wait for earlier fixture clients to leave: psql exits before its backend does."""
    probe = (f"SELECT count(*) FROM pg_stat_activity a JOIN pg_roles r ON r.oid = a.usesysid "
             f"WHERE a.datname = '{DB}' AND NOT r.rolsuper AND r.rolname NOT IN ('{OWNER}', '{held}');")
    deadline = time.monotonic() + 10
    while ok(connection(env, 'postgres'), probe, 'lingering session probe').strip() != '0':
        if time.monotonic() > deadline:
            raise Failed('an earlier fixture session is still connected to database hindsight')
        time.sleep(0.1)


def applied(env, label):
    settle(env)
    result = apply(env)
    if result.returncode != 0 or result.stdout.strip():
        raise Failed(f'{label}: {result.stderr.strip()[-400:]}')


def snapshot(env):
    return json.loads(ok(connection(env), SNAPSHOT, 'catalog snapshot'))


def privileges(env, user):
    return json.loads(ok(connection(env), PRIVILEGES, f'{user} database privileges', user=user))


def apply_refused(env, label, run_as, message, held=OWNER):
    user, database, sslmode = run_as
    settle(env, held)
    result = apply(env, user, database, sslmode)
    if result.returncode == 0 or 'ERROR:  P0001:' not in result.stderr or message not in result.stderr \
            or result.stdout.strip():
        raise Failed(f'{label}: expected guard P0001 "{message}" and no output, '
                     f'got rc={result.returncode} {result.stderr.strip()[-300:]}')


def refusal(env, label, arrange, undo, run_as, message, held=OWNER):
    before = snapshot(env)
    if arrange:
        ok(connection(env), arrange, f'arrange: {label}')
    try:
        arranged = snapshot(env)
        apply_refused(env, label, run_as, message, held)
        if snapshot(env) != arranged:
            raise Failed(f'{label}: the refused apply changed the catalog instead of rolling back')
    finally:
        if undo:
            ok(connection(env), undo, f'undo: {label}')
    if snapshot(env) != before:
        raise Failed(f'{label}: undo did not restore the catalog')


@contextlib.contextmanager
def held_session(env, user):
    """A client of `user` holding a temp table in database hindsight for the duration."""
    sessions = f"FROM pg_stat_activity WHERE usename = '{user}' AND datname = '{DB}'"
    # Separate -c commands, so the temp table commits before the sleep.
    process = subprocess.Popen(['psql', '-X', '-q', '-w', '-c', HELD_TEMP, '-c', 'SELECT pg_sleep(60);'],
                               env={**connection(env), 'PGUSER': user},
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        # Sleeping means the temp table already exists.
        while ok(connection(env, 'postgres'), f"SELECT count(*) {sessions} AND wait_event = 'PgSleep';",
                 'held session probe').strip() != '1':
            if process.poll() is not None or time.monotonic() > deadline:
                raise Failed(f'held {user} session never connected')
            time.sleep(0.2)
        yield
    finally:
        # The timeout makes the call wait until each backend has actually exited.
        ok(connection(env, 'postgres'), f'SELECT pg_terminate_backend(pid, 10000) {sessions};', 'end held session')
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if ok(connection(env, 'postgres'), f'SELECT count(*) {sessions};', 'held session gone').strip() != '0':
            raise Failed(f'held {user} session outlived its termination')


def prove(env):
    ok(connection(env), OWNER_WORK, 'owner work before the revoke', user=OWNER)
    for user in (READER, APP):
        if privileges(env, user) != [True, True, False]:
            raise Failed(f'{user}: the default PUBLIC TEMPORARY baseline is missing, so the revoke would prove nothing')
    before = snapshot(env)
    if before['datacl'] is not None or before['acl_dependencies'] != 0:
        raise Failed('database hindsight does not start from the default ACL')

    for label, case in PRE_APPLY_REFUSALS.items():
        refusal(env, label, *case)
    for user in (APP, READER):
        with held_session(env, user):
            refusal(env, f'held {user} session', None, None, APPLY, SESSION_REFUSAL, held=user)
    if snapshot(env) != before:
        raise Failed('refused applies left the catalog changed')

    applied(env, 'apply as the owner over TLS')
    after = snapshot(env)
    if after != {**before, 'datacl': COMPLETED_ACL}:
        raise Failed('the apply changed more than the ACL of database hindsight')
    ok(connection(env), OWNER_WORK, 'owner work after the revoke', user=OWNER)
    for user in (READER, APP):
        refused(connection(env), TEMP_TABLE, '42501', f'{user} temporary table after the revoke', user=user)
        if privileges(env, user) != [True, False, False]:
            raise Failed(f'{user}: expected CONNECT only on database hindsight')
    applied(env, 'rerun over the completed ACL')
    if snapshot(env) != after:
        raise Failed('the rerun was not a no-op')

    ok(connection(env), STAND_IN, 'owner stand-in routine granted to the reader', user=OWNER)
    granted = snapshot(env)
    if granted['datacl'] != COMPLETED_ACL or granted['acl_dependencies'] != 2:
        raise Failed('the stand-in grants did not record the two reader object ACL dependencies')
    if ok(connection(env), STAND_IN_CALL, 'reader stand-in call', user=READER).strip() != '1':
        raise Failed('the reader cannot call the stand-in routine')
    refused(connection(env), STAND_IN_CALL, '42501', 'app stand-in call', user=APP)
    applied(env, 'rerun with reader object grants')
    if snapshot(env) != granted:
        raise Failed('the rerun with reader object grants was not a no-op')
    ok(connection(env), STAND_IN_UNDO, 'drop the stand-in routine', user=OWNER)
    if snapshot(env) != after:
        raise Failed('the stand-in undo did not restore the catalog')

    for label, case in POST_APPLY_REFUSALS.items():
        refusal(env, label, *case)
    ok(connection(env, 'postgres'), DROP_ROLES, 'DROP ROLE of the reader and a declared role after the revoke')
    if snapshot(env) != after:
        raise Failed('post-apply refusals left the catalog changed')


def main():
    try:
        env = checker_env(os.environ)
    except Refused as error:
        raise SystemExit(f'refused: {error}') from None
    try:
        state = json.loads(ok(connection(env, 'postgres'), SERVER_STATE, 'server inspection'))
    except Failed:
        raise SystemExit('native TLS PostgreSQL fixture unavailable') from None
    if state != EMPTY_SERVER:
        raise SystemExit('refused: server is not an empty dedicated PostgreSQL 16 fixture (needs superuser, default '
                         'databases only, and no other roles, schemas, relations or client sessions)')
    if ok(connection(env, 'postgres'), TLS_SESSION, 'TLS session check').strip() != 't':
        raise SystemExit('fixture session is not using TLS; the TLS guard cannot be proven')
    try:
        ok(connection(env, 'postgres'), BOOTSTRAP_ROLES, 'fixture roles')
        ok(connection(env, 'postgres'), f'CREATE DATABASE {DB} OWNER {OWNER};', 'fixture database')
        ok(connection(env), BOOTSTRAP_OWNED, 'fixture owner data', user=OWNER)
        prove(env)
    finally:
        ok(connection(env, 'postgres'), TEARDOWN, 'fixture teardown')
        if json.loads(ok(connection(env, 'postgres'), SERVER_STATE, 'server inspection')) != EMPTY_SERVER:
            raise SystemExit('fixture teardown left objects behind')
    print('native PostgreSQL hindsight database TEMP revoke TLS fixture PASS')


if __name__ == '__main__':
    main()
