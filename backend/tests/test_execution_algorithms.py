from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.backtesting.models import Bar, OrderIntent, OrderSide, PositionEffect
from backend.execution.contract import ExecutionContext
from backend.execution.registry import validate_execution_request
from backend.execution.runtime import ExecutionRuntime
from backend.l2.engine import L2ExecutionEngine
from backend.l2.models import InstrumentRules, NormalizedBar, QuoteLevel, QuoteSnapshot


def _context() -> ExecutionContext:
  rules = InstrumentRules(lot_size=100)
  return ExecutionContext("l2_quote", rules, datetime.fromisoformat("2026-06-01 14:57:00"))


def _intent(quantity: int = 300) -> OrderIntent:
  return OrderIntent("688126.SH", OrderSide.BUY, quantity, "测试开多", position_effect=PositionEffect.OPEN_LONG)


def test_twap_only_generates_child_after_strictly_later_quote() -> None:
  runtime = ExecutionRuntime("twap", {"durationSeconds": 30, "intervalSeconds": 3, "minChildQuantity": 100, "completionPolicy": "cancel_remaining"}, _context())
  signal_at = datetime.fromisoformat("2026-06-01 09:30:00")

  assert runtime.submit(_intent(), signal_at) == []
  assert runtime.on_quote(signal_at) == []
  children = runtime.on_quote(datetime.fromisoformat("2026-06-01 09:30:03"))

  assert len(children) == 1
  assert children[0].intent.quantity == 100
  assert children[0].intent.metadata["parent_signal_time"] == "2026-06-01 09:30:00"


def test_twap_uses_effective_trading_seconds_across_lunch_break() -> None:
  runtime = ExecutionRuntime("twap", {"durationSeconds": 180, "intervalSeconds": 60, "minChildQuantity": 100, "completionPolicy": "cancel_remaining"}, _context())
  signal_at = datetime.fromisoformat("2026-06-01 11:29:30")

  runtime.submit(_intent(), signal_at)
  first = runtime.on_quote(datetime.fromisoformat("2026-06-01 11:29:33"))
  second = runtime.on_quote(datetime.fromisoformat("2026-06-01 13:00:30"))

  assert len(first) == 1
  assert len(second) == 1
  assert second[0].scheduled_at == datetime.fromisoformat("2026-06-01 13:00:30")


def test_twap_cancels_remaining_quantity_at_deadline() -> None:
  runtime = ExecutionRuntime("twap", {"durationSeconds": 6, "intervalSeconds": 3, "minChildQuantity": 100, "completionPolicy": "cancel_remaining"}, _context())
  signal_at = datetime.fromisoformat("2026-06-01 09:30:00")

  runtime.submit(_intent(), signal_at)
  first_children = runtime.on_quote(datetime.fromisoformat("2026-06-01 09:30:03"))
  runtime.on_fill(first_children[0].intent.metadata.get("parent_order_id"), first_children[0].intent.quantity)
  runtime.on_quote(datetime.fromisoformat("2026-06-01 09:30:06"))

  plan = runtime.result()[0]
  assert runtime.consume_cancelled_parent_ids() == {plan["parentOrderId"]}
  assert plan["status"] == "cancelled"
  assert plan["remainingQuantity"] == 0
  assert plan["cancelledQuantity"] > 0


def test_twap_rejects_non_l2_and_invalid_interval() -> None:
  try:
    validate_execution_request("bar_next_open", "twap", {})
  except ValueError as error:
    assert str(error) == "TWAP 仅支持 L2 盘口回放"
  else:
    raise AssertionError("标准 Bar 不应允许 TWAP")

  try:
    validate_execution_request("l2_quote", "twap", {"durationSeconds": 30, "intervalSeconds": 4, "minChildQuantity": 100})
  except ValueError as error:
    assert "3 秒倍数" in str(error)
  else:
    raise AssertionError("非三秒倍数间隔不应通过")


class _SingleSignalStrategy:
  def on_bar(self, bar: Bar, state: Any) -> list[OrderIntent]:
    return [_intent()]


def test_l2_twap_child_cannot_fill_on_first_quote_after_signal() -> None:
  engine = L2ExecutionEngine(
    _SingleSignalStrategy(), 1_000_000, 0.0, InstrumentRules(lot_size=100),
    execution_algorithm="twap",
    execution_parameters={"durationSeconds": 30, "intervalSeconds": 3, "minChildQuantity": 100, "completionPolicy": "cancel_remaining"},
  )
  engine.begin_day("2026-06-01")
  bar = NormalizedBar("688126.SH", "2026-06-01", "1m", "2026-06-01 09:29:00", "2026-06-01 09:30:00", 10, 10, 10, 10, 100, 1000, "complete", "test")
  engine.process_bar(bar)
  engine.process_quote(_quote("09:30:03", 1))
  assert engine.result()["fills"] == []

  engine.process_quote(_quote("09:30:06", 2))

  assert len(engine.result()["fills"]) == 1
  assert engine.result()["executionPlans"][0]["filledQuantity"] == 100


def _quote(clock: str, sequence: int) -> QuoteSnapshot:
  return QuoteSnapshot(
    "688126.SH", "2026-06-01", f"2026-06-01 {clock}", sequence,
    (QuoteLevel(9.99, 10_000),), (QuoteLevel(10.01, 10_000),), 10.0, "test",
  )
