"""L2 原始回放结果到统一回测响应的适配。"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any


_TRADING_DAYS_PER_YEAR = 242


def normalize_l2_result(raw_result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
  """将 L2 撮合明细转换为概览、历史和信号页共用的结果结构。"""
  if isinstance(raw_result.get("summary"), dict):
    result = dict(raw_result)
    daily_holdings = _normalize_daily_holdings(_records(result.get("dailyHoldings")))
    result["dailyHoldings"] = daily_holdings
    result["equity"] = _normalize_equity_dates(_records(result.get("equity")))
    result["trades"] = [_normalize_trade(item) for item in _records(result.get("trades"))]
    result["skippedSignals"] = [_normalize_skipped_signal(item) for item in _records(result.get("skippedSignals"))]
    result["strategyCategory"] = _strategy_category(payload)
    result["resultSchemaVersion"] = int(result.get("resultSchemaVersion") or 1)
    result["summary"] = {
      **raw_result["summary"],
      **_build_base_metrics(
        _number(payload.get("initialCapital"), 0.0),
        int(_number(payload.get("baseQuantity"), 0)),
        _number(payload.get("baseCostPrice"), 0.0),
        bool(payload.get("t0Enabled", False)),
        daily_holdings,
      ),
    }
    result["strategyMetrics"] = _strategy_metrics(result["strategyCategory"], result["summary"])
    return result

  initial_cash = _number(payload.get("initialCapital"), 0.0)
  base_quantity = int(_number(payload.get("baseQuantity"), 0))
  base_cost_price = _number(payload.get("baseCostPrice"), 0.0)
  initial_capital = initial_cash + base_quantity * base_cost_price
  daily_holdings = _build_daily_holdings(raw_result.get("equity"), raw_result.get("fills"), initial_capital, base_quantity)
  trades = [_fill_to_trade(item) for item in _records(raw_result.get("fills"))]
  skipped_signals = _build_skipped_signals(raw_result)
  summary = _build_summary(initial_capital, trades, daily_holdings, initial_cash, base_quantity, base_cost_price, bool(payload.get("t0Enabled", False)))
  summary.update(_execution_counts(raw_result.get("orders"), trades))
  warnings = [str(item) for item in raw_result.get("warnings", [])]
  benchmark_symbol = payload.get("benchmarkSymbol") or None
  if benchmark_symbol:
    warnings.append("L2 回测暂未计算基准曲线，超额收益显示为 --")

  category = _strategy_category(payload)
  return {
    "strategyCategory": category,
    "resultSchemaVersion": 2,
    "strategyMetrics": _strategy_metrics(category, summary),
    "outputDir": "",
    "summary": summary,
    "equity": [
      {"date": item["date"], "pnl": item["dailyPnl"], "value": item["totalEquity"]}
      for item in daily_holdings
    ],
    "benchmarkSymbol": benchmark_symbol,
    "benchmarkEquity": [],
    "benchmarkReturn": None,
    "excessReturn": None,
    "dailyHoldings": daily_holdings,
    "trades": trades,
    "skippedSignals": skipped_signals,
    "orderEvents": _records(raw_result.get("orders")),
    "dataSnapshot": raw_result.get("dataSnapshot") or {},
    "performance": _mapping(raw_result.get("performance")),
    "experimentManifest": _build_manifest(raw_result, payload),
    "warnings": warnings,
  }


def _build_daily_holdings(
  raw_equity: object,
  raw_fills: object,
  initial_capital: float,
  base_quantity: int = 0,
) -> list[dict[str, Any]]:
  daily_points: dict[str, dict[str, Any]] = {}
  for point in _records(raw_equity):
    date = _date_key(point.get("time"))
    if date:
      daily_points[date] = point

  fills = sorted(_records(raw_fills), key=lambda item: _time_key(item.get("time")))
  fill_index = 0
  long_quantity = base_quantity
  short_quantity = 0
  last_fill_price = 0.0
  previous_total = initial_capital
  holdings: list[dict[str, Any]] = []
  for compact_date, point in sorted(daily_points.items()):
    point_time = _time_key(point.get("time"))
    while fill_index < len(fills) and _time_key(fills[fill_index].get("time")) <= point_time:
      fill = fills[fill_index]
      quantity = max(0, int(_number(fill.get("quantity"), 0)))
      effect = str(fill.get("position_effect") or "")
      if effect == "open_long":
        long_quantity += quantity
      elif effect == "close_long":
        long_quantity = max(0, long_quantity - quantity)
      elif effect == "open_short":
        short_quantity += quantity
      elif effect == "close_short":
        short_quantity = max(0, short_quantity - quantity)
      last_fill_price = _number(fill.get("price"), last_fill_price)
      fill_index += 1

    total_equity = _number(point.get("equity"), previous_total)
    cash = _number(point.get("cash"), total_equity)
    close = _number(point.get("close"), last_fill_price)
    long_quantity = int(_number(point.get("longQty"), long_quantity))
    short_quantity = int(_number(point.get("shortQty"), short_quantity))
    long_value, short_value = _market_values(
      long_quantity, short_quantity, close, total_equity - cash,
    )
    holdings.append({
      "date": _iso_date(compact_date),
      "close": round(close, 4),
      "longQty": long_quantity,
      "shortQty": short_quantity,
      "cash": round(cash, 2),
      "longMarketValue": round(long_value, 2),
      "shortMarketValue": round(short_value, 2),
      "marketValue": round(long_value - short_value, 2),
      "totalEquity": round(total_equity, 2),
      "dailyPnl": round(total_equity - previous_total, 2),
    })
    previous_total = total_equity
  return holdings


def _market_values(
  long_quantity: int,
  short_quantity: int,
  close: float,
  net_market_value: float,
) -> tuple[float, float]:
  if close > 0:
    return long_quantity * close, short_quantity * close
  if long_quantity and not short_quantity:
    return max(0.0, net_market_value), 0.0
  if short_quantity and not long_quantity:
    return 0.0, max(0.0, -net_market_value)
  return 0.0, 0.0


def _fill_to_trade(fill: dict[str, Any]) -> dict[str, Any]:
  metadata = _mapping(fill.get("metadata"))
  side = _normalize_side(fill.get("side"))
  direction = str(metadata.get("direction") or side)
  trade = {
    "time": str(metadata.get("signal_time") or fill.get("time") or ""),
    "signalTime": str(metadata.get("signal_time") or fill.get("time") or ""),
    "executionTime": str(fill.get("time") or ""),
    "direction": direction,
    "action": direction,
    "side": side,
    "anchor": _number(metadata.get("anchor"), 0.0),
    "price": _number(fill.get("price"), 0.0),
    "quantity": int(_number(fill.get("quantity"), 0)),
    "pnl": _number(fill.get("realized_pnl"), 0.0),
    "note": str(fill.get("reason") or ""),
    "priceSource": str(metadata.get("price_source") or "L2 成交模型价格"),
    "status": "filled",
  }
  if metadata.get("strategy_module"):
    trade["strategyModule"] = str(metadata["strategy_module"])
  return trade


def _normalize_trade(trade: dict[str, Any]) -> dict[str, Any]:
  normalized = dict(trade)
  normalized["side"] = _normalize_side(trade.get("side"))
  return normalized


def _build_skipped_signals(raw_result: dict[str, Any]) -> list[dict[str, Any]]:
  signals = [_normalize_skipped_signal(item) for item in _records(raw_result.get("skippedSignals"))]
  events = _records(raw_result.get("orders"))
  accepted = {
    str(item.get("orderId")): item
    for item in events
    if item.get("type") == "accepted" and item.get("orderId")
  }
  for event in events:
    event_type = str(event.get("type") or "")
    if event_type == "rejected":
      signals.append(_event_to_skipped_signal(event, str(event.get("rejectionReason") or "订单被拒绝")))
    elif event_type == "cancelled" and int(_number(event.get("remaining"), 0)) > 0:
      accepted_event = accepted.get(str(event.get("orderId")))
      if accepted_event:
        remaining = int(_number(event.get("remaining"), 0))
        signals.append(_event_to_skipped_signal(
          accepted_event,
          f"{event.get('reason') or '订单撤销'}，剩余 {remaining} 股未成交",
          remaining,
        ))
  return signals


def _normalize_skipped_signal(signal: dict[str, Any]) -> dict[str, Any]:
  side = _normalize_side(signal.get("side"))
  direction = str(signal.get("direction") or signal.get("action") or side)
  return {
    "time": str(signal.get("time") or signal.get("signalTime") or ""),
    "signalTime": str(signal.get("signalTime") or signal.get("time") or ""),
    "executionTime": None,
    "direction": direction,
    "action": str(signal.get("action") or direction),
    "side": side,
    "anchor": _number(signal.get("anchor"), 0.0),
    "price": _number(signal.get("price"), 0.0),
    "quantity": int(_number(signal.get("quantity"), 0)),
    "pnl": 0.0,
    "note": str(signal.get("note") or signal.get("reason") or "策略信号未执行"),
    "priceSource": "",
    "status": "skipped",
  }


def _event_to_skipped_signal(event: dict[str, Any], note: str, quantity: int | None = None) -> dict[str, Any]:
  side = _normalize_side(event.get("side"))
  direction = str(event.get("direction") or side)
  signal = {
    "time": str(event.get("signalTime") or ""),
    "signalTime": str(event.get("signalTime") or ""),
    "executionTime": None,
    "direction": direction,
    "action": str(event.get("action") or direction),
    "side": side,
    "anchor": _number(event.get("anchor"), 0.0),
    "price": _number(event.get("limitPrice"), 0.0),
    "quantity": quantity if quantity is not None else int(_number(event.get("quantity"), 0)),
    "pnl": 0.0,
    "note": f"{event.get('reason') or '策略信号'}：{note}",
    "priceSource": "",
    "status": "skipped",
  }
  if event.get("strategyModule"):
    signal["strategyModule"] = str(event["strategyModule"])
  return signal


def _build_summary(
  initial_capital: float,
  trades: list[dict[str, Any]],
  daily_holdings: list[dict[str, Any]],
  initial_cash: float,
  base_quantity: int,
  base_cost_price: float,
  t0_enabled: bool,
) -> dict[str, Any]:
  daily_pnl = {item["date"]: _number(item.get("dailyPnl"), 0.0) for item in daily_holdings}
  performance = _performance_stats(initial_capital, daily_pnl)
  realized = [trade["pnl"] for trade in trades if abs(trade["pnl"]) > 1e-8]
  wins = [value for value in realized if value > 0]
  losses = [value for value in realized if value < 0]
  final_equity = daily_holdings[-1]["totalEquity"] if daily_holdings else initial_capital
  return {
    "totalPnl": round(final_equity - initial_capital, 2),
    "totalReturn": round(final_equity / initial_capital - 1, 6) if initial_capital > 0 else 0.0,
    "maxDrawdown": round(_max_drawdown_amount(initial_capital, daily_holdings), 2),
    "maxDrawdownRate": performance["maxDrawdownRate"],
    "annualVolatility": performance["annualVolatility"],
    "winRate": round(len(wins) / len(realized), 4) if realized else 0.0,
    "dailyWinRate": round(sum(value > 0 for value in daily_pnl.values()) / len(daily_pnl), 4) if daily_pnl else 0.0,
    "tradeCount": len(trades),
    "winningTrades": len(wins),
    "losingTrades": len(losses),
    "avgWin": round(sum(wins) / len(wins), 2) if wins else 0.0,
    "avgLoss": round(sum(losses) / len(losses), 2) if losses else 0.0,
    "profitLossRatio": round(sum(wins) / abs(sum(losses)), 4) if losses else 0.0,
    "finalEquity": round(final_equity, 2),
    **_build_base_metrics(initial_cash, base_quantity, base_cost_price, t0_enabled, daily_holdings),
    **performance,
  }


def _execution_counts(raw_orders: object, trades: list[dict[str, Any]]) -> dict[str, int]:
  """区分策略接受的委托数与盘口拆分后的成交笔数。"""
  accepted_order_ids = {
    str(item.get("orderId"))
    for item in _records(raw_orders)
    if item.get("type") == "accepted" and item.get("orderId")
  }
  fill_count = len(trades)
  return {
    "orderCount": len(accepted_order_ids) if accepted_order_ids else fill_count,
    "fillCount": fill_count,
    "tradeCount": fill_count,
  }


def _build_base_metrics(
  initial_cash: float,
  base_quantity: int,
  base_cost_price: float,
  t0_enabled: bool,
  daily_holdings: list[dict[str, Any]],
) -> dict[str, float | None]:
  if base_quantity <= 0 or base_cost_price <= 0:
    return {"baseHoldingPnl": None, "baseHoldingReturn": None, "t0ExcessPnl": None, "t0ExcessReturn": None}
  initial_equity = initial_cash + base_quantity * base_cost_price
  final_equity = _number(daily_holdings[-1].get("totalEquity"), initial_equity) if daily_holdings else initial_equity
  final_close = _number(daily_holdings[-1].get("close"), base_cost_price) if daily_holdings else base_cost_price
  base_final_equity = initial_cash + base_quantity * final_close
  base_pnl = base_final_equity - initial_equity
  excess_pnl = final_equity - base_final_equity
  return {
    "baseHoldingPnl": round(base_pnl, 2),
    "baseHoldingReturn": round(base_pnl / initial_equity, 6) if initial_equity > 0 else None,
    "t0ExcessPnl": round(excess_pnl, 2) if t0_enabled else None,
    "t0ExcessReturn": round(excess_pnl / (base_quantity * base_cost_price), 6) if t0_enabled else None,
  }


def _normalize_daily_holdings(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
  return [{**holding, "date": _iso_date(_date_key(holding.get("date")))} for holding in holdings]


def _normalize_equity_dates(equity: list[dict[str, Any]]) -> list[dict[str, Any]]:
  return [{**point, "date": _iso_date(_date_key(point.get("date")))} for point in equity]


def _performance_stats(initial_capital: float, daily_pnl: dict[str, float]) -> dict[str, float]:
  if initial_capital <= 0 or not daily_pnl:
    return {"annualReturn": 0.0, "annualVolatility": 0.0, "annualSharpe": 0.0, "sortino": 0.0, "maxDrawdownRate": 0.0}
  equity = initial_capital
  peak = initial_capital
  returns: list[float] = []
  max_drawdown_rate = 0.0
  for _, pnl in sorted(daily_pnl.items()):
    previous = equity
    equity += pnl
    if previous > 0:
      returns.append(pnl / previous)
    peak = max(peak, equity)
    if peak > 0:
      max_drawdown_rate = max(max_drawdown_rate, (peak - equity) / peak)
  count = len(returns)
  total_return = equity / initial_capital - 1
  annual_return = (1 + total_return) ** (_TRADING_DAYS_PER_YEAR / count) - 1 if count and equity > 0 else 0.0
  average = sum(returns) / count if count else 0.0
  std = _sample_std(returns, average)
  downside = _std([min(value, 0.0) for value in returns], 0.0)
  return {
    "annualReturn": round(annual_return, 6),
    "annualVolatility": round(std * math.sqrt(_TRADING_DAYS_PER_YEAR), 6),
    "annualSharpe": round(average / std * math.sqrt(_TRADING_DAYS_PER_YEAR), 4) if std else 0.0,
    "sortino": round(average / downside * math.sqrt(_TRADING_DAYS_PER_YEAR), 4) if downside else 0.0,
    "maxDrawdownRate": round(max_drawdown_rate, 6),
  }


def _max_drawdown_amount(initial_capital: float, holdings: Iterable[dict[str, Any]]) -> float:
  peak = initial_capital
  drawdown = 0.0
  for item in holdings:
    value = _number(item.get("totalEquity"), initial_capital)
    peak = max(peak, value)
    drawdown = max(drawdown, peak - value)
  return drawdown


def _build_manifest(raw_result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
  return {
    "engineVersion": "l2-quote-v1",
    "signalModel": "on_bar_script",
    "fillModel": "l2_quote",
    "randomSeed": 0,
    "holdingPolicy": payload.get("holdingPolicy", "intraday"),
    "strategyCategory": _strategy_category(payload),
    "resultSchemaVersion": 2,
    "baseQuantity": int(_number(payload.get("baseQuantity"), 0)),
    "baseCostPrice": _number(payload.get("baseCostPrice"), 0.0),
    "t0Enabled": bool(payload.get("t0Enabled", False)),
    "executionEnvironment": "l2-runner",
    "orderEventCount": len(_records(raw_result.get("orders"))),
  }


def _records(value: object) -> list[dict[str, Any]]:
  if not isinstance(value, list):
    return []
  return [dict(item) for item in value if isinstance(item, dict)]


def _normalize_side(value: object) -> str:
  """兼容 HTTP JSON 与 Pipe 传输后的订单方向表示。"""
  side = str(value or "").upper()
  if side.startswith("ORDERSIDE."):
    side = side.rsplit(".", 1)[-1]
  return side


def _mapping(value: object) -> dict[str, Any]:
  return dict(value) if isinstance(value, dict) else {}


def _number(value: object, fallback: float) -> float:
  try:
    number = float(value)  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return fallback
  return number if math.isfinite(number) else fallback


def _strategy_category(payload: dict[str, Any]) -> str:
  category = str(payload.get("strategyCategory") or "")
  if category in {"core_position_overlay", "directional_holding"}:
    return category
  return "core_position_overlay" if int(_number(payload.get("baseQuantity"), 0)) > 0 or bool(payload.get("t0Enabled", False)) else "directional_holding"


def _strategy_metrics(category: str, summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
  if category == "core_position_overlay":
    return {"t0Metrics": {
      "baseHoldingPnl": summary.get("baseHoldingPnl"),
      "baseHoldingReturn": summary.get("baseHoldingReturn"),
      "t0ExcessPnl": summary.get("t0ExcessPnl"),
      "t0ExcessReturn": summary.get("t0ExcessReturn"),
    }}
  return {"holdingMetrics": {
    "totalPnl": summary.get("totalPnl"),
    "totalReturn": summary.get("totalReturn"),
    "maxDrawdown": summary.get("maxDrawdown"),
    "maxDrawdownRate": summary.get("maxDrawdownRate"),
    "finalEquity": summary.get("finalEquity"),
  }}


def _date_key(value: object) -> str:
  digits = "".join(char for char in str(value or "") if char.isdigit())
  return digits[:8] if len(digits) >= 8 else ""


def _iso_date(value: str) -> str:
  return f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 else value


def _time_key(value: object) -> str:
  digits = "".join(char for char in str(value or "") if char.isdigit())
  return digits[:14].ljust(14, "0")


def _std(values: list[float], average: float) -> float:
  if not values:
    return 0.0
  return (sum((value - average) ** 2 for value in values) / len(values)) ** 0.5


def _sample_std(values: list[float], average: float) -> float:
  if len(values) <= 1:
    return 0.0
  return (sum((value - average) ** 2 for value in values) / (len(values) - 1)) ** 0.5
