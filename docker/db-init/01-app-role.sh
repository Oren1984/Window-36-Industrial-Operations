#!/bin/sh
# Runs once, on a fresh database volume. Creates the least-privilege runtime role.
# Table privileges (DML only, no DDL) are granted by `window36.cli migrate-and-seed`.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v app_pw="$W36_APP_DB_PASSWORD" <<'EOSQL'
CREATE ROLE w36_app LOGIN PASSWORD :'app_pw';
GRANT CONNECT ON DATABASE window36 TO w36_app;
GRANT USAGE ON SCHEMA public TO w36_app;
EOSQL
