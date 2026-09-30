"""不可变实验结果的基础评价目录、适配和确定性流水线。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import cmp_to_key
import hashlib
import json
from typing import Any

from backend.experiments.canonicalization import digest, with_version
from backend.experiments.errors import ExperimentError


METRIC_CATALOG_VERSION = "metric_catalog_v1"
DIMENSION_CATALOG_VERSION = "dimension_catalog_v1"
EVALUATION_PIPELINE_VERSION = "basic_evaluation_pipeline_v1"
RESULT_ADAPTER_TYPE = "legacy_single_bar_result"
RESULT_ADAPTER_VERSION = "1"


@dataclass(frozen=True)
class MetricValue:
  value: Decimal | int | str | None
  comparability_key: str | None
  unsupported_reason: str | None = None


@dataclass(frozen=True)
class EvaluationRow:
  identity: dict[str, Any]
  dimensions: dict[str, Any]
  metrics: dict[str, MetricValue]


def metric_catalog() -> dict[str, Any]:
  return {
    "version": METRIC_CATALOG_VERSION,
    "metrics": [
      _metric("totalPnl", "总盈亏", "decimal", "CNY", "higher", "summary.totalPnl"),
      _metric("finalEquity", "期末权益", "decimal", "CNY", "higher", "summary.finalEquity"),
      _metric("totalReturn", "总收益率", "decimal", "ratio", "higher", "summary.totalReturn"),
      _metric("annualReturn", "年化收益率", "decimal", "ratio", "higher", "summary.annualReturn"),
      _metric("maxDrawdown", "最大回撤", "decimal", "CNY", "lower", "summary.maxDrawdown"),
      _metric("maxDrawdownRate", "最大回撤率", "decimal", "ratio", "lower", "summary.maxDrawdownRate"),
      _metric("annualVolatility", "年化波动率", "decimal", "ratio", "lower", "summary.annualVolatility"),
      _metric("annualSharpe", "年化夏普", "decimal", "ratio", "higher", "summary.annualSharpe"),
      _metric("sortino", "索提诺", "decimal", "ratio", "higher", "summary.sortino"),
      _metric("completedTradeCount", "完成交易数", "integer", "count", "neutral", "summary.completedTradeCount"),
      _metric("winRate", "胜率", "decimal", "ratio", "higher", "summary.winRate"),
      _metric("profitLossRatio", "盈亏比", "decimal", "ratio", "higher", "summary.profitLossRatio"),
      _metric("benchmarkReturn", "基准收益率", "decimal", "ratio", "neutral", "benchmarkReturn", benchmark=True),
      _metric("excessReturn", "超额收益率", "decimal", "ratio", "higher", "excessReturn", benchmark=True),
      _metric("queueDurationMs", "排队耗时", "integer", "ms", "lower", "timestamps.queue", operational=True),
      _metric("runDurationMs", "运行耗时", "integer", "ms", "lower", "timestamps.run", operational=True),
      _metric("resultStatus", "结果状态", "string", None, "neutral", "run.status", operational=True),
    ],
  }


def dimension_catalog(experiment_spec: dict[str, Any]) -> dict[str, Any]:
  dimensions = [
    _dimension("stageId", "Stage ID", "string"),
    _dimension("stageIndex", "Stage 顺序", "integer"),
    _dimension("stageType", "Stage 类型", "string"),
    _dimension("dateScenarioId", "日期场景", "string"),
    _dimension("dateRole", "日期用途", "string"),
    _dimension("initialCapital", "初始资金", "decimal", "CNY"),
    _dimension("costScenarioId", "成本场景", "string"),
    _dimension("executionScenarioId", "执行场景", "string"),
    _dimension("fillModel", "成交模型", "string"),
    _dimension("resultStatus", "结果状态", "string"),
  ]
  snapshot = dict(experiment_spec.get("runTemplateSnapshot") or {})
  tunable = dict(snapshot.get("tunableInputCatalog") or {})
  for item in list(tunable.get("inputs") or []):
    if not isinstance(item, dict) or item.get("group") != "strategyParameters":
      continue
    input_id = str(item.get("inputId") or "")
    source_name = str(item.get("sourceName") or "")
    if input_id and source_name:
      dimensions.append(_dimension(
        f"parameter.{input_id}", source_name, str(item.get("type") or "string"),
        source={"group": "strategyParameters", "sourceName": source_name},
      ))
  return {"version": DIMENSION_CATALOG_VERSION, "dimensions": dimensions}


def catalog_snapshot(experiment_spec: dict[str, Any]) -> dict[str, Any]:
  return {
    "metricCatalog": metric_catalog(),
    "dimensionCatalog": dimension_catalog(experiment_spec),
  }


def validate_evaluation_spec(
  evaluation_spec: dict[str, Any],
  catalogs: dict[str, Any],
) -> None:
  metrics = _catalog_by_id(catalogs["metricCatalog"], "metrics", "metricId")
  dimensions = _catalog_by_id(
    catalogs["dimensionCatalog"], "dimensions", "dimensionId",
  )
  for field_id in evaluation_spec["columns"]:
    if field_id not in metrics and field_id not in dimensions:
      raise _unknown_field(field_id)
  for item in evaluation_spec["filters"]:
    definition = metrics.get(item["metricId"])
    if definition is None:
      raise _unknown_metric(item["metricId"])
    _validate_filter_type(item, definition)
  for item in evaluation_spec["sort"]:
    definition = metrics.get(item["metricId"])
    if definition is None:
      raise _unknown_metric(item["metricId"])
    if definition["dataType"] not in {"decimal", "integer", "string"}:
      raise ExperimentError(
        "experiment.evaluation_spec_invalid", "排序指标类型不受支持", 422,
      )


def evaluate(
  evaluation_spec: dict[str, Any],
  catalogs: dict[str, Any],
  source_rows: list[dict[str, Any]],
  result_set_hash: str,
) -> dict[str, Any]:
  validate_evaluation_spec(evaluation_spec, catalogs)
  definitions = _catalog_by_id(catalogs["metricCatalog"], "metrics", "metricId")
  rows = [_adapt_row(item, definitions) for item in source_rows]
  comparison_ids = [
    *[item["metricId"] for item in evaluation_spec["filters"]],
    *[item["metricId"] for item in evaluation_spec["sort"]],
  ]
  _validate_comparability(rows, list(dict.fromkeys(comparison_ids)))
  filtered = [row for row in rows if _passes_filters(row, evaluation_spec["filters"], definitions)]
  ordered = sorted(filtered, key=cmp_to_key(lambda left, right: _compare_rows(
    left, right, evaluation_spec["sort"], definitions,
  )))
  return _output_snapshot(evaluation_spec, catalogs, ordered, len(rows), result_set_hash)


def stable_result_hash(value: Any) -> str:
  encoded = json.dumps(
    value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
  )
  return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def output_hash(output: dict[str, Any]) -> str:
  return digest(output)


def _metric(
  metric_id: str,
  name: str,
  data_type: str,
  unit: str | None,
  direction: str,
  path: str,
  *,
  benchmark: bool = False,
  operational: bool = False,
) -> dict[str, Any]:
  return {
    "metricId": metric_id,
    "displayName": name,
    "dataType": data_type,
    "unit": unit,
    "direction": direction,
    "grain": "trial_execution",
    "currency": "CNY" if unit == "CNY" else None,
    "windowSemantics": "none" if operational else "frozen_date_scenario",
    "annualization": (
      "trading_days_252"
      if metric_id in {"annualReturn", "annualVolatility", "annualSharpe", "sortino"}
      else "none"
    ),
    "benchmarkSemantics": "frozen_benchmark" if benchmark else "none",
    "comparabilityKeyVersion": "metric_comparability_v1",
    "nullSemantics": "unsupported_or_missing_is_null",
    "resultPath": path,
    "adapterType": RESULT_ADAPTER_TYPE,
    "adapterVersion": RESULT_ADAPTER_VERSION,
    "supportedResultSchemaVersions": ["2", "3"],
    "supportedRunPaths": ["strategy"],
    "supportedScopeTypes": ["single"],
    "supportedFillModels": ["bar_next_open"],
    "supportedStrategyCategories": ["directional_holding", "core_position_overlay"],
    "operational": operational,
  }


def _dimension(
  dimension_id: str,
  name: str,
  data_type: str,
  unit: str | None = None,
  *,
  source: dict[str, Any] | None = None,
) -> dict[str, Any]:
  return {
    "dimensionId": dimension_id,
    "displayName": name,
    "dataType": data_type,
    "unit": unit,
    "sortRule": "canonical_ascending",
    "nullSemantics": "missing_is_null",
    "applicableTemplateTypes": ["legacy_single_strategy_v1"],
    "applicableStageTypes": ["finite_grid_v1"],
    "applicableRunPaths": ["strategy"],
    "applicableScopeTypes": ["single"],
    "source": source,
  }


def _catalog_by_id(
  catalog: dict[str, Any],
  collection: str,
  id_field: str,
) -> dict[str, dict[str, Any]]:
  return {
    str(item[id_field]): item
    for item in catalog[collection]
    if isinstance(item, dict) and id_field in item
  }


def _validate_filter_type(item: dict[str, Any], definition: dict[str, Any]) -> None:
  operator = item["operator"]
  if operator in {"is_null", "is_not_null"}:
    return
  if definition["dataType"] == "string" and operator not in {"=", "!="}:
    raise ExperimentError(
      "experiment.evaluation_spec_invalid", "字符串指标只支持等于或不等于过滤", 422,
    )
  if definition["dataType"] in {"decimal", "integer"}:
    _decimal(item["value"], item["metricId"])


def _adapt_row(
  source: dict[str, Any],
  definitions: dict[str, dict[str, Any]],
) -> EvaluationRow:
  result = source.get("result_json")
  schema_version = str(source.get("result_schema_version") or "")
  _validate_adapter_source(source)
  if not isinstance(result, dict):
    raise ExperimentError(
      "experiment.evaluation_result_missing", "评价使用的原始结果不存在", 409,
    )
  if stable_result_hash(result) != source["expected_result_hash"]:
    raise ExperimentError(
      "experiment.evaluation_result_hash_mismatch", "评价使用的原始结果哈希不一致", 409,
    )
  dimensions = _dimensions(source)
  metrics = {
    metric_id: _metric_value(source, result, definition, schema_version)
    for metric_id, definition in definitions.items()
  }
  return EvaluationRow(
    identity={
      "trialId": source["trial_id"],
      "trialExecutionId": source["execution_id"],
      "runId": source["run_id"],
      "stageId": source["stage_id"],
      "stageIndex": int(source["stage_index"]),
      "trialIndex": int(source["trial_index"]),
      "resultHash": source["expected_result_hash"],
      "startDate": dict(source.get("dimension_projection") or {}).get(
        "dateScenario", {},
      ).get("startDate"),
    },
    dimensions=dimensions,
    metrics=metrics,
  )


def _dimensions(source: dict[str, Any]) -> dict[str, Any]:
  assignment = dict(source.get("dimension_projection") or {})
  date = dict(assignment.get("dateScenario") or {})
  cost = dict(assignment.get("costScenario") or {})
  execution = dict(assignment.get("executionScenario") or {})
  values = {
    "stageId": source["stage_id"],
    "stageIndex": int(source["stage_index"]),
    "stageType": source["stage_type"],
    "dateScenarioId": date.get("scenarioId"),
    "dateRole": date.get("role"),
    "initialCapital": assignment.get("initialCapital"),
    "costScenarioId": cost.get("scenarioId"),
    "executionScenarioId": execution.get("scenarioId"),
    "fillModel": source.get("fill_model"),
    "resultStatus": source.get("run_status"),
  }
  parameters = dict(assignment.get("strategyParameters") or {})
  for input_id, source_name in source.get("parameter_dimensions", {}).items():
    values[f"parameter.{input_id}"] = parameters.get(source_name)
  return values


def _validate_adapter_source(source: dict[str, Any]) -> None:
  request = dict(source.get("requested_params") or {})
  supported = (
    source.get("run_path") == "strategy"
    and source.get("scope_type") == "single"
    and source.get("fill_model") == "bar_next_open"
    and request.get("strategyCategory") in {
      "directional_holding", "core_position_overlay",
    }
  )
  if not supported:
    raise ExperimentError(
      "experiment.evaluation_adapter_unsupported",
      "冻结执行路径没有可用的结果指标适配器",
      409,
    )


def _metric_value(
  source: dict[str, Any],
  result: dict[str, Any],
  definition: dict[str, Any],
  schema_version: str,
) -> MetricValue:
  if schema_version not in definition["supportedResultSchemaVersions"]:
    return MetricValue(None, None, f"unsupported_result_schema:{schema_version or 'missing'}")
  raw = _raw_metric(source, result, definition["resultPath"])
  if raw is None:
    return MetricValue(None, None, "metric_missing")
  try:
    value = _typed_value(raw, definition["dataType"], definition["metricId"])
  except ExperimentError as error:
    return MetricValue(None, None, error.code)
  return MetricValue(value, _comparability_key(source, result, definition))


def _raw_metric(source: dict[str, Any], result: dict[str, Any], path: str) -> Any:
  if path == "run.status":
    return source.get("run_status")
  if path == "timestamps.queue":
    return _duration_ms(source.get("queued_at"), source.get("started_at"))
  if path == "timestamps.run":
    return _duration_ms(source.get("started_at"), source.get("finished_at"))
  current: Any = result
  for part in path.split("."):
    if not isinstance(current, dict):
      return None
    current = current.get(part)
  return current


def _duration_ms(start: Any, end: Any) -> int | None:
  if not isinstance(start, datetime) or not isinstance(end, datetime):
    return None
  return max(0, round((end - start).total_seconds() * 1000))


def _typed_value(value: Any, data_type: str, metric_id: str) -> Decimal | int | str:
  if data_type == "decimal":
    return _decimal(value, metric_id)
  if data_type == "integer":
    if isinstance(value, bool):
      raise _invalid_metric(metric_id)
    try:
      return int(value)
    except (TypeError, ValueError, OverflowError) as error:
      raise _invalid_metric(metric_id) from error
  if data_type == "string" and isinstance(value, str):
    return value
  raise _invalid_metric(metric_id)


def _decimal(value: Any, field: str) -> Decimal:
  if isinstance(value, bool):
    raise _invalid_metric(field)
  try:
    parsed = Decimal(str(value))
  except (InvalidOperation, ValueError, TypeError) as error:
    raise _invalid_metric(field) from error
  if not parsed.is_finite():
    raise _invalid_metric(field)
  return parsed


def _comparability_key(
  source: dict[str, Any],
  result: dict[str, Any],
  definition: dict[str, Any],
) -> str:
  assignment = dict(source.get("dimension_projection") or {})
  date = dict(assignment.get("dateScenario") or {})
  request = dict(source.get("requested_params") or {})
  benchmark = result.get("benchmarkSymbol") or request.get("benchmarkSymbol")
  benchmark_equity = result.get("benchmarkEquity")
  benchmark_version = (
    stable_result_hash(benchmark_equity)
    if isinstance(benchmark_equity, list) else None
  )
  key = with_version({
    "version": definition["comparabilityKeyVersion"],
    "metricId": definition["metricId"],
    "metricVersion": "1",
    "grain": definition["grain"],
    "currency": definition["currency"],
    "capitalSemantics": assignment.get("initialCapital"),
    "windowStart": date.get("startDate"),
    "windowEnd": date.get("endDate"),
    "windowPolicy": definition["windowSemantics"],
    "annualizationCalendar": definition["annualization"],
    "benchmarkId": benchmark if definition["benchmarkSemantics"] != "none" else None,
    "benchmarkVersion": (
      benchmark_version if definition["benchmarkSemantics"] != "none" else None
    ),
    "runPath": source.get("run_path"),
    "scopeType": source.get("scope_type"),
    "resultSchemaVersion": source.get("result_schema_version"),
    "normalizerVersion": None,
  })
  if definition["operational"]:
    key.update({"capitalSemantics": None, "windowStart": None, "windowEnd": None})
  return digest(key)


def _validate_comparability(rows: list[EvaluationRow], metric_ids: list[str]) -> None:
  for metric_id in metric_ids:
    domains = {
      row.metrics[metric_id].comparability_key
      for row in rows
      if row.metrics[metric_id].value is not None
    }
    if len(domains) > 1:
      raise ExperimentError(
        "experiment.metric_not_comparable",
        f"指标 {metric_id} 存在多个不可直接比较的口径",
        409,
        details={"metricId": metric_id, "domainCount": len(domains)},
      )


def _passes_filters(
  row: EvaluationRow,
  filters: list[dict[str, Any]],
  definitions: dict[str, dict[str, Any]],
) -> bool:
  for item in filters:
    metric = row.metrics[item["metricId"]]
    operator = item["operator"]
    if operator == "is_null":
      passed = metric.value is None
    elif operator == "is_not_null":
      passed = metric.value is not None
    elif metric.value is None:
      passed = False
    else:
      expected = _filter_value(item["value"], definitions[item["metricId"]])
      passed = _compare_value(metric.value, expected, operator)
    if not passed:
      return False
  return True


def _filter_value(value: Any, definition: dict[str, Any]) -> Decimal | int | str:
  return _typed_value(value, definition["dataType"], definition["metricId"])


def _compare_value(left: Any, right: Any, operator: str) -> bool:
  return {
    ">": left > right,
    ">=": left >= right,
    "<": left < right,
    "<=": left <= right,
    "=": left == right,
    "!=": left != right,
  }[operator]


def _compare_rows(
  left: EvaluationRow,
  right: EvaluationRow,
  sort_items: list[dict[str, Any]],
  definitions: dict[str, dict[str, Any]],
) -> int:
  for item in sort_items:
    left_value = left.metrics[item["metricId"]].value
    right_value = right.metrics[item["metricId"]].value
    compared = _compare_nullable(left_value, right_value, item["direction"])
    if compared:
      return compared
  return _compare_identity(left.identity, right.identity)


def _compare_nullable(left: Any, right: Any, direction: str) -> int:
  if left is None and right is None:
    return 0
  if left is None:
    return 1
  if right is None:
    return -1
  compared = (left > right) - (left < right)
  return -compared if direction == "desc" else compared


def _compare_identity(left: dict[str, Any], right: dict[str, Any]) -> int:
  left_key = (left["stageIndex"], left["trialIndex"], left["trialId"])
  right_key = (right["stageIndex"], right["trialIndex"], right["trialId"])
  return (left_key > right_key) - (left_key < right_key)


def _output_snapshot(
  evaluation_spec: dict[str, Any],
  catalogs: dict[str, Any],
  rows: list[EvaluationRow],
  source_count: int,
  result_set_hash: str,
) -> dict[str, Any]:
  metric_ids = set(_catalog_by_id(catalogs["metricCatalog"], "metrics", "metricId"))
  return with_version({
    "schemaVersion": "basic_evaluation_output_v1",
    "pipelineVersion": EVALUATION_PIPELINE_VERSION,
    "resultSetHash": result_set_hash,
    "sourceRowCount": source_count,
    "matchedRowCount": len(rows),
    "columns": evaluation_spec["columns"],
    "sort": evaluation_spec["sort"],
    "rows": [
      {
        **row.identity,
        "values": {
          field_id: _json_value(
            row.metrics[field_id].value if field_id in metric_ids
            else row.dimensions.get(field_id)
          )
          for field_id in evaluation_spec["columns"]
        },
        "unsupportedReasons": {
          field_id: row.metrics[field_id].unsupported_reason
          for field_id in evaluation_spec["columns"]
          if field_id in metric_ids and row.metrics[field_id].unsupported_reason
        },
      }
      for row in rows
    ],
  })


def _json_value(value: Any) -> Any:
  if isinstance(value, Decimal):
    return format(value, "f")
  return value


def _unknown_field(field_id: str) -> ExperimentError:
  return ExperimentError(
    "experiment.evaluation_field_unknown", f"未知评价字段: {field_id}", 422,
  )


def _unknown_metric(metric_id: str) -> ExperimentError:
  return ExperimentError(
    "experiment.evaluation_metric_unknown", f"未知评价指标: {metric_id}", 422,
  )


def _invalid_metric(metric_id: str) -> ExperimentError:
  return ExperimentError(
    "experiment.evaluation_metric_invalid", f"指标值无效: {metric_id}", 409,
  )
