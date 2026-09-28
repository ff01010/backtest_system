"""回测系统错误的受控分类与执行尝试关联。"""

from __future__ import annotations

import traceback
from dataclasses import asdict, dataclass, replace
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
  attempt_no: int | None = None
  attempt_token: str | None = None
  worker_id: str | None = None
  container_id: str | None = None
  termination_reason: str | None = None
  exit_code: int | None = None
  log_correlation_id: str | None = None

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def system_error_log_record(
  error: Exception,
  source: str,
  stage: str,
  task_id: str | None = None,
  worker_id: str | None = None,
  attempt_context: Any | None = None,
  termination_reason: str | None = None,
  exit_code: int | None = None,
) -> SystemErrorLogRecord | None:
  """记录系统错误及策略子进程拒绝任务时返回的原始诊断。"""
  if isinstance(error, RemoteBacktestError) and error.diagnostic:
    return _attach_attempt(
      _strategy_process_record(error, source, stage, task_id, worker_id),
      attempt_context, termination_reason, exit_code,
    )
  if isinstance(error, BacktestExecutionError):
    detail = error_detail(error)
    if detail.code.startswith("runtime."):
      return _attach_attempt(
        _runtime_record(error, source, stage, task_id, worker_id),
        attempt_context, termination_reason, exit_code,
      )
    if detail.category == "strategy_error":
      return None

  cause = root_cause(error)
  classified = classify_system_error(error)
  if not classified.operator_visible:
    return None
  context = {
    **classified.context,
    **_worker_context(worker_id),
    **_internal_error_context(error),
  }
  return _attach_attempt(SystemErrorLogRecord(
    source=source,
    stage=stage,
    code=classified.code,
    category=classified.category,
    retryable=classified.retryable,
    reason=classified.admin_reason,
    exception_type=type(cause).__name__,
    context=context,
    raw_log_ref=_raw_log_ref(source, stage, task_id, worker_id),
  ), attempt_context, termination_reason, exit_code)


def _strategy_process_record(
  error: RemoteBacktestError,
  source: str,
  stage: str,
  task_id: str | None,
  worker_id: str | None,
) -> SystemErrorLogRecord:
  """记录策略子进程已返回的受限诊断正文，供管理员定位回放失败。"""
  diagnostic = error.diagnostic
  is_runtime_error = error.detail.code.startswith("runtime.")
  context = {"service": "strategy_process", **_worker_context(worker_id)}
  if error.detail.symbol:
    context["symbol"] = error.detail.symbol
  if error.detail.event_time:
    context["eventTime"] = error.detail.event_time
  if diagnostic.get("trace"):
    context["trace"] = diagnostic["trace"][:1_500]
  for key in (
    "missingName", "missingModule", "runtimeVersion", "strategyLine", "supportedPackage",
  ):
    if diagnostic.get(key):
      context[key] = diagnostic[key]
  if error.detail.missing_module:
    context["missingModule"] = error.detail.missing_module
  if error.detail.supported_package:
    context["supportedPackage"] = error.detail.supported_package
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
    raw_log_ref=_raw_log_ref(source, stage, task_id, worker_id),
  )


def _runtime_record(
  error: BacktestExecutionError,
  source: str,
  stage: str,
  task_id: str | None,
  worker_id: str | None,
) -> SystemErrorLogRecord:
  """保留平台运行时错误的受控字段，供独立脱敏统计复用。"""
  detail = error.detail
  context = {
    "runtimeVersion": detail.runtime_version or "legacy",
    **_worker_context(worker_id),
  }
  if detail.missing_name:
    context["missingName"] = detail.missing_name
  if detail.missing_module:
    context["missingModule"] = detail.missing_module
  if detail.supported_package:
    context["supportedPackage"] = detail.supported_package
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
    raw_log_ref=_raw_log_ref(source, stage, task_id, worker_id),
  )


def _attach_attempt(
  record: SystemErrorLogRecord,
  context: Any | None,
  termination_reason: str | None,
  exit_code: int | None,
) -> SystemErrorLogRecord:
  if context is None:
    return replace(
      record, termination_reason=termination_reason, exit_code=exit_code,
    )
  attempt_no = getattr(context, "attempt_no", None)
  attempt_token = getattr(context, "attempt_token", None)
  worker_id = getattr(context, "worker_id", None)
  container_id = getattr(context, "container_id", None)
  correlation_id = str(attempt_token) if attempt_token else None
  raw_log_ref = record.raw_log_ref
  if correlation_id:
    raw_log_ref = f"{raw_log_ref}; attemptToken={correlation_id}"
  return replace(
    record,
    raw_log_ref=raw_log_ref,
    attempt_no=int(attempt_no) if attempt_no is not None else None,
    attempt_token=correlation_id,
    worker_id=str(worker_id) if worker_id else record.worker_id,
    container_id=str(container_id) if container_id else None,
    termination_reason=termination_reason,
    exit_code=exit_code,
    log_correlation_id=correlation_id,
  )


def _raw_log_ref(
  source: str,
  stage: str,
  task_id: str | None,
  worker_id: str | None = None,
) -> str:
  if source == "l2_runner" and worker_id:
    container = worker_id.partition(":")[0]
  else:
    container = {"l2_runner": "quant-backtest-platform-l2-runner-*", "sync_runner": "quant-backtest-backend"}.get(source, source)
  task = task_id or "unknown"
  if worker_id:
    return f"container={container}; workerId={worker_id}; taskId={task}; stage={stage}"
  return f"container={container}; taskId={task}; stage={stage}"


def _worker_context(worker_id: str | None) -> dict[str, str]:
  return {"workerId": worker_id} if worker_id else {}


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
