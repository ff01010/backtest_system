"""将策略母单推进为内部子单，并聚合可追溯执行结果。"""

from __future__ import annotations

from datetime import datetime
from dataclasses import replace
from typing import Any
from uuid import uuid4

from backend.backtesting.models import OrderIntent
from backend.execution.contract import ChildOrder, ExecutionContext, ExecutionPlan, ParentOrder
from backend.execution.registry import get_execution_algorithm


class ExecutionRuntime:
  """单个回测会话内的执行算法状态机。"""

  def __init__(self, algorithm_id: str, parameters: dict[str, Any], context: ExecutionContext) -> None:
    self._algorithm = get_execution_algorithm(algorithm_id)
    self._context = context
    self._parameters = dict(parameters)
    self._plans: dict[str, ExecutionPlan] = {}
    self._cancelled_parent_ids: set[str] = set()

  def begin_day(self, session_end: datetime) -> None:
    self._context = ExecutionContext(self._context.fill_model, self._context.instrument_rules, session_end)

  def submit(self, intent: OrderIntent, signal_at: datetime) -> list[ChildOrder]:
    parent = ParentOrder(str(uuid4()), intent, signal_at, self._algorithm.algorithm_id, self._parameters)
    plan = self._algorithm.create_plan(parent, self._context)
    self._plans[parent.parent_order_id] = plan
    return [self._identify(child, parent) for child in self._algorithm.on_market_event(plan, signal_at, self._context)]

  def on_quote(self, event_time: datetime) -> list[ChildOrder]:
    children: list[ChildOrder] = []
    for parent_order_id, plan in self._plans.items():
      previous_status = plan.status
      children.extend(self._identify(child, plan.parent) for child in self._algorithm.on_market_event(plan, event_time, self._context))
      if previous_status != "cancelled" and plan.status == "cancelled":
        self._cancelled_parent_ids.add(parent_order_id)
    return children

  def on_fill(self, parent_order_id: str | None, quantity: int) -> None:
    plan = self._plans.get(parent_order_id or "")
    if plan is None:
      return
    plan.filled_quantity += quantity
    if plan.remaining_quantity == 0:
      plan.status = "filled"

  def on_rejection(self, parent_order_id: str | None, quantity: int, reason: str) -> None:
    plan = self._plans.get(parent_order_id or "")
    if plan is None:
      return
    plan.rejected_quantity += quantity
    plan.reason = reason
    if plan.remaining_quantity == 0:
      plan.status = "rejected"

  def cancel_open(self, parent_order_id: str | None, quantity: int, reason: str) -> None:
    plan = self._plans.get(parent_order_id or "")
    if plan is None or plan.status in {"cancelled", "filled", "rejected"}:
      return
    plan.cancelled_quantity += quantity
    plan.reason = reason
    if plan.remaining_quantity == 0:
      plan.status = "cancelled"

  def has_pending_work(self) -> bool:
    return any(plan.status in {"pending", "submitted"} and plan.remaining_quantity > 0 for plan in self._plans.values())

  def next_event_after(self) -> datetime | None:
    values = [plan.scheduled_at[plan.next_index] for plan in self._plans.values() if plan.status == "pending" and plan.next_index < len(plan.scheduled_at)]
    values.extend(plan.deadline_at for plan in self._plans.values() if plan.status == "pending" and plan.deadline_at is not None)
    return min(values) if values else None

  def cancel_all(self, reason: str) -> None:
    for plan in self._plans.values():
      if plan.status in {"pending", "submitted"} and plan.remaining_quantity > 0:
        plan.cancelled_quantity += plan.remaining_quantity
        plan.status = "cancelled"
        plan.reason = reason

  def consume_cancelled_parent_ids(self) -> set[str]:
    parent_order_ids = set(self._cancelled_parent_ids)
    self._cancelled_parent_ids.clear()
    return parent_order_ids

  def result(self) -> list[dict[str, Any]]:
    return [self._serialize(plan) for plan in self._plans.values()]

  @staticmethod
  def _identify(child: ChildOrder, parent: ParentOrder) -> ChildOrder:
    metadata = {
      **child.intent.metadata,
      "parent_order_id": parent.parent_order_id,
      "child_index": child.child_index,
      "parent_signal_time": parent.signal_at.isoformat(sep=" "),
      "execution_algorithm": parent.algorithm_id,
    }
    return replace(child, intent=replace(child.intent, metadata=metadata))

  @staticmethod
  def _serialize(plan: ExecutionPlan) -> dict[str, Any]:
    return {
      "parentOrderId": plan.parent.parent_order_id,
      "algorithm": plan.parent.algorithm_id,
      "signalTime": plan.parent.signal_at.isoformat(sep=" "),
      "action": plan.parent.intent.metadata.get("direction", ""),
      "requestedQuantity": plan.requested_quantity,
      "filledQuantity": plan.filled_quantity,
      "cancelledQuantity": plan.cancelled_quantity,
      "rejectedQuantity": plan.rejected_quantity,
      "remainingQuantity": plan.remaining_quantity,
      "status": plan.status,
      "reason": plan.reason,
      "plannedChildCount": len(plan.planned_quantities),
    }
