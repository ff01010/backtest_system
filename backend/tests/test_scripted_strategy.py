"""受控策略脚本契约测试。"""

from pathlib import Path

import pytest

from backend.backtesting.engine import BarBacktestEngine
from backend.backtesting.models import Bar
from backend.strategies.scripted import ScriptedBarStrategy, validate_script_code


def _qmt_script_strategy(
  script_id: str,
  params: dict[str, bool] | None = None,
) -> ScriptedBarStrategy:
  code = Path("backend/strategies/trend_follow_qmt_1m_close_fill.py").read_text(encoding="utf-8")
  return ScriptedBarStrategy(script_id, code, params or {})


def _qmt_entry_bars() -> list[Bar]:
  return [
    Bar("20260727093000", "688981.SH", 10, 10, 10, 10),
    Bar("20260727093500", "688981.SH", 12, 12, 12, 12),
    Bar("20260727094000", "688981.SH", 11, 11, 11, 11),
    Bar("20260727094500", "688981.SH", 13, 13, 13, 13),
    Bar("20260727095000", "688981.SH", 12, 12, 12, 12),
    Bar("20260727095500", "688981.SH", 11, 11, 11, 11),
    Bar("20260727100000", "688981.SH", 12, 12, 12, 12),
    Bar("20260727100500", "688981.SH", 11, 11, 11, 11),
    Bar("20260727101000", "688981.SH", 10.8, 11, 10.5, 10.8),
  ]


def test_script_validator_rejects_invalid_literal_action() -> None:
  with pytest.raises(ValueError, match="订单 action"):
    validate_script_code("""
def on_bar(bar, state):
  return [{'action': 'buy_now', 'quantity': 100}]
""")


def test_script_validator_rejects_non_integer_literal_quantity() -> None:
  with pytest.raises(ValueError, match="quantity 字面量"):
    validate_script_code("""
def on_bar(bar, state):
  return [{'action': 'buy_open', 'quantity': 1.5}]
""")


def test_script_allows_installed_module_imports() -> None:
  strategy = ScriptedBarStrategy(9, """
import math
from decimal import Decimal

def on_bar(bar, state):
  quantity = math.ceil(float(Decimal('99.1')))
  return [{'action': 'buy_open', 'quantity': quantity}]
""", {})
  bar = Bar("202607270930", "688981.SH", 10, 10, 10, 10)

  orders = strategy._to_orders(strategy._callback({}, {}), bar)

  assert orders[0].quantity == 100


def test_script_runtime_rejects_dynamic_boolean_quantity() -> None:
  strategy = ScriptedBarStrategy(1, """
def on_bar(bar, state):
  return []
""", {})
  bar = Bar("202607270930", "688981.SH", 10, 10, 10, 10)

  with pytest.raises(ValueError, match="quantity 必须是大于 0 的整数"):
    strategy._to_orders([{'action': 'buy_open', 'quantity': True}], bar)


def test_script_runtime_skips_disallowed_open_direction() -> None:
  strategy = ScriptedBarStrategy(8, """
def on_bar(bar, state):
  return []
""", {"allowLong": False, "allowShort": True})
  bar = Bar("202607270930", "688981.SH", 10, 10, 10, 10)

  orders = strategy._to_orders([{'action': 'buy_open', 'quantity': 100}], bar)

  assert orders == []
  assert strategy.skipped_signals == [{
    "time": "202607270930",
    "signalTime": "202607270930",
    "executionTime": None,
    "direction": "BUY_OPEN",
    "action": "BUY_OPEN",
    "side": "BUY",
    "anchor": 0.0,
    "price": 10,
    "quantity": 100,
    "pnl": 0.0,
    "note": "方向设置已跳过：策略方案未启用做多",
    "priceSource": "当前 Bar 收盘价（未成交）",
    "status": "skipped",
    "strategyModule": "用户脚本",
  }]


def test_qmt_trend_follow_script_opens_and_stops_short_position() -> None:
  strategy = _qmt_script_strategy(2)
  bars = _qmt_entry_bars() + [
    Bar("20260727101500", "688981.SH", 12, 12, 11.5, 12),
  ]

  result = BarBacktestEngine(1_000_000).run(strategy, bars)

  assert [fill.metadata["direction"] for fill in result.fills] == [
    "SELL_OPEN",
    "BUY_CLOSE",
  ]
  assert [fill.quantity for fill in result.fills] == [1200, 1200]
  assert result.fills[0].time == "20260727100500"
  assert result.fills[0].price == 11
  assert result.fills[1].reason == "stop"


def test_qmt_trend_follow_script_records_skipped_direction_signal() -> None:
  strategy = _qmt_script_strategy(10, {"allowLong": True, "allowShort": False})

  result = BarBacktestEngine(1_000_000).run(strategy, _qmt_entry_bars())

  assert result.fills == []
  assert len(strategy.skipped_signals) == 1
  assert strategy.skipped_signals[0]["action"] == "SELL_OPEN"
  assert strategy.skipped_signals[0]["status"] == "skipped"


def test_qmt_trend_follow_script_applies_two_take_profit_levels() -> None:
  strategy = _qmt_script_strategy(3)
  bars = _qmt_entry_bars() + [
    Bar("20260727101500", "688981.SH", 8, 8.5, 7.5, 8),
    Bar("20260727102000", "688981.SH", 6, 6.5, 5.5, 6),
  ]

  result = BarBacktestEngine(1_000_000).run(strategy, bars)

  assert [fill.reason for fill in result.fills] == [
    "lower_peak_open_short",
    "take_profit_1",
    "take_profit_2",
  ]
  assert [fill.quantity for fill in result.fills] == [1200, 600, 600]


def test_qmt_trend_follow_script_opens_long_when_only_long_is_enabled() -> None:
  strategy = _qmt_script_strategy(7, {"allowLong": True, "allowShort": False})
  bars = [
    Bar("20260727093000", "688981.SH", 10, 10, 10, 10),
    Bar("20260727093100", "688981.SH", 8, 8, 8, 8),
    Bar("20260727093200", "688981.SH", 9, 9, 9, 9),
    Bar("20260727093300", "688981.SH", 8.5, 8.5, 8.5, 8.5),
    Bar("20260727093400", "688981.SH", 9.2, 9.2, 9.2, 9.2),
    Bar("20260727093500", "688981.SH", 8.4, 8.6, 8.4, 8.4),
  ]

  result = BarBacktestEngine(1_000_000).run(strategy, bars)

  assert [fill.metadata["direction"] for fill in result.fills] == [
    "BUY_OPEN",
    "SELL_CLOSE",
  ]
  assert [fill.reason for fill in result.fills] == ["higher_valley_open_long", "stop"]


def test_qmt_trend_follow_script_applies_break_even_and_morning_force_close() -> None:
  strategy = _qmt_script_strategy(4)
  bars = _qmt_entry_bars() + [
    Bar("20260727101500", "688981.SH", 10.5, 11, 10, 10.5),
    Bar("20260727102000", "688981.SH", 10.5, 11, 10, 10.5),
    Bar("20260727102500", "688981.SH", 10.8, 11, 10, 10.8),
  ]

  result = BarBacktestEngine(1_000_000).run(strategy, bars)

  assert [fill.reason for fill in result.fills] == ["lower_peak_open_short", "stop"]
  assert result.fills[-1].quantity == 1200

  morning_strategy = _qmt_script_strategy(5)
  morning_bars = _qmt_entry_bars() + [
    Bar("20260727112900", "688981.SH", 10.5, 11, 10, 10.5),
  ]
  morning_result = BarBacktestEngine(1_000_000).run(morning_strategy, morning_bars)

  assert [fill.reason for fill in morning_result.fills] == [
    "lower_peak_open_short",
    "morning_force",
  ]
  assert morning_result.fills[-1].quantity == 200

  final_strategy = _qmt_script_strategy(6)
  final_bars = _qmt_entry_bars() + [
    Bar("20260727145700", "688981.SH", 10.5, 11, 10, 10.5),
  ]
  final_result = BarBacktestEngine(1_000_000).run(final_strategy, final_bars)

  assert [fill.reason for fill in final_result.fills] == [
    "lower_peak_open_short",
    "afternoon_force",
    "final_force",
  ]
  assert [fill.quantity for fill in final_result.fills] == [1200, 200, 1000]
