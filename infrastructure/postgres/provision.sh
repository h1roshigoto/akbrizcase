#!/bin/sh
# Explicit bootstrap only. Never mounted in API/worker/migrate containers.
set -eu
# Do not enable shell tracing or echo password variables.
MIGRATOR_PASSWORD="$(cat /run/secrets/migrator_password)"
RUNTIME_PASSWORD="$(cat /run/secrets/runtime_password)"
export MIGRATOR_PASSWORD RUNTIME_PASSWORD
[ -n "$MIGRATOR_PASSWORD" ] && [ -n "$RUNTIME_PASSWORD" ]
psql -X -q -v ON_ERROR_STOP=1 -U akbrizcase_owner -d akbrizcase <<'SQL'
-- Suppress statement logging even if the server has verbose global settings.
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
\getenv migrator_password MIGRATOR_PASSWORD
\getenv runtime_password RUNTIME_PASSWORD
SELECT 'CREATE ROLE akbrizcase_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'akbrizcase_migrator') \gexec
SELECT 'CREATE ROLE akbrizcase_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'akbrizcase_runtime') \gexec
-- Existing role names are accepted only with the expected limited authority.
DO $$ BEGIN
    IF EXISTS (
        SELECT FROM pg_roles WHERE rolname IN ('akbrizcase_migrator', 'akbrizcase_runtime')
        AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
    ) OR EXISTS (
        SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
        WHERE r.rolname IN ('akbrizcase_migrator', 'akbrizcase_runtime')
    ) OR EXISTS (
        SELECT FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
        WHERE r.rolname IN ('akbrizcase_migrator', 'akbrizcase_runtime')
    ) THEN
        RAISE EXCEPTION 'Unsafe existing database roles; operator review required';
    END IF;
END $$;
SELECT format('ALTER ROLE akbrizcase_migrator PASSWORD %L', :'migrator_password') \gexec
SELECT format('ALTER ROLE akbrizcase_runtime PASSWORD %L', :'runtime_password') \gexec
REVOKE ALL ON DATABASE akbrizcase FROM PUBLIC;
GRANT CONNECT, CREATE ON DATABASE akbrizcase TO akbrizcase_migrator;
GRANT CONNECT ON DATABASE akbrizcase TO akbrizcase_runtime;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS app AUTHORIZATION akbrizcase_migrator;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
        WHERE n.nspname = 'app' AND r.rolname = 'akbrizcase_migrator'
    ) THEN
        RAISE EXCEPTION 'Unexpected app schema owner; operator review required';
    END IF;
END $$;
GRANT USAGE ON SCHEMA app TO akbrizcase_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE akbrizcase_migrator IN SCHEMA app
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO akbrizcase_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE akbrizcase_migrator IN SCHEMA app
    GRANT USAGE, SELECT ON SEQUENCES TO akbrizcase_runtime;
ALTER ROLE akbrizcase_migrator SET timezone TO 'UTC';
ALTER ROLE akbrizcase_runtime SET timezone TO 'UTC';
SQL
unset MIGRATOR_PASSWORD RUNTIME_PASSWORD
