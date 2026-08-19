"""受控策略脚本契约测试。"""

from pathlib import Path

import pytest

from backend.backtesting.engine import BarBacktestEngine
from backend.backtesting.models import Bar, PortfolioSnapshot, PositionSnapshot
from backend.l2.engine import L2ExecutionEngine
from backend.l2.models import InstrumentRules, NormalizedBar, QuoteLevel, QuoteSnapshot
from backend.services.backtest_errors import StrategyExecutionError, error_summary
from backend.strategies.scripted import ScriptedBarStrategy, quote_mode_for_script, validate_script_code


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


def test_quote_callback_uses_continuous_mode_and_preserves_client_key() -> None:
  strategy = ScriptedBarStrategy(14, """
def on_bar(bar, state):
  return []

def on_quote(quote, state):
  return [{'action': 'buy_open', 'quantity': 100, 'clientOrderKey': 'opening'}]
""", {})
  quote = QuoteSnapshot(
    "688981.SH", "20260727", "2026-07-27 09:30:03", 1,
    (QuoteLevel(10, 100),), (QuoteLevel(10.01, 100),), 10.01,
  )
  orders = strategy.on_quote(quote, PortfolioSnapshot(cash=100_000, equity=100_000, positions={}))

  assert strategy.quote_mode == "continuous"
  assert orders[0].metadata["client_order_key"] == "opening"
  assert quote_mode_for_script("def on_bar(bar, state):\n  return []\n") == "order_driven"


def test_continuous_mode_requires_quote_callback() -> None:
  with pytest.raises(ValueError, match="必须定义 on_quote"):
    ScriptedBarStrategy(15, "def on_bar(bar, state):\n  return []\n", {}, "continuous")


@pytest.mark.parametrize("symbol", ["600584.SH", "688981.SH"])
def test_v6_template_requests_opening_position_from_first_completed_bar(symbol: str) -> None:
  code = Path("backend/strategies/staged_entry_long_v6_l2.py").read_text(encoding="utf-8")
  strategy = ScriptedBarStrategy("v6", code, {"allowLong": True, "allowShort": True})
  portfolio = PortfolioSnapshot(cash=1_000_000, equity=1_000_000, positions={})
  orders = strategy.on_bar(Bar("20260727093000", symbol, 10, 10, 10, 10), portfolio)
  quote = QuoteSnapshot(
    symbol, "20260727", "2026-07-27 09:30:03", 1,
    (QuoteLevel(10, 1_000),), (QuoteLevel(10.01, 1_000),), 10,
  )

  assert strategy.quote_mode == "continuous"
  assert [(item.quantity, item.metadata["client_order_key"]) for item in orders] == [(200, "opening")]
  assert strategy.on_quote(quote, portfolio) == []


def test_v6_template_runs_in_standard_bar_mode() -> None:
  code = Path("backend/strategies/staged_entry_long_v6_l2.py").read_text(encoding="utf-8")
  strategy = ScriptedBarStrategy("v6-standard", code, {"allowLong": True, "allowShort": True})

  result = BarBacktestEngine(
    initial_cash=1_000_000,
    execute_on_next_bar_open=True,
  ).run(strategy, [
    Bar("20260727093000", "688981.SH", 10, 10, 10, 10),
    Bar("20260727093100", "688981.SH", 10, 10, 10, 10),
  ])

  assert [(item.quantity, item.metadata["client_order_key"]) for item in result.fills] == [(200, "opening")]


def test_v6_template_opening_signal_uses_later_l2_quote_for_fill() -> None:
  code = Path("backend/strategies/staged_entry_long_v6_l2.py").read_text(encoding="utf-8")
  strategy = ScriptedBarStrategy("v6-l2", code, {"allowLong": True, "allowShort": True})
  engine = L2ExecutionEngine(strategy, 1_000_000, 0.00008, InstrumentRules())
  bar = NormalizedBar(
    symbol="688981.SH", trade_date="20260727", period="1m",
    start_time="2026-07-27 09:30:00", end_time="2026-07-27 09:31:00",
    open=10, high=10, low=10, close=10, volume=1_000, amount=10_000,
    quality="complete", source_version="test",
  )
  quote = QuoteSnapshot(
    "688981.SH", "20260727", "2026-07-27 09:31:03", 1,
    (QuoteLevel(9.99, 1_000),), (QuoteLevel(10.01, 1_000),), 10, "test",
  )

  engine.begin_day("20260727")
  engine.process_chunk([bar], [quote], [])

  fills = engine.result()["fills"]
  assert [(item["time"], item["quantity"]) for item in fills] == [("2026-07-27 09:31:03", 200)]


def test_script_context_unifies_compact_and_l2_iso_time() -> None:
  strategy = ScriptedBarStrategy(11, """
def on_bar(bar, state):
  return []
""", {})
  portfolio = PortfolioSnapshot(cash=100_000, equity=100_000, positions={})

  compact = strategy._context(Bar("20260727093000", "688981.SH", 10, 10, 10, 10), portfolio)
  l2_iso = strategy._context(Bar("2026-07-27 09:30:00", "688981.SH", 10, 10, 10, 10), portfolio)

  assert compact["time"] == "20260727093000"
  assert compact["datetime"] == "2026-07-27 09:30:00"
  assert compact["tradeDate"] == "2026-07-27"
  assert compact["hhmm"] == 930
  assert l2_iso == compact


def test_script_context_excludes_base_position_from_strategy_position() -> None:
  strategy = ScriptedBarStrategy(13, """
def on_bar(bar, state):
  return []
""", {})
  portfolio = PortfolioSnapshot(
    cash=100_000,
    equity=150_000,
    positions={"688981.SH": PositionSnapshot("688981.SH", 5_000, 10)},
    long_positions_by_key={
      "base_position": PositionSnapshot("688981.SH", 5_000, 10),
      "script:13:overlay_long": PositionSnapshot("688981.SH", 1_000, 11),
    },
    short_positions_by_key={
      "script:13:overlay_short": PositionSnapshot("688981.SH", 400, 12),
    },
    base_position=5_000,
  )

  context = strategy._context(Bar("20260727093000", "688981.SH", 10, 10, 10, 10), portfolio)

  assert context["position"] == 600
  assert context["strategyPosition"] == 600
  assert context["basePosition"] == 5_000


def test_script_context_error_explains_supported_time_contract() -> None:
  strategy = ScriptedBarStrategy(12, """
def on_bar(bar, state):
  return []
""", {})
  portfolio = PortfolioSnapshot(cash=100_000, equity=100_000, positions={})

  with pytest.raises(StrategyExecutionError) as raised:
    strategy.on_bar(Bar("not-a-time", "688981.SH", 10, 10, 10, 10), portfolio)

  assert raised.value.detail.code == "strategy.invalid_time"
  assert raised.value.detail.callback == "on_bar"
  assert raised.value.detail.symbol == "688981.SH"


def test_script_runtime_error_keeps_quote_callback_location_without_raw_exception() -> None:
  strategy = ScriptedBarStrategy("quote-error", """
def on_bar(bar, state):
  return []

def on_quote(quote, state):
  return missing_variable
""", {})
  quote = QuoteSnapshot(
    "688981.SH", "20260727", "2026-07-27 09:30:03", 1,
    (QuoteLevel(10, 100),), (QuoteLevel(10.01, 100),), 10.01,
  )

  with pytest.raises(StrategyExecutionError) as raised:
    strategy.on_quote(quote, PortfolioSnapshot(cash=100_000, equity=100_000, positions={}))

  detail = raised.value.detail
  assert detail.category == "strategy_error"
  assert detail.callback == "on_quote"
  assert detail.event_time == "2026-07-27 09:30:03"
  assert detail.symbol == "688981.SH"
  assert detail.message == "策略引用了未定义的变量或函数。"
  assert "missing_variable" not in error_summary(raised.value, "run-1")["error"]


def test_script_order_event_error_keeps_event_location() -> None:
  strategy = ScriptedBarStrategy("event-error", """
def on_bar(bar, state):
  return []

def on_order_event(event, state):
  raise KeyError('internal_field')
""", {})

  with pytest.raises(StrategyExecutionError) as raised:
    strategy.on_order_event({"symbol": "688981.SH", "signalTime": "2026-07-27 09:31:00"})

  assert raised.value.detail.callback == "on_order_event"
  assert raised.value.detail.event_time == "2026-07-27 09:31:00"
  assert raised.value.detail.symbol == "688981.SH"


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


def test_qmt_trend_follow_script_accepts_l2_iso_completion_time() -> None:
  strategy = _qmt_script_strategy(13)
  result = BarBacktestEngine(1_000_000).run(strategy, [
    Bar("2026-07-27 09:31:00", "688981.SH", 10, 10, 10, 10),
  ])

  assert result.fills == []


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
