"""K 线事件驱动的最小通用回测内核。"""

from __future__ import annotations

from dataclasses import dataclass
from collections import defaultdict
from typing import Callable, Protocol, Sequence

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
) -> float:
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
    long_by_key = self._snapshot_positions_by_key(self._long_positions)
    short_by_key = self._snapshot_positions_by_key(self._short_positions)
    long_positions = self._aggregate_positions(long_by_key)
    short_positions = self._aggregate_positions(short_by_key)
    positions = self._net_positions(long_positions, short_positions)
    return PortfolioSnapshot(
      cash=round(self._cash, 8),
      equity=round(self.equity(), 8),
      positions=positions,
      long_positions=long_positions,
      short_positions=short_positions,
      long_positions_by_key=long_by_key,
      short_positions_by_key=short_by_key,
      realized_pnl=round(self._realized_pnl, 8),
    )

  def mark(self, symbol: str, price: float) -> None:
    if price <= 0:
      raise ValueError("市值价格必须大于 0")
    self._last_prices[symbol] = float(price)

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
      realized_pnl=round(realized_pnl, 8),
      metadata=fill.metadata,
    )
    self._fills.append(applied)
    return applied

  def force_close_orders(self, bar: Bar) -> list[OrderIntent]:
    """为日内规则生成收盘系统结算订单。"""
    orders: list[OrderIntent] = []
    for (symbol, position_key), position in self._long_positions.items():
      if position.quantity > 0:
        orders.append(self._force_close_order(
          bar, symbol, position_key, position.quantity, OrderSide.SELL, PositionEffect.CLOSE_LONG
        ))
    for (symbol, position_key), position in self._short_positions.items():
      if position.quantity > 0:
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
    positions: dict[tuple[str, str], _SidePosition],
  ) -> dict[str, PositionSnapshot]:
    return {
      position_key: PositionSnapshot(symbol, position.quantity, position.average_price)
      for (symbol, position_key), position in positions.items()
      if position.quantity > 0
    }

  @staticmethod
  def _aggregate_positions(
    positions_by_key: dict[str, PositionSnapshot],
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
  ) -> Fill | None:
    price = self._resolve_price(order, bar)
    if price is None:
      return None
    fee = _transaction_fee(order.side, price, order.quantity, commission_rate)
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
      metadata=dict(order.metadata),
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
    return Fill(
      time=bar.time,
      symbol=order.symbol,
      side=order.side,
      quantity=order.quantity,
      price=price,
      fee=_transaction_fee(order.side, price, order.quantity, commission_rate),
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
    execution_model: ExecutionModel | None = None,
    holding_policy: str = "overnight",
  ) -> None:
    if commission_rate < 0:
      raise ValueError("手续费率不能小于 0")
    if holding_policy not in {"intraday", "overnight"}:
      raise ValueError("持仓规则必须为 intraday 或 overnight")
    self._ledger = PortfolioLedger(initial_cash)
    self._commission_rate = commission_rate
    self._execution_model = execution_model or ClosePriceExecutionModel()
    self._holding_policy = holding_policy

  def run(self, strategy: BarStrategy, bars: Sequence[Bar]) -> BacktestResult:
    equity_curve: list[EquityPoint] = []
    portfolio_snapshots: list[PortfolioSnapshot] = []
    for index, bar in enumerate(bars):
      self._ledger.mark(bar.symbol, bar.close)
      orders = strategy.on_bar(bar, self._ledger.snapshot())
      snapshot = self._execute_orders(bar, orders, self._is_day_end(bars, index))
      equity_curve.append(EquityPoint(bar.time, snapshot.equity, snapshot.cash))
      portfolio_snapshots.append(snapshot)
    return BacktestResult(
      self._ledger.fills,
      equity_curve,
      self._ledger.snapshot(),
      portfolio_snapshots,
    )

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
    for index, bar in enumerate(bars):
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
      self._ledger.snapshot(),
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

    def emit(bar: Bar, orders: Sequence[OrderIntent]) -> None:
      nonlocal next_index
      if next_index >= len(bars) or bars[next_index].time != bar.time:
        raise ValueError("流式策略 K 线顺序与回测数据不一致")
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
      self._ledger.snapshot(),
      portfolio_snapshots,
    )

  def _execute_orders(
    self,
    bar: Bar,
    orders: Sequence[OrderIntent],
    is_day_end: bool,
  ) -> PortfolioSnapshot:
    for order in self._eligible_orders(orders, is_day_end):
      if order.symbol != bar.symbol:
        raise ValueError("订单标的与 K 线标的不一致")
      fill = self._execution_model.create_fill(order, bar, self._commission_rate)
      if fill is not None:
        self._ledger.apply_fill(fill)
    if self._holding_policy == "intraday" and is_day_end:
      for order in self._ledger.force_close_orders(bar):
        fill = self._execution_model.create_fill(order, bar, self._commission_rate)
        if fill is not None:
          self._ledger.apply_fill(fill)
    return self._ledger.snapshot()

  def _eligible_orders(
    self,
    orders: Sequence[OrderIntent],
    is_day_end: bool,
  ) -> list[OrderIntent]:
    if self._holding_policy != "intraday" or not is_day_end:
      return list(orders)
    opening_effects = {PositionEffect.OPEN_LONG, PositionEffect.OPEN_SHORT}
    return [order for order in orders if order.position_effect not in opening_effects]

  @staticmethod
  def _is_day_end(bars: Sequence[Bar], index: int) -> bool:
    return index == len(bars) - 1 or bars[index + 1].date != bars[index].date
