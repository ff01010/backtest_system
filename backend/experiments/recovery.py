"""实验失败策略、错误指纹与聚合状态领域规则。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.experiments.canonicalization import digest, with_version
from backend.experiments.errors import ExperimentError


TERMINAL_EXPERIMENT_STATUSES = frozenset({
  "completed", "partially_completed", "failed", "cancelled",
})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
  "preparing": frozenset({
    "preparing", "queued", "running", "cancelling", "completed",
    "partially_completed", "failed", "cancelled", "generation_blocked",
  }),
  "generation_blocked": frozenset({
    "generation_blocked", "preparing", "queued", "running",
    "cancelling", "cancelled",
  }),
  "queued": frozenset({
    "queued", "running", "cancelling", "completed", "partially_completed",
    "failed", "cancelled", "generation_blocked",
  }),
  "running": frozenset({
    "queued", "running", "cancelling", "completed", "partially_completed",
    "failed", "cancelled", "generation_blocked",
  }),
  "cancelling": frozenset({
    "cancelling", "completed", "partially_completed", "cancelled",
  }),
  "completed": frozenset({"completed"}),
  "partially_completed": frozenset({"partially_completed"}),
  "failed": frozenset({"failed"}),
  "cancelled": frozenset({"cancelled"}),
}


@dataclass(frozen=True)
class AggregateFacts:
  """从 Trial 与普通任务真源读取的聚合事实。"""

  planned: int
  generated: int
  pending: int
  queued: int
  running: int
  cancelling: int
  succeeded: int
  failed: int
  cancelled: int
  cancel_requested: bool

  @property
  def terminal(self) -> int:
    return self.succeeded + self.failed + self.cancelled

  @property
  def non_terminal(self) -> int:
    return max(0, self.planned - self.terminal)


def default_failure_policy() -> dict[str, Any]:
  """返回写入每个 Revision 的不可变首期失败策略。"""
  return with_version({
    "version": "failure_policy_v1",
    "mode": "continue",
    "automaticRetry": {
      "allowedCategories": ["system_error"],
      "maxRetriesPerTrial": 1,
      "observationWindowSeconds": 900,
      "cooldownSeconds": 30,
    },
    "circuitBreaker": {
      "consecutiveSameFingerprint": 3,
      "failureRateWindowSize": 10,
      "failureRateMinimumSamples": 5,
      "failureRateThreshold": "0.8000",
      "cooldownSeconds": 300,
      "maxAutomaticRecoveries": 0,
      "automaticRecoveryCategories": [],
    },
  })


def derive_aggregate_status(facts: AggregateFacts) -> str:
  """严格按方案 8.3 的优先级从事实推导主状态。"""
  if facts.cancel_requested and facts.non_terminal > 0:
    return "cancelling"
  if not facts.cancel_requested and facts.generated < facts.planned:
    return "preparing"
  if not facts.cancel_requested and facts.running + facts.cancelling > 0:
    return "running"
  if not facts.cancel_requested and facts.pending + facts.queued > 0:
    return "queued"
  if facts.succeeded == facts.planned:
    return "completed"
  if facts.succeeded > 0:
    return "partially_completed"
  if facts.cancel_requested or facts.cancelled == facts.planned:
    return "cancelled"
  if facts.failed > 0:
    return "failed"
  return "queued"


def validate_status_transition(current: str, target: str) -> None:
  """拒绝把已经接受的终态改写成另一事实。"""
  if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
    raise ExperimentError(
      "experiment.invalid_status_transition",
      f"实验状态不能从 {current} 迁移到 {target}",
      409,
    )


def normalize_failure(
  summary: Any,
  *,
  stage_type: str,
  template_type: str,
  run_path: str,
  scope_type: str,
  failure_stage: str,
  component: str,
) -> dict[str, Any]:
  """只用稳定、脱敏字段形成跨执行可比较的错误指纹。"""
  safe_summary = summary if isinstance(summary, dict) else {}
  detail = safe_summary.get("errorDetail")
  safe_detail = detail if isinstance(detail, dict) else {}
  category = str(safe_detail.get("category") or "system_error")[:64]
  code = str(safe_detail.get("code") or "system.unknown")[:96]
  identity = with_version({
    "errorCode": code,
    "category": category,
    "stageType": stage_type,
    "templateType": template_type,
    "runPath": run_path,
    "scopeType": scope_type,
    "failureStage": failure_stage[:96],
    "executionMode": "backtest",
    "component": component[:96],
  })
  return {
    "category": category,
    "code": code,
    "message": str(safe_detail.get("message") or safe_summary.get("error") or "回测执行失败")[:500],
    "retryable": bool(safe_detail.get("retryable", False)),
    "failureStage": failure_stage[:96],
    "component": component[:96],
    "fingerprint": digest(identity),
  }


def allows_automatic_retry(
  policy: dict[str, Any],
  failure: dict[str, Any],
  *,
  automatic_retry_count: int,
  retry_count_in_window: int,
) -> bool:
  retry = dict(policy.get("automaticRetry") or {})
  maximum = int(retry.get("maxRetriesPerTrial") or 0)
  allowed = {str(item) for item in retry.get("allowedCategories") or []}
  return (
    bool(failure.get("retryable"))
    and str(failure.get("category")) in allowed
    and automatic_retry_count < maximum
    and retry_count_in_window < maximum
  )


def should_open_circuit(
  policy: dict[str, Any],
  *,
  consecutive_count: int,
  window_failures: int,
  window_size: int,
) -> bool:
  circuit = dict(policy.get("circuitBreaker") or {})
  consecutive_limit = int(circuit.get("consecutiveSameFingerprint") or 0)
  minimum = int(circuit.get("failureRateMinimumSamples") or 0)
  threshold = float(circuit.get("failureRateThreshold") or 1)
  consecutive = consecutive_limit > 0 and consecutive_count >= consecutive_limit
  rate = window_size >= minimum and window_size > 0 and window_failures / window_size >= threshold
  return consecutive or rate
