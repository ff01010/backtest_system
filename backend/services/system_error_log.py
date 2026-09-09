"""回测系统错误的受控分类；原始堆栈只保留在容器日志中。"""

from __future__ import annotations

import traceback
from dataclasses import asdict, dataclass
from typing import Any

from backend.services.backtest_errors import BacktestExecutionError, RemoteBacktestError, error_detail
from backend.services.system_error_catalog import classify_system_error, root_cause


@dataclass(frozen=True)
class SystemErrorLogRecord:
  """管理员可见的安全摘要与仅数据库保留的原始诊断。"""

  source: str
  stage: str
  code: str
  category: str
  retryable: bool
  reason: str
  exception_type: str
  context: dict[str, str]
  raw_log_ref: str

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def system_error_log_record(
  error: Exception,
  source: str,
  stage: str,
  task_id: str | None = None,
) -> SystemErrorLogRecord | None:
  """记录系统错误及 worker 拒绝任务时返回的原始诊断。"""
  if isinstance(error, RemoteBacktestError) and error.diagnostic:
    return _remote_worker_record(error, source, stage, task_id)
  detail = error_detail(error)
  if detail.category != "system_error":
    return None
  if isinstance(error, BacktestExecutionError) and detail.code.startswith("runtime."):
    return _runtime_record(error, source, stage, task_id)

  cause = root_cause(error)
  classified = classify_system_error(error)
  context = {
    **classified.context,
    **_internal_error_context(error),
  }
  return SystemErrorLogRecord(
    source=source,
    stage=stage,
    code=classified.code,
    category=classified.category,
    retryable=classified.retryable,
    reason=classified.admin_reason,
    exception_type=type(cause).__name__,
    context=context,
    raw_log_ref=_raw_log_ref(source, stage, task_id),
  )


def _remote_worker_record(
  error: RemoteBacktestError,
  source: str,
  stage: str,
  task_id: str | None,
) -> SystemErrorLogRecord:
  """记录 worker 已返回的受限诊断正文，供管理员定位回放失败。"""
  diagnostic = error.diagnostic
  is_runtime_error = error.detail.code.startswith("runtime.")
  context = {"service": "strategy_worker"}
  if error.detail.symbol:
    context["symbol"] = error.detail.symbol
  if error.detail.event_time:
    context["eventTime"] = error.detail.event_time
  if diagnostic.get("trace"):
    context["trace"] = diagnostic["trace"][:1_500]
  for key in ("missingName", "runtimeVersion", "strategyLine"):
    if diagnostic.get(key):
      context[key] = diagnostic[key]
  context.update(_remote_internal_context(diagnostic))
  return SystemErrorLogRecord(
    source=source,
    stage=stage,
    code=error.detail.code if is_runtime_error else "worker.execution_rejected",
    category="runtime" if is_runtime_error else "worker",
    retryable=error.detail.retryable,
    reason=diagnostic.get("reason") or error.detail.message,
    exception_type=diagnostic.get("exceptionType") or type(error).__name__,
    context=context,
    raw_log_ref=_raw_log_ref(source, stage, task_id),
  )


def _runtime_record(
  error: BacktestExecutionError,
  source: str,
  stage: str,
  task_id: str | None,
) -> SystemErrorLogRecord:
  """保留平台运行时错误的受控字段，供独立脱敏统计复用。"""
  detail = error.detail
  context = {"runtimeVersion": detail.runtime_version or "legacy"}
  if detail.missing_name:
    context["missingName"] = detail.missing_name
  if detail.strategy_line:
    context["strategyLine"] = str(detail.strategy_line)
  return SystemErrorLogRecord(
    source=source,
    stage=stage,
    code=detail.code,
    category="runtime",
    retryable=detail.retryable,
    reason=detail.message,
    exception_type=type(root_cause(error)).__name__,
    context=context,
    raw_log_ref=_raw_log_ref(source, stage, task_id),
  )


def _raw_log_ref(source: str, stage: str, task_id: str | None) -> str:
  container = {"l2_runner": "quant-backtest-platform-l2-runner-*", "sync_runner": "quant-backtest-backend"}.get(source, source)
  task = task_id or "unknown"
  return f"container={container}; taskId={task}; stage={stage}"


def _internal_error_context(error: Exception) -> dict[str, str]:
  return {
    "__internal_raw_message": str(error),
    "__internal_raw_traceback": "".join(traceback.format_exception(type(error), error, error.__traceback__)),
  }


def _remote_internal_context(diagnostic: dict[str, str]) -> dict[str, str]:
  return {
    key: diagnostic[key]
    for key in ("__internal_raw_message", "__internal_raw_traceback")
    if diagnostic.get(key)
  }
