"""通用回测领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class OrderSide(str, Enum):
  """订单方向。"""

  BUY = "BUY"
  SELL = "SELL"


class PositionEffect(str, Enum):
  """订单对指定策略仓位的影响。"""

  OPEN_LONG = "open_long"
  CLOSE_LONG = "close_long"
  OPEN_SHORT = "open_short"
  CLOSE_SHORT = "close_short"


@dataclass(frozen=True)
class Bar:
  """标准化 K 线事件。"""

  time: str
  symbol: str
  open: float
  high: float
  low: float
  close: float
  volume: float = 0.0

  @property
  def date(self) -> str:
    return self.time[:8]


@dataclass(frozen=True)
class OrderIntent:
  """策略产生的交易意图，不包含账户变更。"""

  symbol: str
  side: OrderSide
  quantity: int
  reason: str
  limit_price: float | None = None
  time: str | None = None
  position_effect: PositionEffect | None = None
  position_key: str = "default"
  metadata: dict[str, Any] = field(default_factory=dict)

  def __post_init__(self) -> None:
    if self.quantity <= 0:
      raise ValueError("订单数量必须大于 0")
    if self.limit_price is not None and self.limit_price <= 0:
      raise ValueError("限价必须大于 0")
    if not self.position_key.strip():
      raise ValueError("仓位归属不能为空")


@dataclass(frozen=True)
class Fill:
  """内核产生的成交记录。"""

  time: str
  symbol: str
  side: OrderSide
  quantity: int
  price: float
  fee: float
  reason: str
  position_effect: PositionEffect | None = None
  position_key: str = "default"
  realized_pnl: float = 0.0
  metadata: dict[str, Any] = field(default_factory=dict)

  def __post_init__(self) -> None:
    if self.quantity <= 0:
      raise ValueError("成交数量必须大于 0")
    if self.price <= 0:
      raise ValueError("成交价格必须大于 0")


@dataclass(frozen=True)
class PositionSnapshot:
  """策略可读取的仓位快照。"""

  symbol: str
  quantity: int
  average_price: float


@dataclass(frozen=True)
class PortfolioSnapshot:
  """策略可读取的账户快照。"""

  cash: float
  equity: float
  positions: dict[str, PositionSnapshot]
  long_positions: dict[str, PositionSnapshot] = field(default_factory=dict)
  short_positions: dict[str, PositionSnapshot] = field(default_factory=dict)
  long_positions_by_key: dict[str, PositionSnapshot] = field(default_factory=dict)
  short_positions_by_key: dict[str, PositionSnapshot] = field(default_factory=dict)
  realized_pnl: float = 0.0


@dataclass(frozen=True)
class EquityPoint:
  """回测过程中的权益快照。"""

  time: str
  equity: float
  cash: float


@dataclass(frozen=True)
class BacktestResult:
  """通用内核的标准回测结果。"""

  fills: list[Fill]
  equity_curve: list[EquityPoint]
  final_portfolio: PortfolioSnapshot
  portfolio_snapshots: list[PortfolioSnapshot] = field(default_factory=list)
