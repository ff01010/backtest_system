"""旧策略回测结果到通用领域模型的过渡适配。"""

from __future__ import annotations

from typing import Any, Iterable

from backend.backtesting.models import Fill, OrderSide


def legacy_trades_to_fills(
  trades: Iterable[dict[str, Any]],
  symbol: str,
) -> list[Fill]:
  """把旧引擎的字典交易记录转换为标准成交记录。"""
  fills: list[Fill] = []
  for trade in trades:
    direction = str(trade.get("direction", ""))
    side = OrderSide.BUY if "BUY" in direction else OrderSide.SELL
    quantity = int(trade.get("qty", 0) or 0)
    price = float(trade.get("price", 0) or 0)
    if quantity <= 0 or price <= 0:
      continue
    fills.append(Fill(
      time=str(trade.get("time", "")),
      symbol=symbol,
      side=side,
      quantity=quantity,
      price=price,
      fee=0.0,
      reason=str(trade.get("note", "")),
      realized_pnl=float(trade.get("pnl", 0) or 0),
      metadata={
        "anchor": float(trade.get("anchor", 0) or 0),
        "direction": direction,
      },
    ))
  return fills


def fill_to_api_trade(fill: Fill) -> dict[str, Any]:
  """将内部记账记录转换为前端需要的信号明细。"""
  trade = {
    "time": str(fill.metadata.get("signal_time", fill.time)),
    "signalTime": str(fill.metadata.get("signal_time", fill.time)),
    "executionTime": str(fill.time),
    "direction": str(fill.metadata.get("direction", fill.side.value)),
    "action": str(fill.metadata.get("direction", fill.side.value)),
    "side": fill.side.value,
    "anchor": float(fill.metadata.get("anchor", 0) or 0),
    "price": float(fill.price),
    "quantity": int(fill.quantity),
    "pnl": float(fill.realized_pnl),
    "note": fill.reason,
    "priceSource": str(
      fill.metadata.get(
        "price_source",
        "当前 Bar 收盘价" if "recorded_price" in fill.metadata else "成交模型价格",
      )
    ),
    "status": "filled",
  }
  module = str(fill.metadata.get("strategy_module", ""))
  if module:
    trade["strategyModule"] = module
  return trade
