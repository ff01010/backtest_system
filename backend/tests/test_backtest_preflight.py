from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from backend import app as api_app
from backend import schemas
from backend.services import backtest_preflight, l2_quote_availability, market_data, state_store


def test_preflight_compatibility_endpoint_does_not_query_market_data(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    market_data,
    "preview_backtest_data",
    lambda *_args, **_kwargs: pytest.fail("兼容接口不应查询 Bar 覆盖"),
  )
  result = backtest_preflight.preview(
    "688981.SH", "2026-08-26", "2026-08-28", "bar_next_open", "5m",
  )
  selected = next(item for item in result["timeframes"] if item["timeframe"] == "5m")

  assert selected["available"] is True
  assert selected["missingDataTypes"] == []
  assert result["missingDates"] == []
  assert result["calendarDays"] == []


def test_require_available_is_non_blocking_compatibility_wrapper() -> None:
  result = backtest_preflight.require_available(
    "688981.SH", "5m", "2026-08-26", "2026-08-28", "bar_next_open",
  )

  assert result["preflight"]["available"] is True
  assert result["barSnapshot"] == {}


def test_data_status_returns_retryable_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
  def unavailable(*_: object) -> dict[str, Any]:
    raise market_data.MarketDataError("ClickHouse 查询失败: timed out")

  monkeypatch.setattr(market_data, "status", unavailable)

  with pytest.raises(HTTPException) as captured:
    api_app.data_status("688981.SH", "1d")

  assert captured.value.status_code == 503
  assert captured.value.detail == {
    "code": "data.source_unavailable",
    "message": "行情数据异常，请联系管理员",
    "retryable": True,
  }


def test_data_status_sanitizes_unexpected_metadata_error(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    market_data,
    "status",
    lambda *_: (_ for _ in ()).throw(ValueError("raw metadata value")),
  )

  with pytest.raises(HTTPException) as captured:
    api_app.data_status("688981.SH", "5m")

  assert captured.value.status_code == 503
  assert captured.value.detail["message"] == "行情数据异常，请联系管理员"
  assert "raw metadata value" not in str(captured.value.detail)


def test_data_status_uses_l2_summary_for_l2_mode(monkeypatch: pytest.MonkeyPatch) -> None:
  expected = {
    "symbol": "688981.SH", "timeframe": "5m", "exists": True,
    "firstDate": "2026-07-01", "lastDate": "2026-07-30",
  }
  monkeypatch.setattr(l2_quote_availability, "status", lambda *_: expected)
  monkeypatch.setattr(
    market_data,
    "status",
    lambda *_: pytest.fail("L2 范围不应读取标准 Bar 元数据"),
  )

  result = api_app.data_status("688981.SH", "5m", "l2_quote")

  assert result == {"status": {**expected, "fillModel": "l2_quote"}}


def test_data_status_rejects_non_backtest_timeframe() -> None:
  with pytest.raises(HTTPException) as captured:
    api_app.data_status("688981.SH", "2m", "l2_quote")

  assert captured.value.status_code == 422
  assert captured.value.detail == "不支持的周期: 2m"


def test_strategy_config_save_does_not_run_market_data_preflight(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    backtest_preflight,
    "require_available",
    lambda *_: pytest.fail("保存策略不应执行行情预检"),
  )

  api_app._validate_strategy_config_timeframe({
    "symbol": "688981.SH",
    "timeframe": "5m",
    "paramsJson": {"params": {
      "fillModel": "bar_next_open",
      "startDate": "2026-07-01",
      "endDate": "2026-07-30",
    }},
  })


def test_strategy_config_uses_warmup_declared_by_script() -> None:
  _, config = api_app.strategy_lifecycle._validated_payloads(
    api_app.strategy_lifecycle.StrategySaveCommand(
      name="预热策略",
      description="",
      code="WARMUP_TRADING_DAYS = 140\n\ndef on_bar(bar, state):\n  return []",
      entry_type="on_bar",
      signal_required=False,
      strategy="user_script",
      mode="backtest",
      symbol="688981.SH",
      timeframe="1d",
      benchmark_symbol=None,
      account_id=None,
      params_json={"params": {
        "fillModel": "bar_next_open",
        "startDate": "2026-07-01",
        "endDate": "2026-07-30",
      }},
      strategy_parameters=None,
      enabled=True,
    ),
  )

  assert config["paramsJson"]["params"]["warmupTradingDays"] == 140


def test_saved_warmup_is_derived_from_current_script(monkeypatch: pytest.MonkeyPatch) -> None:
  config = {
    "id": "config-1", "name": "测试策略", "scriptId": "script-1", "strategy": "user_script",
    "mode": "backtest", "symbol": "688981.SH", "timeframe": "1d", "benchmarkSymbol": None,
    "accountId": None, "paramsJson": {
      "params": {"warmupTradingDays": 0, "window": 20},
      "strategyParameterDefinitions": {
        "window": {"type": "integer", "default": 20},
      },
      "strategyParams": {"window": 20},
    },
  }
  script = {
    "id": "script-1", "name": "测试策略", "description": "", "entryType": "on_bar",
    "code": (
      "WARMUP_TRADING_DAYS = 140\n"
      'STRATEGY_PARAMETERS = {"window": 20}\n\n'
      "def on_bar(bar, state):\n  return []"
    ),
  }
  monkeypatch.setattr(state_store, "get_strategy_config", lambda _: config)
  monkeypatch.setattr(state_store, "get_strategy_script", lambda _: script)
  request: dict[str, Any] = {
    "configId": "config-1", "symbol": "688981.SH", "timeframe": "1d", "strategy": "user_script",
    "benchmarkSymbol": None, "accountId": None, "fillModel": "bar_next_open",
    "executionAlgorithm": "immediate", "executionParameters": {},
    "strategyParams": {"window": 10}, "strategyParamOverrides": {"window": 10},
  }

  api_app._prepare_saved_strategy_snapshot(request)

  assert request["warmupTradingDays"] == 140
  assert request["savedStrategyParams"] == {"window": 20}
  assert request["strategyParamOverrides"] == {"window": 10}
  assert request["effectiveStrategyParams"] == {"window": 10}
  assert request["savedConfigSnapshot"]["savedStrategyParams"] == {"window": 20}
  assert request["savedConfigSnapshot"]["effectiveStrategyParams"] == {"window": 10}


def test_complete_strategy_save_uses_one_lifecycle_call(monkeypatch: pytest.MonkeyPatch) -> None:
  captured: list[Any] = []

  def save(command: Any) -> dict[str, dict[str, Any]]:
    captured.append(command)
    return {"script": {"id": "script-1"}, "config": {"id": "config-1"}}

  monkeypatch.setattr(api_app.strategy_lifecycle, "save_strategy", save)
  payload = schemas.CompleteStrategySave(
    script={'name': '完整策略', 'code': 'def on_bar(bar, state):\n  return []'},
    config={
      'name': '完整策略', 'symbol': '688981.SH', 'timeframe': '5m',
      'paramsJson': {'params': {'startDate': '2026-08-27', 'endDate': '2026-08-27'}},
    },
  )

  result = api_app.save_complete_strategy(payload)

  assert result["script"]["id"] == "script-1"
  assert len(captured) == 1
  command = captured[0]
  assert command.script_id is None
  assert command.config_id is None
  assert command.expected_revision is None
  assert command.params_json["params"]["startDate"] == "2026-08-27"
