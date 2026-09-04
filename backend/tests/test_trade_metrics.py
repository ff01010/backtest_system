"""方向性完整闭环指标测试。"""

from backend.backtesting.models import PositionEffect
from backend.backtesting.trade_metrics import ClosedTradeFill, build_closed_trade_metrics


def _fill(effect: PositionEffect, quantity: int, pnl: float = 0.0) -> ClosedTradeFill:
  return ClosedTradeFill("688981.SH", "script:test", effect, quantity, pnl)


def test_partial_closes_count_as_one_completed_directional_trade() -> None:
  metrics = build_closed_trade_metrics([
    _fill(PositionEffect.OPEN_LONG, 1_000),
    _fill(PositionEffect.CLOSE_LONG, 400, 80),
    _fill(PositionEffect.CLOSE_LONG, 600, -30),
  ])

  assert metrics["completedTradeCount"] == 1
  assert metrics["winningTrades"] == 1
  assert metrics["winRate"] == 1.0
  assert metrics["avgWin"] == 50.0


def test_open_directional_position_is_excluded_from_win_rate() -> None:
  metrics = build_closed_trade_metrics([
    _fill(PositionEffect.OPEN_LONG, 1_000),
    _fill(PositionEffect.CLOSE_LONG, 400, 80),
  ])

  assert metrics["completedTradeCount"] == 0
  assert metrics["openTradeCount"] == 1
  assert metrics["winRate"] == 0.0


def test_initial_directional_position_can_complete_one_trade() -> None:
  metrics = build_closed_trade_metrics([
    _fill(PositionEffect.CLOSE_LONG, 600, 120),
    _fill(PositionEffect.CLOSE_LONG, 400, -20),
  ], initial_long_quantity=1_000)

  assert metrics["completedTradeCount"] == 1
  assert metrics["winningTrades"] == 1
  assert metrics["avgWin"] == 100.0
