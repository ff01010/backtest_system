"""执行算法与撮合引擎之间的稳定数据契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from backend.backtesting.models import OrderIntent

if TYPE_CHECKING:
  from backend.l2.models import InstrumentRules


@dataclass(frozen=True)
class ExecutionContext:
  """执行计划运行所需的受限市场与规则上下文。"""

  fill_model: str
  instrument_rules: "InstrumentRules"
  session_end: datetime


@dataclass(frozen=True)
class ParentOrder:
  """策略产生的一次交易意图。"""

  parent_order_id: str
  intent: OrderIntent
  signal_at: datetime
  algorithm_id: str
  parameters: dict[str, Any]


@dataclass(frozen=True)
class ChildOrder:
  """执行算法产生、等待既有撮合器处理的内部委托。"""

  parent_order_id: str
  child_index: int
  scheduled_at: datetime
  intent: OrderIntent


@dataclass
class ExecutionPlan:
  """单个母单在运行期的可序列化状态。"""

  parent: ParentOrder
  planned_quantities: list[int]
  scheduled_at: list[datetime]
  deadline_at: datetime | None = None
  status: str = "pending"
  next_index: int = 0
  filled_quantity: int = 0
  cancelled_quantity: int = 0
  rejected_quantity: int = 0
  child_order_ids: list[str] = field(default_factory=list)
  reason: str = ""

  @property
  def requested_quantity(self) -> int:
    return self.parent.intent.quantity

  @property
  def remaining_quantity(self) -> int:
    return max(0, self.requested_quantity - self.filled_quantity - self.cancelled_quantity - self.rejected_quantity)


class ExecutionAlgorithm(Protocol):
  """所有执行算法的最小协议。"""

  algorithm_id: str
  version: str

  def create_plan(self, parent: ParentOrder, context: ExecutionContext) -> ExecutionPlan:
    """将母单转换为执行计划。"""
    ...

  def on_market_event(self, plan: ExecutionPlan, event_time: datetime, context: ExecutionContext) -> list[ChildOrder]:
    """根据一个已经可见的市场事件产生本次子单。"""
    ...
