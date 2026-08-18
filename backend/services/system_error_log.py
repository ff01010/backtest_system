"""回测系统错误的受控分类；原始堆栈只保留在容器日志中。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from backend.services.backtest_errors import RemoteBacktestError, error_detail
from backend.services.system_error_catalog import classify_system_error, root_cause


@dataclass(frozen=True)
class SystemErrorLogRecord:
  """可供管理员查看、但不包含秘密信息的系统错误记录。"""

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
  """仅将系统错误转换为管理员日志；策略错误继续由原有页面展示。"""
  if error_detail(error).category != "system_error":
    return None
  if isinstance(error, RemoteBacktestError) and error.diagnostic:
    return _remote_worker_record(error, source, stage, task_id)

  cause = root_cause(error)
  classified = classify_system_error(error)
  return SystemErrorLogRecord(
    source=source,
    stage=stage,
    code=classified.code,
    category=classified.category,
    retryable=classified.retryable,
    reason=classified.admin_reason,
    exception_type=type(cause).__name__,
    context=classified.context,
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
  context = {"service": "strategy_worker"}
  if error.detail.symbol:
    context["symbol"] = error.detail.symbol
  if error.detail.event_time:
    context["eventTime"] = error.detail.event_time
  return SystemErrorLogRecord(
    source=source,
    stage=stage,
    code="worker.execution_rejected",
    category="worker",
    retryable=error.detail.retryable,
    reason=diagnostic.get("reason") or error.detail.message,
    exception_type=diagnostic.get("exceptionType") or type(error).__name__,
    context=context,
    raw_log_ref=_raw_log_ref(source, stage, task_id),
  )


def _raw_log_ref(source: str, stage: str, task_id: str | None) -> str:
  container = {"l2_runner": "quant-backtest-l2-runner", "sync_runner": "quant-backtest-backend"}.get(source, source)
  task = task_id or "unknown"
  return f"container={container}; taskId={task}; stage={stage}"
