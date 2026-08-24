"""数据库策略脚本的受控 Bar 执行适配器。"""

from __future__ import annotations

import ast
from typing import Any, Sequence

from backend.backtesting.models import (
  Bar,
  OrderIntent,
  OrderSide,
  PortfolioSnapshot,
  PositionEffect,
)
from backend.l2.models import QuoteSnapshot
from backend.services.backtest_errors import StrategyExecutionError
from backend.strategies.script_time_context import ScriptTimeContextError, normalize_script_bar_time

_ACTIONS: dict[str, tuple[OrderSide, PositionEffect]] = {
  "buy_open": (OrderSide.BUY, PositionEffect.OPEN_LONG),
  "sell_open": (OrderSide.SELL, PositionEffect.OPEN_SHORT),
  "sell_close": (OrderSide.SELL, PositionEffect.CLOSE_LONG),
  "buy_close": (OrderSide.BUY, PositionEffect.CLOSE_SHORT),
}
_BLOCKED_CALLS = {"compile", "eval", "exec", "input", "open"}
_SAFE_BUILTINS = {
  "abs": abs,
  "any": any,
  "bool": bool,
  "dict": dict,
  "float": float,
  "int": int,
  "len": len,
  "max": max,
  "min": min,
  "range": range,
  "round": round,
  "sum": sum,
  "ValueError": ValueError,
  "__import__": __import__,
}


class _ScriptValidator(ast.NodeVisitor):
  """校验脚本接口与订单契约，允许内部策略导入已安装模块。"""

  def visit_Name(self, node: ast.Name) -> None:
    if node.id.startswith("__"):
      raise ValueError("自定义策略脚本不允许使用双下划线名称")
    self.generic_visit(node)

  def visit_Attribute(self, node: ast.Attribute) -> None:
    if node.attr.startswith("__"):
      raise ValueError("自定义策略脚本不允许访问双下划线属性")
    self.generic_visit(node)

  def visit_Call(self, node: ast.Call) -> None:
    if isinstance(node.func, ast.Name) and node.func.id in _BLOCKED_CALLS:
      raise ValueError(f"自定义策略脚本不允许调用 {node.func.id}")
    self.generic_visit(node)


def validate_script_code(code: str) -> None:
  """校验脚本语法及可选的 L2 事件回调接口。"""
  try:
    tree = ast.parse(code)
  except SyntaxError as error:
    raise ValueError(f"Python 语法错误: 第 {error.lineno} 行 {error.msg}") from error
  _ScriptValidator().visit(tree)
  callback = next(
    (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "on_bar"),
    None,
  )
  if callback is None:
    raise ValueError("策略脚本必须定义 on_bar(bar, state)")
  if len(callback.args.args) != 2 or callback.args.vararg or callback.args.kwarg:
    raise ValueError("on_bar 必须恰好接收 bar 与 state 两个参数")
  _validate_literal_orders(callback)
  for callback_name in ("on_quote", "on_order_event"):
    optional_callback = next(
      (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == callback_name),
      None,
    )
    if optional_callback is None:
      continue
    if len(optional_callback.args.args) != 2 or optional_callback.args.vararg or optional_callback.args.kwarg:
      raise ValueError(f"{callback_name} 必须恰好接收 event 与 state 两个参数")
    if callback_name == "on_quote":
      _validate_literal_orders(optional_callback)


def quote_mode_for_script(code: str, configured_mode: str | None = None) -> str:
  """根据已保存脚本确定 L2 快照回放方式。"""
  validate_script_code(code)
  if configured_mode not in {None, "order_driven", "continuous"}:
    raise ValueError("quoteMode 仅支持 order_driven 或 continuous")
  has_quote_callback = any(
    isinstance(node, ast.FunctionDef) and node.name == "on_quote"
    for node in ast.parse(code).body
  )
  if configured_mode == "continuous" and not has_quote_callback:
    raise ValueError("continuous 快照模式必须定义 on_quote(quote, state)")
  if configured_mode == "order_driven" and has_quote_callback:
    raise ValueError("定义 on_quote 的策略必须使用 continuous 快照模式")
  return configured_mode or ("continuous" if has_quote_callback else "order_driven")


def _validate_literal_orders(callback: ast.FunctionDef) -> None:
  """尽早指出脚本中可静态识别的订单动作与数量错误。"""
  for node in ast.walk(callback):
    if not isinstance(node, ast.Dict):
      continue
    values = {
      key.value: value
      for key, value in zip(node.keys, node.values)
      if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    action = values.get("action")
    if isinstance(action, ast.Constant) and action.value not in _ACTIONS:
      raise ValueError("订单 action 仅支持 buy_open/sell_open/sell_close/buy_close")
    quantity = values.get("quantity")
    if isinstance(quantity, ast.Constant):
      if isinstance(quantity.value, bool) or not isinstance(quantity.value, int) or quantity.value <= 0:
        raise ValueError("订单 quantity 字面量必须是大于 0 的整数")


class ScriptedBarStrategy:
  """执行用户 Bar、Quote 与订单回报回调并转换交易动作。"""

  def __init__(
    self,
    script_id: str,
    code: str,
    params: dict[str, Any],
    quote_mode: str | None = None,
  ) -> None:
    try:
      validate_script_code(code)
      self._quote_mode = quote_mode_for_script(code, quote_mode)
    except ValueError as error:
      raise StrategyExecutionError.from_exception(error) from error
    namespace: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    try:
      exec(compile(code, f"strategy_script_{script_id}.py", "exec"), namespace)
    except Exception as error:
      raise StrategyExecutionError.from_exception(error) from error
    self._callback = namespace["on_bar"]
    self._quote_callback = namespace.get("on_quote")
    self._order_event_callback = namespace.get("on_order_event")
    self._execution_params = dict(params)
    self._script_params = {
      key: value
      for key, value in self._execution_params.items()
      if key not in {"allowLong", "allowShort"}
    }
    self._position_key = f"script:{script_id}"
    self._state: dict[str, Any] = {"params": dict(self._script_params)}
    self._skipped_signals: list[dict[str, Any]] = []

  def on_bar(
    self,
    bar: Bar,
    portfolio: PortfolioSnapshot,
  ) -> Sequence[OrderIntent]:
    try:
      context = self._context(bar, portfolio)
      actions = self._callback(context, self._state)
      return self._to_orders(actions, bar.symbol, bar.time, bar.close)
    except StrategyExecutionError:
      raise
    except ScriptTimeContextError as error:
      raise StrategyExecutionError.from_exception(
        ValueError(f"时间上下文无效: {error}"), "on_bar", bar.time, bar.symbol,
      ) from error
    except Exception as error:
      raise StrategyExecutionError.from_exception(error, "on_bar", bar.time, bar.symbol) from error

  def on_quote(self, quote: QuoteSnapshot, portfolio: PortfolioSnapshot) -> Sequence[OrderIntent]:
    """处理连续 L2 快照；新订单由引擎保证只能使用后续快照成交。"""
    if self._quote_callback is None:
      return []
    try:
      time_context = normalize_script_bar_time(quote.time)
      actions = self._quote_callback(self._quote_context(quote, portfolio, time_context), self._state)
      return self._to_orders(actions, quote.symbol, quote.time, quote.last_price or 0.0)
    except StrategyExecutionError:
      raise
    except ScriptTimeContextError as error:
      raise StrategyExecutionError.from_exception(
        ValueError(f"时间上下文无效: {error}"), "on_quote", quote.time, quote.symbol,
      ) from error
    except Exception as error:
      raise StrategyExecutionError.from_exception(error, "on_quote", quote.time, quote.symbol) from error

  def on_order_event(self, event: dict[str, Any]) -> None:
    """将已落账的订单状态变化通知给策略状态机。"""
    if self._order_event_callback is None:
      return
    try:
      self._order_event_callback(dict(event), self._state)
    except StrategyExecutionError:
      raise
    except Exception as error:
      event_time = event.get("time") or event.get("executionTime") or event.get("signalTime")
      raise StrategyExecutionError.from_exception(
        error, "on_order_event", str(event_time) if event_time else None,
        str(event["symbol"]) if event.get("symbol") else None,
      ) from error

  def _context(self, bar: Bar, portfolio: PortfolioSnapshot) -> dict[str, Any]:
    long_qty = self._strategy_position(portfolio.long_positions_by_key, ":overlay_long")
    short_qty = self._strategy_position(portfolio.short_positions_by_key, ":overlay_short")
    time_context = normalize_script_bar_time(bar.time)
    return {
      "time": time_context.time,
      "datetime": time_context.datetime,
      "tradeDate": time_context.trade_date,
      "hhmm": time_context.hhmm,
      "symbol": bar.symbol,
      "open": bar.open,
      "high": bar.high,
      "low": bar.low,
      "close": bar.close,
      "volume": bar.volume,
      "cash": portfolio.cash,
      "equity": portfolio.equity,
      "position": long_qty - short_qty,
      "strategyPosition": long_qty - short_qty,
      "basePosition": portfolio.base_position,
      "sellableQuantity": portfolio.sellable_quantity,
      "t0AvailableQuantity": portfolio.t0_available_quantity,
      "params": self._script_params,
    }

  def _quote_context(
    self,
    quote: QuoteSnapshot,
    portfolio: PortfolioSnapshot,
    time_context: Any,
  ) -> dict[str, Any]:
    long_qty = self._strategy_position(portfolio.long_positions_by_key, ":overlay_long")
    short_qty = self._strategy_position(portfolio.short_positions_by_key, ":overlay_short")
    return {
      "time": time_context.time,
      "datetime": time_context.datetime,
      "tradeDate": time_context.trade_date,
      "hhmm": time_context.hhmm,
      "symbol": quote.symbol,
      "lastPrice": quote.last_price,
      "bids": [{"price": item.price, "volume": item.volume} for item in quote.bids],
      "asks": [{"price": item.price, "volume": item.volume} for item in quote.asks],
      "cash": portfolio.cash,
      "equity": portfolio.equity,
      "position": long_qty - short_qty,
      "strategyPosition": long_qty - short_qty,
      "basePosition": portfolio.base_position,
      "sellableQuantity": portfolio.sellable_quantity,
      "t0AvailableQuantity": portfolio.t0_available_quantity,
      "params": self._script_params,
    }

  def _strategy_position(self, positions: dict[str, Any], overlay_suffix: str) -> int:
    direct = positions.get(self._position_key)
    overlay = positions.get(f"{self._position_key}{overlay_suffix}")
    return (direct.quantity if direct else 0) + (overlay.quantity if overlay else 0)

  def _to_orders(
    self,
    actions: Any,
    symbol: str | Bar,
    signal_time: str | None = None,
    signal_price: float | None = None,
  ) -> list[OrderIntent]:
    """将回调动作转换为订单；保留旧测试使用 Bar 参数的兼容方式。"""
    if isinstance(symbol, Bar):
      legacy_bar = symbol
      symbol, signal_time, signal_price = legacy_bar.symbol, legacy_bar.time, legacy_bar.close
    if signal_time is None or signal_price is None:
      raise ValueError("策略订单缺少信号时间或价格")
    if actions is None:
      return []
    if not isinstance(actions, list):
      raise ValueError("策略回调必须返回订单列表或 None")
    orders: list[OrderIntent] = []
    for action in actions:
      if not isinstance(action, dict):
        raise ValueError("每个订单必须是字典")
      action_name = str(action.get("action", ""))
      if action_name not in _ACTIONS:
        raise ValueError("订单 action 仅支持 buy_open/sell_open/sell_close/buy_close")
      raw_quantity = action.get("quantity", 0)
      if isinstance(raw_quantity, bool) or not isinstance(raw_quantity, int) or raw_quantity <= 0:
        raise ValueError("订单 quantity 必须是大于 0 的整数")
      if not self._is_direction_permitted(action_name):
        self._record_skipped_signal(action_name, raw_quantity, str(action.get("reason", "")), symbol, signal_time, signal_price)
        continue
      quantity = raw_quantity
      side, effect = _ACTIONS[action_name]
      orders.append(OrderIntent(
        symbol=symbol,
        side=side,
        quantity=quantity,
        reason=str(action.get("reason", "自定义策略信号")),
        time=signal_time,
        position_effect=effect,
        position_key=self._position_key_for(effect),
        metadata={
          "direction": action_name.upper(),
          "position_effect": effect.value,
          "recorded_price": signal_price,
          "signal_time": signal_time,
          "strategy_module": "用户脚本",
          "client_order_key": str(action["clientOrderKey"]) if action.get("clientOrderKey") is not None else None,
        },
      ))
    return orders

  def _position_key_for(self, effect: PositionEffect) -> str:
    if not bool(self._execution_params.get("corePositionOverlay", False)):
      return self._position_key
    if effect in {PositionEffect.OPEN_LONG, PositionEffect.CLOSE_LONG}:
      return f"{self._position_key}:overlay_long"
    return f"{self._position_key}:overlay_short"

  @property
  def skipped_signals(self) -> list[dict[str, Any]]:
    """返回因方案方向设置未执行的开仓信号。"""
    return list(self._skipped_signals)

  @property
  def direction_warnings(self) -> list[str]:
    if not self._skipped_signals:
      return []
    return [f"已按策略方向设置跳过 {len(self._skipped_signals)} 条开仓信号"]

  @property
  def quote_mode(self) -> str:
    """返回该脚本要求的 L2 行情回放方式。"""
    return self._quote_mode

  def _is_direction_permitted(self, action_name: str) -> bool:
    """方向设置是统一执行权限，策略代码无需自行读取该参数。"""
    if action_name == "buy_open":
      return bool(self._execution_params.get("allowLong", True))
    if action_name == "sell_open":
      return bool(self._execution_params.get("allowShort", True))
    return True

  def _record_skipped_signal(
    self,
    action_name: str,
    quantity: int,
    reason: str,
    symbol: str,
    signal_time: str,
    signal_price: float,
  ) -> None:
    side, _ = _ACTIONS[action_name]
    direction_label = "做多" if action_name == "buy_open" else "做空"
    self._skipped_signals.append({
      "time": signal_time,
      "signalTime": signal_time,
      "executionTime": None,
      "direction": action_name.upper(),
      "action": action_name.upper(),
      "side": side.value,
      "anchor": 0.0,
      "price": signal_price,
      "quantity": quantity,
      "pnl": 0.0,
      "note": f"方向设置已跳过：策略方案未启用{direction_label}；{reason}".rstrip("；"),
      "priceSource": "当前 Bar 收盘价（未成交）",
      "status": "skipped",
      "strategyModule": "用户脚本",
    })
