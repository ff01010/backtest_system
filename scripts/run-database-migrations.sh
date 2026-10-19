#!/usr/bin/env sh
set -eu

BASELINE_BUILD_ID="6483efebcb183605be69d4453bc2d11f6780e0a1"
BASELINE_MAX_VERSION=56
LEGACY_MAX_VERSION=59
MIGRATION_LOCK_KEY=734205617
SYSTEM_USER_ID="00000000-0000-0000-0000-000000000001"

MIGRATION_DIR="${MIGRATION_DIR:-/migrations}"
MIGRATION_BASELINE_DIR="${MIGRATION_BASELINE_DIR:-/migration-baselines}"
MIGRATION_RUNNER_DIR="${MIGRATION_RUNNER_DIR:-/migration-runner}"
BASELINE_MANIFEST="${MIGRATION_BASELINE_DIR}/${BASELINE_BUILD_ID}.sha256"
LEDGER_SCHEMA="${MIGRATION_RUNNER_DIR}/001_schema_migration_ledger.sql"
APPLICATION_BUILD_ID="${APPLICATION_BUILD_ID:-}"
APP_DB_USER="${APP_DB_USER:-}"
APP_DB_NAME="${APP_DB_NAME:-${PGDATABASE:-}}"
RELEASE_DB_PASSWORD="${RELEASE_DB_PASSWORD:-}"

temp_root=""
inventory_file=""
ledger_file=""
pending_file=""
apply_file=""
lock_pid=""
lock_backend_pid=""
lock_application=""
baseline_mode=""
current_build_id=""
applied_max_version=0

fail() {
  echo "migration_error=$*" >&2
  exit 1
}

cleanup() {
  release_migration_lock
  if [ -n "$temp_root" ] && [ -d "$temp_root" ]; then
    rm -rf "$temp_root"
  fi
}

release_migration_lock() {
  case "$lock_backend_pid" in
    ''|0|*[!0-9]* ) ;;
    * ) sql_scalar "SELECT pg_terminate_backend($lock_backend_pid)" \
      >/dev/null 2>&1 || true ;;
  esac
  if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    kill "$lock_pid" 2>/dev/null || true
    wait "$lock_pid" 2>/dev/null || true
  fi
  lock_pid=""
  lock_backend_pid=""
}

require_runtime() {
  command -v psql >/dev/null 2>&1 || fail "psql is required"
  command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required"
  [ -d "$MIGRATION_DIR" ] || fail "migration directory not found: $MIGRATION_DIR"
  [ -f "$BASELINE_MANIFEST" ] || fail "baseline manifest not found: $BASELINE_MANIFEST"
  [ -f "$LEDGER_SCHEMA" ] || fail "ledger schema not found: $LEDGER_SCHEMA"
  [ -n "$APP_DB_USER" ] || fail "APP_DB_USER is required"
  [ -n "$APP_DB_NAME" ] || fail "APP_DB_NAME or PGDATABASE is required"
  printf '%s\n' "$APPLICATION_BUILD_ID" \
    | grep -Eq '^[0-9a-f]{40}$' \
    || fail "APPLICATION_BUILD_ID must be a 40-character lowercase Git SHA"
}

initialize_temp_files() {
  temp_root="$(mktemp -d)"
  inventory_file="$temp_root/inventory"
  ledger_file="$temp_root/ledger"
  pending_file="$temp_root/pending"
  apply_file="$temp_root/apply.sql"
  : > "$inventory_file"
  : > "$ledger_file"
  : > "$pending_file"
}

parse_version() {
  parsed_version="$(printf '%s' "$1" | sed 's/^0*//')"
  if [ -z "$parsed_version" ]; then
    parsed_version=0
  fi
  printf '%s\n' "$parsed_version"
}

canonical_sha256() {
  sed 's/\r$//' "$1" | sha256sum | awk '{print $1}'
}

build_inventory() {
  previous_version=-1
  find "$MIGRATION_DIR" -maxdepth 1 -type f -name '*.sql' -print \
    | LC_ALL=C sort > "$temp_root/paths"
  [ -s "$temp_root/paths" ] || fail "no migration files found"
  while IFS= read -r path; do
    filename="$(basename "$path")"
    case "$filename" in
      [0-9][0-9][0-9]_[A-Za-z0-9_-]*.sql ) ;;
      * ) fail "invalid migration filename: $filename" ;;
    esac
    version="$(parse_version "${filename%%_*}")"
    [ "$version" -gt 0 ] || fail "migration version must be positive: $filename"
    [ "$version" -ne "$previous_version" ] \
      || fail "duplicate migration version: ${filename%%_*}"
    hash="$(canonical_sha256 "$path")"
    printf '%s|%s|%s|%s\n' "$version" "$filename" "$hash" "$path" \
      >> "$inventory_file"
    previous_version="$version"
  done < "$temp_root/paths"
}

validate_baseline_manifest() {
  awk -F '|' -v max="$BASELINE_MAX_VERSION" \
    '$1 <= max { print $3 "  " $2 }' "$inventory_file" \
    > "$temp_root/current-baseline"
  sed 's/\r$//' "$BASELINE_MANIFEST" > "$temp_root/expected-baseline"
  if ! cmp -s "$temp_root/expected-baseline" \
    "$temp_root/current-baseline"; then
    fail "production baseline checksums do not match migrations through 056"
  fi
}

sql_scalar() {
  psql -X --no-psqlrc -v ON_ERROR_STOP=1 -Atq -c "$1"
}

owner_scalar() {
  psql -X --no-psqlrc -v ON_ERROR_STOP=1 -Atq <<EOSQL | tail -n 1
SET ROLE quant_backtest_owner;
SELECT set_config('app.current_user_id', '$SYSTEM_USER_ID', false);
SELECT set_config('app.current_is_admin', 'true', false);
$1
EOSQL
}

load_ledger() {
  ledger_exists="$(sql_scalar \
    "SELECT to_regclass('public.app_schema_migration') IS NOT NULL")"
  if [ "$ledger_exists" = "t" ]; then
    sql_scalar "SELECT version || '|' || filename || '|' || btrim(sha256)
      FROM app_schema_migration ORDER BY version" > "$ledger_file"
  fi
}

validate_recorded_migrations() {
  applied_max_version=0
  while IFS='|' read -r version filename recorded_hash; do
    [ -n "$version" ] || continue
    expected="$(awk -F '|' -v value="$version" '$1 == value { print $2 "|" $3 }' \
      "$inventory_file")"
    [ -n "$expected" ] || fail "applied migration file is missing: $filename"
    [ "$expected" = "$filename|$recorded_hash" ] \
      || fail "applied migration checksum or filename changed: $filename"
    applied_max_version="$version"
  done < "$ledger_file"
}

database_is_fresh() {
  has_tables="$(sql_scalar "SELECT EXISTS (
    SELECT 1 FROM pg_class relation
    JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
    WHERE namespace.nspname='public' AND relation.relkind IN ('r','p')
  )")"
  [ "$has_tables" = "f" ]
}

validate_baseline_database() {
  current_build_id="$(owner_scalar \
    "SELECT COALESCE((SELECT trim(current_build_id)
      FROM app_deployment_state WHERE singleton_key), '')")"
  [ "$current_build_id" = "$BASELINE_BUILD_ID" ] \
    || fail "unknown production baseline build: ${current_build_id:-missing}"
  structure_ready="$(owner_scalar "SELECT
    to_regclass('public.backtest_experiment_rollout_state') IS NOT NULL
    AND EXISTS (SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='backtest_experiment_revision'
        AND column_name='lifecycle_state')
    AND EXISTS (SELECT 1 FROM backtest_experiment_rollout_state WHERE singleton)")"
  [ "$structure_ready" = "t" ] \
    || fail "production baseline structure or semantic epoch record is missing"
}

prepare_untracked_database() {
  deployment_exists="$(sql_scalar \
    "SELECT to_regclass('public.app_deployment_state') IS NOT NULL")"
  if [ "$deployment_exists" = "t" ]; then
    validate_baseline_database
    baseline_mode="eligible:$BASELINE_BUILD_ID"
    applied_max_version="$BASELINE_MAX_VERSION"
  elif database_is_fresh; then
    baseline_mode="fresh-install"
    current_build_id="none"
    applied_max_version=0
  else
    fail "database has application tables but no migration ledger or deployment state"
  fi
}

build_pending_plan() {
  last_pending=0
  while IFS='|' read -r version filename hash path; do
    if grep -q "^${version}|" "$ledger_file"; then
      continue
    fi
    if [ "$ledger_exists" = "f" ] \
      && [ "$baseline_mode" = "eligible:$BASELINE_BUILD_ID" ] \
      && [ "$version" -le "$BASELINE_MAX_VERSION" ]; then
      continue
    fi
    [ "$version" -gt "$applied_max_version" ] \
      || fail "historical migration inserted at or below applied version: $filename"
    if [ "$version" -gt "$LEGACY_MAX_VERSION" ]; then
      expected_version=$((applied_max_version + 1))
      if [ "$last_pending" -gt "$applied_max_version" ]; then
        expected_version=$((last_pending + 1))
      fi
      [ "$version" -eq "$expected_version" ] \
        || fail "new migration versions are not continuous before: $filename"
    fi
    printf '%s|%s|%s|%s\n' "$version" "$filename" "$hash" "$path" \
      >> "$pending_file"
    last_pending="$version"
  done < "$inventory_file"
}

prepare_plan() {
  build_inventory
  validate_baseline_manifest
  load_ledger
  if [ "$ledger_exists" = "t" ]; then
    baseline_mode="recorded"
    deployment_exists="$(sql_scalar \
      "SELECT to_regclass('public.app_deployment_state') IS NOT NULL")"
    if [ "$deployment_exists" = "t" ]; then
      current_build_id="$(sql_scalar "SELECT COALESCE((SELECT
        trim(current_build_id) FROM app_deployment_state WHERE singleton_key), '')")"
    else
      current_build_id="none"
    fi
    validate_recorded_migrations
  else
    prepare_untracked_database
  fi
  build_pending_plan
}

print_plan() {
  pending_count="$(wc -l < "$pending_file" | tr -d ' ')"
  echo "migration_ledger=$([ "$ledger_exists" = "t" ] && echo enabled || echo absent)"
  echo "baseline=$baseline_mode"
  echo "current_build_id=${current_build_id:-none}"
  echo "target_build_id=$APPLICATION_BUILD_ID"
  echo "applied_max_version=$applied_max_version"
  echo "checksum_status=ok"
  echo "pending_count=$pending_count"
  while IFS='|' read -r version filename hash path; do
    [ -n "$version" ] && echo "pending_migration=$filename"
  done < "$pending_file"
  echo "migration_status=ready"
}

acquire_migration_lock() {
  lock_application="database-migration-lock-$$"
  PGAPPNAME="$lock_application" psql \
    -X --no-psqlrc -v ON_ERROR_STOP=1 -Atq \
    -c "SELECT pg_advisory_lock($MIGRATION_LOCK_KEY);
      SELECT pg_sleep(2147483647);" \
    >"$temp_root/lock.out" 2>"$temp_root/lock.err" &
  lock_pid=$!
  while :; do
    if ! kill -0 "$lock_pid" 2>/dev/null; then
      cat "$temp_root/lock.err" >&2
      fail "database migration lock session exited unexpectedly"
    fi
    lock_backend_pid="$(sql_scalar "SELECT COALESCE((
      SELECT activity.pid FROM pg_stat_activity activity
      JOIN pg_locks held_lock ON held_lock.pid=activity.pid
      WHERE activity.application_name='$lock_application'
        AND held_lock.locktype='advisory' AND held_lock.granted
      LIMIT 1
    ), 0)")"
    [ "$lock_backend_pid" -gt 0 ] && return
    sleep 0.1
  done
}

refresh_plan_after_lock() {
  : > "$inventory_file"
  : > "$ledger_file"
  : > "$pending_file"
  baseline_mode=""
  current_build_id=""
  applied_max_version=0
  prepare_plan
}

ensure_release_role() {
  psql -X --no-psqlrc -v ON_ERROR_STOP=1 \
    --set=app_db_name="$APP_DB_NAME" \
    --set=release_db_password="$RELEASE_DB_PASSWORD" <<'EOSQL'
SELECT format(
  'CREATE ROLE quant_backtest_release_admin LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT',
  :'release_db_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname='quant_backtest_release_admin'
)
\gexec
SELECT format(
  'ALTER ROLE quant_backtest_release_admin PASSWORD %L', :'release_db_password'
)
\gexec
GRANT CONNECT ON DATABASE :"app_db_name" TO quant_backtest_release_admin;
EOSQL
}

write_baseline_registration() {
  cat > "$apply_file" <<EOSQL
SET ROLE quant_backtest_owner;
SELECT set_config('app.current_user_id', '$SYSTEM_USER_ID', false);
SELECT set_config('app.current_is_admin', 'true', false);
DO \$baseline\$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM app_deployment_state
    WHERE singleton_key AND trim(current_build_id)='$BASELINE_BUILD_ID'
  ) THEN
    RAISE EXCEPTION 'unknown production baseline build';
  END IF;
  IF to_regclass('public.backtest_experiment_rollout_state') IS NULL
    OR NOT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name='backtest_experiment_revision'
        AND column_name='lifecycle_state'
    ) OR NOT EXISTS (
      SELECT 1 FROM backtest_experiment_rollout_state WHERE singleton
    ) THEN
    RAISE EXCEPTION 'production baseline structure or semantic epoch is missing';
  END IF;
END
\$baseline\$;
EOSQL
  while IFS= read -r manifest_line; do
    hash="${manifest_line%%  *}"
    filename="${manifest_line#*  }"
    version="$(parse_version "${filename%%_*}")"
    printf "INSERT INTO app_schema_migration (version, filename, sha256, applied_build_id, application_kind) VALUES (%s, '%s', '%s', '%s', 'baseline');\n" \
      "$version" "$filename" "$hash" "$BASELINE_BUILD_ID" >> "$apply_file"
  done < "$BASELINE_MANIFEST"
}

bootstrap_migration_ledger() {
  if [ "$ledger_exists" != "f" ]; then
    return 0
  fi
  psql -X --no-psqlrc -v ON_ERROR_STOP=1 --file="$LEDGER_SCHEMA"
  if [ "$baseline_mode" = "eligible:$BASELINE_BUILD_ID" ]; then
    write_baseline_registration
    psql -X --no-psqlrc -v ON_ERROR_STOP=1 \
      --single-transaction --file="$apply_file"
  fi
}

apply_legacy_migration() {
  version="$1"
  filename="$2"
  hash="$3"
  path="$4"
  echo "applying_migration=$filename"
  psql -X --no-psqlrc -v ON_ERROR_STOP=1 \
    --set=app_db_user="$APP_DB_USER" \
    -c 'SET ROLE quant_backtest_owner' \
    -c "SELECT set_config('app.current_user_id', '$SYSTEM_USER_ID', false)" \
    -c "SELECT set_config('app.current_is_admin', 'true', false)" \
    -f "$path" \
    -c "INSERT INTO app_schema_migration (
      version, filename, sha256, applied_build_id, application_kind
    ) VALUES (
      $version, '$filename', '$hash', '$APPLICATION_BUILD_ID', 'migration'
    )"
}

apply_transactional_migration() {
  version="$1"
  filename="$2"
  hash="$3"
  path="$4"
  wrapper="$temp_root/migration-$version.sql"
  cat > "$wrapper" <<EOSQL
SET ROLE quant_backtest_owner;
SELECT set_config('app.current_user_id', '$SYSTEM_USER_ID', false);
SELECT set_config('app.current_is_admin', 'true', false);
DO \$guard\$
BEGIN
  IF EXISTS (SELECT 1 FROM app_schema_migration WHERE version >= $version) THEN
    RAISE EXCEPTION 'historical migration insertion rejected: $filename';
  END IF;
END
\$guard\$;
\i '$path'
INSERT INTO app_schema_migration (
  version, filename, sha256, applied_build_id, application_kind
) VALUES (
  $version, '$filename', '$hash', '$APPLICATION_BUILD_ID', 'migration'
);
EOSQL
  echo "applying_migration=$filename"
  psql -X --no-psqlrc -v ON_ERROR_STOP=1 \
    --set=app_db_user="$APP_DB_USER" \
    --single-transaction --file="$wrapper"
}

apply_pending_migrations() {
  while IFS='|' read -r version filename hash path; do
    [ -n "$version" ] || continue
    if [ "$version" -le "$LEGACY_MAX_VERSION" ]; then
      apply_legacy_migration "$version" "$filename" "$hash" "$path"
    else
      apply_transactional_migration "$version" "$filename" "$hash" "$path"
    fi
  done < "$pending_file"
}

apply_migrations() {
  [ -n "$RELEASE_DB_PASSWORD" ] || fail "RELEASE_DB_PASSWORD is required for apply"
  acquire_migration_lock
  refresh_plan_after_lock
  ensure_release_role
  bootstrap_migration_ledger
  apply_pending_migrations
  release_migration_lock
  echo "migration_apply=complete"
}

main() {
  mode="${1:-}"
  [ "$#" -eq 1 ] || fail "usage: run-database-migrations.sh plan|apply"
  case "$mode" in
    plan|apply ) ;;
    * ) fail "usage: run-database-migrations.sh plan|apply" ;;
  esac
  require_runtime
  initialize_temp_files
  trap cleanup EXIT HUP INT TERM
  prepare_plan
  print_plan
  if [ "$mode" = "apply" ]; then
    apply_migrations
  fi
}

main "$@"
