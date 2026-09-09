"""所有回测模式共用的日终结算契约。"""

from __future__ import annotations

from typing import Callable, Protocol, TypeVar


class SettlementBar(Protocol):
  """日终估值所需的最小 Bar 契约。"""

  @property
  def symbol(self) -> str:
    """返回结算标的。"""
    ...

  @property
  def close(self) -> float:
    """返回日终结算价。"""
    ...


class SettlementLedger(Protocol):
  """日终重新估值所需的最小账本契约。"""

  def mark(self, symbol: str, price: float) -> None:
    """使用日终结算价重新标记持仓。"""
    ...


SnapshotT = TypeVar("SnapshotT")


def settle_daily_portfolio(
  *,
  last_bar: SettlementBar | None,
  ledger: SettlementLedger,
  complete_trades: Callable[[], None],
  snapshot: Callable[[], SnapshotT],
  missing_bar_error: str = "当天无有效 Bar，无法进行日终结算",
) -> SnapshotT:
  """完成模式特有成交后，统一按最后 Bar 收盘价生成日终快照。"""
  if last_bar is None:
    raise ValueError(missing_bar_error)
  complete_trades()
  ledger.mark(last_bar.symbol, last_bar.close)
  return snapshot()
