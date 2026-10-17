#!/usr/bin/env sh
set -eu

ROOT_DIR="${ROOT_DIR:-/workspace}"
MIGRATION_DIR="$ROOT_DIR/db/migrations"
BASELINE_DIR="$ROOT_DIR/db/migration-baselines"
RUNNER_DIR="$ROOT_DIR/db/migration-runner"
FIXTURE="$ROOT_DIR/db/migration-tests/upgrade-from-056-fixture.sql"
RUNNER="$ROOT_DIR/scripts/run-database-migrations.sh"
TEST_BUILD_ID="ffffffffffffffffffffffffffffffffffffffff"
BASELINE_BUILD_ID="6483efebcb183605be69d4453bc2d11f6780e0a1"
UPGRADE_DB="quant_backtest_upgrade_test"
FRESH_DB="quant_backtest_fresh_test"
UNKNOWN_DB="quant_backtest_unknown_test"
MISSING_DB="quant_backtest_missing_structure_test"

fail() {
  echo "migration_test_error=$*" >&2
  exit 1
}

database_psql() {
  database="$1"
  shift
  PGDATABASE="$database" psql -X --no-psqlrc -v ON_ERROR_STOP=1 "$@"
}

reset_database() {
  database="$1"
  dropdb --if-exists "$database"
  createdb --owner=quant_backtest_owner "$database"
}

run_migrator() {
  database="$1"
  mode="$2"
  directory="${3:-$MIGRATION_DIR}"
  PGDATABASE="$database" \
  APP_DB_NAME="$database" \
  APP_DB_USER="$APP_DB_USER" \
  APPLICATION_BUILD_ID="$TEST_BUILD_ID" \
  RELEASE_DB_PASSWORD="$RELEASE_DB_PASSWORD" \
  MIGRATION_DIR="$directory" \
  MIGRATION_BASELINE_DIR="$BASELINE_DIR" \
  MIGRATION_RUNNER_DIR="$RUNNER_DIR" \
    sh "$RUNNER" "$mode"
}

apply_legacy_range() {
  database="$1"
  maximum="$2"
  for path in "$MIGRATION_DIR"/*.sql; do
    filename="$(basename "$path")"
    version="$(printf '%s' "${filename%%_*}" | sed 's/^0*//')"
    [ "$version" -le "$maximum" ] || continue
    if ! database_psql "$database" --set=app_db_user="$APP_DB_USER" \
      -c 'SET ROLE quant_backtest_owner' \
      -c "SELECT set_config('app.current_user_id', '00000000-0000-0000-0000-000000000001', false)" \
      -c "SELECT set_config('app.current_is_admin', 'true', false)" \
      -f "$path" >/dev/null; then
      return 1
    fi
  done
}

prepare_upgrade_database() {
  reset_database "$UPGRADE_DB"
  if ! apply_legacy_range "$UPGRADE_DB" 56 \
    >/tmp/legacy-prepare.log 2>&1; then
    tail -n 80 /tmp/legacy-prepare.log >&2
    fail "failed to prepare the production baseline database"
  fi
  database_psql "$UPGRADE_DB" -f "$FIXTURE" >/dev/null
}

assert_old_runner_reproduces_rls_failure() {
  if apply_legacy_range "$UPGRADE_DB" 59 \
    >"/tmp/legacy-replay.log" 2>&1; then
    fail "legacy full replay unexpectedly succeeded"
  fi
  grep -q "backtest_publication_status_audit" /tmp/legacy-replay.log \
    || fail "legacy replay did not fail at migration 037 audit RLS"
}

audit_facts() {
  database_psql "$UPGRADE_DB" -Atq -c "
    SET ROLE quant_backtest_owner;
    SELECT set_config('app.current_user_id',
      '00000000-0000-0000-0000-000000000001', false);
    SELECT set_config('app.current_is_admin', 'true', false);
    SELECT count(*) || ':' || min(actor_user_id::text) || ':' ||
      min(event_type) FROM backtest_publication_status_audit;
  " | tail -n 1
}

assert_upgrade_plan_and_apply() {
  before_audit="$(audit_facts)"
  plan="$(run_migrator "$UPGRADE_DB" plan)"
  printf '%s\n' "$plan" | grep -q 'baseline=eligible:' \
    || fail "upgrade plan did not select the fixed production baseline"
  [ "$(printf '%s\n' "$plan" | grep -c '^pending_migration=')" -eq 3 ] \
    || fail "upgrade plan must contain exactly migrations 057 through 059"
  printf '%s\n' "$plan" | grep -q 'pending_migration=057_' \
    || fail "migration 057 is missing from upgrade plan"
  printf '%s\n' "$plan" | grep -q 'pending_migration=058_' \
    || fail "migration 058 is missing from upgrade plan"
  printf '%s\n' "$plan" | grep -q 'pending_migration=059_' \
    || fail "migration 059 is missing from upgrade plan"
  ledger_after_plan="$(database_psql "$UPGRADE_DB" -Atq \
    -c "SELECT to_regclass('public.app_schema_migration') IS NULL")"
  [ "$ledger_after_plan" = "t" ] || fail "plan wrote the migration ledger"
  run_migrator "$UPGRADE_DB" apply >/tmp/upgrade-apply.log
  assert_upgrade_facts "$before_audit"
}

assert_upgrade_facts() {
  before_audit="$1"
  after_audit="$(audit_facts)"
  [ "$before_audit" = "$after_audit" ] \
    || fail "migration 037 audit facts changed during upgrade"
  facts="$(database_psql "$UPGRADE_DB" -Atq -c "
    SET ROLE quant_backtest_owner;
    SELECT set_config('app.current_user_id',
      '00000000-0000-0000-0000-000000000001', false);
    SELECT set_config('app.current_is_admin', 'true', false);
    SELECT
      (SELECT count(*) FROM app_schema_migration WHERE application_kind='baseline'),
      (SELECT count(*) FROM app_schema_migration WHERE application_kind='migration'),
      (SELECT count(*) FROM strategy_config_version
        WHERE config_id='40000000-0000-0000-0000-000000000001'),
      (SELECT count(*) FROM backtest_run WHERE search_text_v1 IS NOT NULL),
      (SELECT count(*) FROM backtest_publication WHERE search_text_v1 IS NOT NULL);
  " | tail -n 1)"
  [ "$facts" = "54|3|1|1|1" ] || fail "unexpected upgrade facts: $facts"
  assert_rls_isolation
  repeat_plan="$(run_migrator "$UPGRADE_DB" apply)"
  printf '%s\n' "$repeat_plan" | grep -q '^pending_count=0$' \
    || fail "repeated upgrade apply was not empty"
  [ "$(audit_facts)" = "$before_audit" ] \
    || fail "repeated apply changed audit facts"
}

assert_rls_isolation() {
  visible="$(database_psql "$UPGRADE_DB" -Atq -c "
    SET ROLE $APP_DB_USER;
    SELECT set_config('app.current_user_id',
      '10000000-0000-0000-0000-000000000002', false);
    SELECT set_config('app.current_is_admin', 'false', false);
    SELECT count(*) FROM user_strategy
      WHERE id='20000000-0000-0000-0000-000000000001';
  " | tail -n 1)"
  [ "$visible" = "0" ] || fail "ordinary user RLS isolation was weakened"
}

assert_fresh_install() {
  reset_database "$FRESH_DB"
  plan="$(run_migrator "$FRESH_DB" plan)"
  printf '%s\n' "$plan" | grep -q '^baseline=fresh-install$' \
    || fail "empty database was not identified as a fresh install"
  run_migrator "$FRESH_DB" apply >/tmp/fresh-apply.log
  expected="$(find "$MIGRATION_DIR" -maxdepth 1 -name '*.sql' | wc -l | tr -d ' ')"
  recorded="$(database_psql "$FRESH_DB" -Atq \
    -c 'SELECT count(*) FROM app_schema_migration')"
  [ "$recorded" = "$expected" ] \
    || fail "fresh install did not record every migration"
  run_migrator "$FRESH_DB" plan | grep -q '^pending_count=0$' \
    || fail "fresh install repeat plan was not empty"
}

copy_migrations() {
  destination="$1"
  rm -rf "$destination"
  mkdir -p "$destination"
  cp "$MIGRATION_DIR"/*.sql "$destination/"
}

assert_checksum_and_contract_guards() {
  copy_migrations /tmp/migrations-tampered
  echo '-- tampered' >> /tmp/migrations-tampered/059_search_projection_backfill.sql
  if run_migrator "$FRESH_DB" plan /tmp/migrations-tampered >/tmp/tampered.log 2>&1; then
    fail "changed applied migration checksum was accepted"
  fi
  grep -q 'checksum or filename changed' /tmp/tampered.log \
    || fail "checksum mismatch was not diagnosed"
  copy_migrations /tmp/migrations-duplicate
  printf 'SELECT 1;\n' > /tmp/migrations-duplicate/060_first.sql
  printf 'SELECT 1;\n' > /tmp/migrations-duplicate/060_second.sql
  if run_migrator "$FRESH_DB" plan /tmp/migrations-duplicate \
    >/tmp/duplicate.log 2>&1; then
    fail "duplicate migration version was accepted"
  fi
  grep -q 'duplicate migration version' /tmp/duplicate.log \
    || fail "duplicate migration version was not diagnosed"
  copy_migrations /tmp/migrations-gap
  printf 'SELECT 1;\n' > /tmp/migrations-gap/061_gap.sql
  if run_migrator "$FRESH_DB" plan /tmp/migrations-gap >/tmp/gap.log 2>&1; then
    fail "non-continuous new migration version was accepted"
  fi
  grep -q 'not continuous' /tmp/gap.log \
    || fail "new migration version gap was not diagnosed"
}

assert_concurrent_apply() {
  copy_migrations /tmp/migrations-concurrent
  cat > /tmp/migrations-concurrent/060_concurrency_probe.sql <<'EOSQL'
SELECT pg_sleep(2);
CREATE TABLE migration_concurrency_probe (id integer PRIMARY KEY);
EOSQL
  run_migrator "$FRESH_DB" apply /tmp/migrations-concurrent \
    >/tmp/concurrent-one.log 2>&1 &
  first_pid=$!
  run_migrator "$FRESH_DB" apply /tmp/migrations-concurrent \
    >/tmp/concurrent-two.log 2>&1 &
  second_pid=$!
  wait "$first_pid" || fail "first concurrent apply failed"
  wait "$second_pid" || fail "second concurrent apply failed"
  applied="$(database_psql "$FRESH_DB" -Atq -c \
    "SELECT count(*) FROM app_schema_migration WHERE version=60")"
  [ "$applied" = "1" ] || fail "concurrent migration was recorded more than once"
}

assert_failed_transaction_rolls_back() {
  cat > /tmp/migrations-concurrent/061_failure_probe.sql <<'EOSQL'
CREATE TABLE migration_failure_probe (id integer PRIMARY KEY);
SELECT migration_function_that_does_not_exist();
EOSQL
  if run_migrator "$FRESH_DB" apply /tmp/migrations-concurrent \
    >/tmp/failure-probe.log 2>&1; then
    fail "failing migration unexpectedly succeeded"
  fi
  facts="$(database_psql "$FRESH_DB" -Atq -c "
    SELECT to_regclass('public.migration_failure_probe') IS NULL,
      NOT EXISTS (SELECT 1 FROM app_schema_migration WHERE version=61);
  ")"
  [ "$facts" = "t|t" ] || fail "failed migration DDL or ledger row did not roll back"
}

create_deployment_stub() {
  database="$1"
  build_id="$2"
  reset_database "$database"
  database_psql "$database" -q -c "
    SET ROLE quant_backtest_owner;
    CREATE TABLE app_deployment_state (
      singleton_key boolean PRIMARY KEY,
      current_build_id char(40) NOT NULL
    );
    INSERT INTO app_deployment_state VALUES (true, '$build_id');
  "
}

assert_baseline_rejections() {
  create_deployment_stub "$UNKNOWN_DB" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  if run_migrator "$UNKNOWN_DB" plan >/tmp/unknown.log 2>&1; then
    fail "unknown production build was accepted"
  fi
  grep -q 'unknown production baseline build' /tmp/unknown.log \
    || fail "unknown build rejection was not diagnosed"
  create_deployment_stub "$MISSING_DB" "$BASELINE_BUILD_ID"
  if run_migrator "$MISSING_DB" plan >/tmp/missing.log 2>&1; then
    fail "baseline with missing structure was accepted"
  fi
  grep -q 'baseline structure' /tmp/missing.log \
    || fail "missing baseline structure was not diagnosed"
}

main() {
  prepare_upgrade_database
  assert_old_runner_reproduces_rls_failure
  prepare_upgrade_database
  assert_upgrade_plan_and_apply
  assert_fresh_install
  assert_checksum_and_contract_guards
  assert_concurrent_apply
  assert_failed_transaction_rolls_back
  assert_baseline_rejections
  echo "database_migration_tests=passed"
}

main "$@"
