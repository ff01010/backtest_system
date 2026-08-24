"""保持既有语义的立即成交执行算法。"""

from __future__ import annotations

from datetime import datetime

from backend.execution.contract import ChildOrder, ExecutionAlgorithm, ExecutionContext, ExecutionPlan, ParentOrder


class ImmediateExecutionAlgorithm(ExecutionAlgorithm):
  """母单立即进入既有撮合队列。"""

  algorithm_id = "immediate"
  version = "immediate-v1"

  def create_plan(self, parent: ParentOrder, context: ExecutionContext) -> ExecutionPlan:
    return ExecutionPlan(parent=parent, planned_quantities=[parent.intent.quantity], scheduled_at=[parent.signal_at])

  def on_market_event(self, plan: ExecutionPlan, event_time: datetime, context: ExecutionContext) -> list[ChildOrder]:
    if plan.status != "pending" or plan.next_index != 0:
      return []
    plan.next_index = 1
    plan.status = "submitted"
    return [ChildOrder(plan.parent.parent_order_id, 0, plan.parent.signal_at, plan.parent.intent)]
