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
from backend.experiments.evaluation_formula import SafeFormula
from backend.experiments.evaluation_statistics import (
  STATISTICS_VERSION,
  aggregate_values,
  local_statistics,
)


METRIC_CATALOG_VERSION = "metric_catalog_v1"
DIMENSION_CATALOG_VERSION = "dimension_catalog_v1"
EVALUATION_PIPELINE_VERSION = "advanced_evaluation_pipeline_v1"
LEGACY_EVALUATION_PIPELINE_VERSION = "basic_evaluation_pipeline_v1"
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


@dataclass(frozen=True)
class GroupRow:
  identity: dict[str, Any]
  dimensions: dict[str, Any]
  metrics: dict[str, MetricValue]
  member_count: int


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
  stage_spec = _first_stage_spec(experiment_spec)
  date_scenarios = list(stage_spec.get("dateScenarios") or [])
  cost_scenarios = list(stage_spec.get("costScenarios") or [])
  dimensions = [
    _dimension("stageId", "Stage ID", "string"),
    _dimension("stageIndex", "Stage 顺序", "integer"),
    _dimension("stageType", "Stage 类型", "string"),
    _dimension(
      "dateScenarioId", "日期场景", "string",
      ordered_values=[item.get("scenarioId") for item in date_scenarios],
    ),
    _dimension("dateScenarioOrder", "日期场景顺序", "integer"),
    _dimension("dateRole", "日期用途", "string"),
    _dimension("dateStart", "开始日期", "string"),
    _dimension("dateEnd", "结束日期", "string"),
    _dimension("adjustmentMode", "复权口径", "string"),
    _dimension("datasetContentDigest", "数据集摘要", "string"),
    _dimension(
      "initialCapital", "初始资金", "decimal", "CNY",
      ordered_values=list(stage_spec.get("capitalScenarios") or []),
    ),
    _dimension(
      "costScenarioId", "成本场景", "string",
      ordered_values=[item.get("scenarioId") for item in cost_scenarios],
    ),
    _dimension("slippage", "滑点", "decimal", "ratio"),
    _dimension(
      "executionScenarioId", "执行场景", "string",
      ordered_values=[
        item.get("scenarioId")
        for item in list(stage_spec.get("executionScenarios") or [])
        if isinstance(item, dict)
      ],
    ),
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
      values = _parameter_values(stage_spec, input_id)
      dimensions.append(_dimension(
        f"parameter.{input_id}", source_name, str(item.get("type") or "string"),
        source={"group": "strategyParameters", "sourceName": source_name},
        ordered_values=values,
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
  base_metrics = _catalog_by_id(catalogs["metricCatalog"], "metrics", "metricId")
  dimensions = _catalog_by_id(
    catalogs["dimensionCatalog"], "dimensions", "dimensionId",
  )
  row_formulas = _formula_definitions(evaluation_spec, "row_formula_v1")
  group_formulas = _formula_definitions(evaluation_spec, "group_formula_v1")
  aggregate_definitions = {
    item["aggregateId"]: item for item in evaluation_spec["aggregates"]
  }
  numeric_base_metrics = {
    metric_id for metric_id, definition in base_metrics.items()
    if definition["dataType"] in {"decimal", "integer"}
  }
  _validate_unique_metric_ids(
    base_metrics, row_formulas, aggregate_definitions, group_formulas,
  )
  row_metrics = {**base_metrics, **row_formulas}
  group_metrics = {**aggregate_definitions, **group_formulas}
  uses_group_output = _uses_group_output(evaluation_spec)
  output_metrics = group_metrics if uses_group_output else row_metrics
  for field_id in evaluation_spec["columns"]:
    if field_id in dimensions:
      if uses_group_output and field_id not in evaluation_spec["groupBy"]:
        raise ExperimentError(
          "experiment.evaluation_spec_invalid",
          f"分组输出只能展示已声明的分组维度: {field_id}",
          422,
        )
    elif field_id not in output_metrics:
      raise _unknown_field(field_id)
  for dimension_id in evaluation_spec["groupBy"]:
    if dimension_id not in dimensions:
      raise _unknown_field(dimension_id)
  _validate_formulas(row_formulas, numeric_base_metrics, allow_prior=True)
  _validate_aggregates(evaluation_spec["aggregates"], row_metrics)
  _validate_formulas(group_formulas, set(aggregate_definitions), allow_prior=False)
  for item in evaluation_spec["filters"]:
    definition = row_metrics.get(item["metricId"])
    if definition is None:
      raise _unknown_metric(item["metricId"])
    _validate_filter_type(item, definition)
  for item in evaluation_spec["groupFilters"]:
    definition = group_metrics.get(item["metricId"])
    if definition is None:
      raise _unknown_metric(item["metricId"])
    _validate_filter_type(item, definition)
  for item in evaluation_spec["sort"]:
    definition = output_metrics.get(item["metricId"])
    if definition is None:
      raise _unknown_metric(item["metricId"])
    if definition["dataType"] not in {"decimal", "integer", "string"}:
      raise ExperimentError(
        "experiment.evaluation_spec_invalid", "排序指标类型不受支持", 422,
      )
  _validate_completeness_dimensions(evaluation_spec, dimensions)
  _validate_visualizations(evaluation_spec, row_metrics, dimensions)


def evaluate(
  evaluation_spec: dict[str, Any],
  catalogs: dict[str, Any],
  source_rows: list[dict[str, Any]],
  result_set_hash: str,
  completeness: dict[str, Any] | None = None,
  pipeline_version: str = EVALUATION_PIPELINE_VERSION,
) -> dict[str, Any]:
  validate_evaluation_spec(evaluation_spec, catalogs)
  definitions = _catalog_by_id(catalogs["metricCatalog"], "metrics", "metricId")
  rows = [_adapt_row(item, definitions) for item in source_rows]
  row_formulas = _formula_definitions(evaluation_spec, "row_formula_v1")
  numeric_names = {
    metric_id for metric_id, definition in definitions.items()
    if definition["dataType"] in {"decimal", "integer"}
  }
  compiled_row_formulas = _compile_formulas(row_formulas, numeric_names)
  rows = [_apply_formulas(row, row_formulas, compiled_row_formulas) for row in rows]
  row_definitions = {**definitions, **row_formulas}
  comparison_ids = [
    *[item["metricId"] for item in evaluation_spec["filters"]],
  ]
  _validate_comparability(rows, list(dict.fromkeys(comparison_ids)))
  filtered = [
    row for row in rows
    if _passes_filters(row, evaluation_spec["filters"], row_definitions)
  ]
  visualizations = _build_visualizations(
    evaluation_spec, catalogs, filtered, row_definitions,
  )
  if _uses_group_output(evaluation_spec):
    output_rows, output_definitions = _group_pipeline(
      evaluation_spec, filtered, row_definitions,
    )
  else:
    output_rows = filtered
    output_definitions = row_definitions
  sort_ids = [item["metricId"] for item in evaluation_spec["sort"]]
  _validate_comparability(output_rows, sort_ids)
  ordered = sorted(output_rows, key=cmp_to_key(lambda left, right: _compare_rows(
    left, right, evaluation_spec["sort"], output_definitions,
  )))
  return _output_snapshot(
    evaluation_spec, catalogs, ordered, len(rows), result_set_hash,
    completeness or complete_snapshot(len(rows)), visualizations,
    pipeline_version,
  )


def stable_result_hash(value: Any) -> str:
  encoded = json.dumps(
    value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
  )
  return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def output_hash(output: dict[str, Any]) -> str:
  return digest(output)


def _first_stage_spec(experiment_spec: dict[str, Any]) -> dict[str, Any]:
  plan = dict(experiment_spec.get("plan") or {})
  stages = list(plan.get("stages") or [])
  return dict(stages[0]) if stages and isinstance(stages[0], dict) else {}


def _parameter_values(stage_spec: dict[str, Any], input_id: str) -> list[Any]:
  for item in list(stage_spec.get("parameterGrid") or []):
    if isinstance(item, dict) and item.get("inputId") == input_id:
      return list(item.get("values") or [])
  return []


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
  ordered_values: list[Any] | None = None,
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
    "orderedValues": ordered_values,
  }


def _formula_definitions(
  evaluation_spec: dict[str, Any],
  formula_type: str,
) -> dict[str, dict[str, Any]]:
  return {
    str(item["metricId"]): {
      **item,
      "dataType": "decimal",
      "grain": "group" if formula_type == "group_formula_v1" else "trial_execution",
      "nullSemantics": "null_propagates_except_coalesce",
    }
    for item in evaluation_spec["derivedMetrics"]
    if item["type"] == formula_type
  }


def _validate_unique_metric_ids(*collections: dict[str, Any]) -> None:
  seen: set[str] = set()
  for collection in collections:
    overlap = seen.intersection(collection)
    if overlap:
      raise ExperimentError(
        "experiment.evaluation_spec_invalid",
        f"评价指标 ID 冲突: {sorted(overlap)[0]}",
        422,
      )
    seen.update(collection)


def _validate_formulas(
  definitions: dict[str, dict[str, Any]],
  available_names: set[str],
  *,
  allow_prior: bool,
) -> None:
  for definition in definitions.values():
    SafeFormula(str(definition["formula"]), available_names)
    if allow_prior:
      available_names.add(str(definition["metricId"]))


def _validate_aggregates(
  aggregates: list[dict[str, Any]],
  row_metrics: dict[str, dict[str, Any]],
) -> None:
  for item in aggregates:
    source = row_metrics.get(item["metricId"])
    if source is None:
      raise _unknown_metric(item["metricId"])
    if source["dataType"] not in {"decimal", "integer"}:
      raise ExperimentError(
        "experiment.evaluation_spec_invalid",
        f"聚合指标必须是数值: {item['metricId']}",
        422,
      )


def _uses_group_output(evaluation_spec: dict[str, Any]) -> bool:
  return bool(
    evaluation_spec["groupBy"]
    or evaluation_spec["aggregates"]
    or any(
      item["type"] == "group_formula_v1"
      for item in evaluation_spec["derivedMetrics"]
    )
    or evaluation_spec["groupFilters"]
  )


def _validate_completeness_dimensions(
  evaluation_spec: dict[str, Any],
  dimensions: dict[str, dict[str, Any]],
) -> None:
  supported = {
    "dateScenarioId", "dateRole", "dateStart", "dateEnd", "initialCapital",
    "costScenarioId", "slippage", "executionScenarioId",
  }
  for item in evaluation_spec["completeness"]["requiredDimensionCoverage"]:
    dimension_id = item["dimensionId"]
    if dimension_id not in dimensions:
      raise _unknown_field(dimension_id)
    if dimension_id not in supported and not dimension_id.startswith("parameter."):
      raise ExperimentError(
        "experiment.evaluation_spec_invalid",
        f"维度 {dimension_id} 不支持覆盖率门禁",
        422,
      )


def _validate_visualizations(
  evaluation_spec: dict[str, Any],
  row_metrics: dict[str, dict[str, Any]],
  dimensions: dict[str, dict[str, Any]],
) -> None:
  for item in evaluation_spec["visualizations"]:
    if item["metricId"] not in row_metrics:
      raise _unknown_metric(item["metricId"])
    if row_metrics[item["metricId"]]["dataType"] not in {"decimal", "integer"}:
      raise ExperimentError(
        "experiment.evaluation_spec_invalid", "图表指标必须是数值", 422,
      )
    _validate_visualization_dimensions(item, dimensions)


def _validate_visualization_dimensions(
  item: dict[str, Any],
  dimensions: dict[str, dict[str, Any]],
) -> None:
  dimension_ids: list[str] = []
  if item["type"] == "parameter_curve_v1":
    dimension_ids = [item["parameterDimensionId"], *item["seriesBy"]]
  elif item["type"] == "parameter_heatmap_v1":
    dimension_ids = [item["xDimensionId"], item["yDimensionId"], *item["facetBy"]]
  for dimension_id in dimension_ids:
    definition = dimensions.get(dimension_id)
    if definition is None:
      raise _unknown_field(dimension_id)
  if item["type"] in {"parameter_curve_v1", "parameter_heatmap_v1"}:
    axes = (
      [item["parameterDimensionId"]]
      if item["type"] == "parameter_curve_v1"
      else [item["xDimensionId"], item["yDimensionId"]]
    )
    if any(
      not axis.startswith("parameter.")
      or dimensions[axis]["dataType"] not in {"integer", "number", "decimal"}
      for axis in axes
    ):
      raise ExperimentError(
        "experiment.evaluation_spec_invalid",
        "参数曲线和热力图只支持有序数值策略参数",
        422,
      )


def _compile_formulas(
  definitions: dict[str, dict[str, Any]],
  available_names: set[str],
  *,
  allow_prior: bool = True,
) -> dict[str, SafeFormula]:
  result: dict[str, SafeFormula] = {}
  for metric_id, definition in definitions.items():
    result[metric_id] = SafeFormula(str(definition["formula"]), available_names)
    if allow_prior:
      available_names.add(metric_id)
  return result


def _apply_formulas(
  row: EvaluationRow | GroupRow,
  definitions: dict[str, dict[str, Any]],
  formulas: dict[str, SafeFormula],
) -> EvaluationRow | GroupRow:
  metrics = dict(row.metrics)
  values = {
    metric_id: value.value if isinstance(value.value, (Decimal, int)) else None
    for metric_id, value in metrics.items()
  }
  for metric_id, definition in definitions.items():
    formula = formulas[metric_id]
    value = formula.evaluate(values, int(definition["precision"]))
    domains = sorted({
      metrics[name].comparability_key
      for name in formula.names
      if metrics[name].comparability_key
    })
    metrics[metric_id] = MetricValue(
      value,
      digest(with_version({"formula": definition["formula"], "domains": domains})),
      "formula_input_null" if value is None else None,
    )
    values[metric_id] = value
  if isinstance(row, GroupRow):
    return GroupRow(row.identity, row.dimensions, metrics, row.member_count)
  return EvaluationRow(row.identity, row.dimensions, metrics)


def _group_pipeline(
  evaluation_spec: dict[str, Any],
  rows: list[EvaluationRow],
  row_definitions: dict[str, dict[str, Any]],
) -> tuple[list[GroupRow], dict[str, dict[str, Any]]]:
  grouped = _group_rows(rows, evaluation_spec["groupBy"])
  aggregate_definitions = {
    item["aggregateId"]: {
      **item,
      "dataType": "integer" if item["function"] == "count" else "decimal",
      "grain": "group",
    }
    for item in evaluation_spec["aggregates"]
  }
  result = [
    _aggregate_group(
      key, members, evaluation_spec["groupBy"],
      evaluation_spec["aggregates"], row_definitions,
    )
    for key, members in grouped
  ]
  group_formulas = _formula_definitions(evaluation_spec, "group_formula_v1")
  compiled = _compile_formulas(
    group_formulas, set(aggregate_definitions), allow_prior=False,
  )
  result = [
    _apply_formulas(row, group_formulas, compiled)
    for row in result
  ]
  definitions = {**aggregate_definitions, **group_formulas}
  filtered = [
    row for row in result
    if _passes_filters(row, evaluation_spec["groupFilters"], definitions)
  ]
  return filtered, definitions


def _group_rows(
  rows: list[EvaluationRow],
  group_by: list[str],
) -> list[tuple[tuple[Any, ...], list[EvaluationRow]]]:
  grouped: dict[str, tuple[tuple[Any, ...], list[EvaluationRow]]] = {}
  for row in rows:
    key = tuple(row.dimensions.get(item) for item in group_by)
    encoded = json.dumps(key, ensure_ascii=False, sort_keys=True, default=str)
    grouped.setdefault(encoded, (key, []))[1].append(row)
  return [grouped[key] for key in sorted(grouped)]


def _aggregate_group(
  key: tuple[Any, ...],
  members: list[EvaluationRow],
  group_by: list[str],
  aggregates: list[dict[str, Any]],
  row_definitions: dict[str, dict[str, Any]],
) -> GroupRow:
  dimensions = dict(zip(group_by, key, strict=True))
  metrics: dict[str, MetricValue] = {}
  for item in aggregates:
    source_values = [row.metrics[item["metricId"]] for row in members]
    domains = {
      value.comparability_key
      for value in source_values
      if value.value is not None and value.comparability_key
    }
    if len(domains) > 1:
      raise ExperimentError(
        "experiment.metric_not_comparable",
        f"指标 {item['metricId']} 存在多个不可直接聚合的口径",
        409,
        details={"metricId": item["metricId"], "domainCount": len(domains)},
      )
    value = aggregate_values(
      [
        metric.value if isinstance(metric.value, (Decimal, int)) else None
        for metric in source_values
      ],
      item["function"],
      item.get("percentile"),
    )
    metrics[item["aggregateId"]] = MetricValue(
      value,
      next(iter(domains), None),
      "all_values_null" if value is None else None,
    )
  first = min(members, key=lambda row: (
    row.identity["stageIndex"], row.identity["trialIndex"], row.identity["trialId"],
  ))
  return GroupRow(
    identity={
      "groupKey": digest(with_version({"dimensions": dimensions})),
      "stageIndex": first.identity["stageIndex"],
      "trialIndex": first.identity["trialIndex"],
      "trialId": first.identity["trialId"],
    },
    dimensions=dimensions,
    metrics=metrics,
    member_count=len(members),
  )


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
      "candidateKey": source.get("candidate_key"),
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
    "dateScenarioOrder": source.get("date_scenario_order"),
    "dateRole": date.get("role"),
    "dateStart": date.get("startDate"),
    "dateEnd": date.get("endDate"),
    "adjustmentMode": source.get("adjustment_mode"),
    "datasetContentDigest": source.get("dataset_content_digest"),
    "initialCapital": assignment.get("initialCapital"),
    "costScenarioId": cost.get("scenarioId"),
    "slippage": cost.get("slippage"),
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
    return MetricValue(
      None,
      _comparability_key(source, result, definition),
      "metric_missing",
    )
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
  left_key = (
    left["stageIndex"], left["trialIndex"], left["trialId"],
    left.get("groupKey", ""),
  )
  right_key = (
    right["stageIndex"], right["trialIndex"], right["trialId"],
    right.get("groupKey", ""),
  )
  return (left_key > right_key) - (left_key < right_key)


def _output_snapshot(
  evaluation_spec: dict[str, Any],
  catalogs: dict[str, Any],
  rows: list[EvaluationRow] | list[GroupRow],
  source_count: int,
  result_set_hash: str,
  completeness: dict[str, Any],
  visualizations: list[dict[str, Any]],
  pipeline_version: str,
) -> dict[str, Any]:
  if pipeline_version == LEGACY_EVALUATION_PIPELINE_VERSION:
    return _legacy_output_snapshot(
      evaluation_spec, rows, source_count, result_set_hash, pipeline_version,
    )
  is_partial = bool(completeness.get("isPartial"))
  return with_version({
    "schemaVersion": "advanced_evaluation_output_v1",
    "pipelineVersion": pipeline_version,
    "statisticsVersion": STATISTICS_VERSION,
    "resultSetHash": result_set_hash,
    "sourceRowCount": source_count,
    "matchedRowCount": len(rows),
    "isPartial": is_partial,
    "reportStatus": "provisional" if is_partial else "complete",
    "candidateSelectionEligible": not is_partial,
    "finalTestEligible": False,
    "datasetNotice": (
      "development、validation 和 reference 仅是冻结的展示与分组标签；"
      "对全部候选运行过的 reference 不是最终 test。"
    ),
    "completeness": completeness,
    "columns": evaluation_spec["columns"],
    "sort": evaluation_spec["sort"],
    "groupBy": evaluation_spec["groupBy"],
    "rows": [
      {
        **row.identity,
        **({"memberCount": row.member_count} if isinstance(row, GroupRow) else {}),
        "values": {
          field_id: _json_value(
            row.metrics[field_id].value if field_id in row.metrics
            else row.dimensions.get(field_id)
          )
          for field_id in evaluation_spec["columns"]
        },
        "unsupportedReasons": {
          field_id: row.metrics[field_id].unsupported_reason
          for field_id in evaluation_spec["columns"]
          if field_id in row.metrics and row.metrics[field_id].unsupported_reason
        },
      }
      for row in rows
    ],
    "visualizations": visualizations,
  })


def _legacy_output_snapshot(
  evaluation_spec: dict[str, Any],
  rows: list[EvaluationRow] | list[GroupRow],
  source_count: int,
  result_set_hash: str,
  pipeline_version: str,
) -> dict[str, Any]:
  return with_version({
    "schemaVersion": "basic_evaluation_output_v1",
    "pipelineVersion": pipeline_version,
    "resultSetHash": result_set_hash,
    "sourceRowCount": source_count,
    "matchedRowCount": len(rows),
    "columns": evaluation_spec["columns"],
    "sort": evaluation_spec["sort"],
    "rows": [{
      **{
        key: value for key, value in row.identity.items()
        if key != "candidateKey"
      },
      "values": {
        field_id: _json_value(
          row.metrics[field_id].value if field_id in row.metrics
          else row.dimensions.get(field_id)
        )
        for field_id in evaluation_spec["columns"]
      },
      "unsupportedReasons": {
        field_id: row.metrics[field_id].unsupported_reason
        for field_id in evaluation_spec["columns"]
        if field_id in row.metrics and row.metrics[field_id].unsupported_reason
      },
    } for row in rows],
  })


def complete_snapshot(row_count: int) -> dict[str, Any]:
  return {
    "plannedTrialCount": row_count,
    "generatedTrialCount": row_count,
    "succeededCount": row_count,
    "failedCount": 0,
    "cancelledCount": 0,
    "missingCount": 0,
    "completionRate": "1.0000",
    "isPartial": False,
    "dimensionCoverage": [],
    "exceptions": [],
  }


def _build_visualizations(
  evaluation_spec: dict[str, Any],
  catalogs: dict[str, Any],
  rows: list[EvaluationRow],
  definitions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
  dimension_definitions = _catalog_by_id(
    catalogs["dimensionCatalog"], "dimensions", "dimensionId",
  )
  result: list[dict[str, Any]] = []
  for item in evaluation_spec["visualizations"]:
    visualization_type = item["type"]
    if visualization_type == "parameter_curve_v1":
      result.append(_parameter_curve(item, rows, dimension_definitions))
    elif visualization_type == "parameter_heatmap_v1":
      result.append(_parameter_heatmap(item, rows, dimension_definitions))
    elif visualization_type == "cost_stress_v1":
      result.append(_cost_stress(item, rows, definitions[item["metricId"]]))
    elif visualization_type == "time_stability_v1":
      result.append(_time_stability(item, rows))
  return result


def _parameter_curve(
  item: dict[str, Any],
  rows: list[EvaluationRow],
  dimensions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
  axis = item["parameterDimensionId"]
  series_by = list(item["seriesBy"])
  _require_analysis_facets(rows, {axis, *series_by})
  grouped = _group_by_dimensions(rows, series_by)
  series: list[dict[str, Any]] = []
  for key, members in grouped:
    by_x = sorted(
      _group_by_dimensions(members, [axis]),
      key=lambda entry: _ordered_value_key(
        entry[0][0], dimensions[axis].get("orderedValues") or [],
      ),
    )
    points = []
    for index, (x_key, x_rows) in enumerate(by_x):
      neighbors = by_x[max(0, index - 1):index + 2]
      neighbor_rows = [row for _, grouped_rows in neighbors for row in grouped_rows]
      points.append({
        "x": x_key[0],
        **_json_statistics(_metric_statistics(x_rows, item["metricId"])),
        "localNeighborhood": {
          "parameterValues": [entry[0][0] for entry in neighbors],
          **_json_statistics(_metric_statistics(neighbor_rows, item["metricId"])),
        },
        "runIds": [row.identity["runId"] for row in x_rows],
      })
    series.append({
      "seriesKey": digest(with_version({"dimensions": dict(zip(series_by, key, strict=True))})),
      "dimensions": dict(zip(series_by, key, strict=True)),
      "points": points,
    })
  return {
    "type": item["type"],
    "metricId": item["metricId"],
    "parameterDimensionId": axis,
    "interpolation": "none",
    "evidence": "actual_discrete_trials_only",
    "series": series,
  }


def _parameter_heatmap(
  item: dict[str, Any],
  rows: list[EvaluationRow],
  dimensions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
  x_axis = item["xDimensionId"]
  y_axis = item["yDimensionId"]
  facet_by = list(item["facetBy"])
  _require_analysis_facets(rows, {x_axis, y_axis, *facet_by})
  facets: list[dict[str, Any]] = []
  for key, members in _group_by_dimensions(rows, facet_by):
    grouped_cells = _group_by_dimensions(members, [x_axis, y_axis])
    x_values = dimensions[x_axis].get("orderedValues") or []
    y_values = dimensions[y_axis].get("orderedValues") or []
    cells = []
    for cell_key, cell_rows in grouped_cells:
      neighbors = [
        (other_key, other_rows)
        for other_key, other_rows in grouped_cells
        if _is_adjacent(cell_key[0], other_key[0], x_values)
        and _is_adjacent(cell_key[1], other_key[1], y_values)
      ]
      neighbor_rows = [row for _, grouped_rows in neighbors for row in grouped_rows]
      cells.append({
        "x": cell_key[0],
        "y": cell_key[1],
        **_json_statistics(_metric_statistics(cell_rows, item["metricId"])),
        "localNeighborhood": {
          "actualCells": [
            {"x": neighbor_key[0], "y": neighbor_key[1]}
            for neighbor_key, _ in neighbors
          ],
          **_json_statistics(_metric_statistics(neighbor_rows, item["metricId"])),
        },
        "runIds": [row.identity["runId"] for row in cell_rows],
      })
    cells.sort(key=lambda cell: (
      _ordered_value_key(cell["y"], y_values),
      _ordered_value_key(cell["x"], x_values),
    ))
    facets.append({
      "facetKey": digest(with_version({"dimensions": dict(zip(facet_by, key, strict=True))})),
      "dimensions": dict(zip(facet_by, key, strict=True)),
      "cells": cells,
    })
  return {
    "type": item["type"],
    "metricId": item["metricId"],
    "xDimensionId": x_axis,
    "yDimensionId": y_axis,
    "interpolation": "none",
    "evidence": "actual_discrete_trials_only",
    "facets": facets,
  }


def _require_analysis_facets(
  rows: list[EvaluationRow],
  represented_dimensions: set[str],
) -> None:
  analysis_dimensions = {
    "dateScenarioId", "initialCapital", "costScenarioId", "executionScenarioId",
    *_parameter_dimension_ids(rows),
  }
  varying = {
    dimension_id for dimension_id in analysis_dimensions
    if len({_stable_key(row.dimensions.get(dimension_id)) for row in rows}) > 1
  }
  missing = sorted(varying - represented_dimensions)
  if missing:
    raise ExperimentError(
      "experiment.evaluation_visualization_ambiguous",
      "参数图存在未固定或未分组的额外实验维度",
      422,
      details={"missingDimensions": missing},
    )


def _parameter_dimension_ids(rows: list[EvaluationRow]) -> set[str]:
  return {
    dimension_id
    for row in rows
    for dimension_id in row.dimensions
    if dimension_id.startswith("parameter.")
  }


def _group_by_dimensions(
  rows: list[EvaluationRow],
  dimensions: list[str],
) -> list[tuple[tuple[Any, ...], list[EvaluationRow]]]:
  grouped: dict[str, tuple[tuple[Any, ...], list[EvaluationRow]]] = {}
  for row in rows:
    key = tuple(row.dimensions.get(item) for item in dimensions)
    encoded = _stable_key(key)
    grouped.setdefault(encoded, (key, []))[1].append(row)
  return [grouped[key] for key in sorted(grouped)]


def _metric_statistics(rows: list[EvaluationRow], metric_id: str) -> dict[str, Any]:
  domains = {
    row.metrics[metric_id].comparability_key
    for row in rows
    if row.metrics[metric_id].comparability_key
  }
  if len(domains) > 1:
    raise ExperimentError(
      "experiment.metric_not_comparable",
      f"指标 {metric_id} 存在多个不可直接聚合的口径",
      409,
      details={"metricId": metric_id, "domainCount": len(domains)},
    )
  values = [
    row.metrics[metric_id].value
    if isinstance(row.metrics[metric_id].value, (Decimal, int)) else None
    for row in rows
  ]
  return local_statistics(values)


def _json_statistics(statistics: dict[str, Any]) -> dict[str, Any]:
  return {key: _json_value(value) for key, value in statistics.items()}


def _ordered_value_key(value: Any, ordered_values: list[Any]) -> tuple[int, Any]:
  encoded_values = [_stable_key(item) for item in ordered_values]
  encoded = _stable_key(value)
  if encoded in encoded_values:
    return (0, encoded_values.index(encoded))
  try:
    return (1, Decimal(str(value)))
  except (InvalidOperation, TypeError, ValueError):
    return (2, str(value))


def _is_adjacent(value: Any, other: Any, ordered_values: list[Any]) -> bool:
  encoded_values = [_stable_key(item) for item in ordered_values]
  value_key = _stable_key(value)
  other_key = _stable_key(other)
  if value_key not in encoded_values or other_key not in encoded_values:
    return value_key == other_key
  return abs(encoded_values.index(value_key) - encoded_values.index(other_key)) <= 1


def _stable_key(value: Any) -> str:
  return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _cost_stress(
  item: dict[str, Any],
  rows: list[EvaluationRow],
  definition: dict[str, Any],
) -> dict[str, Any]:
  baseline_id = item["baselineScenarioId"]
  candidate_dimensions = sorted({
    dimension_id
    for row in rows
    for dimension_id in row.dimensions
    if dimension_id not in {"costScenarioId", "slippage", "resultStatus"}
  })
  comparisons: list[dict[str, Any]] = []
  for key, members in _group_by_dimensions(rows, candidate_dimensions):
    baseline_rows = [
      row for row in members
      if row.dimensions.get("costScenarioId") == baseline_id
    ]
    if not baseline_rows:
      continue
    baseline = _single_metric_value(baseline_rows, item["metricId"])
    baseline_domain = baseline_rows[0].metrics[item["metricId"]].comparability_key
    for row in members:
      scenario_id = row.dimensions.get("costScenarioId")
      if scenario_id == baseline_id:
        continue
      pressure_domain = row.metrics[item["metricId"]].comparability_key
      if (
        baseline_domain is not None
        and pressure_domain is not None
        and baseline_domain != pressure_domain
      ):
        raise ExperimentError(
          "experiment.metric_not_comparable",
          f"指标 {item['metricId']} 的成本场景口径不可比较",
          409,
        )
      value = row.metrics[item["metricId"]].value
      numeric = value if isinstance(value, (Decimal, int)) else None
      delta = _subtract_nullable(numeric, baseline)
      relative = _relative_change(delta, baseline)
      direction = str(definition.get("direction") or "neutral")
      degradation = (
        -relative if relative is not None and direction == "higher"
        else relative if relative is not None and direction == "lower"
        else None
      )
      threshold = _optional_decimal(item.get("degradationThreshold"))
      comparisons.append({
        "candidateDimensions": dict(zip(candidate_dimensions, key, strict=True)),
        "baselineScenarioId": baseline_id,
        "pressureScenarioId": scenario_id,
        "baselineSlippage": baseline_rows[0].dimensions.get("slippage"),
        "pressureSlippage": row.dimensions.get("slippage"),
        "baselineValue": _json_value(baseline),
        "pressureValue": _json_value(numeric),
        "delta": _json_value(delta),
        "relativeChange": _json_value(relative),
        "degradation": _json_value(degradation),
        "metricDirection": direction,
        "crossedThreshold": (
          degradation is not None
          and threshold is not None
          and degradation >= threshold
        ),
        "baselineRunId": baseline_rows[0].identity["runId"],
        "pressureRunId": row.identity["runId"],
      })
  return {
    "type": item["type"],
    "metricId": item["metricId"],
    "source": "independent_slippage_trials",
    "displayOnlyAdjustment": False,
    "comparisons": comparisons,
  }


def _time_stability(
  item: dict[str, Any],
  rows: list[EvaluationRow],
) -> dict[str, Any]:
  grouped: dict[str, list[EvaluationRow]] = {}
  for row in rows:
    candidate_key = str(row.identity.get("candidateKey") or "")
    grouped.setdefault(candidate_key, []).append(row)
  candidates: list[dict[str, Any]] = []
  for candidate_key in sorted(grouped):
    members = grouped[candidate_key]
    values = [{
      "dateScenarioId": row.dimensions.get("dateScenarioId"),
      "dateScenarioOrder": row.dimensions.get("dateScenarioOrder"),
      "role": row.dimensions.get("dateRole"),
      "startDate": row.dimensions.get("dateStart"),
      "endDate": row.dimensions.get("dateEnd"),
      "adjustmentMode": row.dimensions.get("adjustmentMode"),
      "datasetContentDigest": row.dimensions.get("datasetContentDigest"),
      "value": _json_value(row.metrics[item["metricId"]].value),
      "runId": row.identity["runId"],
    } for row in members]
    values.sort(key=lambda value: (
      value["dateScenarioOrder"] if value["dateScenarioOrder"] is not None else 10**9,
      str(value["dateScenarioId"]),
    ))
    candidates.append({
      "candidateKey": candidate_key,
      "values": values,
      "comparisonMode": "side_by_side_frozen_scenarios",
    })
  return {
    "type": item["type"],
    "metricId": item["metricId"],
    "finalTestEligible": False,
    "referenceSemantics": "reference_is_not_final_test",
    "candidates": candidates,
  }


def _single_metric_value(
  rows: list[EvaluationRow],
  metric_id: str,
) -> Decimal | int | None:
  values = [
    row.metrics[metric_id].value
    for row in rows
    if isinstance(row.metrics[metric_id].value, (Decimal, int))
  ]
  if len(values) > 1:
    raise ExperimentError(
      "experiment.evaluation_visualization_ambiguous",
      "成本压力基线存在多个未分组结果",
      422,
    )
  return values[0] if values else None


def _subtract_nullable(
  left: Decimal | int | None,
  right: Decimal | int | None,
) -> Decimal | None:
  if left is None or right is None:
    return None
  return Decimal(left) - Decimal(right)


def _relative_change(
  delta: Decimal | None,
  baseline: Decimal | int | None,
) -> Decimal | None:
  if delta is None or baseline is None or Decimal(baseline) == 0:
    return None
  return delta / abs(Decimal(baseline))


def _optional_decimal(value: Any) -> Decimal | None:
  if value is None:
    return None
  return _decimal(value, "degradationThreshold")


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
