#!/usr/bin/env bash
# Creates the role Groschen connects as: a login role that is not a superuser,
# cannot bypass row-level security, create roles or databases, or replicate,
# and that owns the groschen database (AD-29, NFR-13).
#
# The postgres image runs this once from /docker-entrypoint-initdb.d, as its
# superuser, on an empty data volume only. The superuser is used for nothing
# else. CI runs the same script against its Postgres service.
#
# Inputs (environment):
#   POSTGRES_USER             superuser to connect as (set by the image)
#   POSTGRES_DB               database the app role will own (set by the image)
#   GROSCHEN_DB_USER          app role name, default groschen
#   GROSCHEN_DB_PGPASS_FILE   the app's .pgpass secret, default
#                             /run/secrets/groschen_db_pgpass; its first line is
#                             host:port:database:user:password
# Connection settings (PGHOST, PGPORT) come from the usual libpq variables.
set -euo pipefail

app_user=${GROSCHEN_DB_USER:-groschen}
database=${POSTGRES_DB:?POSTGRES_DB is required}
superuser=${POSTGRES_USER:?POSTGRES_USER is required}
pgpass=${GROSCHEN_DB_PGPASS_FILE:-/run/secrets/groschen_db_pgpass}

die() {
  echo "groschen initdb: $*" >&2
  exit 1
}

[ -r "$pgpass" ] || die "cannot read $pgpass (run scripts/init-secrets.sh first)"
[ "$app_user" != "$superuser" ] || die "the app role must not be the image superuser ($superuser)"

line=$(head -n 1 "$pgpass")
IFS=: read -r _host _port line_db line_user password <<<"$line"
[ "$line_user" = "$app_user" ] || die "pgpass user is not $app_user"
[ "$line_db" = "$database" ] || [ "$line_db" = "*" ] || die "pgpass database is not $database"
# init-secrets.sh writes hex. Refuse anything that would need pgpass escaping.
[[ $password =~ ^[A-Za-z0-9]{32,}$ ]] || die "pgpass password must be 32 or more letters or digits"

# The password reaches psql through its environment (\getenv), never argv.
GROSCHEN_APP_PASSWORD=$password psql -X -q -v ON_ERROR_STOP=1 \
  --username "$superuser" --dbname "$database" \
  -v app_user="$app_user" -v database="$database" <<'SQL'
-- A failing statement must not be written to the server log with the password.
SET log_min_error_statement = panic;
\getenv app_password GROSCHEN_APP_PASSWORD
SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') AS create_role \gset
\if :create_role
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password';
\endif
ALTER ROLE :"app_user" WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD :'app_password';
-- Owning the database also gives CREATE on its public schema (PostgreSQL 15+
-- grants that to pg_database_owner), which the migrations need.
ALTER DATABASE :"database" OWNER TO :"app_user";
\unset app_password

SELECT r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication AS too_strong,
       pg_get_userbyid(d.datdba) <> r.rolname AS not_owner
  FROM pg_roles r, pg_database d
 WHERE r.rolname = :'app_user' AND d.datname = :'database' \gset
\if :too_strong
DO $$ BEGIN RAISE EXCEPTION 'groschen initdb: app role still has elevated attributes'; END $$;
\endif
\if :not_owner
DO $$ BEGIN RAISE EXCEPTION 'groschen initdb: app role does not own the database'; END $$;
\endif
SQL

echo "groschen initdb: role $app_user owns $database (NOSUPERUSER NOBYPASSRLS)"
