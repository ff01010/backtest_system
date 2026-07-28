"""趋势网格旧引擎到订单意图协议的兼容插件。"""

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
  return effects.get(direction, "")


class LegacyTrendGridIntentStrategy:
  """将旧趋势网格引擎输出转换为通用订单意图。"""

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
    self.legacy_engine: Any | None = None
    self.total_pnl = 0.0

  def generate_orders(self, bars: Sequence[Bar]) -> Sequence[OrderIntent]:
    if not bars:
      return []
    engine = self._engine_factory(self._config)
    frame = pd.DataFrame([{
      "time": bar.time,
      "open": bar.open,
      "high": bar.high,
      "low": bar.low,
      "close": bar.close,
      "volume": bar.volume,
    } for bar in bars])
    engine.run(frame, self._htf_bars)
    self.total_pnl = float(engine.report())
    self.legacy_engine = engine

    trades = list(engine.trades_a) + list(engine.trades_b)
    last_time_by_date = {bar.date: bar.time for bar in bars}
    return [
      self._to_order(trade, bars[0].symbol, last_time_by_date)
      for trade in trades
    ]

  @staticmethod
  def _to_order(
    trade: dict[str, Any],
    symbol: str,
    last_time_by_date: dict[str, str],
  ) -> OrderIntent:
    direction = str(trade.get("direction", ""))
    recorded_time = str(trade["time"])
    scheduled_time = last_time_by_date.get(recorded_time, recorded_time)
    metadata = {
      "anchor": float(trade.get("anchor", 0) or 0),
      "direction": direction,
      "recorded_pnl": float(trade.get("pnl", 0) or 0),
      "recorded_price": float(trade["price"]),
      "recorded_time": recorded_time,
      "signal_time": scheduled_time,
    }
    effect = _position_effect(direction)
    if effect:
      metadata["position_effect"] = effect.value
    return OrderIntent(
      symbol=symbol,
      side=OrderSide.BUY if "BUY" in direction else OrderSide.SELL,
      quantity=int(trade["qty"]),
      reason=str(trade.get("note", "")),
      time=scheduled_time,
      position_effect=effect,
      metadata=metadata,
    )


class LegacyTrendGridOnBarStrategy:
  """以旧引擎状态机驱动的逐 K 线订单意图策略。"""

  def __init__(
    self,
    config: dict[str, Any],
    engine_factory: Callable[[dict[str, Any]], Any],
    htf_bars: pd.DataFrame | None = None,
  ) -> None:
    self._config = config
    self._engine_factory = engine_factory
    self._htf_bars = htf_bars
    self.legacy_engine: Any | None = None
    self.total_pnl = 0.0

  def stream(
    self,
    bars: Sequence[Bar],
    emit: Callable[[Bar, Sequence[OrderIntent]], None],
  ) -> None:
    if not bars:
      return
    engine = self._engine_factory(self._config)
    bars_by_time = {bar.time: bar for bar in bars}
    trade_counts = {"a": 0, "b": 0}

    def on_bar_complete(row: pd.Series) -> None:
      time = str(row["time"])
      bar = bars_by_time.get(time)
      if bar is None:
        raise ValueError(f"旧引擎返回未知 K 线: {time}")
      new_trades = (
        list(engine.trades_a[trade_counts["a"]:])
        + list(engine.trades_b[trade_counts["b"]:])
      )
      trade_counts["a"] = len(engine.trades_a)
      trade_counts["b"] = len(engine.trades_b)
      orders = [self._to_order(trade, bar) for trade in new_trades]
      emit(bar, orders)

    frame = pd.DataFrame([{
      "time": bar.time,
      "open": bar.open,
      "high": bar.high,
      "low": bar.low,
      "close": bar.close,
      "volume": bar.volume,
    } for bar in bars])
    engine.run(frame, self._htf_bars, on_bar_complete)
    self.total_pnl = float(engine.report())
    self.legacy_engine = engine

  @staticmethod
  def _to_order(
    trade: dict[str, Any],
    bar: Bar,
    position_key: str = "default",
  ) -> OrderIntent:
    direction = str(trade.get("direction", ""))
    metadata = {
      "anchor": float(trade.get("anchor", 0) or 0),
      "direction": direction,
      "recorded_pnl": float(trade.get("pnl", 0) or 0),
      "recorded_price": float(trade["price"]),
      "recorded_time": str(trade.get("time", bar.time)),
      "signal_time": bar.time,
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


class LegacyTrendGridBarStrategy:
  """将趋势网格状态机作为真正的逐 Bar 策略接入通用内核。"""

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
        LegacyTrendGridOnBarStrategy._to_order(trade, bar, position_key)
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


from backend.strategies.trend_grid import TrendGridBarStrategy
