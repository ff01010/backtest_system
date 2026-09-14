#!/usr/bin/env sh
set -eu

psql \
  --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_db_name="$APP_DB_NAME" \
  --set=app_db_user="$APP_DB_USER" \
  --set=app_db_password="$APP_DB_PASSWORD" \
  --set=release_db_password="$RELEASE_DB_PASSWORD" <<'EOSQL'
SELECT format(
  'CREATE ROLE quant_backtest_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'quant_backtest_owner'
)
\gexec

SELECT format(
  'CREATE ROLE quant_backtest_release_admin LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'release_db_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'quant_backtest_release_admin'
)
\gexec

SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'app_db_user',
  :'app_db_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = :'app_db_user'
)
\gexec

SELECT format(
  'CREATE DATABASE %I OWNER %I ENCODING ''UTF8'' TEMPLATE template0',
  :'app_db_name',
  'quant_backtest_owner'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = :'app_db_name'
)
\gexec

GRANT CONNECT ON DATABASE :"app_db_name" TO :"app_db_user";
GRANT CONNECT ON DATABASE :"app_db_name" TO quant_backtest_release_admin;
EOSQL
