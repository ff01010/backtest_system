"""基于有效交易秒和三秒盘口的受限 TWAP 执行算法。"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from backend.backtesting.models import OrderIntent
from backend.execution.contract import ChildOrder, ExecutionAlgorithm, ExecutionContext, ExecutionPlan, ParentOrder


_MORNING_START = time(9, 30)
_MORNING_END = time(11, 30)
_AFTERNOON_START = time(13, 0)


class TwapExecutionAlgorithm(ExecutionAlgorithm):
  """在有效交易窗口中均匀提交子单，窗口结束后取消余额。"""

  algorithm_id = "twap"
  version = "twap-l2-v1"

  def create_plan(self, parent: ParentOrder, context: ExecutionContext) -> ExecutionPlan:
    duration = int(parent.parameters["durationSeconds"])
    interval = int(parent.parameters["intervalSeconds"])
    available = _trading_seconds_between(parent.signal_at, context.session_end)
    actual_duration = min(duration, available)
    if actual_duration <= 0:
      return ExecutionPlan(parent, [], [], status="cancelled", cancelled_quantity=parent.intent.quantity, reason="收盘前无可用执行窗口")
    deadline = _add_trading_seconds(parent.signal_at, actual_duration)
    scheduled = _schedule(parent.signal_at, actual_duration, interval, context.session_end)
    quantities = _split_quantities(parent.intent.quantity, len(scheduled), int(parent.parameters["minChildQuantity"]), context)
    return ExecutionPlan(parent, quantities, scheduled, deadline_at=deadline)

  def on_market_event(self, plan: ExecutionPlan, event_time: datetime, context: ExecutionContext) -> list[ChildOrder]:
    if plan.status != "pending" or event_time <= plan.parent.signal_at:
      return []
    if plan.deadline_at is not None and event_time >= plan.deadline_at:
      plan.status = "cancelled"
      plan.cancelled_quantity += plan.remaining_quantity
      plan.reason = "执行窗口结束，取消剩余数量"
      return []
    if event_time < plan.scheduled_at[plan.next_index]:
      return []
    index = plan.next_index
    plan.next_index += 1
    return [ChildOrder(plan.parent.parent_order_id, index, plan.scheduled_at[index], _child_intent(plan, index))]


def _child_intent(plan: ExecutionPlan, index: int) -> OrderIntent:
  intent = plan.parent.intent
  metadata = {**intent.metadata, "parent_order_id": plan.parent.parent_order_id, "child_index": index, "execution_algorithm": "twap"}
  return type(intent)(intent.symbol, intent.side, plan.planned_quantities[index], intent.reason, intent.limit_price, intent.time, intent.position_effect, intent.position_key, metadata)


def _schedule(start: datetime, duration: int, interval: int, session_end: datetime) -> list[datetime]:
  offsets = list(range(0, duration, interval))
  return [point for point in (_add_trading_seconds(start, offset) for offset in offsets) if point <= session_end]


def _split_quantities(quantity: int, slots: int, minimum: int, context: ExecutionContext) -> list[int]:
  if slots <= 0:
    return []
  step = context.instrument_rules.order_quantity_step
  minimum = max(minimum, context.instrument_rules.minimum_order_quantity)
  usable_slots = min(slots, max(1, quantity // minimum))
  base = max(minimum, (quantity // usable_slots // step) * step)
  quantities = [base] * (usable_slots - 1)
  quantities.append(quantity - sum(quantities))
  return quantities


def _add_trading_seconds(start: datetime, seconds: int) -> datetime:
  current = _next_trading_time(start)
  remaining = seconds
  while remaining > 0:
    end = _session_end(current)
    available = int((end - current).total_seconds())
    if remaining <= available:
      return current + timedelta(seconds=remaining)
    remaining -= available
    current = datetime.combine(current.date(), _AFTERNOON_START)
  return current


def _next_trading_time(value: datetime) -> datetime:
  morning = datetime.combine(value.date(), _MORNING_START)
  lunch_end = datetime.combine(value.date(), _AFTERNOON_START)
  if value < morning:
    return morning
  if value.time() > time(11, 30) and value < lunch_end:
    return lunch_end
  return value


def _session_end(value: datetime) -> datetime:
  return datetime.combine(value.date(), time(11, 30) if value.time() < _AFTERNOON_START else time(14, 57))


def _trading_seconds_between(start: datetime, end: datetime) -> int:
  current = _next_trading_time(start)
  if current >= end:
    return 0
  total = 0
  while current < end:
    boundary = min(_session_end(current), end)
    total += max(0, int((boundary - current).total_seconds()))
    if boundary >= end or boundary.time() >= time(14, 57):
      break
    current = datetime.combine(current.date(), _AFTERNOON_START)
  return total
