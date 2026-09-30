"""评价聚合与参数平原使用的冻结统计定义。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from backend.experiments.errors import ExperimentError


STATISTICS_VERSION = "evaluation_statistics_v1_nearest_rank"
DECIMAL_PRECISION = 34


def aggregate_values(
  values: list[Decimal | int | None],
  function: str,
  percentile: str | None = None,
) -> Decimal | int | None:
  numeric = [_decimal(value) for value in values if value is not None]
  if function == "count":
    return len(numeric)
  if not numeric:
    return None
  ordered = sorted(numeric)
  if function == "min":
    return ordered[0]
  if function == "max":
    return ordered[-1]
  if function == "median":
    return _median(ordered)
  if function == "mean":
    return _mean(ordered)
  if function == "sample_stddev":
    return _sample_stddev(ordered)
  if function == "nearest_rank_percentile":
    return _nearest_rank(ordered, percentile)
  raise ExperimentError(
    "experiment.evaluation_spec_invalid",
    f"未知聚合函数: {function}",
    422,
  )


def local_statistics(values: list[Decimal | int | None]) -> dict[str, Any]:
  numeric = [_decimal(value) for value in values if value is not None]
  if not numeric:
    return {
      "count": 0,
      "median": None,
      "min": None,
      "sampleStddev": None,
      "range": None,
    }
  minimum = min(numeric)
  maximum = max(numeric)
  return {
    "count": len(numeric),
    "median": _median(sorted(numeric)),
    "min": minimum,
    "sampleStddev": _sample_stddev(numeric),
    "range": maximum - minimum,
  }


def _decimal(value: Decimal | int) -> Decimal:
  parsed = Decimal(value) if isinstance(value, int) else value
  if not parsed.is_finite():
    raise _non_finite()
  return parsed


def _mean(values: list[Decimal]) -> Decimal:
  with localcontext() as context:
    context.prec = DECIMAL_PRECISION
    value = sum(values, Decimal(0)) / Decimal(len(values))
  return _finite(value)


def _median(values: list[Decimal]) -> Decimal:
  middle = len(values) // 2
  if len(values) % 2:
    return values[middle]
  with localcontext() as context:
    context.prec = DECIMAL_PRECISION
    value = (values[middle - 1] + values[middle]) / Decimal(2)
  return _finite(value)


def _sample_stddev(values: list[Decimal]) -> Decimal | None:
  if len(values) < 2:
    return None
  with localcontext() as context:
    context.prec = DECIMAL_PRECISION
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values) - 1)
    try:
      result = variance.sqrt()
    except InvalidOperation as error:
      raise _non_finite() from error
  return _finite(result)


def _nearest_rank(values: list[Decimal], percentile: str | None) -> Decimal:
  if percentile is None:
    raise ExperimentError(
      "experiment.evaluation_spec_invalid",
      "最近秩分位数缺少 percentile",
      422,
    )
  rank_value = Decimal(percentile) * Decimal(len(values))
  rank = int(rank_value.to_integral_value(rounding="ROUND_CEILING"))
  if rank < 1 or rank > len(values):
    raise ExperimentError(
      "experiment.evaluation_spec_invalid",
      "最近秩分位数必须大于 0 且不超过 1",
      422,
    )
  return values[rank - 1]


def _finite(value: Decimal) -> Decimal:
  if not value.is_finite():
    raise _non_finite()
  return value


def _non_finite() -> ExperimentError:
  return ExperimentError(
    "experiment.evaluation_statistic_non_finite",
    "评价统计产生了非有限结果",
    422,
  )
