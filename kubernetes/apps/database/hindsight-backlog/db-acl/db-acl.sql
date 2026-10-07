-- Not referenced by any Kustomization: nothing applies this file yet.
-- Removes PUBLIC's TEMPORARY on database hindsight and grants it to nobody: the
-- owner keeps CTc and superusers bypass ACLs, while every other role loses
-- TEMPORARY on this database only. No per-role grant is added, because one would
-- record a shared dependency that blocks DROP ROLE. Apply over TLS as the
-- hindsight role itself with psql -X -v ON_ERROR_STOP=1 -f; every guard refuses
-- and rolls back on drift, and a rerun over the completed ACL is a no-op.
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $guard$
DECLARE
 db_oid oid := (SELECT oid FROM pg_database WHERE datname = current_database());
 owner_oid oid := (SELECT oid FROM pg_roles WHERE rolname = 'hindsight');
 reader_oid oid := (SELECT oid FROM pg_roles WHERE rolname = 'hindsight_backlog_metrics');
BEGIN
 IF session_user <> 'hindsight' OR current_user <> 'hindsight'
    OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
  RAISE EXCEPTION 'apply as a non-superuser session of the hindsight role itself';
 END IF;
 IF current_database() <> 'hindsight'
    OR (SELECT datdba FROM pg_database WHERE oid = db_oid) IS DISTINCT FROM owner_oid THEN
  RAISE EXCEPTION 'run in database hindsight, owned by hindsight';
 END IF;
 -- Over a Unix socket this is always false, so the apply must use TCP with TLS.
 IF (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) IS NOT TRUE THEN
  RAISE EXCEPTION 'this session is not using TLS';
 END IF;
 IF current_setting('server_version_num')::int / 10000 <> 16 OR pg_is_in_recovery() THEN
  RAISE EXCEPTION 'expected a PostgreSQL 16 primary';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE oid = reader_oid AND rolcanlogin AND NOT rolsuper AND NOT rolinherit
                AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolreplication AND NOT rolbypassrls
                AND rolconnlimit = 2 AND rolvaliduntil IS NULL)
    OR EXISTS (SELECT 1 FROM pg_auth_members WHERE reader_oid IN (member, roleid)) THEN
  RAISE EXCEPTION 'hindsight_backlog_metrics attributes or memberships drifted';
 END IF;
 -- The managed application roles plus CNPG's app and streaming_replica. A new
 -- role must be reviewed for the lost TEMPORARY before this file accepts it.
 IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname !~ '^pg_' AND NOT rolsuper
            AND rolname NOT IN ('hindsight', 'hindsight_backlog_metrics', 'app', 'streaming_replica',
                                'toolhive', 'zabbix', 'glycemicgpt_bot', 'paperclip')) THEN
  RAISE EXCEPTION 'a role outside the declared roster exists';
 END IF;
 -- The reader too: a backend that already created its temp namespace keeps
 -- using it after the revoke, so only the owner may be connected.
 IF EXISTS (SELECT 1 FROM pg_stat_activity a JOIN pg_roles r ON r.oid = a.usesysid
            WHERE a.datid = db_oid AND NOT r.rolsuper AND a.usesysid <> owner_oid) THEN
  RAISE EXCEPTION 'another non-superuser session is connected to database hindsight';
 END IF;
 -- Every PG16 per-database owner column, because pg_shdepend records no owner
 -- that is pinned, such as a predefined role; the pg_shdepend scan still covers
 -- classes not listed. pg_database_owner stands for the database owner on the
 -- standard public schema and is accepted there alone.
 IF EXISTS (SELECT 1 FROM (
             SELECT nspowner AS owner FROM pg_namespace
              WHERE NOT (nspname = 'public' AND nspowner = 'pg_database_owner'::regrole)
             UNION ALL SELECT relowner FROM pg_class
             UNION ALL SELECT proowner FROM pg_proc
             UNION ALL SELECT typowner FROM pg_type
             UNION ALL SELECT collowner FROM pg_collation
             UNION ALL SELECT conowner FROM pg_conversion
             UNION ALL SELECT oprowner FROM pg_operator
             UNION ALL SELECT opcowner FROM pg_opclass
             UNION ALL SELECT opfowner FROM pg_opfamily
             UNION ALL SELECT cfgowner FROM pg_ts_config
             UNION ALL SELECT dictowner FROM pg_ts_dict
             UNION ALL SELECT stxowner FROM pg_statistic_ext
             UNION ALL SELECT lanowner FROM pg_language
             UNION ALL SELECT extowner FROM pg_extension
             UNION ALL SELECT fdwowner FROM pg_foreign_data_wrapper
             UNION ALL SELECT srvowner FROM pg_foreign_server
             UNION ALL SELECT evtowner FROM pg_event_trigger
             UNION ALL SELECT pubowner FROM pg_publication
             UNION ALL SELECT subowner FROM pg_subscription WHERE subdbid = db_oid
             UNION ALL SELECT lomowner FROM pg_largeobject_metadata
             UNION ALL SELECT refobjid FROM pg_shdepend
              WHERE dbid = db_oid AND refclassid = 'pg_authid'::regclass AND deptype = 'o') o
            LEFT JOIN pg_roles r ON r.oid = o.owner
            WHERE o.owner <> owner_oid AND r.rolsuper IS NOT TRUE) THEN
  RAISE EXCEPTION 'an object in database hindsight is owned by a role other than hindsight or a superuser';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_shdepend WHERE classid = 'pg_database'::regclass AND objid = db_oid AND deptype = 'a') THEN
  RAISE EXCEPTION 'a role holds an explicit privilege on database hindsight';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_default_acl d, aclexplode(d.defaclacl) a WHERE a.grantee IN (0::oid, reader_oid)) THEN
  RAISE EXCEPTION 'a default privilege grants to PUBLIC or hindsight_backlog_metrics';
 END IF;
 IF (SELECT datacl FROM pg_database WHERE oid = db_oid) IS NOT NULL
    AND ARRAY(SELECT a::text FROM pg_database d, unnest(d.datacl) a WHERE d.oid = db_oid ORDER BY a::text COLLATE "C")
        <> ARRAY['=c/hindsight', 'hindsight=CTc/hindsight'] THEN
  RAISE EXCEPTION 'database hindsight has an ACL this file did not produce';
 END IF;
END
$guard$;
REVOKE TEMPORARY ON DATABASE hindsight FROM PUBLIC;
DO $verify$
DECLARE
 db_oid oid := (SELECT oid FROM pg_database WHERE datname = 'hindsight');
BEGIN
 IF ARRAY(SELECT a::text FROM pg_database d, unnest(d.datacl) a WHERE d.oid = db_oid ORDER BY a::text COLLATE "C")
    <> ARRAY['=c/hindsight', 'hindsight=CTc/hindsight']
    OR ARRAY[has_database_privilege('hindsight', db_oid, 'CREATE'), has_database_privilege('hindsight', db_oid, 'CONNECT'),
             has_database_privilege('hindsight', db_oid, 'TEMPORARY'),
             has_database_privilege('hindsight_backlog_metrics', db_oid, 'CONNECT'),
             has_database_privilege('hindsight_backlog_metrics', db_oid, 'TEMPORARY'),
             has_database_privilege('hindsight_backlog_metrics', db_oid, 'CREATE')]
       <> ARRAY[true, true, true, true, false, false]
    OR EXISTS (SELECT 1 FROM pg_shdepend WHERE classid = 'pg_database'::regclass AND objid = db_oid AND deptype = 'a') THEN
  RAISE EXCEPTION 'database hindsight privileges after the revoke are not the expected shape';
 END IF;
END
$verify$;
COMMIT;
