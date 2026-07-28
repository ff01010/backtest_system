"""趋势网格的正式 Bar 策略实现。"""

from __future__ import annotations

from typing import Any, Callable, Sequence

import pandas as pd

from backend.backtesting.models import (
  Bar,
  OrderIntent,
  OrderSide,
  PortfolioSnapshot,
  PositionEffect,
)


def _position_effect(direction: str) -> PositionEffect | None:
  effects = {
    "BUY_OPEN": PositionEffect.OPEN_LONG,
    "SELL_OPEN": PositionEffect.OPEN_SHORT,
    "SELL_COVER": PositionEffect.CLOSE_LONG,
    "STOP_SELL": PositionEffect.CLOSE_LONG,
    "FORCE_SELL": PositionEffect.CLOSE_LONG,
    "BUY_COVER": PositionEffect.CLOSE_SHORT,
    "STOP_BUY": PositionEffect.CLOSE_SHORT,
    "FORCE_BUY": PositionEffect.CLOSE_SHORT,
  }
  return effects.get(direction)


def _trade_to_order(
  trade: dict[str, Any],
  bar: Bar,
  position_key: str,
  module: str,
) -> OrderIntent:
  direction = str(trade.get("direction", ""))
  metadata = {
    "anchor": float(trade.get("anchor", 0) or 0),
    "direction": direction,
    "recorded_pnl": float(trade.get("pnl", 0) or 0),
    "recorded_price": float(trade["price"]),
    "recorded_time": str(trade.get("time", bar.time)),
    "signal_time": bar.time,
    "strategy_module": module,
  }
  effect = _position_effect(direction)
  if effect:
    metadata["position_effect"] = effect.value
  return OrderIntent(
    symbol=bar.symbol,
    side=OrderSide.BUY if "BUY" in direction else OrderSide.SELL,
    quantity=int(trade["qty"]),
    reason=str(trade.get("note", "")),
    time=bar.time,
    position_effect=effect,
    position_key=position_key,
    metadata=metadata,
  )


class TrendGridBarStrategy:
  """将趋势网格状态机接入通用 Bar 回测内核。"""

  def __init__(
    self,
    config: dict[str, Any],
    engine_factory: Callable[[dict[str, Any]], Any],
    htf_bars: pd.DataFrame | None = None,
    day_end_times: set[str] | None = None,
  ) -> None:
    self._config = config
    self._engine_factory = engine_factory
    self._htf_bars = htf_bars
    self._engine: Any | None = None
    self._trade_counts = {"A": 0, "B": 0}
    self._day_end_times = day_end_times or set()
    self._holding_policy = str(config.get("backtest", {}).get("holding_policy", "overnight"))
    self._recently_settled_keys: set[str] = set()
    self.warnings: list[str] = []

  def on_bar(
    self,
    bar: Bar,
    portfolio: PortfolioSnapshot,
  ) -> Sequence[OrderIntent]:
    engine = self._ensure_engine()
    is_intraday_day_end = (
      self._holding_policy == "intraday" and bar.time in self._day_end_times
    )
    if is_intraday_day_end:
      engine._entry_locked = True
    engine.on_bar(pd.Series({
      "time": bar.time,
      "open": bar.open,
      "high": bar.high,
      "low": bar.low,
      "close": bar.close,
      "volume": bar.volume,
    }), self._htf_bars)
    orders = self._filter_stale_close_orders(self._new_orders(engine, bar), portfolio, bar)
    if is_intraday_day_end:
      self._recently_settled_keys.update({"trend_grid:A", "trend_grid:B"})
      engine.settle_intraday_positions()
    return orders

  def _ensure_engine(self) -> Any:
    if self._engine is None:
      self._engine = self._engine_factory(self._config)
    return self._engine

  def _new_orders(self, engine: Any, bar: Bar) -> list[OrderIntent]:
    orders: list[OrderIntent] = []
    for module, trades in (("A", engine.trades_a), ("B", engine.trades_b)):
      start = self._trade_counts[module]
      self._trade_counts[module] = len(trades)
      position_key = f"trend_grid:{module}"
      orders.extend(
        _trade_to_order(trade, bar, position_key, module)
        for trade in trades[start:]
      )
    return orders

  def _filter_stale_close_orders(
    self,
    orders: list[OrderIntent],
    portfolio: PortfolioSnapshot,
    bar: Bar,
  ) -> list[OrderIntent]:
    filtered: list[OrderIntent] = []
    for order in orders:
      if not self._is_stale_intraday_close(order, portfolio):
        filtered.append(order)
        continue
      direction = str(order.metadata.get("direction", order.position_effect or "平仓"))
      self.warnings.append(
        f"{bar.time} 已跳过 {direction}：该仓位已在前一交易日日终结算"
      )
      self._recently_settled_keys.discard(order.position_key)
    return filtered

  def _is_stale_intraday_close(
    self,
    order: OrderIntent,
    portfolio: PortfolioSnapshot,
  ) -> bool:
    if self._holding_policy != "intraday" or order.position_key not in self._recently_settled_keys:
      return False
    if order.position_effect == PositionEffect.CLOSE_LONG:
      position = portfolio.long_positions_by_key.get(order.position_key)
    elif order.position_effect == PositionEffect.CLOSE_SHORT:
      position = portfolio.short_positions_by_key.get(order.position_key)
    else:
      return False
    return position is None or position.quantity == 0
