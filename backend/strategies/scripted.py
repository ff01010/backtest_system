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

_ACTIONS: dict[str, tuple[OrderSide, PositionEffect]] = {
  "buy_open": (OrderSide.BUY, PositionEffect.OPEN_LONG),
  "sell_open": (OrderSide.SELL, PositionEffect.OPEN_SHORT),
  "sell_close": (OrderSide.SELL, PositionEffect.CLOSE_LONG),
  "buy_close": (OrderSide.BUY, PositionEffect.CLOSE_SHORT),
}
_BLOCKED_CALLS = {"compile", "eval", "exec", "input", "open"}
_SAFE_BUILTINS = {
  "abs": abs,
  "bool": bool,
  "float": float,
  "int": int,
  "len": len,
  "max": max,
  "min": min,
  "range": range,
  "round": round,
  "sum": sum,
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
  """校验脚本语法和统一的 on_bar 接口。"""
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
  """执行用户 on_bar(bar, state) 函数并转换其订单动作。"""

  def __init__(self, script_id: int, code: str, params: dict[str, Any]) -> None:
    validate_script_code(code)
    namespace: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}
    exec(compile(code, f"strategy_script_{script_id}.py", "exec"), namespace)
    self._callback = namespace["on_bar"]
    self._params = dict(params)
    self._position_key = f"script:{script_id}"
    self._state: dict[str, Any] = {"params": self._params}
    self._skipped_signals: list[dict[str, Any]] = []

  def on_bar(
    self,
    bar: Bar,
    portfolio: PortfolioSnapshot,
  ) -> Sequence[OrderIntent]:
    context = self._context(bar, portfolio)
    try:
      actions = self._callback(context, self._state)
    except Exception as error:
      raise ValueError(f"策略脚本在 {bar.time} 执行失败: {error}") from error
    return self._to_orders(actions, bar)

  def _context(self, bar: Bar, portfolio: PortfolioSnapshot) -> dict[str, Any]:
    long_qty = portfolio.long_positions_by_key.get(self._position_key)
    short_qty = portfolio.short_positions_by_key.get(self._position_key)
    return {
      "time": bar.time,
      "symbol": bar.symbol,
      "open": bar.open,
      "high": bar.high,
      "low": bar.low,
      "close": bar.close,
      "volume": bar.volume,
      "cash": portfolio.cash,
      "equity": portfolio.equity,
      "position": (long_qty.quantity if long_qty else 0) - (short_qty.quantity if short_qty else 0),
      "params": self._params,
    }

  def _to_orders(self, actions: Any, bar: Bar) -> list[OrderIntent]:
    if actions is None:
      return []
    if not isinstance(actions, list):
      raise ValueError("on_bar 必须返回订单列表或 None")
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
        self._record_skipped_signal(action_name, raw_quantity, str(action.get("reason", "")), bar)
        continue
      quantity = raw_quantity
      side, effect = _ACTIONS[action_name]
      orders.append(OrderIntent(
        symbol=bar.symbol,
        side=side,
        quantity=quantity,
        reason=str(action.get("reason", "自定义策略信号")),
        time=bar.time,
        position_effect=effect,
        position_key=self._position_key,
        metadata={
          "direction": action_name.upper(),
          "position_effect": effect.value,
          "recorded_price": bar.close,
          "signal_time": bar.time,
          "strategy_module": "用户脚本",
        },
      ))
    return orders

  @property
  def skipped_signals(self) -> list[dict[str, Any]]:
    """返回因方案方向设置未执行的开仓信号。"""
    return list(self._skipped_signals)

  @property
  def direction_warnings(self) -> list[str]:
    if not self._skipped_signals:
      return []
    return [f"已按策略方向设置跳过 {len(self._skipped_signals)} 条开仓信号"]

  def _is_direction_permitted(self, action_name: str) -> bool:
    """方向设置是统一执行权限，策略代码无需自行读取该参数。"""
    if action_name == "buy_open":
      return bool(self._params.get("allowLong", True))
    if action_name == "sell_open":
      return bool(self._params.get("allowShort", True))
    return True

  def _record_skipped_signal(
    self,
    action_name: str,
    quantity: int,
    reason: str,
    bar: Bar,
  ) -> None:
    side, _ = _ACTIONS[action_name]
    direction_label = "做多" if action_name == "buy_open" else "做空"
    self._skipped_signals.append({
      "time": bar.time,
      "signalTime": bar.time,
      "executionTime": None,
      "direction": action_name.upper(),
      "action": action_name.upper(),
      "side": side.value,
      "anchor": 0.0,
      "price": bar.close,
      "quantity": quantity,
      "pnl": 0.0,
      "note": f"方向设置已跳过：策略方案未启用{direction_label}；{reason}".rstrip("；"),
      "priceSource": "当前 Bar 收盘价（未成交）",
      "status": "skipped",
      "strategyModule": "用户脚本",
    })
