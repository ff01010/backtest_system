"""回测失败的安全对外错误模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BacktestErrorDetail:
  """可返回前端的回测错误详情，绝不包含原始异常文本。"""

  category: str
  code: str
  message: str
  hint: str | None = None
  callback: str | None = None
  event_time: str | None = None
  symbol: str | None = None
  retryable: bool = False
  task_id: str | None = None

  def to_dict(self) -> dict[str, Any]:
    return {
      "category": self.category,
      "code": self.code,
      "message": self.message,
      "hint": self.hint,
      "callback": self.callback,
      "eventTime": self.event_time,
      "symbol": self.symbol,
      "retryable": self.retryable,
      "taskId": self.task_id,
    }

  def with_task_id(self, task_id: str | None) -> "BacktestErrorDetail":
    return BacktestErrorDetail(
      self.category, self.code, self.message, self.hint, self.callback,
      self.event_time, self.symbol, self.retryable, task_id,
    )


class BacktestExecutionError(RuntimeError):
  """已经完成脱敏的回测领域异常。"""

  def __init__(self, detail: BacktestErrorDetail) -> None:
    super().__init__(detail.message)
    self.detail = detail


class StrategyExecutionError(BacktestExecutionError, ValueError):
  """策略脚本或其返回的订单契约错误。"""

  @classmethod
  def from_exception(
    cls,
    error: Exception,
    callback: str | None = None,
    event_time: str | None = None,
    symbol: str | None = None,
  ) -> "StrategyExecutionError":
    message, hint = _strategy_message(error)
    return cls(BacktestErrorDetail(
      category="strategy_error",
      code=_strategy_code(error),
      message=message,
      hint=hint,
      callback=callback,
      event_time=event_time,
      symbol=symbol,
      retryable=False,
    ))


class RemoteBacktestError(BacktestExecutionError):
  """保留 worker 已脱敏的结构化错误，避免 HTTP 传输时退化为字符串。"""

  @classmethod
  def from_dict(cls, value: dict[str, Any]) -> "RemoteBacktestError":
    return cls(BacktestErrorDetail(
      category=str(value.get("category") or "system_error"),
      code=str(value.get("code") or "system.internal"),
      message=str(value.get("message") or "回测执行异常，请稍后重试。"),
      hint=_optional_text(value.get("hint")),
      callback=_optional_text(value.get("callback")),
      event_time=_optional_text(value.get("eventTime")),
      symbol=_optional_text(value.get("symbol")),
      retryable=bool(value.get("retryable", False)),
      task_id=_optional_text(value.get("taskId")),
    ))


def error_detail(error: Exception, task_id: str | None = None) -> BacktestErrorDetail:
  """把任意异常降级为可公开的结构化错误。"""
  if isinstance(error, BacktestExecutionError):
    return error.detail.with_task_id(task_id or error.detail.task_id)
  if isinstance(error, FileNotFoundError):
    return BacktestErrorDetail(
      "system_error", "data.unavailable", "所选区间没有可用行情数据。",
      "请检查标的、日期和周期后重试。", retryable=False, task_id=task_id,
    )
  return BacktestErrorDetail(
    "system_error", "system.internal", "回测执行异常，请稍后重试。",
    "如持续出现，请提供任务编号给维护人员。", retryable=True, task_id=task_id,
  )


def error_summary(error: Exception, task_id: str | None = None) -> dict[str, Any]:
  """生成可持久化且兼容旧任务列表的失败摘要。"""
  detail = error_detail(error, task_id)
  return {"error": detail.message, "errorDetail": detail.to_dict()}


def _strategy_code(error: Exception) -> str:
  text = str(error)
  if "on_quote" in text:
    return "strategy.callback_contract"
  if "on_bar" in text:
    return "strategy.callback_contract"
  if "action" in text:
    return "strategy.invalid_action"
  if "quantity" in text:
    return "strategy.invalid_quantity"
  if "返回订单列表" in text:
    return "strategy.invalid_return"
  if "时间上下文" in text:
    return "strategy.invalid_time"
  if isinstance(error, SyntaxError):
    return "strategy.syntax_error"
  return "strategy.runtime_error"


def _strategy_message(error: Exception) -> tuple[str, str]:
  """用异常类型和受控契约文本提供可操作提示，不回显脚本自定义内容。"""
  text = str(error)
  if "continuous 快照模式必须定义 on_quote" in text:
    return "continuous 快照模式必须定义 on_quote(quote, state)。", "请定义 on_quote，或将快照模式改为 order_driven。"
  if "定义 on_quote 的策略必须使用 continuous 快照模式" in text:
    return "定义 on_quote 的策略必须使用 continuous 快照模式。", "请将快照模式改为 continuous，或删除 on_quote。"
  if "on_bar" in text and "两个参数" in text:
    return "on_bar 回调函数参数不符合接口。", "on_bar 必须恰好接收 bar 与 state 两个参数。"
  if "action" in text:
    return "订单 action 不符合当前策略接口。", "仅可使用 buy_open、sell_open、sell_close 或 buy_close。"
  if "quantity" in text:
    return "订单 quantity 不符合当前策略接口。", "quantity 必须是大于 0 的整数。"
  if "返回订单列表" in text:
    return "策略回调返回值不符合接口。", "每个回调必须返回订单字典组成的列表，或返回 []。"
  if "时间上下文" in text:
    return "策略收到的时间上下文无法解析。", "请确认策略仅按手册中的 time、datetime、tradeDate 与 hhmm 字段使用时间。"
  if isinstance(error, NameError):
    return "策略引用了未定义的变量或函数。", "请先初始化变量，或确认变量名与当前回调提供的字段一致。"
  if isinstance(error, KeyError):
    return "策略读取了当前回调未提供的字段。", "请对照策略编写手册检查 bar、quote、event 或 state 的字段名。"
  if isinstance(error, IndexError):
    return "策略访问的列表或盘口档位超出范围。", "读取盘口档位前请先检查 bids 或 asks 的长度。"
  if isinstance(error, TypeError):
    return "策略使用了不匹配的数据类型或函数参数。", "请检查数值转换、空值判断和回调函数参数。"
  if isinstance(error, SyntaxError):
    return "策略脚本存在 Python 语法错误。", "请检查报错行附近的缩进、括号、冒号和字符串。"
  return "策略脚本运行时发生异常。", "请检查当前回调中的变量初始化、字段访问和返回值。"


def _optional_text(value: Any) -> str | None:
  return value if isinstance(value, str) and value else None
