"""L2 数据源与撮合器之间共享的标准模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from backend.backtesting.models import OrderIntent, OrderSide


def parse_timestamp(value: str) -> datetime:
  """解析 API 与 ClickHouse 统一使用的本地时间字符串。"""
  normalized = value.replace("T", " ")
  try:
    return datetime.fromisoformat(normalized)
  except ValueError as error:
    raise ValueError(f"无效的 L2 时间戳: {value}") from error


@dataclass(frozen=True)
class SourceMappingProfile:
  """受控部署配置中的外部表字段映射。"""

  source_name: str
  table_name: str
  columns: dict[str, str]
  source_version: str | None = None
  price_scale: float = 1.0
  price_scales: dict[str, float] = field(default_factory=dict)
  volume_scale: float = 1.0
  amount_scale: float = 1.0
  time_semantics: str = "event_time"
  quality_column: str | None = None
  trade_value: str | None = None

  def __post_init__(self) -> None:
    if not self.source_name.strip() or not self.table_name.strip():
      raise ValueError("数据源名称和表名不能为空")
    if min(self.price_scale, self.volume_scale, self.amount_scale, *self.price_scales.values()) <= 0:
      raise ValueError("数据缩放系数必须大于 0")


@dataclass(frozen=True)
class NormalizedBar:
  """已完成 Bar；结束时间是触发 on_bar 的唯一时间点。"""

  symbol: str
  trade_date: str
  period: str
  start_time: str
  end_time: str
  open: float
  high: float
  low: float
  close: float
  volume: float
  amount: float
  quality: str
  source_version: str
  snapshot_count: int = 0
  max_gap_ms: int = 0
  valid_price_count: int = 0
  actual_duration_seconds: int = 0

  def __post_init__(self) -> None:
    if min(self.open, self.high, self.low, self.close) <= 0:
      raise ValueError("Bar 价格必须大于 0")
    if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
      raise ValueError("Bar OHLC 关系无效")
    if parse_timestamp(self.start_time) >= parse_timestamp(self.end_time):
      raise ValueError("Bar 开始时间必须早于结束时间")


@dataclass(frozen=True)
class QuoteLevel:
  """盘口单档报价。"""

  price: float
  volume: int

  def __post_init__(self) -> None:
    if self.price <= 0 or self.volume < 0:
      raise ValueError("盘口价格必须大于 0，数量不能小于 0")


@dataclass(frozen=True)
class QuoteSnapshot:
  """十档盘口快照。"""

  symbol: str
  trade_date: str
  time: str
  sequence: int
  bids: tuple[QuoteLevel, ...]
  asks: tuple[QuoteLevel, ...]
  last_price: float | None = None
  source_version: str = ""

  def __post_init__(self) -> None:
    if not self.bids and not self.asks:
      raise ValueError("十档快照至少需要一侧有效盘口")
    if self.sequence < 0:
      raise ValueError("快照序号不能小于 0")


@dataclass(frozen=True)
class TradeEvent:
  """用于确认被动限价成交的逐笔成交事件。"""

  symbol: str
  trade_date: str
  time: str
  sequence: int
  trade_id: str
  price: float
  volume: int
  side: str
  source_version: str = ""

  def __post_init__(self) -> None:
    if self.price <= 0 or self.volume <= 0:
      raise ValueError("逐笔成交价格和数量必须大于 0")
    if not self.trade_id.strip() or self.sequence < 0:
      raise ValueError("逐笔成交必须具有稳定序号")


@dataclass(frozen=True)
class CoverageSnapshot:
  """某个 L2 数据源在请求范围内的完整性与版本。"""

  source_name: str
  symbol: str
  start_date: str
  end_date: str
  source_version: str | None
  complete: bool
  warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstrumentRules:
  """回测所需的最小标的规则快照。"""

  tick_size: float = 0.01
  lot_size: int = 100
  min_order_quantity: int | None = None
  quantity_step: int | None = None
  lower_limit: float | None = None
  upper_limit: float | None = None
  halted: bool = False
  allow_short: bool = False

  def __post_init__(self) -> None:
    if self.tick_size <= 0 or self.lot_size <= 0:
      raise ValueError("最小变动价位和交易单位必须大于 0")
    if self.minimum_order_quantity <= 0 or self.order_quantity_step <= 0:
      raise ValueError("最小委托数量和数量递增单位必须大于 0")

  @property
  def minimum_order_quantity(self) -> int:
    return self.min_order_quantity if self.min_order_quantity is not None else self.lot_size

  @property
  def order_quantity_step(self) -> int:
    return self.quantity_step if self.quantity_step is not None else self.lot_size

  def is_valid_open_quantity(self, quantity: int) -> bool:
    return quantity >= self.minimum_order_quantity and (
      quantity - self.minimum_order_quantity
    ) % self.order_quantity_step == 0


@dataclass
class L2Order:
  """L2 撮合中的未完成订单。"""

  order_id: str
  intent: OrderIntent
  effective_at: datetime
  remaining_quantity: int
  created_sequence: int
  status: str = "pending"
  filled_quantity: int = 0
  metadata: dict[str, Any] = field(default_factory=dict)

  @property
  def side(self) -> OrderSide:
    return self.intent.side

  @property
  def is_market(self) -> bool:
    return self.intent.limit_price is None
