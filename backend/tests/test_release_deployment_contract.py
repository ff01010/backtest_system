# -*- coding: utf-8 -*-
"""发布脚本与 Compose 的关键不可变镜像契约。"""

import hashlib
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_ROOT = ROOT / "db" / "migrations"
MIGRATION_RUNNER = ROOT / "scripts" / "run-database-migrations.sh"
LOCAL_RELEASE_CHECK = ROOT / "scripts" / "release-check.ps1"
MIGRATION_LEDGER_SCHEMA = (
  ROOT / "db" / "migration-runner" / "001_schema_migration_ledger.sql"
)
PRODUCTION_BASELINE_BUILD = "6483efebcb183605be69d4453bc2d11f6780e0a1"
PRODUCTION_BASELINE = (
  ROOT / "db" / "migration-baselines" / f"{PRODUCTION_BASELINE_BUILD}.sha256"
)
LEGACY_MIGRATION_MAX_VERSION = 59
LEGACY_NON_TRANSACTIONAL_MIGRATIONS = frozenset(
  {
    "009_normalize_trading_date_json.sql",
    "018_research_signal_snapshot_artifacts.sql",
    "019_research_signal_result_outbox.sql",
    "020_research_authorization_codes.sql",
    "038_market_instrument_legacy_secid.sql",
  },
)
ALLOWED_MIGRATION_PERMISSION_ROLES = frozenset(
  {
    ':"app_db_user"',
    "PUBLIC",
    "quant_backtest_release_admin",
  },
)
MIGRATION_PERMISSION_ROLE_PATTERN = re.compile(
  r"\b(?:GRANT|REVOKE)\b[^;]*?\b(?:TO|FROM)\s+([^\s;]+)\s*;",
  re.IGNORECASE | re.DOTALL,
)


def test_migrations_only_grant_or_revoke_declared_roles() -> None:
  unexpected_roles: list[str] = []

  for migration in sorted(MIGRATION_ROOT.glob("*.sql")):
    sql = migration.read_text(encoding="utf-8")
    for match in MIGRATION_PERMISSION_ROLE_PATTERN.finditer(sql):
      role = match.group(1)
      if role not in ALLOWED_MIGRATION_PERMISSION_ROLES:
        unexpected_roles.append(f"{migration.name}: {role}")

  assert unexpected_roles == []


def test_migration_files_follow_versioned_transaction_contract() -> None:
  invalid_migrations: list[str] = []

  for migration in sorted(MIGRATION_ROOT.glob("*.sql")):
    sql = migration.read_text(encoding="utf-8")
    version = int(migration.name.split("_", 1)[0])
    has_begin = re.search(r"(?m)^BEGIN;\s*$", sql) is not None
    has_commit = re.search(r"(?m)^COMMIT;\s*$", sql) is not None
    if version <= LEGACY_MIGRATION_MAX_VERSION:
      if migration.name not in LEGACY_NON_TRANSACTIONAL_MIGRATIONS and not (
        has_begin and has_commit
      ):
        invalid_migrations.append(migration.name)
    elif has_begin or has_commit:
      invalid_migrations.append(migration.name)

  assert invalid_migrations == []


@pytest.mark.parametrize(
  "compose_path",
  (
    ROOT / "deploy" / "compose.infrastructure.yml",
    ROOT / "deploy" / "compose.test.yml",
  ),
)
def test_compose_services_use_the_shared_migration_runner(compose_path: Path) -> None:
  compose = compose_path.read_text(encoding="utf-8")
  assert "for file in /migrations/*.sql" not in compose
  assert "run-database-migrations.sh" in compose


def test_migration_runner_exposes_plan_and_apply_with_admin_rls_context() -> None:
  runner = MIGRATION_RUNNER.read_text(encoding="utf-8")
  role = runner.index("SET ROLE quant_backtest_owner")
  user_context = runner.index("app.current_user_id", role)
  admin_context = runner.index("app.current_is_admin", user_context)

  assert "plan|apply" in runner
  assert 'if [ "$mode" = "apply" ]' in runner
  assert "pg_advisory_lock" in runner
  assert "--single-transaction" in runner
  assert "sha256sum" in runner
  assert role < user_context < admin_context


def test_production_baseline_covers_every_existing_migration_through_056() -> None:
  entries = [
    line.split()[1]
    for line in PRODUCTION_BASELINE.read_text(encoding="utf-8").splitlines()
    if line.strip()
  ]
  expected = [
    migration.name
    for migration in sorted(MIGRATION_ROOT.glob("*.sql"))
    if int(migration.name.split("_", 1)[0]) <= 56
  ]

  assert entries == expected
  assert all(
    re.fullmatch(r"[0-9a-f]{64}\s{2}\d{3}_[A-Za-z0-9_-]+\.sql", line)
    for line in PRODUCTION_BASELINE.read_text(encoding="utf-8").splitlines()
    if line.strip()
  )


def test_production_baseline_checksums_are_line_ending_independent() -> None:
  recorded = {
    filename: checksum
    for checksum, filename in (
      line.split()
      for line in PRODUCTION_BASELINE.read_text(encoding="utf-8").splitlines()
      if line.strip()
    )
  }
  expected = {
    migration.name: hashlib.sha256(
      migration.read_bytes().replace(b"\r\n", b"\n"),
    ).hexdigest()
    for migration in sorted(MIGRATION_ROOT.glob("*.sql"))
    if int(migration.name.split("_", 1)[0]) <= 56
  }

  assert recorded == expected
  runner = MIGRATION_RUNNER.read_text(encoding="utf-8")
  assert 'sed \'s/\\r$//\' "$BASELINE_MANIFEST"' in runner


def test_migration_ledger_is_owner_only_and_records_application_kind() -> None:
  schema = MIGRATION_LEDGER_SCHEMA.read_text(encoding="utf-8")

  assert "CREATE TABLE IF NOT EXISTS app_schema_migration" in schema
  assert "baseline" in schema
  assert "migration" in schema
  assert "ALTER TABLE app_schema_migration OWNER TO quant_backtest_owner" in schema
  assert "REVOKE ALL ON app_schema_migration FROM PUBLIC" in schema


def test_backend_gate_runs_upgrade_regression_before_fresh_install() -> None:
  script = (ROOT / "scripts" / "test-backend.ps1").read_text(encoding="utf-8")
  compose = (ROOT / "deploy" / "compose.test.yml").read_text(encoding="utf-8")

  assert script.index("migration-upgrade-test") < script.index("backend-tests")
  assert "test-database-migrations.sh" in compose


def test_personal_release_check_wraps_the_complete_local_gate() -> None:
  script = LOCAL_RELEASE_CHECK.read_text(encoding="utf-8")
  attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")

  assert '"develop"' in script
  assert "git status --porcelain" in script
  assert "git fetch origin main develop" in script
  assert "origin/main..HEAD" in script
  assert "test-backend.ps1" in script
  assert "git rev-parse HEAD" in script
  assert "*.sh text eol=lf" in attributes
  assert "backend/requirements.lock text eol=lf" in attributes


def test_server_release_has_read_only_status_and_confirmed_preflight() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  status_function = script.split("deployment_status() {", 1)[1].split("\n}", 1)[0]
  prebuild = script.index("\nbuild_release_images\n")
  confirmation = script.index("\nconfirm_release_target\n")
  gate_close = script.index("\nclose_submission_gate\n")

  assert "status)" in script
  assert "release)" in script
  assert "git fetch" not in status_function
  assert "git pull" not in status_function
  assert "submission_gate=" in status_function
  assert "bar_runner_count=" in status_function
  assert "l2_runner_count=" in status_function
  assert prebuild < confirmation < gate_close
  assert "输入目标提交前 8 位" in script


def test_release_preserves_old_images_and_only_auto_recovers_before_migration() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  cleanup_trap = script.index("\ntrap deployment_exit_cleanup EXIT\n")
  prebuild = script.index("\nbuild_release_images\n")
  gate_close = script.index("\nclose_submission_gate\n")
  migration = script.index('"${compose[@]}" --profile migration run --rm migrate apply')

  assert "preserve_current_service_images" in script
  assert "quant-backtest-recovery/" in script
  assert "restore_pre_migration_release" in script
  assert "migration_started=false" in script
  assert "cutover_started=true" in script
  assert cleanup_trap < prebuild < gate_close < migration
  assert 'if [[ "$cutover_started" == "true"' in script
  assert '&& "$migration_started" == "false"' in script
  assert 'if [[ "$cutover_started" == "true" ]]; then\n    report_incomplete_release' in script
  assert 'recovery_services+=("$service")' in script
  assert '"${recovery_services[@]}" || return 1' in script
  assert "up -d --no-build --remove-orphans" in script


def test_agent_rules_name_one_click_release_maintenance_triggers() -> None:
  rules = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

  assert "## 一键发布脚本维护" in rules
  assert "scripts/release-check.ps1" in rules
  assert "scripts/deploy-server.sh" in rules
  for trigger in (
    "服务",
    "Dockerfile",
    "数据库迁移",
    "提交门禁",
    "健康检查",
    "环境变量",
    "构建标识",
  ):
    assert trigger in rules


def test_public_api_submission_quota_only_limits_inflight_work() -> None:
  settings = (ROOT / "backend" / "settings.py").read_text(encoding="utf-8")
  env_example = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")

  assert '"TREND_GRID_PUBLIC_API_MAX_INFLIGHT", 100, 1, 100' in settings
  assert "TREND_GRID_PUBLIC_API_MAX_INFLIGHT=100" in env_example
  assert "PUBLIC_API_DAILY_SUBMIT_LIMIT" not in settings
  assert "PUBLIC_API_DAILY_SUBMIT_LIMIT" not in env_example


def test_scheduler_capacity_constraint_matches_configurable_maximum() -> None:
  settings = (ROOT / "backend" / "settings.py").read_text(encoding="utf-8")
  migration = (
    MIGRATION_ROOT / "055_backtest_scheduler_capacity_limit.sql"
  ).read_text(encoding="utf-8")

  assert "BACKTEST_CONFIGURABLE_MAX_CONCURRENCY = 64" in settings
  assert "DROP CONSTRAINT IF EXISTS" in migration
  assert "backtest_scheduler_capacity_override_limit_check" in migration
  assert "CHECK (override_limit BETWEEN 8 AND 64)" in migration


def test_release_admin_uses_target_sha_and_preserves_recovery_image() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )

  assert "quant-backtest-release-admin:1" not in script
  assert "quant-backtest-release-admin:1" not in compose
  assert 'RELEASE_ADMIN_IMAGE="quant-backtest-release-admin:${APPLICATION_BUILD_ID}"' in script
  assert "build release-admin" in script
  assert "verify_release_admin_image" in script
  assert "quant-backtest-release-admin:recovery-v1" in script
  assert "${RELEASE_ADMIN_IMAGE" in compose


def test_deployment_has_no_trade_calendar_runtime_dependency() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )

  assert "calendar-sync" not in script
  assert "verify_trade_calendar" not in script
  assert "calendar-sync" not in compose
  assert "backend.trade_calendar_sync" not in compose


def test_deploy_script_reexecutes_after_self_update_under_same_lock() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")

  assert "QUANT_DEPLOY_LOCK_HELD" in script
  assert "QUANT_DEPLOY_REEXECUTED" in script
  assert "script_digest_before" in script
  assert "script_digest_after" in script


def test_nginx_preserves_external_host_port_for_same_origin_csrf() -> None:
  nginx = (ROOT / "deploy" / "nginx.conf").read_text(encoding="utf-8")

  assert "proxy_set_header Host $host;" not in nginx
  assert nginx.count("proxy_set_header Host $http_host;") == nginx.count(
    "proxy_pass http://quant-backtest-backend:8000",
  )


def test_deploy_script_rejects_incomplete_market_directory() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")

  assert "verify_market_instrument_directory" in script
  assert "没有活跃北交所股票" in script
  assert "没有 920 开头的活跃北交所股票" in script
  assert script.index("\nverify_market_instrument_directory\n") < script.index(
    '\ngeneration="$(docker exec quant-backtest-postgres',
  )


def test_signal_export_runner_is_deployed_and_health_checked_with_backend() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )

  assert "signal-export-runner:" in compose
  assert "python', '-m', 'backend.signal_export_runner" in compose
  assert "quant-backtest-artifacts:/runtime/artifacts" in compose
  assert "backend.signal_export_runner', '--healthcheck" in compose
  assert (
    "backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker experiment-purge-worker signal-export-runner"
    in script
  )
  assert "strategy-worker" not in script
  assert "strategy-worker:" not in compose
  assert "Dockerfile.worker" not in compose
  assert 'runner_containers_ready "signal-export-runner" "1"' in script
  assert '"signalExportRunner":{"healthy":true}' in script


def test_experiment_coordinator_is_deployed_and_health_checked_with_backend() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )

  assert "experiment-coordinator:" in compose
  assert "python', '-m', 'backend.experiment_coordinator" in compose
  assert 'runner_containers_ready "experiment-coordinator" "1"' in script
  assert (
    "backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker experiment-purge-worker signal-export-runner"
    in script
  )


def test_evaluation_worker_is_deployed_and_health_checked_with_backend() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )

  assert "evaluation-worker:" in compose
  assert "python', '-m', 'backend.evaluation_worker" in compose
  assert "backend.evaluation_worker', '--healthcheck" in compose
  assert 'runner_containers_ready "evaluation-worker" "1"' in script


def test_evaluation_export_worker_is_deployed_and_health_checked_with_backend() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )

  assert "evaluation-export-worker:" in compose
  assert "python', '-m', 'backend.evaluation_export_worker" in compose
  assert "backend.evaluation_export_worker', '--healthcheck" in compose
  assert "quant-backtest-artifacts:/runtime/artifacts" in compose
  assert 'runner_containers_ready "evaluation-export-worker" "1"' in script
  assert '"evaluationExportWorker":{"healthy":true}' in script


def test_local_start_waits_for_all_experiment_workers() -> None:
  script = (ROOT / "scripts" / "start-local.ps1").read_text(encoding="utf-8")

  assert "$requiredWorkerServices = @(" in script
  for service in (
    "experiment-coordinator",
    "evaluation-worker",
    "evaluation-export-worker",
    "experiment-purge-worker",
    "signal-export-runner",
  ):
    assert f"'{service}'" in script
  assert "$upArguments += $requiredWorkerServices" in script
  assert "Test-ServiceContainersReady -Service $_ -ExpectedCount 1" in script
  assert "$response.evaluationExportWorker.healthy -eq $true" in script


def test_retired_strategy_worker_is_absent_from_runtime_and_deployment() -> None:
  paths = (
    ROOT / "backend" / "worker_app.py",
    ROOT / "backend" / "services" / "strategy_worker.py",
    ROOT / "deploy" / "Dockerfile.worker",
  )
  deployment_text = "\n".join(
    path.read_text(encoding="utf-8")
    for path in (
      ROOT / "deploy" / "compose.infrastructure.yml",
      ROOT / "deploy" / "compose.clickhouse.yml",
      ROOT / "scripts" / "deploy-server.sh",
      ROOT / "scripts" / "start-local.ps1",
    )
  )

  assert all(not path.exists() for path in paths)
  assert "strategy-worker" not in deployment_text
  assert "quant-backtest-worker-net" not in deployment_text


def test_signal_export_runner_health_is_readable_but_only_admin_writable() -> None:
  migration = (
    ROOT / "db" / "migrations" / "040_backtest_signal_detail_exports.sql"
  ).read_text(encoding="utf-8")

  assert "signal_detail_export_runner_read" in migration
  assert "FOR SELECT\n  USING (true)" in migration
  assert "signal_detail_export_runner_admin_write" in migration
  assert "FOR ALL\n  USING (app.current_is_admin())" in migration


def test_observability_services_are_pinned_isolated_and_started_before_application() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )
  socket_proxy = compose.split("  docker-socket-proxy:", 1)[1].split(
    "\n  loki:", 1,
  )[0]

  assert "grafana/loki:3.7.6@sha256:" in compose
  assert "grafana/alloy:v1.19.2@sha256:" in compose
  assert "docker-socket-proxy:v0.4.2@sha256:" in compose
  assert "pids_limit: 128" in socket_proxy
  assert "quant-backtest-observability-net" in compose
  assert "internal: true" in compose
  assert "driver: local" in compose
  assert "max-size: ${TREND_GRID_LOG_LOCAL_MAX_SIZE:-20m}" in compose
  assert "POST: '0'" in compose
  assert "docker-socket-proxy/haproxy.cfg.template" in compose
  assert "com.trend-grid.logging-network" in compose
  assert "__meta_docker_container_label_com_trend_grid_logging_network" in (
    ROOT / "deploy" / "alloy" / "config.alloy"
  ).read_text(encoding="utf-8")
  assert compose.count("backend.runner_supervisor") == 2
  assert "- l2\n      - --processes\n      - '1'" in compose
  assert "- bar\n      - --processes\n      - '1'" in compose
  assert script.index("up -d --build docker-socket-proxy loki alloy") < script.index(
    "\nclose_submission_gate\n",
  )
  assert script.index("run --rm migrate apply") < script.index(
    "up -d --no-build container-event-observer",
  )
  assert script.index(
    "backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker experiment-purge-worker signal-export-runner",
  ) < (
    script.index("if ! wait_for_observability; then")
  )


def test_observability_failure_cannot_stop_core_release_after_cutover() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")

  preflight = script.index("up -d --build docker-socket-proxy loki alloy")
  gate_close = script.index("\nclose_submission_gate\n")
  core_stop = script.index("\nstop_submission_services\n")
  assert preflight < gate_close < core_stop

  migration = script.index("run --rm migrate apply")
  observer = script.index("up -d --no-build container-event-observer")
  core_start = script.index(
    "backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker experiment-purge-worker signal-export-runner",
  )

  assert core_stop < migration < observer < core_start
  assert 'if "${compose[@]}" up -d --build docker-socket-proxy loki alloy' in script
  assert 'if "${compose[@]}" up -d --no-build container-event-observer' in script
  assert "fail " not in script[preflight:gate_close]
  assert "fail " not in script[observer:core_start]
  gate_open = script.index("\nopen_submission_gate\n")
  core_health = script.index('wait_for_application "$http_port"')
  assert core_start < core_health < gate_open
  assert gate_open < script.index("if ! wait_for_observability; then")
  assert gate_open < script.index("核心回测服务已完成发布")
  observability_check = script.split("wait_for_observability() {", 1)[1].split(
    "\n}\n", 1,
  )[0]
  assert "return 1" in observability_check
  assert "fail " not in observability_check
  assert "可观测性未通过" in script


def test_deploy_holds_database_claim_barrier_until_new_runners_are_healthy() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  state_store = (ROOT / "backend" / "services" / "state_store.py").read_text(
    encoding="utf-8",
  )

  assert "741_852_963" in state_store
  assert "backtest_barrier_lock_key=741852963" in script
  assert "pg_try_advisory_xact_lock_shared" in state_store
  acquire = script.index("\n  acquire_deployment_barrier\n")
  wait_active = script.index("\n  wait_for_active_backtests\n")
  migration = script.index('"${compose[@]}" --profile migration run --rm migrate apply')
  health = script.index('wait_for_application "$http_port"', migration)
  final_verify = script.index(
    'verify_deployment_barrier || fail "应用健康检查完成前 PostgreSQL 回测领取屏障已丢失"',
  )
  release = script.index("\nrelease_deployment_barrier\n", final_verify)
  gate_open = script.index("\nopen_submission_gate\n")

  assert acquire < wait_active < migration < health < final_verify < release < gate_open
  assert "pause_legacy_runners_for_bootstrap" in script
  assert 'docker rm --force "$container_id"' in script
  assert "resume_bootstrap_runners" in script
  assert "pg_terminate_backend(${barrier_backend_pid})" in script
  assert "PGAPPNAME='${barrier_session_name}'" in script
  assert "SELECT pid FROM pg_stat_activity WHERE application_name=" in script


def test_deploy_plans_migrations_before_closing_submission_gate() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")

  postgres = script.index('"${compose[@]}" up -d postgres')
  plan = script.index('"${compose[@]}" --profile migration run --rm migrate plan')
  compose_validation = script.index('"${compose[@]}" config >/dev/null')
  gate_close = script.index("\nclose_submission_gate\n")
  apply = script.index('"${compose[@]}" --profile migration run --rm migrate apply')

  assert compose_validation < postgres < plan < gate_close < apply


def test_bar_runner_compose_uses_one_shm_capacity_configuration() -> None:
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(encoding="utf-8")
  env_example = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
  runner = compose.split("  bar-runner:", 1)[1].split("\n  experiment-coordinator:", 1)[0]

  expected = "${TREND_GRID_BAR_RUNNER_SHM_SIZE_BYTES:-134217728}"
  assert f"TREND_GRID_BAR_RUNNER_SHM_SIZE_BYTES: {expected}" in runner
  assert f"shm_size: {expected}" in runner
  assert "TREND_GRID_BAR_IPC_MODE=legacy" in env_example
  assert "TREND_GRID_BAR_RUNNER_SHM_SIZE_BYTES=134217728" in env_example
  assert "append_bar_ipc_env_defaults" in (
    ROOT / "scripts" / "deploy-server.sh"
  ).read_text(encoding="utf-8")


def test_socket_proxy_uses_exact_read_only_endpoint_allowlist() -> None:
  proxy = (
    ROOT / "deploy" / "docker-socket-proxy" / "haproxy.cfg.template"
  ).read_text(encoding="utf-8")

  assert "http-request deny unless METH_GET" in proxy
  assert "/containers/json/?$" in proxy
  assert "/containers/[a-zA-Z0-9_.-]+/logs/?$" in proxy
  assert "/events/?$" in proxy
  assert "^(/v[0-9.]+)?/containers }" not in proxy
  assert "/archive" not in proxy
  assert "/export" not in proxy


def test_socket_proxy_reserves_pid_capacity_for_healthcheck() -> None:
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )
  proxy = (
    ROOT / "deploy" / "docker-socket-proxy" / "haproxy.cfg.template"
  ).read_text(encoding="utf-8")
  service = compose.split("  docker-socket-proxy:", 1)[1].split("\n  loki:", 1)[0]
  thread_match = re.search(r"^\s*nbthread\s+(\d+)\s*$", proxy, re.MULTILINE)
  pid_limit_match = re.search(r"^\s*pids_limit:\s*(\d+)\s*$", service, re.MULTILINE)

  assert thread_match is not None
  assert pid_limit_match is not None
  thread_count = int(thread_match.group(1))
  pid_limit = int(pid_limit_match.group(1))
  assert thread_count <= 4
  assert pid_limit >= thread_count + 4


def test_alloy_log_stream_avoids_refresh_interval_http_timeout() -> None:
  config = (ROOT / "deploy" / "alloy" / "config.alloy").read_text(
    encoding="utf-8",
  )
  discovery = config.split('discovery.docker "local" {', 1)[1].split(
    "\n}", 1,
  )[0]
  source = config.split('loki.source.docker "containers" {', 1)[1].split(
    "\n}", 1,
  )[0]

  assert '"http://docker-socket-proxy:2375"' in discovery
  assert '"tcp://docker-socket-proxy:2375"' in source
  assert "refresh_interval" not in source


def test_socket_proxy_expires_abandoned_half_closed_log_streams() -> None:
  proxy = (
    ROOT / "deploy" / "docker-socket-proxy" / "haproxy.cfg.template"
  ).read_text(encoding="utf-8")
  frontend = proxy.split("frontend docker", 1)[1].split(
    "backend dockerapi", 1,
  )[0]
  stream_backend = proxy.split("backend dockerstream", 1)[1]

  assert "timeout client-fin 5s" in frontend
  assert "option abortonclose" in stream_backend
  assert "timeout server 0" in stream_backend
  assert "timeout server-fin 5s" in stream_backend


def test_observability_config_changes_recreate_only_their_consumers() -> None:
  compose = (ROOT / "deploy" / "compose.infrastructure.yml").read_text(
    encoding="utf-8",
  )
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  socket_proxy = compose.split("  docker-socket-proxy:", 1)[1].split(
    "\n  loki:", 1,
  )[0]
  alloy = compose.split("  alloy:", 1)[1].split(
    "\n  container-event-observer:", 1,
  )[0]

  assert "export_observability_config_digests()" in script
  assert script.count("export_observability_config_digests") >= 3
  assert "deploy/docker-socket-proxy/haproxy.cfg.template" in script
  assert "deploy/alloy/config.alloy" in script
  assert (
    "com.trend-grid.config-digest: "
    "${TREND_GRID_DOCKER_SOCKET_PROXY_CONFIG_DIGEST:-dev}"
  ) in socket_proxy
  assert (
    "com.trend-grid.config-digest: ${TREND_GRID_ALLOY_CONFIG_DIGEST:-dev}"
  ) in alloy


SOCKET_PROXY_IMAGE = (
  "ghcr.io/tecnativa/docker-socket-proxy:v0.4.2@"
  "sha256:1f3a6f303320723d199d2316a3e82b2e2685d86c275d5e3deeaf182573b47476"
)


def _run_docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ["docker", *args],
    check=check,
    capture_output=True,
    text=True,
    encoding="utf-8",
  )


def _wait_for_socket_proxy(port: int) -> None:
  ping_url = f"http://127.0.0.1:{port}/_ping"
  for _ in range(30):
    try:
      with urllib.request.urlopen(ping_url, timeout=1) as response:
        if response.read() == b"OK":
          return
    except OSError:
      time.sleep(0.2)
  pytest.fail("Docker Socket Proxy 未能在 6 秒内就绪")


def _proxy_fd_count(name: str) -> int:
  result = _run_docker(
    "exec",
    name,
    "sh",
    "-c",
    "for pid in $(pidof haproxy); do ls /proc/$pid/fd; done | wc -l",
  )
  return int(result.stdout.strip())


def _open_proxy_log_streams(name: str, port: int) -> list[socket.socket]:
  request = (
    f"GET /containers/{name}/logs?stdout=1&follow=1&tail=0 HTTP/1.1\r\n"
    f"Host: 127.0.0.1:{port}\r\n\r\n"
  ).encode("ascii")
  streams: list[socket.socket] = []
  for _ in range(16):
    stream = socket.create_connection(("127.0.0.1", port), timeout=2)
    stream.sendall(request)
    streams.append(stream)
  return streams


@pytest.mark.skipif(
  os.environ.get("TREND_GRID_RUN_DOCKER_INTEGRATION") != "1",
  reason="需要显式启用本机 Docker 集成测试",
)
def test_socket_proxy_reclaims_cancelled_log_streams() -> None:
  """真实 HAProxy 与 Docker socket 必须回收客户端取消的日志流。"""
  name = f"trend-grid-proxy-test-{uuid.uuid4().hex[:12]}"
  config = ROOT / "deploy" / "docker-socket-proxy" / "haproxy.cfg.template"
  streams: list[socket.socket] = []
  try:
    _run_docker(
      "run",
      "--detach",
      "--name",
      name,
      "--publish",
      "127.0.0.1::2375",
      "--env",
      "BIND_CONFIG=:2375",
      "--env",
      "CONTAINERS=1",
      "--env",
      "LOG_LEVEL=warning",
      "--volume",
      "/var/run/docker.sock:/var/run/docker.sock:ro",
      "--volume",
      f"{config.resolve()}:/usr/local/etc/haproxy/haproxy.cfg.template:ro",
      SOCKET_PROXY_IMAGE,
    )
    port_result = _run_docker("port", name, "2375/tcp")
    port = int(port_result.stdout.strip().rsplit(":", 1)[1])
    _wait_for_socket_proxy(port)
    baseline = _proxy_fd_count(name)
    streams = _open_proxy_log_streams(name, port)
    assert _proxy_fd_count(name) >= baseline + 16
    for stream in streams:
      stream.close()
    streams.clear()
    time.sleep(7)
    assert _proxy_fd_count(name) <= baseline + 6
  finally:
    for stream in streams:
      stream.close()
    _run_docker("rm", "--force", name, check=False)


def test_production_defaults_provide_32_runner_pool_with_guarded_concurrency() -> None:
  settings = (ROOT / "backend" / "settings.py").read_text(encoding="utf-8")
  env_example = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  runner_validation = script.split("read_runner_replicas() {", 1)[1].split(
    "\n}", 1,
  )[0]

  assert '"TREND_GRID_BACKTEST_BASE_CONCURRENCY", 16' in settings
  assert '"TREND_GRID_BACKTEST_BURST_MAX_CONCURRENCY",\n  24,' in settings
  assert "TREND_GRID_BAR_RUNNER_REPLICAS=16" in env_example
  assert "TREND_GRID_L2_RUNNER_REPLICAS=16" in env_example
  assert "TREND_GRID_BACKTEST_BASE_CONCURRENCY=16" in env_example
  assert "TREND_GRID_BACKTEST_BURST_MAX_CONCURRENCY=24" in env_example
  assert 'echo "16"' in runner_validation
  assert "1 至 32" in runner_validation


def test_deploy_incrementally_adds_documented_auth_session_defaults() -> None:
  settings = (ROOT / "backend" / "settings.py").read_text(encoding="utf-8")
  env_example = (ROOT / "deploy" / ".env.example").read_text(encoding="utf-8")
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  expected = {
    "TREND_GRID_AUTH_SESSION_IDLE_HOURS": "24",
    "TREND_GRID_AUTH_SESSION_MAX_DAYS": "7",
    "TREND_GRID_AUTH_SESSION_TOUCH_INTERVAL_SECONDS": "300",
  }

  for name, value in expected.items():
    assert f'{name}={value}' in env_example
    assert f'{name}={value}' in script
    assert f'"{name}"' in settings
  for explanation in (
    "用户连续无操作达到该时长后",
    "单次登录的最长有效天数",
    "最近活动时间的最小写入间隔",
  ):
    assert explanation in env_example
    assert explanation in script
  assert "存在重复定义" in script
  assert 'cp --preserve=mode "$env_file" "$env_backup_file"' in script
  assert 'mktemp "${env_file}.tmp.XXXXXX"' in script
  assert "env_defaults_changed=true" in script
  assert "deploy/.env.example deploy/.env" not in script


def _auth_env_harness(tmp_path: Path) -> tuple[str, Path]:
  """提取真实发布函数，供临时配置行为测试复用。"""
  if os.name == "nt":
    pytest.skip("增量发布配置行为测试仅在原生 POSIX 发布环境执行")
  bash = shutil.which("bash")
  if bash is None:
    pytest.skip("当前环境没有 bash，无法执行发布配置增量测试")
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")
  function_block = "env_key_count() {" + script.split(
    "env_key_count() {", 1,
  )[1].split("\nclose_submission_gate() {", 1)[0]
  harness = tmp_path / "env-defaults-harness.sh"
  harness.write_text("\n".join([
    "#!/usr/bin/env bash",
    "set -euo pipefail",
    'env_file="$1"',
    'env_defaults_changed=false',
    'env_backup_file="${env_file}.backup-before-managed-update"',
    'fail() { echo "发布终止：$1" >&2; exit 1; }',
    function_block,
    "append_auth_session_env_defaults",
    "validate_auth_session_env",
  ]), encoding="utf-8")
  return bash, harness


def _run_auth_env_harness(
  bash: str, harness: Path, env_file: Path, check: bool,
) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    [bash, harness.name, env_file.name], cwd=env_file.parent,
    check=check, capture_output=True, text=True, encoding="utf-8",
  )


def test_auth_session_env_defaults_are_idempotent_and_preserve_existing_values(
  tmp_path: Path,
) -> None:
  bash, harness = _auth_env_harness(tmp_path)
  env_file = tmp_path / "server.env"
  original = "PRIVATE_VALUE=keep\nTREND_GRID_AUTH_SESSION_MAX_DAYS=14\n"
  env_file.write_text(original, encoding="utf-8")

  _run_auth_env_harness(bash, harness, env_file, check=True)
  first_result = env_file.read_text(encoding="utf-8")
  assert "PRIVATE_VALUE=keep" in first_result
  assert "TREND_GRID_AUTH_SESSION_MAX_DAYS=14" in first_result
  assert "TREND_GRID_AUTH_SESSION_IDLE_HOURS=24" in first_result
  assert "TREND_GRID_AUTH_SESSION_TOUCH_INTERVAL_SECONDS=300" in first_result
  assert "用户连续无操作达到该时长后" in first_result
  assert "最近活动时间的最小写入间隔" in first_result
  assert env_file.with_name(
    "server.env.backup-before-managed-update",
  ).read_text(encoding="utf-8") == original

  _run_auth_env_harness(bash, harness, env_file, check=True)
  assert env_file.read_text(encoding="utf-8") == first_result


def test_auth_session_env_defaults_reject_duplicate_definitions(tmp_path: Path) -> None:
  bash, harness = _auth_env_harness(tmp_path)
  duplicate_env = tmp_path / "duplicate.env"
  duplicate_env.write_text(
    "TREND_GRID_AUTH_SESSION_IDLE_HOURS=24\n"
    "TREND_GRID_AUTH_SESSION_IDLE_HOURS=48\n",
    encoding="utf-8",
  )
  duplicate_result = _run_auth_env_harness(
    bash, harness, duplicate_env, check=False,
  )
  assert duplicate_result.returncode != 0
  assert "存在重复定义" in duplicate_result.stderr
