"""与策略无关的权益曲线统计。"""

from __future__ import annotations

import math
from typing import Sequence

from backend.backtesting.models import EquityPoint

_TRADING_DAYS_PER_YEAR = 242
_ANNUAL_RISK_FREE_RATE = 0.0


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

  daily_risk_free_rate = (1 + _ANNUAL_RISK_FREE_RATE) ** (1 / _TRADING_DAYS_PER_YEAR) - 1
  average_return = sum(returns) / len(returns) if returns else 0.0
  average_excess_return = sum(value - daily_risk_free_rate for value in returns) / len(returns) if returns else 0.0
  volatility = _sample_std(returns, average_return)
  final_equity = values[-1]
  return {
    "totalPnl": round(final_equity - initial_cash, 8),
    "totalReturn": round(final_equity / initial_cash - 1, 8),
    "maxDrawdownRate": round(max_drawdown_rate, 8),
    "annualVolatility": round(volatility * math.sqrt(_TRADING_DAYS_PER_YEAR), 8),
    "annualSharpe": round(
      average_excess_return / volatility * math.sqrt(_TRADING_DAYS_PER_YEAR) if volatility > 0 else 0.0,
      8,
    ),
  }


def _sample_std(values: Sequence[float], average: float) -> float:
  if len(values) <= 1:
    return 0.0
  variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
  return math.sqrt(variance)
