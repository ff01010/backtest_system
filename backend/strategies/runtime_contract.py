"""用户策略的版本化 Python 运行时契约。"""

from __future__ import annotations

import builtins
import os
import re
import traceback
from collections.abc import Callable
from types import MappingProxyType
from typing import Any


SCRIPT_RUNTIME_VERSION = "python3.12-runtime-v2"
BLOCKED_BUILTIN_NAMES = frozenset({
  "breakpoint",
  "compile",
  "eval",
  "exec",
  "exit",
  "help",
  "input",
  "open",
  "quit",
})
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class StrategyRuntimePolicyError(ValueError):
  """策略调用了运行时产品契约明确禁止的内置能力。"""

  def __init__(self, name: str) -> None:
    super().__init__(f"策略运行环境不允许调用 {name}")
    self.name = name


class StrategyRuntimeVersionError(RuntimeError):
  """任务快照声明的策略运行时与当前执行器不兼容。"""

  def __init__(self, requested_version: str) -> None:
    super().__init__("任务策略运行时版本与当前执行器不兼容")
    self.requested_version = requested_version


def _blocked_builtin(name: str) -> Callable[..., Any]:
  def reject(*_: Any, **__: Any) -> Any:
    raise StrategyRuntimePolicyError(name)

  return reject


def _runtime_builtins() -> MappingProxyType[str, Any]:
  """提供固定 Python 的标准内置能力；禁止项只定义产品行为。"""
  values = dict(vars(builtins))
  for name in BLOCKED_BUILTIN_NAMES:
    values[name] = _blocked_builtin(name)
  return MappingProxyType(values)


RUNTIME_BUILTINS = _runtime_builtins()


def validate_runtime_version(version: object) -> None:
  """允许无版本的历史任务，新任务必须与当前固定运行时一致。"""
  if version in {None, "", "legacy", SCRIPT_RUNTIME_VERSION}:
    return
  raise StrategyRuntimeVersionError(str(version))


def drop_strategy_process_privileges() -> None:
  """Linux Runner 以 root 启动时，将用户策略子进程降为 nobody。"""
  if os.name != "posix" or not hasattr(os, "getuid") or os.getuid() != 0:
    return
  os.setgroups([])
  os.setgid(65534)
  os.setuid(65534)


def is_standard_builtin_name(name: str | None) -> bool:
  return bool(name and name in vars(builtins))


def missing_name(error: BaseException) -> str | None:
  """只提取单个 Python 标识符，不返回任意异常正文。"""
  value = getattr(error, "name", None)
  if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
    return value
  text = str(error)
  for pattern in (
    r"name ['\"]([^'\"]+)['\"] is not defined",
    r"local variable ['\"]([^'\"]+)['\"]",
    r"free variable ['\"]([^'\"]+)['\"]",
  ):
    match = re.search(pattern, text)
    if match and _IDENTIFIER.fullmatch(match.group(1)):
      return match.group(1)
  return None


def strategy_source_line(error: BaseException) -> int | None:
  """定位动态策略文件行号，不返回源码行。"""
  frames = traceback.extract_tb(error.__traceback__)
  strategy_frames = [frame for frame in frames if frame.filename.startswith("strategy_script_")]
  return strategy_frames[-1].lineno if strategy_frames else None
