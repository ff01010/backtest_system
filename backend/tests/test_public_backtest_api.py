from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend import app as api_app
from backend import schemas


def _config() -> dict[str, object]:
  return {
    "id": "00000000-0000-0000-0000-000000000007",
    "name": "测试策略",
    "mode": "backtest",
    "enabled": True,
    "published": True,
    "scriptId": "00000000-0000-0000-0000-000000000011",
    "accountId": None,
    "strategy": "user_script",
    "symbol": "688981.SH",
    "benchmarkSymbol": "688981.SH",
    "timeframe": "5m",
    "paramsJson": {
      "params": {
        "startDate": "2026-06-01",
        "endDate": "2026-06-03",
        "initialCapital": "1000000",
        "commissionRate": "0.0003",
        "slippage": "0.001",
        "allowLong": True,
        "allowShort": True,
        "strategyCategory": "directional_holding",
        "holdingPolicy": "overnight",
        "fillModel": "bar_next_open",
        "executionAlgorithm": "immediate",
        "baseQuantity": "0",
        "baseCostPrice": "0",
        "initialPositionEnabled": False,
        "t0Enabled": False,
        "window": "20",
      },
    },
  }


def test_public_request_only_overrides_allowed_runtime_fields(monkeypatch: pytest.MonkeyPatch) -> None:
  config = _config()
  monkeypatch.setattr(api_app.state_store, "get_strategy_config", lambda _: config)
  monkeypatch.setattr(api_app.state_store, "get_strategy_script", lambda _: {
    "id": config["scriptId"],
    "name": "测试策略",
    "description": "",
    "code": "def on_bar(bar, state):\n  return []\n",
    "entryType": "on_bar",
  })
  monkeypatch.setattr(
    api_app.backtest_preflight,
    "require_available",
    lambda *_: pytest.fail("公共回测提交不应执行行情预检"),
  )
  payload = schemas.PublicBacktestCreateRequest(
    config_id=str(config["id"]),
    overrides={"start_date": "2026-06-02", "initial_capital": 200_000},
  )

  request = api_app._build_public_backtest_request(payload)

  assert request["symbol"] == "688981.SH"
  assert request["startDate"] == "2026-06-02"
  assert request["endDate"] == "2026-06-03"
  assert request["initialCapital"] == 200_000
  assert request["initialPositionEnabled"] is False
  assert request["strategyParams"] == {"allowLong": True, "allowShort": True, "strategyCategory": "directional_holding", "holdingPolicy": "overnight", "fillModel": "bar_next_open", "baseQuantity": "0", "baseCostPrice": "0", "initialPositionEnabled": False, "t0Enabled": False, "window": "20"}


def test_public_request_rejects_unpublished_config(monkeypatch: pytest.MonkeyPatch) -> None:
  config = _config()
  config["published"] = False
  monkeypatch.setattr(api_app.state_store, "get_strategy_config", lambda _: config)
  payload = schemas.PublicBacktestCreateRequest(config_id=str(config["id"]))

  with pytest.raises(HTTPException, match="尚未发布"):
    api_app._build_public_backtest_request(payload)


def test_public_request_hash_is_stable_and_changes_with_override() -> None:
  first = schemas.PublicBacktestCreateRequest(
    config_id="00000000-0000-0000-0000-000000000007",
    overrides={"start_date": "2026-06-01"},
  )
  same = schemas.PublicBacktestCreateRequest(
    config_id="00000000-0000-0000-0000-000000000007",
    overrides={"start_date": "2026-06-01"},
  )
  changed = schemas.PublicBacktestCreateRequest(
    config_id="00000000-0000-0000-0000-000000000007",
    overrides={"start_date": "2026-06-02"},
  )

  assert api_app._public_request_hash(first) == api_app._public_request_hash(same)
  assert api_app._public_request_hash(first) != api_app._public_request_hash(changed)


def test_public_run_payload_exposes_overlay_result_contract() -> None:
  payload = api_app._public_run_payload({
    "id": "run-1",
    "displayNo": 39,
    "configId": "config-1",
    "symbol": "688981.SH",
    "timeframe": "5m",
    "fillModel": "bar_next_open",
    "status": "succeeded",
    "startDate": "2026-09-01",
    "endDate": "2026-09-03",
    "summaryJson": {},
    "paramsJson": {
      "strategyCategory": "core_position_overlay",
      "overlaySettlementPolicy": "intraday_restore",
    },
    "resultJson": {
      "strategyCategory": "core_position_overlay",
      "resultSchemaVersion": 3,
      "experimentManifest": {"overlaySettlementPolicy": "cross_day_carry"},
    },
    "createdAt": "2026-09-04T10:00:00+08:00",
    "finishedAt": "2026-09-04T10:01:00+08:00",
  })

  assert payload["strategy_category"] == "core_position_overlay"
  assert payload["overlay_settlement_policy"] == "cross_day_carry"
  assert payload["result_schema_version"] == 3


def test_backtest_submission_rejects_unavailable_runner(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(api_app.state_store, "has_healthy_backtest_runner", lambda _: False)

  with pytest.raises(HTTPException) as raised:
    api_app._require_backtest_runner("bar_next_open")

  assert raised.value.status_code == 503
  assert raised.value.detail == "回测执行服务暂不可用，请稍后重试"


def test_health_reports_runner_degradation_without_internal_counts(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(api_app.state_store, "get_backtest_runner_health", lambda: {
    "status": "degraded",
    "runners": {"bar": {"healthy": 0, "queued": 3}},
  })
  monkeypatch.setattr(
    api_app.state_store, "signal_export_runner_available", lambda: True,
  )

  assert api_app.health() == {
    "status": "degraded",
    "backtest": "unavailable",
    "applicationBuildId": api_app.settings.APPLICATION_BUILD_ID,
    "clientCompatibilityVersion": api_app.settings.CLIENT_COMPATIBILITY_VERSION,
    "strategyRuntimeVersion": api_app.SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": api_app.RUNTIME_CAPABILITY_DIGEST,
    "runtimeCapabilities": api_app._verified_runtime_capabilities,
    "signalExportRunner": {"healthy": True},
    "observability": {"status": "disabled"},
  }
