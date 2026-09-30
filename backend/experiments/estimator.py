"""标准 Bar 实验资源估算 Adapter。"""

from __future__ import annotations

from typing import Any, Protocol

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
    scenario_bars = [
      (int(item["formalDateCount"]) + int(item["warmupDateCount"])) * bars_per_day
      for item in dataset_snapshots
    ]
    repeat_count = planned_trial_count // max(date_scenario_count, 1)
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
  minutes = int(timeframe.removesuffix("m"))
  return max(1, (240 + minutes - 1) // minutes)
