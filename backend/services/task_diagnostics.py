"""任务执行诊断深模块：固定上下文、脱敏事件与 Attempt 生命周期。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time
import traceback
from typing import Any
from uuid import UUID

from backend import settings
from backend.time_utils import SHANGHAI_TIMEZONE
from backend.services import state_store
from backend.services.backtest_errors import error_detail
from backend.services.backtest_faults import find_backtest_fault
from backend.strategies.runtime_contract import (
  RUNTIME_CAPABILITY_DIGEST,
  SCRIPT_RUNTIME_VERSION,
)


SCHEMA_VERSION = "task-log-v1"
EVENTS = {
  "attempt.claimed", "attempt.started", "attempt.stage_changed",
  "strategy_process.started", "strategy_process.exited",
  "attempt.succeeded", "attempt.failed", "attempt.cancelled",
  "attempt.interrupted", "runner_process.exited", "container.oom",
  "container.died", "container.restarted", "log_pipeline.degraded",
  "log_pipeline.recovered",
}
STAGES = {
  "attempt.claimed", "bar.initialize", "bar.backtest", "l2.initialize",
  "l2.bar_load", "l2.quote_coverage", "l2.strategy_session",
  "l2.market_replay", "l2.result_finalize", "runner.supervisor",
  "container.lifecycle", "observability.pipeline",
}
TERMINATION_REASONS = {
  "completed", "exception", "strategy_exit_nonzero", "strategy_signal",
  "timeout", "cancelled_by_user", "lease_lost", "lease_expired",
  "runner_exit_nonzero", "container_oom", "container_died",
  "host_interrupted", "unknown_interruption",
}
STATUSES = {"running", "succeeded", "failed", "cancelled", "interrupted"}
LEVELS = {"info", "warning", "error", "critical"}
OPTIONAL_FIELDS = {
  "status", "errorCode", "exceptionType", "exitCode", "terminationReason",
  "durationMs", "signalNumber", "truncated", "originalCharacters",
  "contentDigest", "frameSummary",
}
EXTERNAL_FIELDS = {
  "service", "event", "message", "runId", "attemptNo", "attemptToken",
  "jobKind", "workerId", "containerId", "processId", "stage", "status",
  "errorCode", "exceptionType", "exitCode", "terminationReason", "durationMs",
  "applicationBuildId", "runtimeVersion", "capabilityDigest",
}
_MESSAGE_LIMIT = 4 * 1024
_EXCEPTION_LIMIT = 64 * 1024
_TIMELINE_EVENT_ORDER = {
  "attempt.claimed": 10,
  "attempt.started": 10,
  "attempt.stage_changed": 20,
  "strategy_process.started": 30,
  "attempt.error_recorded": 80,
  "strategy_process.exited": 85,
  "runner_process.exited": 85,
  "container.oom": 85,
  "container.died": 85,
  "container.restarted": 85,
  "attempt.succeeded": 90,
  "attempt.failed": 90,
  "attempt.cancelled": 90,
  "attempt.interrupted": 90,
}
_SECRET_ASSIGNMENT = re.compile(
  r"(?i)\b(password|passwd|token|secret|authorization|api[_-]?key)\b"
  r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
)
_CONNECTION_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|redis|https?)://[^\s]+")


@dataclass(frozen=True)
class AttemptContext:
  run_id: str
  attempt_no: int
  attempt_token: str
  job_kind: str
  worker_id: str
  container_id: str
  process_id: int | None
  service: str
  application_build_id: str
  runtime_version: str
  capability_digest: str

  @classmethod
  def from_job(
    cls,
    job: dict[str, Any],
    job_kind: str,
    worker_id: str,
    *,
    service: str,
  ) -> "AttemptContext":
    if job_kind not in {"bar", "l2"}:
      raise ValueError("任务类型无效")
    container_id, separator, process_text = worker_id.partition(":")
    return cls(
      run_id=str(job["runId"]),
      attempt_no=int(job.get("attemptNo") or 1),
      attempt_token=str(job["attemptToken"]),
      job_kind=job_kind,
      worker_id=worker_id,
      container_id=container_id[:64] or "unknown",
      process_id=int(process_text) if separator and process_text.isdigit() else None,
      service=service,
      application_build_id=settings.APPLICATION_BUILD_ID,
      runtime_version=SCRIPT_RUNTIME_VERSION,
      capability_digest=RUNTIME_CAPABILITY_DIGEST,
    )


class AttemptDiagnostics:
  """Runner 只通过该接口切换阶段并输出低频诊断事件。"""

  def __init__(self, context: AttemptContext) -> None:
    self.context = context
    self._stage = "attempt.claimed"
    self._started_at = time.monotonic()

  @classmethod
  def start(
    cls,
    job: dict[str, Any],
    job_kind: str,
    worker_id: str,
    *,
    service: str,
  ) -> "AttemptDiagnostics":
    diagnostics = cls(AttemptContext.from_job(
      job, job_kind, worker_id, service=service,
    ))
    diagnostics.emit("attempt.started", "执行尝试已启动", stage="attempt.claimed")
    return diagnostics

  @property
  def stage_name(self) -> str:
    return self._stage

  def stage(self, stage: str) -> None:
    _validate_stage(stage)
    if _is_uuid(self.context.run_id) and _is_uuid(self.context.attempt_token):
      if not state_store.update_backtest_attempt_stage(
        self.context.run_id, self.context.attempt_token, stage,
      ):
        raise RuntimeError("Attempt 已结束或执行租约已失效")
    self._stage = stage
    self.emit("attempt.stage_changed", "执行阶段已切换", stage=stage)

  def record_exception(self, error: Exception) -> dict[str, Any]:
    diagnostic = exception_diagnostic(error)
    reason, exit_code = termination_from_error(error)
    self.emit(
      "attempt.failed", diagnostic["reason"], level="error", stage=self._stage,
      status="failed", errorCode=error_detail(error).code,
      exceptionType=diagnostic["exceptionType"], exitCode=exit_code,
      terminationReason=reason, frameSummary=diagnostic["frameSummary"],
    )
    return diagnostic

  def finish(
    self,
    status: str,
    termination_reason: str,
    *,
    error_code: str | None = None,
    exit_code: int | None = None,
  ) -> None:
    event = {
      "succeeded": "attempt.succeeded",
      "failed": "attempt.failed",
      "cancelled": "attempt.cancelled",
      "interrupted": "attempt.interrupted",
    }[status]
    level = "info" if status == "succeeded" else "warning" if status == "cancelled" else "error"
    fields: dict[str, Any] = {
      "status": status,
      "terminationReason": termination_reason,
      "durationMs": round((time.monotonic() - self._started_at) * 1000, 3),
    }
    if error_code:
      fields["errorCode"] = error_code
    if exit_code is not None:
      fields["exitCode"] = exit_code
    self.emit(event, "执行尝试已结束", level=level, stage=self._stage, **fields)

  def emit(
    self,
    event: str,
    message: str,
    *,
    level: str = "info",
    stage: str | None = None,
    **fields: Any,
  ) -> None:
    emit_task_event(build_task_event(
      self.context, event, message, level=level, stage=stage, **fields,
    ))


def build_task_event(
  context: AttemptContext,
  event: str,
  message: str,
  *,
  level: str = "info",
  stage: str | None = None,
  **fields: Any,
) -> dict[str, Any]:
  if event not in EVENTS:
    raise ValueError("任务日志事件名无效")
  if level not in LEVELS:
    raise ValueError("任务日志级别无效")
  unknown = set(fields) - OPTIONAL_FIELDS
  if unknown:
    raise ValueError(f"任务日志字段不在允许列表：{sorted(unknown)}")
  if stage is not None:
    _validate_stage(stage)
  if fields.get("status") is not None and fields["status"] not in STATUSES:
    raise ValueError("Attempt 状态无效")
  if (
    fields.get("terminationReason") is not None
    and fields["terminationReason"] not in TERMINATION_REASONS
  ):
    raise ValueError("Attempt 终止原因无效")
  sanitized, metadata = sanitize_text(message, _MESSAGE_LIMIT)
  payload: dict[str, Any] = {
    "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    "schemaVersion": SCHEMA_VERSION,
    "level": level,
    "service": context.service,
    "event": event,
    "message": sanitized,
    "runId": context.run_id,
    "attemptNo": context.attempt_no,
    "attemptToken": context.attempt_token,
    "jobKind": context.job_kind,
    "workerId": context.worker_id,
    "containerId": context.container_id,
    "processId": context.process_id,
    "applicationBuildId": context.application_build_id,
    "runtimeVersion": context.runtime_version,
    "capabilityDigest": context.capability_digest,
  }
  if stage is not None:
    payload["stage"] = stage
  payload.update({key: value for key, value in fields.items() if value is not None})
  payload.update(metadata)
  return payload


def exception_diagnostic(error: BaseException) -> dict[str, Any]:
  reason, metadata = sanitize_text(str(error), _EXCEPTION_LIMIT)
  frames = traceback.extract_tb(error.__traceback__)[-12:]
  frame_summary = " > ".join(
    f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
    for frame in frames
  )
  return {
    "exceptionType": type(error).__name__,
    "reason": reason,
    "frameSummary": frame_summary,
    **metadata,
  }


def termination_from_error(error: BaseException) -> tuple[str, int | None]:
  exit_code = getattr(error, "exit_code", None)
  explicit_reason = getattr(error, "termination_reason", None)
  if explicit_reason in TERMINATION_REASONS:
    return str(explicit_reason), exit_code if isinstance(exit_code, int) else None
  fault = find_backtest_fault(error)
  if fault is not None and fault.code == "task.inactive":
    return "lease_lost", None
  return "exception", exit_code if isinstance(exit_code, int) else None


def sanitize_text(value: str, limit: int) -> tuple[str, dict[str, Any]]:
  normalized = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
  redacted = _CONNECTION_URL.sub("<REDACTED_URL>", normalized)
  redacted = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<REDACTED>", redacted)
  if len(redacted) <= limit:
    return redacted, {}
  digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
  return redacted[:limit], {
    "truncated": True,
    "originalCharacters": len(redacted),
    "contentDigest": digest,
  }


def emit_task_event(event: dict[str, Any]) -> None:
  """stdout 日志写入失败不得改变业务状态。"""
  try:
    _event_logger().log(
      getattr(logging, str(event["level"]).upper()),
      json.dumps(event, ensure_ascii=False, separators=(",", ":")),
    )
  except Exception:
    logging.getLogger(__name__).exception("结构化任务日志写入失败")


def emit_external_event(event: dict[str, Any], *, level: str = "info") -> None:
  """Supervisor 与容器观察器复用同一单行 JSON 输出。"""
  try:
    payload = build_external_event(event, level=level)
    _event_logger().log(
      getattr(logging, level.upper()),
      json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    )
  except Exception:
    logging.getLogger(__name__).exception("外部结构化任务日志写入失败")


def build_external_event(event: dict[str, Any], *, level: str = "info") -> dict[str, Any]:
  """校验受信调用方事件，避免任意 Docker 属性进入集中日志。"""
  unknown = set(event) - EXTERNAL_FIELDS
  if unknown:
    raise ValueError(f"外部任务日志字段不在允许列表：{sorted(unknown)}")
  if level not in LEVELS or event.get("event") not in EVENTS:
    raise ValueError("外部任务日志事件或级别无效")
  stage = event.get("stage")
  if stage is not None:
    _validate_stage(str(stage))
  status = event.get("status")
  if status is not None and status not in STATUSES:
    raise ValueError("Attempt 状态无效")
  termination_reason = event.get("terminationReason")
  if termination_reason is not None and termination_reason not in TERMINATION_REASONS:
    raise ValueError("Attempt 终止原因无效")
  message, metadata = sanitize_text(str(event.get("message") or ""), _MESSAGE_LIMIT)
  return {
    "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    "schemaVersion": SCHEMA_VERSION,
    "level": level,
    **event,
    "message": message,
    **metadata,
  }


def get_admin_run_diagnostics(
  run_id: str,
  attempt_token: str | None = None,
) -> dict[str, Any]:
  """组合 PostgreSQL 事实和可降级 Loki 时间线。"""
  from backend.services import loki_client

  diagnostics = state_store.get_backtest_run_diagnostics(run_id)
  try:
    log_status, log_events = loki_client.query_task_events(
      run_id, diagnostics["attempts"], attempt_token,
    )
  except loki_client.LokiUnavailableError:
    log_status, log_events = "unavailable", []
  database_events = _database_timeline(diagnostics["attempts"], diagnostics["errors"])
  timeline = [*database_events, *log_events]
  timeline.sort(key=_timeline_sort_key)
  return {
    **diagnostics,
    "logStatus": log_status,
    "timeline": timeline[:settings.LOG_QUERY_MAX_LINES],
  }


def _timeline_sort_key(item: dict[str, Any]) -> tuple[float, int, str]:
  """统一数据库本地时间与 Loki ISO 时间，并稳定排列同秒生命周期。"""
  timestamp = str(item.get("timestamp") or item.get("occurredAt") or "")
  try:
    if "T" in timestamp:
      parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
      if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
      parsed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=SHANGHAI_TIMEZONE,
      )
    epoch = parsed.timestamp()
  except ValueError:
    epoch = float("inf")
  event = str(item.get("event") or "")
  return epoch, _TIMELINE_EVENT_ORDER.get(event, 50), event


def _database_timeline(
  attempts: list[dict[str, Any]],
  errors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
  events: list[dict[str, Any]] = []
  for attempt in attempts:
    events.append({
      "source": "database",
      "event": "attempt.started",
      "timestamp": attempt["startedAt"],
      "attemptNo": attempt["attemptNo"],
      "attemptToken": attempt["attemptToken"],
      "stage": "attempt.claimed",
      "status": "running",
      "message": "执行尝试已领取",
    })
    if attempt.get("finishedAt"):
      events.append({
        "source": "database",
        "event": f"attempt.{attempt['status']}",
        "timestamp": attempt["finishedAt"],
        "attemptNo": attempt["attemptNo"],
        "attemptToken": attempt["attemptToken"],
        "stage": attempt["stage"],
        "status": attempt["status"],
        "terminationReason": attempt.get("terminationReason"),
        "exitCode": attempt.get("exitCode"),
        "errorCode": attempt.get("errorCode"),
        "message": "执行尝试已结束",
      })
  for error in errors:
    events.append({
      "source": "database",
      "event": "attempt.error_recorded",
      "timestamp": error["occurredAt"],
      "attemptNo": error.get("attemptNo"),
      "attemptToken": error.get("attemptToken"),
      "stage": error["stage"],
      "errorCode": error["code"],
      "exceptionType": error["exceptionType"],
      "message": error["reason"],
    })
  return events


def _event_logger() -> logging.Logger:
  event_logger = logging.getLogger("task_events")
  if not event_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    event_logger.addHandler(handler)
    event_logger.propagate = False
  return event_logger


def _validate_stage(stage: str) -> None:
  if stage not in STAGES:
    raise ValueError("Attempt 阶段名无效")


def _is_uuid(value: str) -> bool:
  try:
    UUID(value)
  except ValueError:
    return False
  return True
