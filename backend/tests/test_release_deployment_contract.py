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
