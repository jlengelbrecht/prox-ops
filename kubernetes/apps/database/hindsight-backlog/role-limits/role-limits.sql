-- Caps temporary-file use of hindsight_backlog_metrics at 128MB per process as a
-- role default only: temp_file_limit is superuser-context, so the role cannot
-- raise or reset it, and no SET grant on the parameter is issued. 128MB is a
-- measured spill bound, not a memory or queue-size claim. Apply over TLS as the
-- CNPG superuser with psql -X -v ON_ERROR_STOP=1 -f; every guard refuses and rolls
-- back on drift (unknown settings are never overwritten), and a rerun is a no-op.
BEGIN;
SET LOCAL search_path = pg_catalog, pg_temp;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $guard$
DECLARE
 db_oid oid := (SELECT oid FROM pg_database WHERE datname = current_database());
 owner_oid oid := (SELECT oid FROM pg_roles WHERE rolname = 'hindsight');
 reader_oid oid := (SELECT oid FROM pg_roles WHERE rolname = 'hindsight_backlog_metrics');
 reader_deps bigint := (SELECT count(*) FROM pg_shdepend
                        WHERE refclassid = 'pg_authid'::regclass AND refobjid = reader_oid);
 reader_config text[] := (SELECT setconfig FROM pg_db_role_setting WHERE setdatabase = 0 AND setrole = reader_oid);
 -- Every schema and routine privilege the reader holds here: on whose object, from
 -- which grantor, and whether it may pass the privilege on.
 reader_grants text[] := ARRAY(SELECT g FROM (
   SELECT 'schema ' || n.nspname || CASE WHEN n.nspowner = owner_oid THEN ' owner ' ELSE ' other ' END
          || a.privilege_type || CASE WHEN a.grantor = owner_oid THEN ' from owner' ELSE ' from other' END
          || CASE WHEN a.is_grantable THEN ' grantable' ELSE '' END AS g
     FROM pg_namespace n, aclexplode(n.nspacl) a WHERE a.grantee = reader_oid
   UNION ALL
   SELECT CASE WHEN p.prokind = 'f' THEN 'function ' ELSE 'routine ' END || p.oid::regprocedure::text
          || CASE WHEN p.proowner = owner_oid THEN ' owner ' ELSE ' other ' END
          || a.privilege_type || CASE WHEN a.grantor = owner_oid THEN ' from owner' ELSE ' from other' END
          || CASE WHEN a.is_grantable THEN ' grantable' ELSE '' END
     FROM pg_proc p, aclexplode(p.proacl) a WHERE a.grantee = reader_oid) t ORDER BY g COLLATE "C");
 preimage text;
BEGIN
 -- By attribute, never by name: the superuser's name is a private value.
 IF session_user <> current_user OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) IS NOT TRUE THEN
  RAISE EXCEPTION 'apply as a superuser session without SET ROLE';
 END IF;
 IF current_database() <> 'hindsight' THEN
  RAISE EXCEPTION 'run in database hindsight';
 END IF;
 IF (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) IS NOT TRUE THEN
  RAISE EXCEPTION 'this session is not using TLS';
 END IF;
 IF current_setting('server_version_num')::int / 10000 <> 16 OR pg_is_in_recovery() THEN
  RAISE EXCEPTION 'expected a PostgreSQL 16 primary';
 END IF;
 -- Without the revoke the reader could still fill disk through temporary tables,
 -- which temp_file_limit does not count.
 IF ARRAY(SELECT a::text FROM pg_database d, unnest(d.datacl) a WHERE d.oid = db_oid ORDER BY a::text COLLATE "C")
    <> ARRAY['=c/hindsight', 'hindsight=CTc/hindsight'] THEN
  RAISE EXCEPTION 'database hindsight does not carry the TEMPORARY revoke';
 END IF;
 IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE oid = reader_oid AND rolcanlogin AND NOT rolsuper AND NOT rolinherit
                AND NOT rolcreaterole AND NOT rolcreatedb AND NOT rolreplication AND NOT rolbypassrls
                AND rolconnlimit = 2 AND rolvaliduntil IS NULL)
    OR EXISTS (SELECT 1 FROM pg_auth_members WHERE reader_oid IN (member, roleid)) THEN
  RAISE EXCEPTION 'hindsight_backlog_metrics attributes or memberships drifted';
 END IF;
 -- The cap holds only while changing it needs superuser.
 IF (SELECT context || '/' || unit FROM pg_settings WHERE name = 'temp_file_limit') IS DISTINCT FROM 'superuser/kB' THEN
  RAISE EXCEPTION 'temp_file_limit is not a superuser parameter in kB';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_parameter_acl WHERE parname = 'temp_file_limit')
    OR has_parameter_privilege('hindsight_backlog_metrics', 'temp_file_limit', 'SET') THEN
  RAISE EXCEPTION 'a privilege on temp_file_limit has been granted';
 END IF;
 -- At most the cap itself, beside none or exactly the five approved session defaults
 -- in their stored text; any other value is refused rather than overwritten.
 IF ARRAY(SELECT c FROM unnest(reader_config) c WHERE split_part(c, '=', 1) = 'temp_file_limit')
      NOT IN ('{}', ARRAY['temp_file_limit=128MB'])
    OR ARRAY(SELECT c FROM unnest(reader_config) c WHERE split_part(c, '=', 1) <> 'temp_file_limit'
             ORDER BY c COLLATE "C")
      NOT IN ('{}', ARRAY['default_transaction_read_only=on', 'idle_in_transaction_session_timeout=5s',
                          'idle_session_timeout=30s', 'lock_timeout=1s', 'statement_timeout=5s']) THEN
  RAISE EXCEPTION 'hindsight_backlog_metrics carries settings this file does not accept';
 END IF;
 -- A per-database role setting outranks the role default this file writes.
 IF EXISTS (SELECT 1 FROM pg_db_role_setting WHERE setrole IN (reader_oid, owner_oid) AND setdatabase <> 0) THEN
  RAISE EXCEPTION 'a per-database setting exists for hindsight or hindsight_backlog_metrics';
 END IF;
 IF EXISTS (SELECT 1 FROM pg_db_role_setting s, unnest(s.setconfig) c
            WHERE s.setrole = 0 AND split_part(c, '=', 1) = 'temp_file_limit') THEN
  RAISE EXCEPTION 'a temp_file_limit default for every role exists';
 END IF;
 -- Evidence of configuration only: both reader rules exist, in this order, in a file
 -- that parsed cleanly. An earlier rule matching the reader would still decide its
 -- logins, so this does not show where the reader can authenticate.
 IF EXISTS (SELECT 1 FROM pg_hba_file_rules WHERE error IS NOT NULL)
    OR NOT EXISTS (SELECT 1 FROM pg_hba_file_rules a, pg_hba_file_rules b
                   WHERE a.type = 'hostssl' AND a.database = ARRAY['hindsight']
                     AND a.user_name = ARRAY['hindsight_backlog_metrics'] AND a.address = 'all'
                     AND a.auth_method = 'scram-sha-256'
                     AND b.type = 'host' AND b.database = ARRAY['all']
                     AND b.user_name = ARRAY['hindsight_backlog_metrics'] AND b.address = 'all'
                     AND b.auth_method = 'reject' AND a.rule_number < b.rule_number) THEN
  RAISE EXCEPTION 'pg_hba lacks the reader TLS rule ahead of the reader reject rule, or has an error';
 END IF;
 -- None, or exactly the two grants of the backlog function's own DDL: USAGE on its
 -- owner schema and EXECUTE on its owner zero-argument function, both from the owner
 -- without grant option. Those two are the reader's only shared dependencies, in
 -- any database, so no other object, owner or policy names it. The CASE is in
 -- parentheses because PL/pgSQL ends an IF condition at the first unnested THEN.
 IF reader_deps NOT IN (0, 2) OR reader_grants <> (CASE reader_deps WHEN 0 THEN '{}'::text[] ELSE ARRAY[
      'function hindsight_metrics.runnable_backlog() owner EXECUTE from owner',
      'schema hindsight_metrics owner USAGE from owner'] END) THEN
  RAISE EXCEPTION 'hindsight_backlog_metrics holds privileges this file does not expect';
 END IF;
 -- Temporary files are not its only way to fill disk: CREATE on any schema, whether
 -- granted to it or to PUBLIC, would let it write tables.
 IF EXISTS (SELECT 1 FROM pg_namespace WHERE has_schema_privilege(reader_oid, oid, 'CREATE')) THEN
  RAISE EXCEPTION 'hindsight_backlog_metrics can create objects in a schema';
 END IF;
 -- A live session keeps its old setting until it reconnects, and a login between
 -- this check and the commit is not seen here: reader sessions are counted again in
 -- a fresh transaction after the commit, before anything connects as the reader.
 IF EXISTS (SELECT 1 FROM pg_stat_activity WHERE usesysid = reader_oid) THEN
  RAISE EXCEPTION 'a hindsight_backlog_metrics session is connected';
 END IF;
 -- The reader's other entries in stored order, every other role setting and the
 -- parameter ACL, kept for this transaction only so the verify can prove the ALTER
 -- touched nothing else. It ends with the commit.
 preimage := set_config('hindsight_role_limits.preimage',
  coalesce((SELECT array_agg(c ORDER BY n)::text FROM unnest(reader_config) WITH ORDINALITY u(c, n)
            WHERE split_part(c, '=', 1) <> 'temp_file_limit'), '-')
  || ' ' || coalesce((SELECT array_agg(s ORDER BY s.setdatabase, s.setrole)::text FROM pg_db_role_setting s
            WHERE NOT (s.setdatabase = 0 AND s.setrole = reader_oid)), '-')
  || ' ' || coalesce((SELECT array_agg(p ORDER BY p.oid)::text FROM pg_parameter_acl p), '-'), true);
END
$guard$;
ALTER ROLE hindsight_backlog_metrics SET temp_file_limit = '128MB';
DO $verify$
DECLARE
 reader_oid oid := (SELECT oid FROM pg_roles WHERE rolname = 'hindsight_backlog_metrics');
 reader_config text[] := (SELECT setconfig FROM pg_db_role_setting WHERE setdatabase = 0 AND setrole = reader_oid);
BEGIN
 IF ARRAY(SELECT c FROM unnest(reader_config) c WHERE split_part(c, '=', 1) = 'temp_file_limit')
      <> ARRAY['temp_file_limit=128MB']
    OR EXISTS (SELECT 1 FROM pg_db_role_setting WHERE setrole = reader_oid AND setdatabase <> 0)
    OR has_parameter_privilege('hindsight_backlog_metrics', 'temp_file_limit', 'SET')
    OR current_setting('hindsight_role_limits.preimage') IS DISTINCT FROM (
  coalesce((SELECT array_agg(c ORDER BY n)::text FROM unnest(reader_config) WITH ORDINALITY u(c, n)
            WHERE split_part(c, '=', 1) <> 'temp_file_limit'), '-')
  || ' ' || coalesce((SELECT array_agg(s ORDER BY s.setdatabase, s.setrole)::text FROM pg_db_role_setting s
            WHERE NOT (s.setdatabase = 0 AND s.setrole = reader_oid)), '-')
  || ' ' || coalesce((SELECT array_agg(p ORDER BY p.oid)::text FROM pg_parameter_acl p), '-')) THEN
  RAISE EXCEPTION 'role settings or parameter privileges after the cap are not the expected shape';
 END IF;
END
$verify$;
COMMIT;
