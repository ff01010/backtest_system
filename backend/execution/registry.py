"""执行算法注册、默认值与请求校验。"""

from __future__ import annotations

from typing import Any

from backend.execution.algorithms import ImmediateExecutionAlgorithm, TwapExecutionAlgorithm
from backend.execution.contract import ExecutionAlgorithm


_ALGORITHMS = {
  "immediate": ImmediateExecutionAlgorithm(),
  "twap": TwapExecutionAlgorithm(),
}
_TWAP_DEFAULTS = {"durationSeconds": 300, "intervalSeconds": 30, "minChildQuantity": 100, "completionPolicy": "cancel_remaining"}


def get_execution_algorithm(algorithm_id: str) -> ExecutionAlgorithm:
  try:
    return _ALGORITHMS[algorithm_id]
  except KeyError as error:
    raise ValueError(f"不支持的执行方式: {algorithm_id}") from error


def validate_execution_request(fill_model: str, algorithm_id: object, parameters: object) -> dict[str, Any]:
  normalized_id = str(algorithm_id or "immediate")
  get_execution_algorithm(normalized_id)
  if normalized_id == "immediate":
    return {"executionAlgorithm": "immediate", "executionParameters": {}}
  if fill_model != "l2_quote":
    raise ValueError("TWAP 仅支持 L2 盘口回放")
  values = {**_TWAP_DEFAULTS, **(dict(parameters) if isinstance(parameters, dict) else {})}
  _validate_twap(values)
  return {"executionAlgorithm": normalized_id, "executionParameters": values}


def execution_algorithm_snapshot(algorithm_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
  algorithm = get_execution_algorithm(algorithm_id)
  return {"id": algorithm.algorithm_id, "version": algorithm.version, "parameters": dict(parameters)}


def _validate_twap(values: dict[str, Any]) -> None:
  duration = _positive_int(values.get("durationSeconds"), "执行窗口")
  interval = _positive_int(values.get("intervalSeconds"), "拆单间隔")
  _positive_int(values.get("minChildQuantity"), "最小子单数量")
  if duration < 3:
    raise ValueError("执行窗口至少为 3 秒")
  if interval % 3 != 0 or interval > duration:
    raise ValueError("拆单间隔必须是 3 秒倍数且不大于执行窗口")
  if values.get("completionPolicy") != "cancel_remaining":
    raise ValueError("首期 TWAP 仅支持窗口结束后取消剩余数量")


def _positive_int(value: object, label: str) -> int:
  try:
    parsed = int(value)  # type: ignore[arg-type]
  except (TypeError, ValueError) as error:
    raise ValueError(f"{label}必须为正整数") from error
  if parsed <= 0 or str(parsed) != str(value).strip():
    raise ValueError(f"{label}必须为正整数")
  return parsed
