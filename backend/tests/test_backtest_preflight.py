from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from backend import app as api_app
from backend import schemas
from backend.services import backtest_preflight, state_store


def test_preflight_rejects_future_trade_date(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(backtest_preflight, '_shanghai_today', lambda: date(2026, 8, 27))
  monkeypatch.setattr(state_store, 'list_open_trade_dates', lambda *_: ['2026-08-27', '2026-08-28'])
  monkeypatch.setattr(backtest_preflight.bar_data, 'derived_bar_available_dates', lambda *_: ['2026-08-27'])
  monkeypatch.setattr(state_store, 'is_trade_calendar_range_covered', lambda *_: True)

  result = backtest_preflight.preview('688981.SH', '2026-08-27', '2026-08-28', 'bar_next_open', '5m')
  selected = next(item for item in result['timeframes'] if item['timeframe'] == '5m')

  assert selected['available'] is False
  assert selected['missingDataTypes'] == ['data.future_date']
  assert next(item for item in result['calendarDays'] if item['date'] == '2026-08-28')['status'] == 'future'
  with pytest.raises(backtest_preflight.PreflightError):
    backtest_preflight.require_available('688981.SH', '5m', '2026-08-27', '2026-08-28', 'bar_next_open')


def test_preflight_blocks_when_trading_calendar_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
  def unavailable_calendar(*_: object) -> list[str]:
    raise state_store.DatabaseError('calendar unavailable')

  monkeypatch.setattr(backtest_preflight, '_shanghai_today', lambda: date(2026, 8, 27))
  monkeypatch.setattr(state_store, 'list_open_trade_dates', unavailable_calendar)

  result = backtest_preflight.preview('688981.SH', '2026-08-26', '2026-08-27', 'bar_next_open', '5m')
  selected = next(item for item in result['timeframes'] if item['timeframe'] == '5m')

  assert selected['available'] is False
  assert selected['missingDataTypes'] == ['data.calendar_unavailable']
  assert {item['status'] for item in result['calendarDays']} == {'unknown'}


def test_preflight_blocks_when_calendar_is_not_fully_initialized(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(backtest_preflight, "_shanghai_today", lambda: date(2026, 8, 27))
  monkeypatch.setattr(state_store, "list_open_trade_dates", lambda *_: [])
  monkeypatch.setattr(state_store, "is_trade_calendar_range_covered", lambda *_: False)

  result = backtest_preflight.preview("688981.SH", "2026-08-26", "2026-08-27", "bar_next_open", "5m")
  selected = next(item for item in result["timeframes"] if item["timeframe"] == "5m")

  assert selected["missingDataTypes"] == ["data.calendar_unavailable"]
  assert {item["status"] for item in result["calendarDays"]} == {"unknown"}

def test_complete_strategy_save_uses_one_store_call(monkeypatch: pytest.MonkeyPatch) -> None:
  captured: dict[str, Any] = {}

  def save(
    script: dict[str, Any], config: dict[str, Any], script_id: str | None, config_id: str | None,
  ) -> dict[str, dict[str, Any]]:
    captured.update(script=script, config=config, script_id=script_id, config_id=config_id)
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
  assert captured['config']['scriptId'] is None
