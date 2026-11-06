#!/bin/sh
# Only the fresh, independently passworded synthetic restore instance uses this.
set -eu
MIGRATOR_PASSWORD="$(cat /run/secrets/restore_migrator_password)"
RUNTIME_PASSWORD="$(cat /run/secrets/restore_runtime_password)"
export MIGRATOR_PASSWORD RUNTIME_PASSWORD
psql -X -q -v ON_ERROR_STOP=1 -U akbrizcase_owner -d akbrizcase <<'SQL'
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
\getenv migrator_password MIGRATOR_PASSWORD
\getenv runtime_password RUNTIME_PASSWORD
CREATE ROLE akbrizcase_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE akbrizcase_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
SELECT format('ALTER ROLE akbrizcase_migrator PASSWORD %L', :'migrator_password') \gexec
SELECT format('ALTER ROLE akbrizcase_runtime PASSWORD %L', :'runtime_password') \gexec
REVOKE ALL ON DATABASE akbrizcase FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE akbrizcase TO akbrizcase_migrator;
GRANT CONNECT ON DATABASE akbrizcase TO akbrizcase_runtime;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
COMMENT ON DATABASE akbrizcase IS 'akbrizcase:synthetic-seed:test';
CREATE EXTENSION pg_trgm WITH SCHEMA public;
-- No app schema, no migrations, no existing data. pg_restore creates all of it.
SQL
unset MIGRATOR_PASSWORD RUNTIME_PASSWORD
