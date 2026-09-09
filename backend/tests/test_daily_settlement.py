"""统一日终结算契约测试。"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from backend.backtesting import settle_daily_portfolio
from backend.backtesting.engine import BarBacktestEngine, PortfolioLedger, RecordedPriceExecutionModel
from backend.backtesting.fees import FeeProfile
from backend.backtesting.models import Bar, Fill, OrderIntent, OrderSide, PortfolioSnapshot, PositionEffect


def test_future_strategy_settlement_uses_bar_close_after_its_fill() -> None:
  ledger = PortfolioLedger(initial_cash=10_000)
  ledger.initialize_long_position("688981.SH", 100, 10, "future:base")
  last_bar = Bar("202607231500", "688981.SH", 10, 10, 10, 10)

  def complete_future_strategy_trades() -> None:
    ledger.apply_fill(Fill(
      last_bar.time,
      last_bar.symbol,
      OrderSide.SELL,
      100,
      9.9,
      0,
      "未来策略卖出底仓",
      PositionEffect.CLOSE_LONG,
      "future:base",
    ))
    ledger.apply_fill(Fill(
      last_bar.time,
      last_bar.symbol,
      OrderSide.BUY,
      100,
      10.1,
      0,
      "未来策略恢复底仓",
      PositionEffect.OPEN_LONG,
      "future:base",
    ))

  snapshot_count = 0

  def create_snapshot() -> PortfolioSnapshot:
    nonlocal snapshot_count
    snapshot_count += 1
    return ledger.snapshot()

  snapshot = settle_daily_portfolio(
    last_bar=last_bar,
    ledger=ledger,
    complete_trades=complete_future_strategy_trades,
    snapshot=create_snapshot,
  )

  assert [fill.price for fill in ledger.fills] == [9.9, 10.1]
  assert snapshot.cash == 9_980
  assert snapshot.equity == 10_980
  assert snapshot_count == 1


def test_daily_settlement_rejects_missing_bar_before_trades() -> None:
  ledger = PortfolioLedger(initial_cash=10_000)
  trades_completed = False

  def complete_trades() -> None:
    nonlocal trades_completed
    trades_completed = True

  with pytest.raises(ValueError, match="当天无有效 Bar，无法进行日终结算"):
    settle_daily_portfolio(
      last_bar=None,
      ledger=ledger,
      complete_trades=complete_trades,
      snapshot=ledger.snapshot,
    )

  assert trades_completed is False


def _daily_sell_order(bar: Bar) -> OrderIntent:
  return OrderIntent(
    bar.symbol,
    OrderSide.SELL,
    100,
    "卖出底仓",
    time=bar.time,
    position_effect=PositionEffect.OPEN_SHORT,
    metadata={"recorded_price": bar.close},
  )


class _DailySellBatchStrategy:
  def generate_orders(self, bars: Sequence[Bar]) -> list[OrderIntent]:
    return [_daily_sell_order(bar) for bar in bars]


class _DailySellStreamingStrategy:
  def stream(
    self,
    bars: Sequence[Bar],
    emit: Callable[[Bar, Sequence[OrderIntent]], None],
  ) -> None:
    for bar in bars:
      emit(bar, [_daily_sell_order(bar)])


class _NoOrdersBarStrategy:
  def on_bar(self, bar: Bar, portfolio: PortfolioSnapshot) -> list[OrderIntent]:
    return []


def _settlement_engine() -> BarBacktestEngine:
  return BarBacktestEngine(
    initial_cash=100_000,
    fee_profile=FeeProfile(minimum_commission=0),
    execution_model=RecordedPriceExecutionModel(slippage=0.01),
    base_quantity=100,
    base_cost_price=10,
    t0_enabled=True,
    strategy_category="core_position_overlay",
  )


def _two_daily_bars() -> list[Bar]:
  return [
    Bar("202607231500", "688981.SH", 10, 10, 10, 10),
    Bar("202607241500", "688981.SH", 12, 12, 12, 12),
  ]


def test_batch_path_marks_each_trading_day_at_bar_close() -> None:
  result = _settlement_engine().run_batch(_DailySellBatchStrategy(), _two_daily_bars())

  assert [fill.price for fill in result.fills] == pytest.approx([9.9, 10.1, 11.88, 12.12])
  assert [fill.fee for fill in result.fills] == [0.5841, 0.0909, 0.70092, 0.10908]
  assert [snapshot.equity for snapshot in result.portfolio_snapshots] == [100_979.325, 101_154.515]
  assert result.final_portfolio == result.portfolio_snapshots[-1]


def test_streaming_path_marks_each_trading_day_at_bar_close() -> None:
  result = _settlement_engine().run_stream(_DailySellStreamingStrategy(), _two_daily_bars())

  assert [fill.price for fill in result.fills] == pytest.approx([9.9, 10.1, 11.88, 12.12])
  assert [fill.fee for fill in result.fills] == [0.5841, 0.0909, 0.70092, 0.10908]
  assert [snapshot.equity for snapshot in result.portfolio_snapshots] == [100_979.325, 101_154.515]
  assert result.final_portfolio == result.portfolio_snapshots[-1]


def test_standard_bar_paths_reject_missing_formal_bar() -> None:
  warmup_bar = Bar("202607221500", "688981.SH", 10, 10, 10, 10)

  with pytest.raises(ValueError, match="无有效 Bar，无法进行日终结算"):
    BarBacktestEngine(10_000).run(_NoOrdersBarStrategy(), [warmup_bar], warmup_bar_count=1)
  with pytest.raises(ValueError, match="无有效 Bar，无法进行日终结算"):
    BarBacktestEngine(10_000).run_batch(_DailySellBatchStrategy(), [])
  with pytest.raises(ValueError, match="无有效 Bar，无法进行日终结算"):
    BarBacktestEngine(10_000).run_stream(_DailySellStreamingStrategy(), [])
