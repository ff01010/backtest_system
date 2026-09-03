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
    api_app.backtest_preflight, "require_available", lambda *_: {"barSnapshot": {"provider": "test"}},
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
