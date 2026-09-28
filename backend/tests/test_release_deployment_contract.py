# -*- coding: utf-8 -*-
"""发布脚本与 Compose 的关键不可变镜像契约。"""

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
  assert "backend bar-runner l2-runner signal-export-runner" in script
  assert "strategy-worker" not in script
  assert "strategy-worker:" not in compose
  assert "Dockerfile.worker" not in compose
  assert 'runner_containers_ready "signal-export-runner" "1"' in script
  assert '"signalExportRunner":{"healthy":true}' in script


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
  assert "wait_for_observability" in script
  assert script.index("run --rm migrate") < script.index(
    "up -d --build docker-socket-proxy loki alloy container-event-observer",
  )
  assert script.index("wait_for_observability") < script.index(
    "backend bar-runner l2-runner signal-export-runner",
  )


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
