from __future__ import annotations

from typing import Any, Sequence

from backend.backtesting import Bar, BarBacktestEngine, NextOpenExecutionModel, OrderIntent, OrderSide


class _WarmupStrategy:
  def __init__(self) -> None:
    self.calls: list[str] = []
    self.events: list[str] = []

  def on_bar(self, bar: Bar, _: Any) -> Sequence[OrderIntent]:
    self.calls.append(bar.time)
    return [OrderIntent(bar.symbol, OrderSide.BUY, 100, "warmup-order", time=bar.time)]

  def on_order_event(self, event: dict[str, Any]) -> None:
    self.events.append(str(event["type"]))


def _bar(time: str) -> Bar:
  return Bar(time=time, symbol="688981.SH", open=10.0, high=11.0, low=9.0, close=10.5, volume=100)


def test_warmup_updates_strategy_without_orders_or_result_side_effects() -> None:
  strategy = _WarmupStrategy()
  engine = BarBacktestEngine(100_000, execution_model=NextOpenExecutionModel(), execute_on_next_bar_open=True)

  result = engine.run(strategy, [_bar("202607291500"), _bar("202607301500")], warmup_bar_count=1)

  assert strategy.calls == ["202607291500", "202607301500"]
  assert strategy.events == ["accepted", "unexecuted"]
  assert result.fills == []
  assert len(result.portfolio_snapshots) == 1
  assert len(result.unexecuted_orders) == 1
