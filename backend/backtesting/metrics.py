"""与策略无关的权益曲线统计。"""

from __future__ import annotations

import math
from typing import Sequence

from backend.backtesting.models import EquityPoint


def compute_performance_metrics(
  initial_cash: float,
  equity_curve: Sequence[EquityPoint],
) -> dict[str, float]:
  """根据权益曲线计算收益、回撤、波动率和夏普。"""
  if initial_cash <= 0:
    raise ValueError("初始资金必须大于 0")
  if not equity_curve:
    return {
      "totalPnl": 0.0,
      "totalReturn": 0.0,
      "maxDrawdownRate": 0.0,
      "annualVolatility": 0.0,
      "annualSharpe": 0.0,
    }

  values = [initial_cash] + [point.equity for point in equity_curve]
  returns = [
    values[index] / values[index - 1] - 1
    for index in range(1, len(values))
    if values[index - 1] > 0
  ]
  peak = initial_cash
  max_drawdown_rate = 0.0
  for value in values[1:]:
    peak = max(peak, value)
    if peak > 0:
      max_drawdown_rate = max(max_drawdown_rate, (peak - value) / peak)

  average_return = sum(returns) / len(returns) if returns else 0.0
  variance = (
    sum((value - average_return) ** 2 for value in returns) / len(returns)
    if returns else 0.0
  )
  volatility = math.sqrt(variance)
  final_equity = values[-1]
  return {
    "totalPnl": round(final_equity - initial_cash, 8),
    "totalReturn": round(final_equity / initial_cash - 1, 8),
    "maxDrawdownRate": round(max_drawdown_rate, 8),
    "annualVolatility": round(volatility * math.sqrt(242), 8),
    "annualSharpe": round(
      average_return / volatility * math.sqrt(242) if volatility > 0 else 0.0,
      8,
    ),
  }
