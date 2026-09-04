"""方向性持仓的完整闭环交易指标。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from backend.backtesting.models import PositionEffect


_EPSILON = 1e-8


@dataclass(frozen=True)
class ClosedTradeFill:
  """计算方向性闭环所需的最小成交字段。"""

  symbol: str
  position_key: str
  position_effect: PositionEffect | None
  quantity: int
  realized_pnl: float


@dataclass
class _OpenPosition:
  quantity: int = 0
  realized_pnl: float = 0.0


def build_closed_trade_metrics(
  fills: Iterable[ClosedTradeFill],
  initial_long_quantity: int = 0,
) -> dict[str, float | int | None]:
  """按同一持仓键从建立到归零计算方向性完整闭环。"""
  positions: dict[tuple[str, str, str], _OpenPosition] = {}
  completed_pnl: list[float] = []
  initial_remaining = max(0, initial_long_quantity)

  for fill in fills:
    effect = fill.position_effect
    if effect in {PositionEffect.OPEN_LONG, PositionEffect.OPEN_SHORT}:
      direction = "long" if effect == PositionEffect.OPEN_LONG else "short"
      key = (fill.symbol, fill.position_key, direction)
      positions.setdefault(key, _OpenPosition()).quantity += fill.quantity
      continue
    if effect not in {PositionEffect.CLOSE_LONG, PositionEffect.CLOSE_SHORT}:
      continue
    direction = "long" if effect == PositionEffect.CLOSE_LONG else "short"
    key = (fill.symbol, fill.position_key, direction)
    position = positions.setdefault(key, _OpenPosition())
    if direction == "long" and position.quantity == 0 and initial_remaining > 0:
      position.quantity = initial_remaining
      initial_remaining = 0
    if fill.quantity > position.quantity:
      raise ValueError("方向性平仓成交超过待闭环持仓")
    position.quantity -= fill.quantity
    position.realized_pnl += fill.realized_pnl
    if position.quantity == 0:
      completed_pnl.append(position.realized_pnl)
      positions.pop(key)

  wins = [value for value in completed_pnl if value > _EPSILON]
  losses = [value for value in completed_pnl if value < -_EPSILON]
  breakeven = len(completed_pnl) - len(wins) - len(losses)
  return {
    "winRate": round(len(wins) / (len(wins) + len(losses)), 4) if wins or losses else 0.0,
    "winningTrades": len(wins),
    "losingTrades": len(losses),
    "breakevenTrades": breakeven,
    "avgWin": round(sum(wins) / len(wins), 2) if wins else None,
    "avgLoss": round(sum(losses) / len(losses), 2) if losses else None,
    "profitLossRatio": round(abs(sum(wins) / sum(losses)), 4) if wins and losses else None,
    "completedTradeCount": len(completed_pnl),
    "openTradeCount": len(positions) + (1 if initial_remaining > 0 else 0),
    "tradeMetricVersion": 3,
  }
