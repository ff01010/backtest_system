# -*- coding: utf-8 -*-
"""发布脚本与 Compose 的关键不可变镜像契约。"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


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
    "backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker signal-export-runner"
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
    "backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker signal-export-runner"
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

  assert "grafana/loki:3.7.6@sha256:" in compose
  assert "grafana/alloy:v1.19.2@sha256:" in compose
  assert "docker-socket-proxy:v0.4.2@sha256:" in compose
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
  assert script.index("run --rm migrate") < script.index(
    "up -d --build container-event-observer",
  )
  assert script.index(
    "backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker signal-export-runner",
  ) < (
    script.index("if ! wait_for_observability; then")
  )


def test_observability_failure_cannot_stop_core_release_after_cutover() -> None:
  script = (ROOT / "scripts" / "deploy-server.sh").read_text(encoding="utf-8")

  preflight = script.index("up -d --build docker-socket-proxy loki alloy")
  gate_close = script.index("\nclose_submission_gate\n")
  core_stop = script.index("\nstop_submission_and_wait_for_backtests\n")
  assert preflight < gate_close < core_stop

  migration = script.index("run --rm migrate")
  observer = script.index("up -d --build container-event-observer")
  core_start = script.index(
    "backend bar-runner l2-runner experiment-coordinator evaluation-worker evaluation-export-worker signal-export-runner",
  )

  assert core_stop < migration < observer < core_start
  assert 'if "${compose[@]}" up -d --build docker-socket-proxy loki alloy' in script
  assert 'if "${compose[@]}" up -d --build container-event-observer' in script
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
