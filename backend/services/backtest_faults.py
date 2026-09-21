"""回测失败的稳定语义载体，不包含用户可见文案或日志策略。"""

from __future__ import annotations

from typing import Mapping


class BacktestFault(RuntimeError):
  """由稳定错误码和受控上下文描述的回测领域失败。"""

  def __init__(
    self,
    code: str,
    message: str,
    context: Mapping[str, str] | None = None,
  ) -> None:
    super().__init__(message)
    self.code = code
    self.context = dict(context or {})


def find_backtest_fault(error: BaseException) -> BacktestFault | None:
  """优先保留外层已经赋予语义的失败，不让底层原因覆盖它。"""
  current: BaseException | None = error
  visited: set[int] = set()
  while current is not None and id(current) not in visited:
    visited.add(id(current))
    if isinstance(current, BacktestFault):
      return current
    current = current.__cause__ or current.__context__
  return None
