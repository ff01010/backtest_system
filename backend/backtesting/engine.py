"""K 线事件驱动的最小通用回测内核。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections import defaultdict
from typing import Any, Callable, Protocol, Sequence

from backend.backtesting.fees import FeeProfile
from backend.backtesting.models import (
  BacktestResult,
  Bar,
  EquityPoint,
  Fill,
  OrderIntent,
  OrderSide,
  PositionEffect,
  PortfolioSnapshot,
  PositionSnapshot,
)

_STAMP_DUTY_RATE = 0.0005
_TRANSFER_FEE_RATE = 0.00001


def _transaction_fee(
  side: OrderSide,
  price: float,
  quantity: int,
  commission_rate: float,
  fee_profile: FeeProfile | None = None,
) -> float:
  if fee_profile is not None:
    return fee_profile.fee_for_fill(side, price, quantity)[0]
  fee_rate = commission_rate + _TRANSFER_FEE_RATE
  if side == OrderSide.SELL:
    fee_rate += _STAMP_DUTY_RATE
  return round(price * quantity * fee_rate, 8)


class BarStrategy(Protocol):
  """所有 K 线策略需实现的最小协议。"""

  def on_bar(
    self,
    bar: Bar,
    portfolio: PortfolioSnapshot,
  ) -> Sequence[OrderIntent]:
    """根据当前已知状态输出交易意图。"""
    ...


class ExecutionModel(Protocol):
  """成交模型协议。"""

  def create_fill(
    self,
    order: OrderIntent,
    bar: Bar,
    commission_rate: float,
    fee_profile: FeeProfile | None = None,
  ) -> Fill | None:
    """返回成交记录；订单在当前 K 线无法成交时返回 None。"""
    ...


class BatchStrategy(Protocol):
  """兼容性策略协议：一次生成带时间戳的订单意图。"""

  def generate_orders(self, bars: Sequence[Bar]) -> Sequence[OrderIntent]:
    """基于完整历史 K 线生成订单意图。"""
    ...


class StreamingStrategy(Protocol):
  """流式策略协议：每根 K 线完成后立即发出订单意图。"""

  def stream(
    self,
    bars: Sequence[Bar],
    emit: Callable[[Bar, Sequence[OrderIntent]], None],
  ) -> None:
    """驱动策略状态机，并为每根 K 线调用一次 emit。"""
    ...


@dataclass(frozen=True)
class _AcceptedBarOrder:
  """标准 Bar 模式待成交订单及其回调关联标识。"""

  order_id: str
  intent: OrderIntent


@dataclass
class _SidePosition:
  quantity: int = 0
  average_price: float = 0.0


class PortfolioLedger:
  """统一维护现金、双向持仓、交易成本和已实现盈亏。"""

  def __init__(self, initial_cash: float) -> None:
    if initial_cash <= 0:
      raise ValueError("初始资金必须大于 0")
    self._cash = float(initial_cash)
    self._long_positions: dict[tuple[str, str], _SidePosition] = {}
    self._short_positions: dict[tuple[str, str], _SidePosition] = {}
    self._last_prices: dict[str, float] = {}
    self._fills: list[Fill] = []
    self._realized_pnl = 0.0

  @property
  def fills(self) -> list[Fill]:
    return list(self._fills)

  def snapshot(self) -> PortfolioSnapshot:
    long_by_identity = self._snapshot_positions_by_identity(self._long_positions)
    short_by_identity = self._snapshot_positions_by_identity(self._short_positions)
    long_by_key = self._snapshot_positions_by_key(long_by_identity)
    short_by_key = self._snapshot_positions_by_key(short_by_identity)
    long_positions = self._aggregate_positions(long_by_identity)
    short_positions = self._aggregate_positions(short_by_identity)
    positions = self._net_positions(long_positions, short_positions)
    return PortfolioSnapshot(
      cash=round(self._cash, 8),
      equity=round(self.equity(), 8),
      positions=positions,
      long_positions=long_positions,
      short_positions=short_positions,
      long_positions_by_key=long_by_key,
      short_positions_by_key=short_by_key,
      long_positions_by_identity=long_by_identity,
      short_positions_by_identity=short_by_identity,
      realized_pnl=round(self._realized_pnl, 8),
    )

  def mark(self, symbol: str, price: float) -> None:
    if price <= 0:
      raise ValueError("市值价格必须大于 0")
    self._last_prices[symbol] = float(price)

  def initialize_long_position(self, symbol: str, quantity: int, average_price: float, position_key: str) -> None:
    """登记回测开始前已持有的外部底仓，不变动可用现金。"""
    if quantity <= 0 or average_price <= 0:
      raise ValueError("初始底仓数量和成本价必须大于 0")
    position = self._long_positions.setdefault((symbol, position_key), _SidePosition())
    self._add_position(position, quantity, average_price)
    self.mark(symbol, average_price)

  def equity(self) -> float:
    long_value = self._market_value(self._long_positions)
    short_value = self._market_value(self._short_positions)
    return self._cash + long_value - short_value

  def apply_fill(self, fill: Fill) -> Fill:
    """应用成交；显式开平仓优先，缺失时按净仓位兼容处理。"""
    effect = fill.position_effect.value if fill.position_effect else str(
      fill.metadata.get("position_effect", "")
    )
    if effect:
      realized_pnl = self._apply_explicit_fill(fill, effect)
    else:
      realized_pnl = self._apply_net_fill(fill)
    self._realized_pnl += realized_pnl
    self.mark(fill.symbol, fill.price)

    applied = Fill(
      time=fill.time,
      symbol=fill.symbol,
      side=fill.side,
      quantity=fill.quantity,
      price=fill.price,
      fee=fill.fee,
      reason=fill.reason,
      position_effect=fill.position_effect,
      position_key=fill.position_key,
      realized_pnl=round(realized_pnl, 8),
      metadata=fill.metadata,
    )
    self._fills.append(applied)
    return applied

  def force_close_orders(self, bar: Bar, excluded_position_keys: set[str] | None = None) -> list[OrderIntent]:
    """为日内规则生成收盘系统结算订单。"""
    orders: list[OrderIntent] = []
    excluded_position_keys = excluded_position_keys or set()
    for (symbol, position_key), position in self._long_positions.items():
      if position.quantity > 0 and position_key not in excluded_position_keys:
        orders.append(self._force_close_order(
          bar, symbol, position_key, position.quantity, OrderSide.SELL, PositionEffect.CLOSE_LONG
        ))
    for (symbol, position_key), position in self._short_positions.items():
      if position.quantity > 0 and position_key not in excluded_position_keys:
        orders.append(self._force_close_order(
          bar, symbol, position_key, position.quantity, OrderSide.BUY, PositionEffect.CLOSE_SHORT
        ))
    return orders

  @staticmethod
  def _force_close_order(
    bar: Bar,
    symbol: str,
    position_key: str,
    quantity: int,
    side: OrderSide,
    effect: PositionEffect,
  ) -> OrderIntent:
    direction = "FORCE_SELL" if side == OrderSide.SELL else "FORCE_BUY"
    return OrderIntent(
      symbol=symbol,
      side=side,
      quantity=quantity,
      reason="尾盘强平",
      time=bar.time,
      position_effect=effect,
      position_key=position_key,
      metadata={
        "direction": direction,
        "recorded_price": bar.close,
        "signal_time": bar.time,
        "source": "system",
      },
    )

  def _apply_explicit_fill(self, fill: Fill, effect: str) -> float:
    if effect == "open_long":
      self._open_long(fill)
      return 0.0
    if effect == "open_short":
      self._open_short(fill)
      return 0.0
    if effect == "close_long":
      return self._close_long(fill)
    if effect == "close_short":
      return self._close_short(fill)
    raise ValueError(f"不支持的持仓动作: {effect}")

  def _apply_net_fill(self, fill: Fill) -> float:
    if fill.side == OrderSide.BUY:
      return self._close_then_open(fill, self._short_positions, self._close_short, self._open_long)
    return self._close_then_open(fill, self._long_positions, self._close_long, self._open_short)

  def _close_then_open(
    self,
    fill: Fill,
    opposite_positions: dict[str, _SidePosition],
    close: Callable[[Fill], float],
    open_position: Callable[[Fill], None],
  ) -> float:
    position_id = (fill.symbol, fill.position_key)
    closable = min(fill.quantity, opposite_positions.get(position_id, _SidePosition()).quantity)
    if closable == 0:
      open_position(fill)
      return 0.0
    close_fill = self._split_fill(fill, closable)
    realized_pnl = close(close_fill)
    remaining = fill.quantity - closable
    if remaining > 0:
      open_position(self._split_fill(fill, remaining))
    return realized_pnl

  @staticmethod
  def _split_fill(fill: Fill, quantity: int) -> Fill:
    fee = fill.fee * quantity / fill.quantity
    return Fill(
      time=fill.time,
      symbol=fill.symbol,
      side=fill.side,
      quantity=quantity,
      price=fill.price,
      fee=fee,
      reason=fill.reason,
      position_effect=fill.position_effect,
      position_key=fill.position_key,
      metadata=dict(fill.metadata),
    )

  def _open_long(self, fill: Fill) -> None:
    position = self._long_positions.setdefault(self._position_id(fill), _SidePosition())
    unit_cost = (fill.price * fill.quantity + fill.fee) / fill.quantity
    self._add_position(position, fill.quantity, unit_cost)
    self._cash -= fill.price * fill.quantity + fill.fee

  def _open_short(self, fill: Fill) -> None:
    position = self._short_positions.setdefault(self._position_id(fill), _SidePosition())
    unit_proceeds = (fill.price * fill.quantity - fill.fee) / fill.quantity
    self._add_position(position, fill.quantity, unit_proceeds)
    self._cash += fill.price * fill.quantity - fill.fee

  def _close_long(self, fill: Fill) -> float:
    position = self._require_position(self._long_positions, fill)
    realized_pnl = fill.price * fill.quantity - fill.fee - position.average_price * fill.quantity
    self._reduce_position(position, fill.quantity)
    self._cash += fill.price * fill.quantity - fill.fee
    return realized_pnl

  def _close_short(self, fill: Fill) -> float:
    position = self._require_position(self._short_positions, fill)
    realized_pnl = position.average_price * fill.quantity - fill.price * fill.quantity - fill.fee
    self._reduce_position(position, fill.quantity)
    self._cash -= fill.price * fill.quantity + fill.fee
    return realized_pnl

  @staticmethod
  def _add_position(position: _SidePosition, quantity: int, average_price: float) -> None:
    total_quantity = position.quantity + quantity
    position.average_price = (
      position.quantity * position.average_price + quantity * average_price
    ) / total_quantity
    position.quantity = total_quantity

  @staticmethod
  def _reduce_position(position: _SidePosition, quantity: int) -> None:
    if quantity > position.quantity:
      raise ValueError("平仓数量超过可用持仓")
    position.quantity -= quantity
    if position.quantity == 0:
      position.average_price = 0.0

  @staticmethod
  def _require_position(
    positions: dict[tuple[str, str], _SidePosition],
    fill: Fill,
  ) -> _SidePosition:
    position = positions.get((fill.symbol, fill.position_key))
    if position is None or position.quantity < fill.quantity:
      raise ValueError("平仓数量超过可用持仓")
    return position

  def _market_value(self, positions: dict[tuple[str, str], _SidePosition]) -> float:
    return sum(
      position.quantity * self._last_prices.get(symbol, position.average_price)
      for (symbol, _), position in positions.items()
    )

  @staticmethod
  def _snapshot_positions_by_key(
    positions: dict[tuple[str, str], PositionSnapshot],
  ) -> dict[str, PositionSnapshot]:
    return {
      position_key: position
      for (_, position_key), position in positions.items()
    }

  @staticmethod
  def _snapshot_positions_by_identity(
    positions: dict[tuple[str, str], _SidePosition],
  ) -> dict[tuple[str, str], PositionSnapshot]:
    return {
      (symbol, position_key): PositionSnapshot(symbol, position.quantity, position.average_price)
      for (symbol, position_key), position in positions.items()
      if position.quantity > 0
    }

  @staticmethod
  def _aggregate_positions(
    positions_by_key: dict[tuple[str, str], PositionSnapshot],
  ) -> dict[str, PositionSnapshot]:
    totals: dict[str, _SidePosition] = {}
    for position in positions_by_key.values():
      aggregate = totals.setdefault(position.symbol, _SidePosition())
      PortfolioLedger._add_position(aggregate, position.quantity, position.average_price)
    return {
      symbol: PositionSnapshot(symbol, position.quantity, position.average_price)
      for symbol, position in totals.items()
      if position.quantity > 0
    }

  @staticmethod
  def _position_id(fill: Fill) -> tuple[str, str]:
    return fill.symbol, fill.position_key

  @staticmethod
  def _net_positions(
    long_positions: dict[str, PositionSnapshot],
    short_positions: dict[str, PositionSnapshot],
  ) -> dict[str, PositionSnapshot]:
    positions: dict[str, PositionSnapshot] = {}
    for symbol in set(long_positions) | set(short_positions):
      long = long_positions.get(symbol)
      short = short_positions.get(symbol)
      quantity = (long.quantity if long else 0) - (short.quantity if short else 0)
      if quantity == 0:
        continue
      average_price = long.average_price if quantity > 0 and long else short.average_price
      positions[symbol] = PositionSnapshot(symbol, quantity, average_price)
    return positions


AccountLedger = PortfolioLedger


class ClosePriceExecutionModel:
  """默认 K 线成交模型：市价按收盘价，限价按当前 K 线可得最优价。"""

  def create_fill(
    self,
    order: OrderIntent,
    bar: Bar,
    commission_rate: float,
    fee_profile: FeeProfile | None = None,
  ) -> Fill | None:
    price = self._resolve_price(order, bar)
    if price is None:
      return None
    fee = _transaction_fee(order.side, price, order.quantity, commission_rate, fee_profile)
    metadata = dict(order.metadata)
    if fee_profile is not None:
      metadata["fee_breakdown"] = fee_profile.fee_for_fill(order.side, price, order.quantity)[1]
    return Fill(
      time=bar.time,
      symbol=order.symbol,
      side=order.side,
      quantity=order.quantity,
      price=price,
      fee=fee,
      reason=order.reason,
      position_effect=order.position_effect,
      position_key=order.position_key,
      metadata=metadata,
    )

  @staticmethod
  def _resolve_price(order: OrderIntent, bar: Bar) -> float | None:
    if order.limit_price is None:
      return bar.close
    if order.side == OrderSide.BUY:
      if bar.low > order.limit_price:
        return None
      return min(bar.open, order.limit_price)
    if bar.high < order.limit_price:
      return None
    return max(bar.open, order.limit_price)


class RecordedPriceExecutionModel:
  """按迁移基线中记录的成交价格回放订单。"""

  def __init__(self, slippage: float = 0.0) -> None:
    if slippage < 0:
      raise ValueError("滑点不能小于 0")
    self._slippage = float(slippage)

  def create_fill(
    self,
    order: OrderIntent,
    bar: Bar,
    commission_rate: float,
    fee_profile: FeeProfile | None = None,
  ) -> Fill | None:
    raw_price = order.metadata.get("recorded_price")
    if raw_price is None:
      raise ValueError("回放订单缺少 recorded_price")
    base_price = float(raw_price)
    price = base_price * (1 + self._slippage) if order.side == OrderSide.BUY else base_price * (1 - self._slippage)
    metadata = dict(order.metadata)
    metadata.update({
      "base_price": base_price,
      "slippage_rate": self._slippage,
      "slippage_amount": abs(price - base_price),
      "price_source": f"当前 Bar 收盘价 + 固定滑点 {self._slippage * 10_000:.2f} bp",
    })
    fee = _transaction_fee(order.side, price, order.quantity, commission_rate, fee_profile)
    if fee_profile is not None:
      metadata["fee_breakdown"] = fee_profile.fee_for_fill(order.side, price, order.quantity)[1]
    return Fill(
      time=bar.time,
      symbol=order.symbol,
      side=order.side,
      quantity=order.quantity,
      price=price,
      fee=fee,
      reason=order.reason,
      position_effect=order.position_effect,
      position_key=order.position_key,
      metadata=metadata,
    )


class NextOpenExecutionModel:
  """标准 Bar 回放：信号在收盘后产生，下一根可交易 Bar 的开盘成交。"""

  def __init__(self, slippage: float = 0.0001) -> None:
    if slippage < 0:
      raise ValueError("滑点不能小于 0")
    self._slippage = float(slippage)

  def create_fill(
    self,
    order: OrderIntent,
    bar: Bar,
    commission_rate: float,
    fee_profile: FeeProfile | None = None,
  ) -> Fill | None:
    is_system_settlement = order.metadata.get("source") == "system"
    base_price = bar.close if is_system_settlement else bar.open
    price = base_price * (1 + self._slippage) if order.side == OrderSide.BUY else base_price * (1 - self._slippage)
    metadata = dict(order.metadata)
    metadata.update({
      "base_price": base_price,
      "slippage_rate": self._slippage,
      "slippage_amount": abs(price - base_price),
      "planned_execution_time": bar.time,
      "execution_mode": "bar_next_open",
      "price_source": "尾盘收盘价强平" if is_system_settlement else f"下一根 Bar 开盘价 + 固定滑点 {self._slippage * 10_000:.2f} bp",
    })
    fee = _transaction_fee(order.side, price, order.quantity, commission_rate, fee_profile)
    if fee_profile is not None:
      metadata["fee_breakdown"] = fee_profile.fee_for_fill(order.side, price, order.quantity)[1]
    return Fill(
      time=bar.time,
      symbol=order.symbol,
      side=order.side,
      quantity=order.quantity,
      price=price,
      fee=fee,
      reason=order.reason,
      position_effect=order.position_effect,
      position_key=order.position_key,
      metadata=metadata,
    )


class BarBacktestEngine:
  """将策略意图、成交模型和账户账本组合为可复用回测流程。"""

  def __init__(
    self,
    initial_cash: float,
    commission_rate: float = 0.0,
    fee_profile: FeeProfile | None = None,
    execution_model: ExecutionModel | None = None,
    holding_policy: str = "overnight",
    execute_on_next_bar_open: bool = False,
    base_quantity: int = 0,
    base_cost_price: float = 0.0,
    t0_enabled: bool = False,
    strategy_category: str = "directional_holding",
    overlay_settlement_policy: str = "intraday_restore",
  ) -> None:
    if commission_rate < 0:
      raise ValueError("手续费率不能小于 0")
    if holding_policy not in {"intraday", "overnight"}:
      raise ValueError("持仓规则必须为 intraday 或 overnight")
    if base_quantity < 0 or (base_quantity > 0 and base_cost_price <= 0):
      raise ValueError("底仓数量和成本价无效")
    if t0_enabled and base_quantity <= 0:
      raise ValueError("启用底仓做 T 时底仓数量必须大于 0")
    if strategy_category not in {"core_position_overlay", "directional_holding"}:
      raise ValueError("策略类别无效")
    if overlay_settlement_policy not in {"intraday_restore", "cross_day_carry"}:
      raise ValueError("增强仓结算规则无效")
    self._ledger = PortfolioLedger(initial_cash)
    self._commission_rate = commission_rate
    self._fee_profile = fee_profile
    self._execution_model = execution_model or ClosePriceExecutionModel()
    self._holding_policy = holding_policy
    self._strategy_category = strategy_category
    self._overlay_settlement_policy = overlay_settlement_policy
    self._execute_on_next_bar_open = execute_on_next_bar_open
    self._base_quantity = base_quantity
    self._base_cost_price = base_cost_price
    self._t0_enabled = t0_enabled
    self._t0_remaining = base_quantity if t0_enabled else 0
    self._t0_date: str | None = None
    self._order_event_sequence = 0
    self._day_sellable_by_key: dict[tuple[str, str], int] = {}
    self._initial_position_key = "base_position"

  def run(
    self,
    strategy: BarStrategy,
    bars: Sequence[Bar],
    warmup_bar_count: int = 0,
  ) -> BacktestResult:
    if warmup_bar_count < 0 or warmup_bar_count > len(bars):
      raise ValueError("预热 Bar 数量无效")
    equity_curve: list[EquityPoint] = []
    portfolio_snapshots: list[PortfolioSnapshot] = []
    warmup_bars = bars[:warmup_bar_count]
    formal_bars = bars[warmup_bar_count:]
    for bar in warmup_bars:
      strategy.on_bar(bar, self._snapshot())
    if formal_bars and self._base_quantity > 0:
      self._initialize_position(strategy, formal_bars[0].symbol)
    self._order_event_sequence = 0
    pending_orders: list[_AcceptedBarOrder] = []
    for index, bar in enumerate(formal_bars):
      self._begin_day_if_needed(bar)
      if pending_orders:
        self._execute_accepted_orders(strategy, bar, pending_orders, False)
        pending_orders = []
      self._ledger.mark(bar.symbol, bar.close)
      orders = strategy.on_bar(bar, self._snapshot())
      is_day_end = self._is_day_end(formal_bars, index)
      accepted_orders = self._accept_orders(strategy, orders, is_day_end, bar.time)
      if self._execute_on_next_bar_open:
        pending_orders = accepted_orders
        snapshot = self._close_day_if_needed_with_events(strategy, bar, is_day_end)
      else:
        snapshot = self._execute_accepted_orders(strategy, bar, accepted_orders, is_day_end)
      equity_curve.append(EquityPoint(bar.time, snapshot.equity, snapshot.cash))
      portfolio_snapshots.append(snapshot)
    unexecuted_orders = [item.intent for item in pending_orders]
    for item in pending_orders:
      self._notify_order_event(strategy, self._order_event(
        item, "unexecuted", formal_bars[-1].time, rejection_reason="回测区间结束，下一开盘委托未执行",
      ))
    return BacktestResult(
      self._ledger.fills,
      equity_curve,
      self._snapshot(),
      portfolio_snapshots,
      unexecuted_orders,
    )

  def _accept_orders(
    self,
    strategy: BarStrategy,
    orders: Sequence[OrderIntent],
    is_day_end: bool,
    event_time: str,
  ) -> list[_AcceptedBarOrder]:
    accepted: list[_AcceptedBarOrder] = []
    normalized_orders = [self._normalize_overlay_order(order) for order in orders]
    for order in self._eligible_orders(normalized_orders, is_day_end):
      self._order_event_sequence += 1
      item = _AcceptedBarOrder(f"bar-{self._order_event_sequence}", order)
      accepted.append(item)
      self._notify_order_event(strategy, self._order_event(item, "accepted", event_time))
    return accepted

  def _execute_accepted_orders(
    self,
    strategy: BarStrategy,
    bar: Bar,
    orders: Sequence[_AcceptedBarOrder],
    is_day_end: bool,
  ) -> PortfolioSnapshot:
    for item in orders:
      executable_order = self._limit_order_to_available_position(item.intent)
      if executable_order is None:
        rejection_reason = self._execution_rejection_reason(item.intent) or "订单没有可成交数量"
        self._notify_order_event(strategy, self._order_event(
          item, "rejected", bar.time, rejection_reason=rejection_reason,
        ))
        continue
      execution_item = _AcceptedBarOrder(item.order_id, executable_order)
      fill = self._execution_model.create_fill(executable_order, bar, self._commission_rate, self._fee_profile)
      if fill is None:
        self._notify_order_event(strategy, self._order_event(
          item, "rejected", bar.time, rejection_reason="标准 Bar 成交模型未产生有效成交",
        ))
        continue
      applied_fill = self._ledger.apply_fill(fill)
      if executable_order.position_effect == PositionEffect.CLOSE_LONG:
        position_id = (executable_order.symbol, executable_order.position_key)
        self._day_sellable_by_key[position_id] = max(
          0, self._day_sellable_by_key.get(position_id, 0) - applied_fill.quantity,
        )
      if self._uses_t0_capacity(executable_order):
        self._t0_remaining = max(0, self._t0_remaining - applied_fill.quantity)
      self._notify_order_event(strategy, self._order_event(execution_item, "filled", applied_fill.time, applied_fill))
    return self._close_day_if_needed_with_events(strategy, bar, is_day_end)

  def _close_day_if_needed_with_events(
    self,
    strategy: BarStrategy,
    bar: Bar,
    is_day_end: bool,
  ) -> PortfolioSnapshot:
    if self._restores_at_day_end() and is_day_end:
      orders = self._settlement_orders(bar)
      accepted = self._accept_orders(strategy, orders, False, bar.time)
      self._execute_accepted_orders(strategy, bar, accepted, False)
    return self._snapshot()

  @staticmethod
  def _notify_order_event(strategy: BarStrategy, event: dict[str, Any]) -> None:
    callback = getattr(strategy, "on_order_event", None)
    if callable(callback):
      callback(event)

  @staticmethod
  def _order_event(
    item: _AcceptedBarOrder,
    event_type: str,
    event_time: str,
    fill: Fill | None = None,
    rejection_reason: str | None = None,
  ) -> dict[str, Any]:
    order = item.intent
    metadata = order.metadata
    direction = str(metadata.get("direction") or order.side.value)
    event: dict[str, Any] = {
      "type": event_type,
      "orderId": item.order_id,
      "symbol": order.symbol,
      "side": order.side.value,
      "direction": direction,
      "action": direction,
      "quantity": fill.quantity if fill is not None else order.quantity,
      "reason": order.reason,
      "signalTime": str(metadata.get("signal_time") or order.time or event_time),
      "clientOrderKey": metadata.get("client_order_key"),
      "positionEffect": order.position_effect.value if order.position_effect else None,
      "source": str(metadata.get("source") or "strategy"),
    }
    if event_type == "accepted":
      event["effectiveAt"] = event_time
    if fill is not None:
      event.update({
        "time": fill.time,
        "executionTime": fill.time,
        "price": fill.price,
        "commission": fill.fee,
        "cumulativeQuantity": fill.quantity,
        "remaining": 0,
      })
    if rejection_reason:
      event["rejectionReason"] = rejection_reason
    return event

  def run_batch(
    self,
    strategy: BatchStrategy,
    bars: Sequence[Bar],
  ) -> BacktestResult:
    """执行带时间戳订单的兼容策略，保证订单只在对应 K 线成交。"""
    scheduled_orders: dict[str, list[OrderIntent]] = defaultdict(list)
    for order in strategy.generate_orders(bars):
      if order.time is None:
        raise ValueError("批处理策略订单必须包含 time")
      scheduled_orders[order.time].append(order)

    equity_curve: list[EquityPoint] = []
    portfolio_snapshots: list[PortfolioSnapshot] = []
    if bars and self._base_quantity > 0:
      self._initialize_position(strategy, bars[0].symbol)  # type: ignore[arg-type]
    for index, bar in enumerate(bars):
      self._begin_day_if_needed(bar)
      self._ledger.mark(bar.symbol, bar.close)
      snapshot = self._execute_orders(
        bar,
        scheduled_orders.pop(bar.time, []),
        self._is_day_end(bars, index),
      )
      equity_curve.append(EquityPoint(bar.time, snapshot.equity, snapshot.cash))
      portfolio_snapshots.append(snapshot)

    if scheduled_orders:
      missing_times = ", ".join(sorted(scheduled_orders)[:3])
      raise ValueError(f"订单时间不在回测 K 线中: {missing_times}")
    return BacktestResult(
      self._ledger.fills,
      equity_curve,
      self._snapshot(),
      portfolio_snapshots,
    )

  def run_stream(
    self,
    strategy: StreamingStrategy,
    bars: Sequence[Bar],
  ) -> BacktestResult:
    """在每根 K 线完成后，立即处理策略发出的订单意图。"""
    equity_curve: list[EquityPoint] = []
    portfolio_snapshots: list[PortfolioSnapshot] = []
    next_index = 0
    if bars and self._base_quantity > 0:
      self._initialize_position(strategy, bars[0].symbol)  # type: ignore[arg-type]

    def emit(bar: Bar, orders: Sequence[OrderIntent]) -> None:
      nonlocal next_index
      if next_index >= len(bars) or bars[next_index].time != bar.time:
        raise ValueError("流式策略 K 线顺序与回测数据不一致")
      self._begin_day_if_needed(bar)
      self._ledger.mark(bar.symbol, bar.close)
      snapshot = self._execute_orders(bar, orders, self._is_day_end(bars, next_index))
      equity_curve.append(EquityPoint(bar.time, snapshot.equity, snapshot.cash))
      portfolio_snapshots.append(snapshot)
      next_index += 1

    strategy.stream(bars, emit)
    if next_index != len(bars):
      raise ValueError("流式策略未处理全部 K 线")
    return BacktestResult(
      self._ledger.fills,
      equity_curve,
      self._snapshot(),
      portfolio_snapshots,
    )

  def _execute_orders(
    self,
    bar: Bar,
    orders: Sequence[OrderIntent],
    is_day_end: bool,
  ) -> PortfolioSnapshot:
    normalized = [self._normalize_overlay_order(order) for order in orders]
    for order in self._eligible_orders(normalized, is_day_end):
      if order.symbol != bar.symbol:
        raise ValueError("订单标的与 K 线标的不一致")
      executable_order = self._limit_order_to_available_position(order)
      if executable_order is None:
        continue
      fill = self._execution_model.create_fill(executable_order, bar, self._commission_rate, self._fee_profile)
      if fill is not None:
        applied_fill = self._ledger.apply_fill(fill)
        if executable_order.position_effect == PositionEffect.CLOSE_LONG:
          position_id = (executable_order.symbol, executable_order.position_key)
          self._day_sellable_by_key[position_id] = max(
            0, self._day_sellable_by_key.get(position_id, 0) - applied_fill.quantity,
          )
        if self._uses_t0_capacity(executable_order):
          self._t0_remaining = max(0, self._t0_remaining - fill.quantity)
    return self._close_day_if_needed(bar, is_day_end)

  def _close_day_if_needed(self, bar: Bar, is_day_end: bool) -> PortfolioSnapshot:
    if self._restores_at_day_end() and is_day_end:
      for order in self._settlement_orders(bar):
        fill = self._execution_model.create_fill(order, bar, self._commission_rate, self._fee_profile)
        if fill is not None:
          self._ledger.apply_fill(fill)
    return self._snapshot()

  def _begin_day_if_needed(self, bar: Bar) -> None:
    if self._t0_date == bar.date:
      return
    self._t0_date = bar.date
    self._t0_remaining = self._base_quantity if self._t0_enabled else 0
    snapshot = self._ledger.snapshot()
    self._day_sellable_by_key = {
      identity: position.quantity
      for identity, position in snapshot.long_positions_by_identity.items()
    }

  def _eligible_orders(
    self,
    orders: Sequence[OrderIntent],
    is_day_end: bool,
  ) -> list[OrderIntent]:
    eligible = list(orders)
    if self._t0_enabled:
      remaining = self._t0_remaining
      t0_eligible: list[OrderIntent] = []
      for order in eligible:
        if not self._uses_t0_capacity(order):
          t0_eligible.append(order)
          continue
        executable_quantity = min(order.quantity, remaining)
        if executable_quantity <= 0:
          continue
        t0_eligible.append(
          order if executable_quantity == order.quantity else replace(order, quantity=executable_quantity)
        )
        remaining -= executable_quantity
      eligible = t0_eligible
    if not self._restores_at_day_end() or not is_day_end:
      return eligible
    opening_effects = {PositionEffect.OPEN_LONG, PositionEffect.OPEN_SHORT}
    return [order for order in eligible if order.position_effect not in opening_effects]

  def _uses_t0_capacity(self, order: OrderIntent) -> bool:
    if self._is_overlay_mode():
      return self._t0_enabled and order.metadata.get("source") != "system"
    return self._t0_enabled and order.position_effect in {PositionEffect.OPEN_LONG, PositionEffect.OPEN_SHORT}

  def _initialize_position(self, strategy: BarStrategy, symbol: str) -> None:
    self._initial_position_key = (
      "base_position" if self._is_overlay_mode()
      else str(getattr(strategy, "position_key", "default"))
    )
    self._ledger.initialize_long_position(
      symbol, self._base_quantity, self._base_cost_price, self._initial_position_key,
    )

  def _normalize_overlay_order(self, order: OrderIntent) -> OrderIntent:
    if not self._is_overlay_mode():
      return order
    effect = PositionEffect.OPEN_LONG if order.side == OrderSide.BUY else PositionEffect.CLOSE_LONG
    metadata = {
      **order.metadata,
      "strategy_position_effect": order.position_effect.value if order.position_effect else None,
      "strategy_position_key": order.position_key,
      "inventory_source": "base_delivery" if order.side == OrderSide.SELL else "overlay_inventory",
    }
    return OrderIntent(
      symbol=order.symbol,
      side=order.side,
      quantity=order.quantity,
      reason=order.reason,
      limit_price=order.limit_price,
      time=order.time,
      position_effect=effect,
      position_key="base_position",
      metadata=metadata,
    )

  def _execution_rejection_reason(self, order: OrderIntent) -> str | None:
    if order.position_effect != PositionEffect.CLOSE_LONG:
      return None
    snapshot = self._ledger.snapshot()
    position = snapshot.long_positions_by_identity.get((order.symbol, order.position_key))
    if position is None or position.quantity <= 0:
      return "平多数量超过实际持仓"
    sellable = self._day_sellable_by_key.get((order.symbol, order.position_key), 0)
    return "T+1 可卖数量不足" if sellable <= 0 else None

  def _limit_order_to_available_position(self, order: OrderIntent) -> OrderIntent | None:
    if order.position_effect != PositionEffect.CLOSE_LONG:
      return order
    snapshot = self._ledger.snapshot()
    position = snapshot.long_positions_by_identity.get((order.symbol, order.position_key))
    if position is None:
      return None
    sellable = self._day_sellable_by_key.get((order.symbol, order.position_key), 0)
    executable_quantity = min(order.quantity, position.quantity, sellable)
    if executable_quantity <= 0:
      return None
    if executable_quantity == order.quantity:
      return order
    return replace(order, quantity=executable_quantity)

  def _settlement_orders(self, bar: Bar) -> list[OrderIntent]:
    if not self._is_overlay_mode():
      excluded = {self._initial_position_key} if self._base_quantity > 0 else None
      return self._ledger.force_close_orders(bar, excluded)
    snapshot = self._ledger.snapshot()
    position = snapshot.long_positions_by_identity.get((bar.symbol, "base_position"))
    actual_quantity = position.quantity if position else 0
    difference = self._base_quantity - actual_quantity
    if difference == 0:
      return []
    side = OrderSide.BUY if difference > 0 else OrderSide.SELL
    effect = PositionEffect.OPEN_LONG if difference > 0 else PositionEffect.CLOSE_LONG
    return [OrderIntent(
      symbol=bar.symbol,
      side=side,
      quantity=abs(difference),
      reason="尾盘恢复底仓",
      time=bar.time,
      position_effect=effect,
      position_key="base_position",
      metadata={"direction": "FORCE_SETTLEMENT", "source": "system", "recorded_price": bar.close},
    )]

  def _is_overlay_mode(self) -> bool:
    return self._strategy_category == "core_position_overlay"

  def _restores_at_day_end(self) -> bool:
    if self._is_overlay_mode():
      return self._overlay_settlement_policy == "intraday_restore"
    return self._holding_policy == "intraday"

  def _snapshot(self) -> PortfolioSnapshot:
    snapshot = self._ledger.snapshot()
    actual_quantity = sum(item.quantity for item in snapshot.long_positions.values()) - sum(
      item.quantity for item in snapshot.short_positions.values()
    )
    return PortfolioSnapshot(
      cash=snapshot.cash,
      equity=snapshot.equity,
      positions=snapshot.positions,
      long_positions=snapshot.long_positions,
      short_positions=snapshot.short_positions,
      long_positions_by_key=snapshot.long_positions_by_key,
      short_positions_by_key=snapshot.short_positions_by_key,
      long_positions_by_identity=snapshot.long_positions_by_identity,
      short_positions_by_identity=snapshot.short_positions_by_identity,
      realized_pnl=snapshot.realized_pnl,
      base_position=self._base_quantity,
      overlay_quantity=actual_quantity - self._base_quantity if self._is_overlay_mode() else None,
      sellable_quantity=sum(self._day_sellable_by_key.values()),
      t0_available_quantity=self._t0_remaining if self._t0_enabled else None,
    )

  @staticmethod
  def _is_day_end(bars: Sequence[Bar], index: int) -> bool:
    return index == len(bars) - 1 or bars[index + 1].date != bars[index].date
