"""标准 Bar 实验资源估算 Adapter。"""

from __future__ import annotations

from typing import Any, Protocol

from backend.experiments.errors import ExperimentError
from backend.experiments.resolvers import ResolvedRunTemplate


class ResourceEstimator(Protocol):
  version: str

  def estimate(
    self,
    template: ResolvedRunTemplate,
    dataset_snapshots: tuple[dict[str, Any], ...],
    planned_trial_count: int,
    date_scenario_count: int,
  ) -> dict[str, Any]:
    """返回服务端权威资源估算。"""


class SingleBarResourceEstimator:
  version = "single_bar_estimator_v1"

  def estimate(
    self,
    template: ResolvedRunTemplate,
    dataset_snapshots: tuple[dict[str, Any], ...],
    planned_trial_count: int,
    date_scenario_count: int,
  ) -> dict[str, Any]:
    timeframe = str(template.snapshot["fixedStructure"]["timeframe"])
    bars_per_day = _bars_per_day(timeframe)
    scenario_bars = [_scenario_bars(item, bars_per_day) for item in dataset_snapshots]
    if (
      date_scenario_count <= 0
      or len(scenario_bars) != date_scenario_count
      or planned_trial_count <= 0
      or planned_trial_count % date_scenario_count != 0
    ):
      raise _unavailable_estimate()
    repeat_count = planned_trial_count // date_scenario_count
    estimated_bars = sum(scenario_bars) * repeat_count
    resource_class = "light"
    if estimated_bars > 500_000:
      resource_class = "heavy"
    elif estimated_bars > 100_000:
      resource_class = "medium"
    return {
      "estimatorVersion": self.version,
      "runType": "standard_bar",
      "timeframe": timeframe,
      "estimatedBarCount": estimated_bars,
      "maxBarsPerTrial": max(scenario_bars, default=0),
      "resourceClass": resource_class,
      "confidence": "coverage_metadata",
    }


def _bars_per_day(timeframe: str) -> int:
  if timeframe == "1d":
    return 1
  if not timeframe.endswith("m"):
    raise _unavailable_estimate()
  try:
    minutes = int(timeframe[:-1])
  except ValueError as error:
    raise _unavailable_estimate() from error
  if minutes <= 0:
    raise _unavailable_estimate()
  return max(1, (240 + minutes - 1) // minutes)


def _scenario_bars(snapshot: dict[str, Any], bars_per_day: int) -> int:
  formal_days = snapshot.get("formalDateCount")
  warmup_days = snapshot.get("warmupDateCount")
  counts = (formal_days, warmup_days)
  if any(isinstance(value, bool) or not isinstance(value, int) for value in counts):
    raise _unavailable_estimate()
  total_days = int(formal_days) + int(warmup_days)
  if formal_days <= 0 or warmup_days < 0 or total_days <= 0:
    raise _unavailable_estimate()
  return total_days * bars_per_day


def _unavailable_estimate() -> ExperimentError:
  return ExperimentError(
    "experiment.capacity_unavailable",
    "当前数据或周期元数据不足，无法形成可靠资源估算",
    503,
    retryable=True,
  )
