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
