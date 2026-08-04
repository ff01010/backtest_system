"""通用回测内核的关键行为测试。"""

from __future__ import annotations

import math

from backend.backtesting.engine import BarBacktestEngine, RecordedPriceExecutionModel
from backend.backtesting.legacy import fill_to_api_trade, legacy_trades_to_fills
from backend.backtesting.models import Bar, OrderIntent, OrderSide, PortfolioSnapshot, PositionEffect
from backend.services.backtest_runner import _compute_performance_stats
from backend.backtesting.trend_grid_compat import (
  LegacyTrendGridIntentStrategy,
  LegacyTrendGridOnBarStrategy,
  TrendGridBarStrategy,
)


class RoundTripStrategy:
  """第一根 K 线买入、第二根 K 线卖出的测试策略。"""

  def on_bar(
    self,
    bar: Bar,
    portfolio: PortfolioSnapshot,
  ) -> list[OrderIntent]:
    if not portfolio.positions:
      return [OrderIntent(bar.symbol, OrderSide.BUY, 100, "open")]
    return [OrderIntent(bar.symbol, OrderSide.SELL, 100, "close")]


def test_bar_engine_applies_fills_and_realized_pnl() -> None:
  engine = BarBacktestEngine(initial_cash=10_000, commission_rate=0.001)
  bars = [
    Bar("202607230930", "688981.SH", 10, 10, 10, 10),
    Bar("202607230935", "688981.SH", 11, 11, 11, 11),
  ]

  result = engine.run(RoundTripStrategy(), bars)

  assert len(result.fills) == 2
  assert result.fills[1].realized_pnl == 97.329
  assert result.final_portfolio.positions == {}
  assert result.final_portfolio.cash == 10_097.329
  assert result.final_portfolio.equity == 10_097.329


def test_performance_stats_uses_sample_standard_deviation_for_sharpe() -> None:
  stats = _compute_performance_stats(100.0, {
    "20260730": 1.0,
    "20260731": 2.02,
  })

  assert math.isclose(stats["annualVolatility"], 0.11, rel_tol=1e-5)
  assert math.isclose(stats["annualSharpe"], 33.0, rel_tol=1e-5)


def test_limit_order_waits_until_price_is_reachable() -> None:
  class LimitStrategy:
    def on_bar(
      self,
      bar: Bar,
      portfolio: PortfolioSnapshot,
    ) -> list[OrderIntent]:
      return [OrderIntent(bar.symbol, OrderSide.BUY, 100, "limit", 9.5)]

  engine = BarBacktestEngine(initial_cash=10_000)
  result = engine.run(LimitStrategy(), [
    Bar("202607230930", "688981.SH", 10, 10.5, 9.8, 10),
    Bar("202607230935", "688981.SH", 10, 10.2, 9.4, 9.8),
  ])

  assert len(result.fills) == 1
  assert result.fills[0].price == 9.5


def test_legacy_trade_bridge_preserves_frontend_shape() -> None:
  fills = legacy_trades_to_fills([{
    "time": "202607230930",
    "direction": "BUY_OPEN",
    "anchor": 9.8,
    "price": 10.0,
    "qty": 200,
    "pnl": 0,
    "note": "test",
  }], "688981.SH")

  assert fill_to_api_trade(fills[0]) == {
    "time": "202607230930",
    "signalTime": "202607230930",
    "executionTime": "202607230930",
    "direction": "BUY_OPEN",
    "action": "BUY_OPEN",
    "side": "BUY",
    "anchor": 9.8,
    "price": 10.0,
    "quantity": 200,
    "pnl": 0.0,
    "note": "test",
    "priceSource": "成交模型价格",
    "status": "filled",
  }


def test_batch_engine_replays_orders_on_their_recorded_bar() -> None:
  class RecordedStrategy:
    def generate_orders(self, bars: list[Bar]) -> list[OrderIntent]:
      return [OrderIntent(
        symbol=bars[1].symbol,
        side=OrderSide.SELL,
        quantity=100,
        reason="replay",
        time=bars[1].time,
        metadata={"recorded_price": 10.8, "recorded_pnl": 12.5},
      )]

  bars = [
    Bar("202607230930", "688981.SH", 10, 10, 10, 10),
    Bar("202607230935", "688981.SH", 11, 11, 11, 11),
  ]
  engine = BarBacktestEngine(10_000, execution_model=RecordedPriceExecutionModel())

  result = engine.run_batch(RecordedStrategy(), bars)

  assert result.fills[0].time == "202607230935"
  assert result.fills[0].price == 10.8
  assert fill_to_api_trade(result.fills[0])["pnl"] == 0.0


def test_portfolio_keeps_long_and_short_positions_separately() -> None:
  class GrossPositionStrategy:
    def on_bar(
      self,
      bar: Bar,
      portfolio: PortfolioSnapshot,
    ) -> list[OrderIntent]:
      if not portfolio.long_positions and not portfolio.short_positions:
        return [
          OrderIntent(bar.symbol, OrderSide.BUY, 100, "open long", metadata={
            "position_effect": "open_long",
          }, position_key="trend_grid:A"),
          OrderIntent(bar.symbol, OrderSide.SELL, 50, "open short", metadata={
            "position_effect": "open_short",
          }, position_key="trend_grid:B"),
        ]
      return []

  result = BarBacktestEngine(initial_cash=10_000).run(GrossPositionStrategy(), [
    Bar("202607230930", "688981.SH", 10, 10, 10, 10),
  ])

  snapshot = result.portfolio_snapshots[0]
  assert snapshot.long_positions["688981.SH"].quantity == 100
  assert snapshot.short_positions["688981.SH"].quantity == 50
  assert snapshot.positions["688981.SH"].quantity == 50
  assert snapshot.long_positions_by_key["trend_grid:A"].quantity == 100
  assert snapshot.short_positions_by_key["trend_grid:B"].quantity == 50


def test_recorded_price_execution_model_applies_full_transaction_cost() -> None:
  class RecordedRoundTripStrategy:
    def on_bar(
      self,
      bar: Bar,
      portfolio: PortfolioSnapshot,
    ) -> list[OrderIntent]:
      if not portfolio.positions:
        return [OrderIntent(bar.symbol, OrderSide.BUY, 100, "open", metadata={
          "recorded_price": 10,
          "position_effect": "open_long",
        })]
      return [OrderIntent(bar.symbol, OrderSide.SELL, 100, "close", metadata={
        "recorded_price": 11,
        "position_effect": "close_long",
      })]

  result = BarBacktestEngine(
    initial_cash=10_000,
    commission_rate=0.001,
    execution_model=RecordedPriceExecutionModel(),
  ).run(RecordedRoundTripStrategy(), [
    Bar("202607230930", "688981.SH", 10, 10, 10, 10),
    Bar("202607230935", "688981.SH", 11, 11, 11, 11),
  ])

  assert result.fills[0].fee == 1.01
  assert result.fills[1].fee == 1.661
  assert result.final_portfolio.equity == 10_097.329


def test_intraday_policy_blocks_final_bar_entries_and_forces_close() -> None:
  class DayStrategy:
    def on_bar(
      self,
      bar: Bar,
      portfolio: PortfolioSnapshot,
    ) -> list[OrderIntent]:
      if bar.time.endswith("0930"):
        return [OrderIntent(
          bar.symbol, OrderSide.BUY, 100, "open", position_effect=PositionEffect.OPEN_LONG
        )]
      return [OrderIntent(
        bar.symbol, OrderSide.SELL, 100, "late open", position_effect=PositionEffect.OPEN_SHORT
      )]

  result = BarBacktestEngine(initial_cash=10_000, holding_policy="intraday").run(
    DayStrategy(), [
      Bar("202607230930", "688981.SH", 10, 10, 10, 10),
      Bar("202607231455", "688981.SH", 11, 11, 11, 11),
    ]
  )

  assert len(result.fills) == 2
  assert result.fills[-1].metadata["direction"] == "FORCE_SELL"
  assert result.final_portfolio.long_positions == {}
  assert result.final_portfolio.short_positions == {}


def test_trend_grid_strategy_syncs_internal_positions_after_intraday_settlement() -> None:
  class FakeLegacyEngine:
    def __init__(self, _config: dict[str, object]) -> None:
      self.trades_a: list[dict[str, object]] = []
      self.trades_b: list[dict[str, object]] = []
      self.has_open_position = False
      self.settlement_count = 0
      self._entry_locked = False

    def on_bar(self, row: object, _htf_bars: object) -> None:
      time = str(row["time"])
      if time == "202607230930":
        self.has_open_position = True
        self.trades_a.append({
          "time": time,
          "direction": "BUY_OPEN",
          "anchor": 10,
          "price": 10,
          "qty": 200,
          "pnl": 0,
          "note": "open",
        })
      elif time == "202607240930" and self.has_open_position:
        self.trades_a.append({
          "time": time,
          "direction": "SELL_COVER",
          "anchor": 10,
          "price": 10,
          "qty": 200,
          "pnl": 0,
          "note": "stale close",
        })

    def settle_intraday_positions(self) -> None:
      self.has_open_position = False
      self.settlement_count += 1

  strategy = TrendGridBarStrategy(
    {"backtest": {"holding_policy": "intraday"}},
    FakeLegacyEngine,
    day_end_times={"202607231450", "202607240930"},
  )
  result = BarBacktestEngine(initial_cash=10_000, holding_policy="intraday").run(
    strategy,
    [
      Bar("202607230930", "688981.SH", 10, 10, 10, 10),
      Bar("202607231450", "688981.SH", 10, 10, 10, 10),
      Bar("202607240930", "688981.SH", 10, 10, 10, 10),
    ],
  )

  assert [fill.metadata["direction"] for fill in result.fills] == ["BUY_OPEN", "FORCE_SELL"]
  assert strategy._engine is not None
  assert strategy._engine.settlement_count == 2


def test_trend_grid_compatibility_strategy_emits_standard_orders() -> None:
  class FakeLegacyEngine:
    def __init__(self, config: dict[str, object]) -> None:
      self.config = config
      self.trades_a = [{
        "time": "202607230930",
        "direction": "BUY_OPEN",
        "anchor": 9.8,
        "price": 10.1,
        "qty": 200,
        "pnl": 0.0,
        "note": "open",
      }]
      self.trades_b: list[dict[str, object]] = []

    def run(self, bars: object, htf_bars: object) -> None:
      assert len(bars) == 1
      assert htf_bars is None

    def report(self) -> float:
      return 3.5

  strategy = LegacyTrendGridIntentStrategy({}, FakeLegacyEngine)
  bars = [Bar("202607230930", "688981.SH", 10, 10, 10, 10)]

  orders = strategy.generate_orders(bars)

  assert strategy.total_pnl == 3.5
  assert orders[0].side == OrderSide.BUY
  assert orders[0].time == "202607230930"
  assert orders[0].metadata["recorded_price"] == 10.1
  assert orders[0].metadata["position_effect"] == "open_long"


def test_trend_grid_compatibility_strategy_schedules_daily_records() -> None:
  class FakeLegacyEngine:
    trades_a = [{
      "time": "20260723",
      "direction": "SELL_OPEN",
      "anchor": 10.2,
      "price": 10.0,
      "qty": 200,
      "pnl": 0.0,
      "note": "daily",
    }]
    trades_b: list[dict[str, object]] = []

    def __init__(self, config: dict[str, object]) -> None:
      pass

    def run(self, bars: object, htf_bars: object) -> None:
      pass

    def report(self) -> float:
      return 0.0

  strategy = LegacyTrendGridIntentStrategy({}, FakeLegacyEngine)
  orders = strategy.generate_orders([
    Bar("202607230930", "688981.SH", 10, 10, 10, 10),
    Bar("202607231500", "688981.SH", 10, 10, 10, 10),
  ])

  assert orders[0].time == "202607231500"
  assert orders[0].metadata["recorded_time"] == "20260723"
  assert orders[0].metadata["signal_time"] == "202607231500"


def test_trend_grid_on_bar_strategy_emits_order_on_current_bar() -> None:
  class FakeLegacyEngine:
    def __init__(self, config: dict[str, object]) -> None:
      self.trades_a: list[dict[str, object]] = []
      self.trades_b: list[dict[str, object]] = []

    def run(self, bars: object, htf_bars: object, on_bar_complete: object) -> None:
      self.trades_a.append({
        "time": "20260723",
        "direction": "BUY_OPEN",
        "anchor": 9.9,
        "price": 10.2,
        "qty": 200,
        "pnl": 0.0,
        "note": "stream",
      })
      on_bar_complete({"time": "202607230930"})

    def report(self) -> float:
      return 0.0

  strategy = LegacyTrendGridOnBarStrategy({}, FakeLegacyEngine)
  core_engine = BarBacktestEngine(10_000, execution_model=RecordedPriceExecutionModel())
  result = core_engine.run_stream(strategy, [
    Bar("202607230930", "688981.SH", 10, 10.3, 9.9, 10),
  ])

  assert result.fills[0].time == "202607230930"
  assert result.fills[0].price == 10.2
  assert fill_to_api_trade(result.fills[0])["time"] == "202607230930"


def test_trend_grid_bar_strategy_emits_orders_per_completed_bar() -> None:
  class FakeLegacyEngine:
    def __init__(self, config: dict[str, object]) -> None:
      self.trades_a: list[dict[str, object]] = []
      self.trades_b: list[dict[str, object]] = []

    def on_bar(self, row: object, htf_bars: object) -> None:
      self.trades_a.append({
        "time": "202607230930",
        "direction": "BUY_OPEN",
        "anchor": 9.9,
        "price": 10.2,
        "qty": 200,
        "pnl": 0.0,
        "note": "stream",
      })

  strategy = TrendGridBarStrategy({}, FakeLegacyEngine)
  bar = Bar("202607230930", "688981.SH", 10, 10.3, 9.9, 10)
  orders = strategy.on_bar(bar, PortfolioSnapshot(10_000, 10_000, {}))

  assert orders[0].time == bar.time
  assert orders[0].position_key == "trend_grid:A"
  assert orders[0].position_effect == PositionEffect.OPEN_LONG
