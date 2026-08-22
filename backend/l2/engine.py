"""按 Bar 触发策略、按 L2 盘口撮合的基础执行引擎。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from typing import Any, Sequence
from uuid import uuid4

from backend.backtesting.engine import PortfolioLedger
from backend.backtesting.fees import FeeProfile
from backend.backtesting.models import Bar, Fill, OrderIntent, OrderSide, PositionEffect
from backend.execution.contract import ChildOrder, ExecutionContext
from backend.execution.runtime import ExecutionRuntime
from backend.l2.models import InstrumentRules, L2Order, NormalizedBar, QuoteLevel, QuoteSnapshot, TradeEvent, parse_timestamp

_STAMP_DUTY_RATE = 0.0005
_TRANSFER_FEE_RATE = 0.00001
_BASE_POSITION_KEY = "base_position"
_OVERLAY_LONG_SUFFIX = ":overlay_long"
_OVERLAY_SHORT_SUFFIX = ":overlay_short"


class L2ExecutionEngine:
  """基础 L2 回放；不试图重建交易所真实队列。"""

  def __init__(
    self,
    strategy: Any,
    initial_cash: float,
    commission_rate: float,
    rules: InstrumentRules,
    latency_ms: int = 0,
    passive_participation_rate: float = 0.1,
    holding_policy: str = "intraday",
    base_quantity: int = 0,
    base_cost_price: float = 0.0,
    t0_enabled: bool = False,
    fee_profile: FeeProfile | None = None,
    execution_algorithm: str = "immediate",
    execution_parameters: dict[str, Any] | None = None,
  ) -> None:
    if latency_ms < 0 or not 0 < passive_participation_rate <= 1:
      raise ValueError("L2 延迟或被动参与率无效")
    if holding_policy not in {"intraday", "overnight"}:
      raise ValueError("持仓规则必须为 intraday 或 overnight")
    if base_quantity < 0 or (base_quantity > 0 and base_cost_price <= 0):
      raise ValueError("底仓数量和成本价无效")
    if t0_enabled and base_quantity <= 0:
      raise ValueError("启用底仓做 T 时底仓数量必须大于 0")
    self._strategy = strategy
    self._ledger = PortfolioLedger(initial_cash)
    self._commission_rate = commission_rate
    self._fee_profile = fee_profile or FeeProfile(
      commission_rate=commission_rate,
      transfer_fee_rate=_TRANSFER_FEE_RATE,
      stamp_duty_rate=_STAMP_DUTY_RATE,
      minimum_commission=0.0,
    )
    self._rules = rules
    self._execution_algorithm = execution_algorithm
    self._execution_parameters = dict(execution_parameters or {})
    self._execution_runtime = ExecutionRuntime(
      execution_algorithm,
      self._execution_parameters,
      ExecutionContext("l2_quote", rules, datetime.max),
    )
    self._latency = timedelta(milliseconds=latency_ms)
    self._participation = passive_participation_rate
    self._holding_policy = holding_policy
    self._target_base_quantity = base_quantity
    self._t0_enabled = t0_enabled
    self._orders: list[L2Order] = []
    self._order_events: list[dict[str, Any]] = []
    self._equity: list[dict[str, Any]] = []
    self._warnings: list[str] = []
    self._last_quote: QuoteSnapshot | None = None
    self._last_bar: NormalizedBar | None = None
    self._day_sellable_by_key: dict[tuple[str, str], int] = {}
    self._sellable_reserved: dict[str, tuple[str, str, int]] = {}
    self._t0_available = 0
    self._t0_reserved: dict[str, int] = {}
    self._sequence = 0
    self._base_initialized = False
    self._base_cost_price = base_cost_price
    self._order_fee_state: dict[str, tuple[float, float]] = {}

  def initialize_base_position(self, symbol: str) -> None:
    """在首个交易日前将外部底仓登记到当前标的。"""
    if self._target_base_quantity <= 0:
      return
    self._ledger.initialize_long_position(symbol, self._target_base_quantity, self._base_cost_price, _BASE_POSITION_KEY)
    self._base_initialized = True

  def begin_day(self, trade_date: str) -> None:
    """锁定 T+1 当日可卖数量，并拒绝未完成订单跨日。"""
    if self._orders:
      self._cancel_open_orders("日终撤单")
    snapshot = self._ledger.snapshot()
    self._day_sellable_by_key = {
      (position.symbol, position_key): position.quantity
      for position_key, position in snapshot.long_positions_by_key.items()
    }
    self._t0_available = min(self._target_base_quantity, sum(item.quantity for item in snapshot.long_positions.values())) if self._t0_enabled else 0
    self._t0_reserved = {}
    self._sellable_reserved = {}
    self._last_quote = None
    self._last_bar = None
    self._execution_runtime.begin_day(parse_timestamp(f"{trade_date} 14:57:00"))
    self._order_events.append({"type": "day_started", "tradeDate": trade_date})

  def process_chunk(
    self,
    bars: Sequence[NormalizedBar],
    quotes: Sequence[QuoteSnapshot],
    trades: Sequence[TradeEvent],
  ) -> None:
    """按确定时间线处理一个不跨交易日的数据块。"""
    events = self._timeline(bars, quotes, trades)
    for _, kind, value in events:
      if kind == "bar":
        self.process_bar(value)
      elif kind == "quote":
        self.process_quote(value)
      else:
        self._on_trade(value)

  def process_bar(self, bar: NormalizedBar) -> None:
    """处理一根完成 Bar；订单在该时刻之后才允许成交。"""
    self._on_bar(bar)

  def process_quote(self, quote: QuoteSnapshot) -> None:
    """先撮合旧订单，再将当前快照交给可选实时策略回调。"""
    self._on_quote(quote)

  def has_pending_orders(self) -> bool:
    """返回是否仍需后续盘口完成当前订单。"""
    return any(order.status == "pending" for order in self._orders) or self._execution_runtime.has_pending_work()

  def next_quote_after(self) -> str | None:
    """返回下一条可能影响待成交订单的严格排他时间边界。"""
    effective_times = [order.effective_at for order in self._orders if order.status == "pending"]
    next_execution_event = self._execution_runtime.next_event_after()
    if next_execution_event is not None:
      effective_times.append(next_execution_event)
    return min(effective_times).isoformat(sep=" ") if effective_times else None

  def requires_day_end_quote(self) -> bool:
    """日内模式有仓位时，需要收盘前最后盘口执行强平。"""
    if self._holding_policy != "intraday":
      return False
    snapshot = self._ledger.snapshot()
    return bool(snapshot.long_positions_by_key or snapshot.short_positions_by_key)

  def finish_day(self, trade_date: str) -> None:
    """收盘撤单并在日内模式用最后有效盘口尝试平仓。"""
    self._cancel_open_orders("日终撤单")
    self._execution_runtime.cancel_all("日终撤单")
    if self._holding_policy == "intraday":
      self._force_close_at_last_quote(trade_date)
    if self._last_bar is not None:
      self._record_equity(self._last_bar)
    self._order_events.append({"type": "day_finished", "tradeDate": trade_date})

  def result(self) -> dict[str, Any]:
    """返回受限规模的结果，不包含原始 L2 数据。"""
    snapshot = self._snapshot()
    return {
      "fills": [asdict(item) for item in self._ledger.fills],
      "equity": self._equity,
      "orders": self._order_events,
      "executionPlans": self._execution_runtime.result(),
      "finalPortfolio": asdict(snapshot),
      "warnings": self._warnings,
    }

  def _timeline(
    self,
    bars: Sequence[NormalizedBar], quotes: Sequence[QuoteSnapshot], trades: Sequence[TradeEvent],
  ) -> list[tuple[tuple[Any, int, int], str, Any]]:
    items: list[tuple[tuple[Any, int, int], str, Any]] = []
    for index, bar in enumerate(bars):
      items.append(((parse_timestamp(bar.end_time), 0, index), "bar", bar))
    for quote in quotes:
      items.append(((parse_timestamp(quote.time), 1, quote.sequence), "quote", quote))
    for trade in trades:
      items.append(((parse_timestamp(trade.time), 2, trade.sequence), "trade", trade))
    return sorted(items, key=lambda item: item[0])

  def _on_bar(self, bar: NormalizedBar) -> None:
    if bar.quality not in {"complete", "ok"}:
      raise ValueError(f"Bar 数据质量不可用于 L2 回测: {bar.quality}")
    self._ledger.mark(bar.symbol, bar.close)
    core_bar = Bar(bar.end_time, bar.symbol, bar.open, bar.high, bar.low, bar.close, bar.volume)
    for intent in self._strategy.on_bar(core_bar, self._snapshot()):
      self._submit(intent, parse_timestamp(bar.end_time))
    self._last_bar = bar

  def _submit(self, intent: OrderIntent, signal_time: Any) -> None:
    intent = self._normalize_overlay_intent(intent)
    intents, rejection_reason = self._split_close_intent(intent)
    if rejection_reason:
      self._record_rejection(intent, signal_time, rejection_reason)
      return
    if self._uses_t0_capacity(intent) and intent.quantity > self._t0_remaining():
      self._record_rejection(intent, signal_time, "底仓做 T 剩余额度不足")
      return
    for item in intents:
      if not self._valid_execution_intent(item, signal_time):
        continue
      for child in self._execution_runtime.submit(item, signal_time):
        self._submit_child(child, signal_time)

  def _submit_child(self, child: ChildOrder, signal_time: Any) -> None:
    self._submit_one(child.intent, child.scheduled_at, signal_time)

  def _submit_one(self, intent: OrderIntent, effective_at: Any, signal_time: Any | None = None) -> None:
    if self._rules.halted:
      self._record_rejection(intent, signal_time, "标的停牌")
      return
    if intent.position_effect in {PositionEffect.OPEN_LONG, PositionEffect.OPEN_SHORT} and not self._rules.is_valid_open_quantity(intent.quantity):
      self._record_rejection(intent, signal_time, "开仓数量不符合标的交易单位")
      return
    if intent.position_effect == PositionEffect.OPEN_SHORT and not self._rules.allow_short:
      self._record_rejection(intent, signal_time, "标的不允许卖空")
      return
    if intent.limit_price is not None and not self._valid_limit(intent.limit_price):
      self._record_rejection(intent, signal_time, "限价不符合标的规则")
      return
    client_order_key = intent.metadata.get("client_order_key")
    if client_order_key and any(
      order.status == "pending" and order.intent.metadata.get("client_order_key") == client_order_key
      for order in self._orders
    ):
      self._record_rejection(intent, signal_time, "clientOrderKey 存在未终结订单")
      return
    self._sequence += 1
    order = L2Order(str(uuid4()), intent, effective_at + self._latency, intent.quantity, self._sequence)
    self._orders.append(order)
    if self._uses_t0_capacity(intent):
      self._t0_reserved[order.order_id] = intent.quantity
    if intent.position_effect == PositionEffect.CLOSE_LONG:
      self._sellable_reserved[order.order_id] = (intent.symbol, intent.position_key, intent.quantity)
    self._record_acceptance(order, signal_time or effective_at)

  def _on_quote(self, quote: QuoteSnapshot) -> None:
    self._last_quote = quote
    budget = self._depth_budget(quote)
    quote_time = parse_timestamp(quote.time)
    for order in self._eligible_orders(quote_time):
      if self._is_active(order, quote):
        self._fill_against_depth(order, quote, budget)
    for child in self._execution_runtime.on_quote(quote_time):
      self._submit_child(child, child.intent.metadata.get("parent_signal_time", quote_time))
    self._cancel_expired_execution_orders()
    callback = getattr(self._strategy, "on_quote", None)
    if callable(callback):
      for intent in callback(quote, self._snapshot()):
        self._submit(intent, quote_time)

  def _on_trade(self, trade: TradeEvent) -> None:
    trade_time = parse_timestamp(trade.time)
    available = int(trade.volume * self._participation)
    for order in self._eligible_orders(trade_time):
      if available <= 0:
        break
      if not order.is_market and self._passive_price_reached(order, trade.price):
        filled = min(order.remaining_quantity, available)
        self._apply_fill(order, filled, trade.price, trade.time, "逐笔保守成交确认")
        available -= filled

  def _eligible_orders(self, event_time: Any) -> list[L2Order]:
    return sorted(
      (item for item in self._orders if item.status == "pending" and item.effective_at < event_time),
      key=lambda item: (item.effective_at, item.created_sequence),
    )

  def _is_active(self, order: L2Order, quote: QuoteSnapshot) -> bool:
    if order.is_market:
      return True
    levels = quote.asks if order.side == OrderSide.BUY else quote.bids
    if not levels:
      return False
    best = levels[0].price
    return order.intent.limit_price >= best if order.side == OrderSide.BUY else order.intent.limit_price <= best

  def _fill_against_depth(self, order: L2Order, quote: QuoteSnapshot, budget: dict[tuple[str, int], int]) -> None:
    levels = quote.asks if order.side == OrderSide.BUY else quote.bids
    for index, level in enumerate(levels):
      if order.remaining_quantity <= 0 or not self._price_permitted(order, level):
        break
      key = ("ask" if order.side == OrderSide.BUY else "bid", index)
      quantity = min(order.remaining_quantity, budget[key])
      if quantity > 0:
        self._apply_fill(order, quantity, level.price, quote.time, "十档盘口主动成交")
        budget[key] -= quantity

  def _apply_fill(self, order: L2Order, quantity: int, price: float, time: str, source: str) -> None:
    if quantity <= 0 or not self._can_afford(order, quantity, price):
      return
    fee, breakdown = self._commit_fee(order, price, quantity)
    metadata = dict(order.intent.metadata)
    metadata.update({
      "price_source": source,
      "order_id": order.order_id,
      "signal_time": metadata.get("signal_time", str(order.effective_at)),
      "fee_breakdown": breakdown,
      "execution_mode": "l2_quote",
    })
    fill = Fill(time, order.intent.symbol, order.side, quantity, price, fee, order.intent.reason, order.intent.position_effect, order.intent.position_key, metadata=metadata)
    self._ledger.apply_fill(fill)
    self._execution_runtime.on_fill(order.intent.metadata.get("parent_order_id"), quantity)
    order.remaining_quantity -= quantity
    order.filled_quantity += quantity
    if order.intent.position_effect == PositionEffect.CLOSE_LONG:
      position_id = (order.intent.symbol, order.intent.position_key)
      self._day_sellable_by_key[position_id] = max(0, self._day_sellable_by_key.get(position_id, 0) - quantity)
      reserved = self._sellable_reserved.get(order.order_id)
      if reserved:
        self._sellable_reserved[order.order_id] = (reserved[0], reserved[1], max(0, reserved[2] - quantity))
    if self._uses_t0_capacity(order.intent):
      self._t0_available = max(0, self._t0_available - quantity)
      self._t0_reserved[order.order_id] = max(0, self._t0_reserved.get(order.order_id, 0) - quantity)
    if order.remaining_quantity == 0:
      order.status = "filled"
    event = {
      "type": "fill", "orderId": order.order_id, "time": time, "quantity": quantity,
      "price": price, "remaining": order.remaining_quantity, "source": source,
    }
    self._order_events.append(event)
    self._notify_strategy_event({
      **event,
      "type": "filled" if order.status == "filled" else "partial_fill",
      "cumulativeQuantity": order.filled_quantity,
      "commission": fee,
      "clientOrderKey": order.intent.metadata.get("client_order_key"),
      "positionEffect": order.intent.position_effect.value if order.intent.position_effect else None,
    })

  def _force_close_at_last_quote(self, trade_date: str) -> None:
    portfolio = self._ledger.snapshot()
    if self._target_base_quantity == 0:
      for position_key, position in portfolio.long_positions_by_key.items():
        if position.quantity > 0:
          self._force_close(position.symbol, position_key, OrderSide.SELL, position.quantity)
      for position_key, position in portfolio.short_positions_by_key.items():
        if position.quantity > 0:
          self._force_close(position.symbol, position_key, OrderSide.BUY, position.quantity)
      return
    if self._last_quote is None:
      raise ValueError(f"{trade_date} 无有效盘口，无法恢复底仓")
    self._settle_overlay_positions(portfolio)
    self._restore_base_position()

  def _settle_overlay_positions(self, portfolio: Any) -> None:
    for position_key, position in portfolio.long_positions_by_key.items():
      if position_key != _BASE_POSITION_KEY and position.quantity > 0:
        self._force_settlement(
          position.symbol, OrderSide.SELL, PositionEffect.CLOSE_LONG,
          position.quantity, position_key, "尾盘平策略多仓",
        )
    portfolio = self._ledger.snapshot()
    for position_key, position in portfolio.short_positions_by_key.items():
      if position.quantity > 0:
        self._force_settlement(
          position.symbol, OrderSide.BUY, PositionEffect.CLOSE_SHORT,
          position.quantity, position_key, "尾盘平策略空仓",
        )

  def _restore_base_position(self) -> None:
    portfolio = self._ledger.snapshot()
    base_position = portfolio.long_positions_by_key.get(_BASE_POSITION_KEY)
    long_quantity = base_position.quantity if base_position else 0
    symbol = base_position.symbol if base_position else self._last_quote.symbol
    difference = self._target_base_quantity - long_quantity
    if difference == 0:
      return
    side = OrderSide.BUY if difference > 0 else OrderSide.SELL
    effect = PositionEffect.OPEN_LONG if side == OrderSide.BUY else PositionEffect.CLOSE_LONG
    if side == OrderSide.BUY:
      self._force_settlement(symbol, side, effect, abs(difference), _BASE_POSITION_KEY, "尾盘恢复底仓")
      return
    self._force_settlement(symbol, side, effect, abs(difference), _BASE_POSITION_KEY, "尾盘恢复底仓")

  def _force_close(self, symbol: str, position_key: str, side: OrderSide, quantity: int) -> None:
    effect = PositionEffect.CLOSE_LONG if side == OrderSide.SELL else PositionEffect.CLOSE_SHORT
    intent = OrderIntent(symbol, side, quantity, "尾盘强平", position_effect=effect, position_key=position_key, metadata={"direction": "FORCE_CLOSE", "source": "system"})
    self._sequence += 1
    order = L2Order(str(uuid4()), intent, parse_timestamp(self._last_quote.time), quantity, self._sequence)
    self._record_acceptance(order, self._last_quote.time)
    budget = self._depth_budget(self._last_quote)
    self._fill_against_depth(order, self._last_quote, budget)
    if order.remaining_quantity:
      self._order_events.append({
        "type": "cancelled",
        "orderId": order.order_id,
        "remaining": order.remaining_quantity,
        "reason": "尾盘强平未成交",
      })
      self._warnings.append(f"{symbol} 尾盘强平剩余 {order.remaining_quantity} 股未成交")

  def _force_settlement(
    self,
    symbol: str,
    side: OrderSide,
    effect: PositionEffect,
    quantity: int,
    position_key: str,
    reason: str,
  ) -> None:
    intent = OrderIntent(symbol, side, quantity, reason, position_effect=effect, position_key=position_key, metadata={"direction": "FORCE_SETTLEMENT", "source": "system"})
    self._sequence += 1
    order = L2Order(str(uuid4()), intent, parse_timestamp(self._last_quote.time), quantity, self._sequence)
    self._record_acceptance(order, self._last_quote.time)
    budget = self._depth_budget(self._last_quote)
    self._fill_against_depth(order, self._last_quote, budget)
    if order.remaining_quantity:
      price, source = self._settlement_price(side)
      self._apply_forced_fill(order, price, source)

  def _settlement_price(self, side: OrderSide) -> tuple[float, str]:
    levels = self._last_quote.asks if side == OrderSide.BUY else self._last_quote.bids
    if not levels:
      opposite = self._last_quote.bids if side == OrderSide.BUY else self._last_quote.asks
      if not opposite:
        raise ValueError("尾盘盘口双边为空，无法强制结算")
      return opposite[0].price, "尾盘涨跌停强制结算"
    return levels[0].price, "尾盘盘口深度不足强制结算"

  def _apply_forced_fill(self, order: L2Order, price: float, source: str) -> None:
    quantity = order.remaining_quantity
    fee, breakdown = self._commit_fee(order, price, quantity)
    metadata = dict(order.intent.metadata)
    metadata.update({
      "price_source": source,
      "order_id": order.order_id,
      "signal_time": self._last_quote.time,
      "fee_breakdown": breakdown,
      "execution_mode": "l2_quote",
    })
    fill = Fill(self._last_quote.time, order.intent.symbol, order.side, quantity, price, fee, order.intent.reason, order.intent.position_effect, order.intent.position_key, metadata=metadata)
    self._ledger.apply_fill(fill)
    order.filled_quantity += quantity
    order.remaining_quantity = 0
    order.status = "filled"
    event = {
      "type": "fill", "orderId": order.order_id, "time": self._last_quote.time,
      "quantity": quantity, "price": price, "remaining": 0, "source": source,
    }
    self._order_events.append(event)
    self._notify_strategy_event({
      **event,
      "type": "filled",
      "cumulativeQuantity": order.filled_quantity,
      "commission": fee,
      "clientOrderKey": order.intent.metadata.get("client_order_key"),
      "positionEffect": order.intent.position_effect.value if order.intent.position_effect else None,
    })
    self._warnings.append(f"{order.intent.symbol} {source}")

  def _cancel_open_orders(self, reason: str) -> None:
    for order in self._orders:
      if order.status == "pending":
        order.status = "cancelled"
        self._release_t0_reservation(order)
        self._release_sellable_reservation(order)
        self._execution_runtime.cancel_open(order.intent.metadata.get("parent_order_id"), order.remaining_quantity, reason)
        event = {"type": "cancelled", "orderId": order.order_id, "remaining": order.remaining_quantity, "reason": reason}
        self._order_events.append(event)
        self._notify_strategy_event({
          **event,
          "clientOrderKey": order.intent.metadata.get("client_order_key"),
          "positionEffect": order.intent.position_effect.value if order.intent.position_effect else None,
        })

  def _cancel_expired_execution_orders(self) -> None:
    parent_order_ids = self._execution_runtime.consume_cancelled_parent_ids()
    if not parent_order_ids:
      return
    reason = "执行窗口结束，取消剩余数量"
    for order in self._orders:
      if order.status != "pending" or order.intent.metadata.get("parent_order_id") not in parent_order_ids:
        continue
      order.status = "cancelled"
      self._release_t0_reservation(order)
      self._release_sellable_reservation(order)
      self._order_events.append({
        "type": "cancelled",
        "orderId": order.order_id,
        "remaining": order.remaining_quantity,
        "reason": reason,
      })
      self._notify_strategy_event({
        "type": "cancelled",
        "time": order.effective_at,
        "orderId": order.order_id,
        "reason": reason,
      })

  def _record_equity(self, bar: NormalizedBar) -> None:
    snapshot = self._snapshot()
    self._equity.append({
      "time": bar.end_time,
      "equity": snapshot.equity,
      "cash": snapshot.cash,
      "close": bar.close,
      "longQty": sum(item.quantity for item in snapshot.long_positions.values()),
      "shortQty": sum(item.quantity for item in snapshot.short_positions.values()),
      "baseQty": snapshot.long_positions_by_key.get(_BASE_POSITION_KEY).quantity if _BASE_POSITION_KEY in snapshot.long_positions_by_key else 0,
      "overlayLongQty": sum(
        position.quantity for position_key, position in snapshot.long_positions_by_key.items()
        if position_key.endswith(_OVERLAY_LONG_SUFFIX)
      ),
      "overlayShortQty": sum(
        position.quantity for position_key, position in snapshot.short_positions_by_key.items()
        if position_key.endswith(_OVERLAY_SHORT_SUFFIX)
      ),
    })

  def _record_acceptance(self, order: L2Order, signal_time: Any) -> None:
    event = {
      "type": "accepted",
      "orderId": order.order_id,
      "signalTime": str(signal_time),
      "effectiveAt": str(order.effective_at),
      **self._signal_fields(order.intent),
    }
    self._order_events.append(event)
    self._notify_strategy_event({
      **event,
      "clientOrderKey": order.intent.metadata.get("client_order_key"),
      "positionEffect": order.intent.position_effect.value if order.intent.position_effect else None,
    })

  def _record_rejection(self, intent: OrderIntent, signal_time: Any, reason: str) -> None:
    self._execution_runtime.on_rejection(intent.metadata.get("parent_order_id"), intent.quantity, reason)
    event = {
      "type": "rejected",
      "signalTime": str(signal_time),
      "rejectionReason": reason,
      **self._signal_fields(intent),
    }
    self._order_events.append(event)
    self._notify_strategy_event({
      **event,
      "clientOrderKey": intent.metadata.get("client_order_key"),
      "positionEffect": intent.position_effect.value if intent.position_effect else None,
    })

  @staticmethod
  def _signal_fields(intent: OrderIntent) -> dict[str, Any]:
    metadata = intent.metadata
    direction = str(metadata.get("direction") or intent.side.value)
    return {
      "symbol": intent.symbol,
      "side": intent.side.value,
      "direction": direction,
      "action": direction,
      "quantity": intent.quantity,
      "reason": intent.reason,
      "limitPrice": intent.limit_price,
      "strategyModule": metadata.get("strategy_module"),
      "clientOrderKey": metadata.get("client_order_key"),
      "parentOrderId": metadata.get("parent_order_id"),
      "childIndex": metadata.get("child_index"),
      "executionAlgorithm": metadata.get("execution_algorithm", "immediate"),
    }

  def _notify_strategy_event(self, event: dict[str, Any]) -> None:
    """只在账本与订单状态已更新后通知脚本，避免策略读取旧状态。"""
    callback = getattr(self._strategy, "on_order_event", None)
    if callable(callback):
      callback(event)

  def _valid_execution_intent(self, intent: OrderIntent, signal_time: Any) -> bool:
    if self._execution_algorithm != "twap":
      return True
    minimum = int(self._execution_parameters.get("minChildQuantity", 0))
    if minimum <= 0 or minimum > intent.quantity:
      self._record_rejection(intent, signal_time, "TWAP 最小子单数量超过母单数量")
      return False
    if intent.position_effect in {PositionEffect.OPEN_LONG, PositionEffect.OPEN_SHORT} and not self._rules.is_valid_open_quantity(minimum):
      self._record_rejection(intent, signal_time, "TWAP 最小子单数量不符合标的交易单位")
      return False
    return True

  def _valid_limit(self, price: float) -> bool:
    if self._rules.lower_limit is not None and price < self._rules.lower_limit:
      return False
    if self._rules.upper_limit is not None and price > self._rules.upper_limit:
      return False
    return abs(round(price / self._rules.tick_size) * self._rules.tick_size - price) < 1e-8

  @staticmethod
  def _depth_budget(quote: QuoteSnapshot) -> dict[tuple[str, int], int]:
    return {
      **{("bid", index): level.volume for index, level in enumerate(quote.bids)},
      **{("ask", index): level.volume for index, level in enumerate(quote.asks)},
    }

  @staticmethod
  def _price_permitted(order: L2Order, level: QuoteLevel) -> bool:
    if order.intent.limit_price is None:
      return True
    return level.price <= order.intent.limit_price if order.side == OrderSide.BUY else level.price >= order.intent.limit_price

  @staticmethod
  def _passive_price_reached(order: L2Order, trade_price: float) -> bool:
    return trade_price <= order.intent.limit_price if order.side == OrderSide.BUY else trade_price >= order.intent.limit_price

  def _can_afford(self, order: L2Order, quantity: int, price: float) -> bool:
    if order.intent.position_effect != PositionEffect.OPEN_LONG:
      return True
    fee, _ = self._preview_fee(order, price, quantity)
    return self._ledger.snapshot().cash + 1e-8 >= price * quantity + fee

  def _uses_t0_capacity(self, intent: OrderIntent) -> bool:
    return (
      self._t0_enabled
      and intent.position_effect in {PositionEffect.OPEN_LONG, PositionEffect.OPEN_SHORT}
      and intent.position_key != _BASE_POSITION_KEY
    )

  def _t0_remaining(self) -> int:
    return max(0, self._t0_available - sum(self._t0_reserved.values()))

  def _release_t0_reservation(self, order: L2Order) -> None:
    self._t0_reserved.pop(order.order_id, None)

  def _release_sellable_reservation(self, order: L2Order) -> None:
    self._sellable_reserved.pop(order.order_id, None)

  def _sellable_remaining(self, symbol: str) -> int:
    sellable = sum(quantity for (item_symbol, _), quantity in self._day_sellable_by_key.items() if item_symbol == symbol)
    reserved = sum(quantity for item_symbol, _, quantity in self._sellable_reserved.values() if item_symbol == symbol)
    return max(0, sellable - reserved)

  def _sellable_for_position(self, symbol: str, position_key: str) -> int:
    position_id = (symbol, position_key)
    reserved = sum(
      quantity for item_symbol, item_key, quantity in self._sellable_reserved.values()
      if (item_symbol, item_key) == position_id
    )
    return max(0, self._day_sellable_by_key.get(position_id, 0) - reserved)

  def _split_close_intent(self, intent: OrderIntent) -> tuple[list[OrderIntent], str | None]:
    if intent.position_effect != PositionEffect.CLOSE_LONG:
      return [intent], None
    snapshot = self._ledger.snapshot()
    if self._is_overlay_mode():
      position = snapshot.long_positions_by_key.get(intent.position_key)
      if position is None or intent.quantity > position.quantity:
        return [], "平多数量超过策略多仓"
      return [intent], None
    position_keys = [intent.position_key]
    if _BASE_POSITION_KEY in snapshot.long_positions_by_key and intent.position_key != _BASE_POSITION_KEY:
      position_keys.append(_BASE_POSITION_KEY)
    total_position = sum(
      snapshot.long_positions_by_key.get(position_key).quantity
      for position_key in position_keys
      if snapshot.long_positions_by_key.get(position_key)
    )
    if intent.quantity > total_position:
      return [], "平多数量超过实际持仓"
    remaining = intent.quantity
    items: list[OrderIntent] = []
    for position_key in position_keys:
      quantity = min(remaining, self._sellable_for_position(intent.symbol, position_key))
      if quantity <= 0:
        continue
      metadata = intent.metadata if position_key == intent.position_key else {**intent.metadata, "base_position_used": True}
      items.append(replace(intent, quantity=quantity, position_key=position_key, metadata=metadata))
      remaining -= quantity
      if remaining == 0:
        return items, None
    return [], "T+1 可卖数量不足"

  def _normalize_overlay_intent(self, intent: OrderIntent) -> OrderIntent:
    if not self._is_overlay_mode():
      return intent
    if intent.position_effect in {PositionEffect.OPEN_LONG, PositionEffect.CLOSE_LONG}:
      return replace(intent, position_key=self._overlay_position_key(intent.position_key, _OVERLAY_LONG_SUFFIX))
    if intent.position_effect in {PositionEffect.OPEN_SHORT, PositionEffect.CLOSE_SHORT}:
      return replace(intent, position_key=self._overlay_position_key(intent.position_key, _OVERLAY_SHORT_SUFFIX))
    return intent

  def _is_overlay_mode(self) -> bool:
    return self._target_base_quantity > 0 and self._holding_policy == "intraday"

  @staticmethod
  def _overlay_position_key(position_key: str, suffix: str) -> str:
    return position_key if position_key.endswith(suffix) else f"{position_key}{suffix}"

  def _snapshot(self):
    snapshot = self._ledger.snapshot()
    base_position = snapshot.long_positions_by_key.get(_BASE_POSITION_KEY)
    return snapshot.__class__(
      cash=snapshot.cash, equity=snapshot.equity, positions=snapshot.positions,
      long_positions=snapshot.long_positions, short_positions=snapshot.short_positions,
      long_positions_by_key=snapshot.long_positions_by_key, short_positions_by_key=snapshot.short_positions_by_key,
      long_positions_by_identity=snapshot.long_positions_by_identity,
      short_positions_by_identity=snapshot.short_positions_by_identity,
      realized_pnl=snapshot.realized_pnl, base_position=self._target_base_quantity,
      sellable_quantity=sum(
        self._sellable_for_position(symbol, position_key)
        for symbol, position_key in self._day_sellable_by_key
      ),
      t0_available_quantity=self._t0_remaining() if self._t0_enabled else None,
    )

  def _preview_fee(self, order: L2Order, price: float, quantity: int) -> tuple[float, dict[str, float]]:
    previous_notional, previous_commission = self._order_fee_state.get(order.order_id, (0.0, 0.0))
    return self._fee_profile.fee_for_fill(
      order.side,
      price,
      quantity,
      previous_notional,
      previous_commission,
    )

  def _commit_fee(self, order: L2Order, price: float, quantity: int) -> tuple[float, dict[str, float]]:
    fee, breakdown = self._preview_fee(order, price, quantity)
    previous_notional, previous_commission = self._order_fee_state.get(order.order_id, (0.0, 0.0))
    self._order_fee_state[order.order_id] = (
      previous_notional + price * quantity,
      previous_commission + breakdown["commission"],
    )
    return fee, breakdown
