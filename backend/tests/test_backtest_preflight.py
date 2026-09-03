from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi import HTTPException

from backend import app as api_app
from backend import schemas
from backend.services import backtest_preflight, market_data, state_store


def _qmt_preview(*_: object) -> dict[str, Any]:
  resolution = market_data.BacktestDataResolution(
    timeframe="5m", source_period="5m", derivation=None,
    formal_dates=("2026-08-27",), warmup_dates=(), available_warmup_days=0,
  )
  return {
    "available": True, "resolution": resolution, "missingDates": [], "missingDataTypes": [],
    "availableWarmupDays": 0, "reason": "QMT 原始数据可用",
  }


def test_preflight_rejects_future_trade_date(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(backtest_preflight, '_shanghai_today', lambda: date(2026, 8, 27))
  monkeypatch.setattr(state_store, 'list_open_trade_dates', lambda *_: ['2026-08-27', '2026-08-28'])
  monkeypatch.setattr(market_data, "preview_backtest_data", _qmt_preview)
  monkeypatch.setattr(state_store, 'is_trade_calendar_range_covered', lambda *_: True)

  result = backtest_preflight.preview('688981.SH', '2026-08-27', '2026-08-28', 'bar_next_open', '5m')
  selected = next(item for item in result['timeframes'] if item['timeframe'] == '5m')

  assert selected['available'] is False
  assert selected['missingDataTypes'] == ['data.future_date']
  assert next(item for item in result['calendarDays'] if item['date'] == '2026-08-28')['status'] == 'future'
  with pytest.raises(backtest_preflight.PreflightError):
    backtest_preflight.require_available('688981.SH', '5m', '2026-08-27', '2026-08-28', 'bar_next_open')


def test_preflight_uses_qmt_when_trading_calendar_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
  def unavailable_calendar(*_: object) -> list[str]:
    raise state_store.DatabaseError('calendar unavailable')

  monkeypatch.setattr(backtest_preflight, '_shanghai_today', lambda: date(2026, 8, 27))
  monkeypatch.setattr(state_store, 'list_open_trade_dates', unavailable_calendar)
  monkeypatch.setattr(market_data, "preview_backtest_data", _qmt_preview)

  result = backtest_preflight.preview('688981.SH', '2026-08-26', '2026-08-27', 'bar_next_open', '5m')
  selected = next(item for item in result['timeframes'] if item['timeframe'] == '5m')

  assert selected['available'] is True
  assert selected['missingDataTypes'] == []
  assert {item['status'] for item in result['calendarDays']} == {'unknown'}


def test_preflight_uses_qmt_when_calendar_is_not_fully_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(backtest_preflight, "_shanghai_today", lambda: date(2026, 8, 27))
  monkeypatch.setattr(state_store, "list_open_trade_dates", lambda *_: [])
  monkeypatch.setattr(state_store, "is_trade_calendar_range_covered", lambda *_: False)
  monkeypatch.setattr(market_data, "preview_backtest_data", _qmt_preview)

  result = backtest_preflight.preview("688981.SH", "2026-08-26", "2026-08-27", "bar_next_open", "5m")
  selected = next(item for item in result["timeframes"] if item["timeframe"] == "5m")

  assert selected["available"] is True
  assert selected["missingDataTypes"] == []
  assert {item["status"] for item in result["calendarDays"]} == {"unknown"}


def test_data_status_returns_retryable_source_error(monkeypatch: pytest.MonkeyPatch) -> None:
  def unavailable(*_: object) -> dict[str, Any]:
    raise market_data.MarketDataError("ClickHouse 查询失败: timed out")

  monkeypatch.setattr(market_data, "status", unavailable)

  with pytest.raises(HTTPException) as captured:
    api_app.data_status("688981.SH", "1d")

  assert captured.value.status_code == 503
  assert captured.value.detail == {
    "code": "data.source_unavailable",
    "message": "行情数据源暂时不可用，请稍后重试。",
    "retryable": True,
  }


def test_strategy_config_uses_warmup_declared_by_script() -> None:
  config = {"paramsJson": {"params": {"fillModel": "bar_next_open"}}}
  script = {"code": "WARMUP_TRADING_DAYS = 140\n\ndef on_bar(bar, state):\n  return []"}

  api_app._apply_script_warmup_to_config(script, config)

  assert config["paramsJson"]["params"]["warmupTradingDays"] == 140


def test_saved_warmup_is_derived_from_current_script(monkeypatch: pytest.MonkeyPatch) -> None:
  config = {
    "id": "config-1", "name": "测试策略", "scriptId": "script-1", "strategy": "user_script",
    "mode": "backtest", "symbol": "688981.SH", "timeframe": "1d", "benchmarkSymbol": None,
    "accountId": None, "paramsJson": {"params": {"warmupTradingDays": 0}},
  }
  script = {
    "id": "script-1", "name": "测试策略", "description": "", "entryType": "on_bar",
    "code": "WARMUP_TRADING_DAYS = 140\n\ndef on_bar(bar, state):\n  return []",
  }
  monkeypatch.setattr(state_store, "get_strategy_config", lambda _: config)
  monkeypatch.setattr(state_store, "get_strategy_script", lambda _: script)
  request: dict[str, Any] = {
    "configId": "config-1", "symbol": "688981.SH", "timeframe": "1d", "strategy": "user_script",
    "benchmarkSymbol": None, "accountId": None, "fillModel": "bar_next_open",
    "executionAlgorithm": "immediate", "executionParameters": {}, "strategyParams": {},
  }

  api_app._prepare_saved_strategy_snapshot(request)

  assert request["warmupTradingDays"] == 140


def test_complete_strategy_save_uses_one_store_call(monkeypatch: pytest.MonkeyPatch) -> None:
  captured: dict[str, Any] = {}

  def save(
    script: dict[str, Any], config: dict[str, Any], script_id: str | None, config_id: str | None,
    expected_revision: int | None,
  ) -> dict[str, dict[str, Any]]:
    captured.update(
      script=script,
      config=config,
      script_id=script_id,
      config_id=config_id,
      expected_revision=expected_revision,
    )
    return {'script': {'id': 'script-1'}, 'config': {'id': 'config-1'}}

  monkeypatch.setattr(api_app, '_validate_strategy_script', lambda _: None)
  monkeypatch.setattr(api_app, '_validate_strategy_config_timeframe', lambda _: None)
  monkeypatch.setattr(api_app.settings, 'validate_symbol', lambda _: True)
  monkeypatch.setattr(api_app.state_store, 'save_complete_strategy', save)
  payload = schemas.CompleteStrategySave(
    script={'name': '完整策略', 'code': 'def on_bar(bar, state):\n  return []'},
    config={
      'name': '完整策略', 'symbol': '688981.SH', 'timeframe': '5m',
      'paramsJson': {'params': {'startDate': '2026-08-27', 'endDate': '2026-08-27'}},
    },
  )

  result = api_app.save_complete_strategy(payload)

  assert result['script']['id'] == 'script-1'
  assert captured['script_id'] is None
  assert captured['config_id'] is None
  assert captured['expected_revision'] is None
  assert captured['config']['scriptId'] is None
