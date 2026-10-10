from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from backend import app as app_module
from backend.experiments.contracts import EvaluationSpecRequest
from backend.experiments.errors import ExperimentError
from backend.experiments.evaluation import (
  DIMENSION_CATALOG_VERSION,
  EVALUATION_PIPELINE_VERSION,
  METRIC_CATALOG_VERSION,
  LEGACY_EVALUATION_PIPELINE_VERSION,
  catalog_snapshot,
  evaluate,
  output_hash,
  stable_result_hash,
  validate_evaluation_spec,
)
from backend.experiments.evaluation_formula import SafeFormula
from backend.experiments.evaluation_repository import (
  _build_completeness_snapshot,
  _enforce_completeness_policy,
)
from backend.experiments.evaluation_statistics import aggregate_values


ROOT = Path(__file__).resolve().parents[2]


def _experiment_spec() -> dict[str, Any]:
  return {
    "runTemplateSnapshot": {
      "templateType": "legacy_single_strategy_v1",
      "runPath": "strategy",
      "scopeType": "single",
      "fixedStructure": {"fillModel": "bar_next_open"},
      "tunableInputCatalog": {
        "inputs": [{
          "inputId": "strategyParameters.lookback",
          "sourceName": "lookback",
          "type": "integer",
          "group": "strategyParameters",
        }],
      },
    },
  }


def _evaluation_spec(**overrides: Any) -> dict[str, Any]:
  value = {
    "schemaVersion": "evaluation_spec_v1",
    "name": "基础评价",
    "source": {
      "experimentRevisionId": "10000000-0000-4000-8000-000000000001",
      "stageIds": ["20000000-0000-4000-8000-000000000001"],
      "executionSelection": "selected_execution_at_run_creation",
    },
    "catalogs": {
      "metricCatalogVersion": METRIC_CATALOG_VERSION,
      "dimensionCatalogVersion": DIMENSION_CATALOG_VERSION,
      "normalizerVersion": None,
    },
    "completeness": {
      "allowPartial": False,
      "minimumCompletionRate": "1.0000",
      "requiredDimensionCoverage": [],
    },
    "columns": ["parameter.strategyParameters.lookback", "totalReturn", "annualSharpe"],
    "derivedMetrics": [],
    "filters": [],
    "groupBy": [],
    "aggregates": [],
    "groupFilters": [],
    "sort": [{
      "type": "metric_sort_v1",
      "metricId": "annualSharpe",
      "direction": "desc",
    }],
    "visualizations": [],
    "reportSections": [],
  }
  value.update(overrides)
  return value


def _source(
  trial_index: int,
  total_return: float,
  sharpe: float | None,
  *,
  capital: str = "1000000",
) -> dict[str, Any]:
  result = {
    "resultSchemaVersion": 3,
    "summary": {
      "totalReturn": total_return,
      "annualSharpe": sharpe,
      "completedTradeCount": 10,
    },
    "benchmarkSymbol": "000300.SH",
  }
  return {
    "execution_id": f"30000000-0000-4000-8000-{trial_index:012d}",
    "run_id": f"40000000-0000-4000-8000-{trial_index:012d}",
    "trial_id": f"50000000-0000-4000-8000-{trial_index:012d}",
    "stage_id": "20000000-0000-4000-8000-000000000001",
    "stage_index": 0,
    "stage_type": "finite_grid_v1",
    "trial_index": trial_index,
    "result_schema_version": "3",
    "result_json": result,
    "expected_result_hash": stable_result_hash(result),
    "actual_data_hash": "d" * 64,
    "dimension_projection": {
      "strategyParameters": {"lookback": 10 + trial_index},
      "dateScenario": {
        "scenarioId": "development",
        "role": "development",
        "startDate": "2025-01-01",
        "endDate": "2025-03-31",
      },
      "initialCapital": capital,
      "costScenario": {"scenarioId": "base"},
      "executionScenario": {"scenarioId": "immediate"},
    },
    "parameter_dimensions": {"strategyParameters.lookback": "lookback"},
    "requested_params": {
      "benchmarkSymbol": "000300.SH",
      "strategyCategory": "directional_holding",
    },
    "fill_model": "bar_next_open",
    "run_path": "strategy",
    "scope_type": "single",
    "run_status": "succeeded",
    "queued_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    "started_at": datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    "finished_at": datetime(2025, 1, 1, 0, 0, 3, tzinfo=timezone.utc),
  }


def test_catalogs_publish_stable_metric_and_dynamic_dimension_ids() -> None:
  catalogs = catalog_snapshot(_experiment_spec())
  metrics = {item["metricId"]: item for item in catalogs["metricCatalog"]["metrics"]}
  dimensions = {
    item["dimensionId"]: item
    for item in catalogs["dimensionCatalog"]["dimensions"]
  }

  assert catalogs["metricCatalog"]["version"] == METRIC_CATALOG_VERSION
  assert catalogs["dimensionCatalog"]["version"] == DIMENSION_CATALOG_VERSION
  assert metrics["totalReturn"]["unit"] == "ratio"
  assert metrics["totalReturn"]["grain"] == "trial_execution"
  assert metrics["totalReturn"]["supportedRunPaths"] == ["strategy"]
  assert metrics["totalReturn"]["supportedResultSchemaVersions"] == ["2", "3"]
  assert metrics["maxDrawdownRate"]["direction"] == "lower"
  assert metrics["annualSharpe"]["nullSemantics"] == "unsupported_or_missing_is_null"
  assert metrics["sortino"]["annualization"] == "trading_days_252"
  assert "parameter.strategyParameters.lookback" in dimensions


def test_basic_evaluation_sorts_deterministically_and_puts_null_last() -> None:
  catalogs = catalog_snapshot(_experiment_spec())
  sources = [
    _source(2, 0.2, None),
    _source(1, 0.1, 1.5),
    _source(0, 0.3, 1.5),
  ]

  first = evaluate(_evaluation_spec(), catalogs, sources, "a" * 64)
  second = evaluate(_evaluation_spec(), catalogs, list(reversed(sources)), "a" * 64)

  assert first == second
  assert first["pipelineVersion"] == EVALUATION_PIPELINE_VERSION
  assert [row["trialIndex"] for row in first["rows"]] == [0, 1, 2]
  assert first["rows"][2]["values"]["annualSharpe"] is None
  assert first["rows"][2]["unsupportedReasons"]["annualSharpe"] == "metric_missing"
  assert output_hash(first) == output_hash(second)


def test_legacy_pipeline_keeps_basic_output_schema_for_queued_runs() -> None:
  output = evaluate(
    _evaluation_spec(),
    catalog_snapshot(_experiment_spec()),
    [_source(0, 0.1, 1.0)],
    "9" * 64,
    pipeline_version=LEGACY_EVALUATION_PIPELINE_VERSION,
  )

  assert output["schemaVersion"] == "basic_evaluation_output_v1"
  assert output["pipelineVersion"] == LEGACY_EVALUATION_PIPELINE_VERSION
  assert "completeness" not in output
  assert "candidateKey" not in output["rows"][0]


def test_null_metric_never_passes_normal_filter_or_becomes_zero() -> None:
  spec = _evaluation_spec(filters=[{
    "type": "metric_comparison_v1",
    "metricId": "annualSharpe",
    "operator": ">=",
    "value": "0",
  }])
  output = evaluate(
    spec,
    catalog_snapshot(_experiment_spec()),
    [_source(0, 0.1, None), _source(1, 0.2, 0.0)],
    "b" * 64,
  )

  assert output["matchedRowCount"] == 1
  assert output["rows"][0]["trialIndex"] == 1


def test_cross_domain_sort_is_rejected_without_normalizer() -> None:
  with pytest.raises(ExperimentError, match="多个不可直接比较") as raised:
    evaluate(
      _evaluation_spec(),
      catalog_snapshot(_experiment_spec()),
      [_source(0, 0.1, 1.0), _source(1, 0.2, 2.0, capital="2000000")],
      "c" * 64,
    )

  assert raised.value.code == "experiment.metric_not_comparable"


def test_result_hash_mismatch_fails_before_ranking() -> None:
  source = _source(0, 0.1, 1.0)
  source["expected_result_hash"] = "f" * 64

  with pytest.raises(ExperimentError) as raised:
    evaluate(
      _evaluation_spec(), catalog_snapshot(_experiment_spec()), [source], "d" * 64,
    )

  assert raised.value.code == "experiment.evaluation_result_hash_mismatch"


def test_unknown_catalog_ids_and_union_types_are_rejected() -> None:
  catalogs = catalog_snapshot(_experiment_spec())
  invalid = _evaluation_spec(columns=["runnerTemporaryField"])
  with pytest.raises(ExperimentError) as raised:
    validate_evaluation_spec(invalid, catalogs)
  assert raised.value.code == "experiment.evaluation_field_unknown"

  request = _evaluation_spec(filters=[{
    "type": "unknown_filter_v9",
    "metricId": "totalReturn",
    "operator": ">=",
    "value": "0",
  }])
  with pytest.raises(ValidationError):
    EvaluationSpecRequest.model_validate(request)


def test_safe_formula_rejects_attacks_depth_and_division_by_zero() -> None:
  for expression in (
    "__import__('os').system('whoami')",
    "annualSharpe.__class__",
    "annualSharpe[0]",
    "[value for value in annualSharpe]",
  ):
    with pytest.raises(ExperimentError) as raised:
      SafeFormula(expression, {"annualSharpe"})
    assert raised.value.code == "experiment.evaluation_formula_invalid"

  nested = "annualSharpe"
  for _ in range(21):
    nested = f"abs({nested})"
  with pytest.raises(ExperimentError, match="嵌套深度"):
    SafeFormula(nested, {"annualSharpe"})

  formula = SafeFormula("annualSharpe / totalReturn", {"annualSharpe", "totalReturn"})
  with pytest.raises(ExperimentError) as divided:
    formula.evaluate({"annualSharpe": Decimal("1"), "totalReturn": Decimal("0")}, 4)
  assert divided.value.code == "experiment.evaluation_formula_division_by_zero"

  non_numeric = _evaluation_spec(derivedMetrics=[{
    "type": "row_formula_v1",
    "metricId": "badStatusScore",
    "displayName": "非法状态评分",
    "formula": "resultStatus + 1",
    "precision": 4,
  }])
  with pytest.raises(ExperimentError) as invalid_metric:
    validate_evaluation_spec(non_numeric, catalog_snapshot(_experiment_spec()))
  assert invalid_metric.value.code == "experiment.evaluation_formula_invalid"


def test_formula_filter_group_aggregate_and_group_formula_use_frozen_order() -> None:
  spec = _evaluation_spec(
    columns=["dateScenarioId", "meanScore", "groupScore"],
    derivedMetrics=[
      {
        "type": "row_formula_v1",
        "metricId": "customScore",
        "displayName": "自定义评分",
        "formula": "coalesce(annualSharpe, 0) + totalReturn",
        "precision": 4,
      },
      {
        "type": "group_formula_v1",
        "metricId": "groupScore",
        "displayName": "分组评分",
        "formula": "meanScore * 2",
        "precision": 4,
      },
    ],
    filters=[{
      "type": "metric_comparison_v1",
      "metricId": "customScore",
      "operator": ">",
      "value": "0.15",
    }],
    groupBy=["dateScenarioId"],
    aggregates=[{
      "type": "metric_aggregate_v1",
      "aggregateId": "meanScore",
      "metricId": "customScore",
      "function": "mean",
      "percentile": None,
    }],
    sort=[{
      "type": "metric_sort_v1",
      "metricId": "groupScore",
      "direction": "desc",
    }],
  )
  spec = EvaluationSpecRequest.model_validate(spec).model_dump(mode="json")

  output = evaluate(
    spec,
    catalog_snapshot(_experiment_spec()),
    [_source(0, 0.1, None), _source(1, 0.2, 1.0)],
    "f" * 64,
  )

  assert output["sourceRowCount"] == 2
  assert output["matchedRowCount"] == 1
  assert output["rows"][0]["memberCount"] == 1
  assert output["rows"][0]["members"] == [{
    "trialId": "50000000-0000-4000-8000-000000000001",
    "trialExecutionId": "30000000-0000-4000-8000-000000000001",
    "runId": "40000000-0000-4000-8000-000000000001",
    "stageId": "20000000-0000-4000-8000-000000000001",
    "stageIndex": 0,
    "trialIndex": 1,
    "resultHash": output["rows"][0]["members"][0]["resultHash"],
    "startDate": "2025-01-01",
  }]
  assert output["rows"][0]["values"] == {
    "dateScenarioId": "development",
    "meanScore": "1.2000",
    "groupScore": "2.4000",
  }


def test_candidate_rate_distribution_normalizer_aggregates_frozen_scenarios() -> None:
  first = _source(0, 0.1, 1.0, capital="1000000")
  second = _source(1, 0.3, 2.0, capital="2000000")
  first["dimension_projection"]["strategyParameters"]["lookback"] = 20
  second["dimension_projection"]["strategyParameters"]["lookback"] = 20
  second["dimension_projection"]["dateScenario"] = {
    "scenarioId": "validation",
    "role": "validation",
    "startDate": "2025-04-01",
    "endDate": "2025-06-30",
  }
  spec = _evaluation_spec(
    catalogs={
      "metricCatalogVersion": METRIC_CATALOG_VERSION,
      "dimensionCatalogVersion": DIMENSION_CATALOG_VERSION,
      "normalizerVersion": "candidate_rate_distribution_v1",
    },
    columns=["parameter.strategyParameters.lookback", "medianSharpe"],
    groupBy=["parameter.strategyParameters.lookback"],
    aggregates=[{
      "type": "metric_aggregate_v1",
      "aggregateId": "medianSharpe",
      "metricId": "annualSharpe",
      "function": "median",
      "percentile": None,
    }],
    sort=[{
      "type": "metric_sort_v1",
      "metricId": "medianSharpe",
      "direction": "desc",
    }],
  )
  normalized = EvaluationSpecRequest.model_validate(spec).model_dump(mode="json")

  output = evaluate(
    normalized, catalog_snapshot(_experiment_spec()), [first, second], "8" * 64,
  )

  assert output["rows"][0]["memberCount"] == 2
  assert output["rows"][0]["values"]["medianSharpe"] == "1.5"


def test_candidate_rate_distribution_normalizer_does_not_merge_amount_metrics() -> None:
  first = _source(0, 0.1, 1.0, capital="1000000")
  second = _source(1, 0.3, 2.0, capital="2000000")
  first["dimension_projection"]["strategyParameters"]["lookback"] = 20
  second["dimension_projection"]["strategyParameters"]["lookback"] = 20
  first["result_json"]["summary"]["totalPnl"] = 100
  second["result_json"]["summary"]["totalPnl"] = 200
  first["expected_result_hash"] = stable_result_hash(first["result_json"])
  second["expected_result_hash"] = stable_result_hash(second["result_json"])
  spec = _evaluation_spec(
    catalogs={
      "metricCatalogVersion": METRIC_CATALOG_VERSION,
      "dimensionCatalogVersion": DIMENSION_CATALOG_VERSION,
      "normalizerVersion": "candidate_rate_distribution_v1",
    },
    columns=["meanPnl"],
    groupBy=["parameter.strategyParameters.lookback"],
    aggregates=[{
      "type": "metric_aggregate_v1",
      "aggregateId": "meanPnl",
      "metricId": "totalPnl",
      "function": "mean",
      "percentile": None,
    }],
    sort=[{"type": "metric_sort_v1", "metricId": "meanPnl", "direction": "desc"}],
  )
  normalized = EvaluationSpecRequest.model_validate(spec).model_dump(mode="json")

  with pytest.raises(ExperimentError) as raised:
    evaluate(
      normalized, catalog_snapshot(_experiment_spec()), [first, second], "7" * 64,
    )

  assert raised.value.code == "experiment.metric_not_comparable"


def test_statistical_boundaries_use_sample_stddev_and_nearest_rank() -> None:
  values = [Decimal("1"), Decimal("2"), Decimal("4"), None]

  assert aggregate_values(values, "count") == 3
  assert aggregate_values(values, "median") == Decimal("2")
  assert aggregate_values([Decimal("1"), Decimal("3")], "median") == Decimal("2")
  assert aggregate_values([Decimal("1")], "sample_stddev") is None
  assert aggregate_values(values, "nearest_rank_percentile", "0.5") == Decimal("2")
  assert aggregate_values([None], "mean") is None


def test_parameter_curve_uses_only_actual_discrete_points() -> None:
  spec = _evaluation_spec(
    visualizations=[{
      "type": "parameter_curve_v1",
      "parameterDimensionId": "parameter.strategyParameters.lookback",
      "metricId": "totalReturn",
      "seriesBy": [],
    }],
  )
  output = evaluate(
    spec,
    catalog_snapshot(_experiment_spec()),
    [_source(0, 0.1, 1.0), _source(2, 0.3, 1.0)],
    "a" * 64,
  )

  curve = output["visualizations"][0]
  assert curve["interpolation"] == "none"
  assert [point["x"] for point in curve["series"][0]["points"]] == [10, 12]
  assert curve["series"][0]["points"][0]["median"] == "0.1"
  assert curve["series"][0]["points"][0]["localNeighborhood"]["parameterValues"] == [10, 12]
  assert curve["series"][0]["points"][0]["localNeighborhood"]["range"] == "0.2"


def test_reference_dataset_is_never_claimed_as_final_test() -> None:
  source = _source(0, 0.1, 1.0)
  source["dimension_projection"]["dateScenario"]["role"] = "reference"
  source["candidate_key"] = "candidate-1"
  source["date_scenario_order"] = 2
  source["adjustment_mode"] = "forward"
  source["dataset_content_digest"] = "c" * 64
  spec = _evaluation_spec(visualizations=[{
    "type": "time_stability_v1",
    "metricId": "totalReturn",
  }])

  output = evaluate(spec, catalog_snapshot(_experiment_spec()), [source], "b" * 64)

  assert output["finalTestEligible"] is False
  assert "不代表最终测试" in output["datasetNotice"]
  assert output["visualizations"][0]["referenceSemantics"] == "reference_is_not_final_test"
  candidate = output["visualizations"][0]["candidates"][0]
  assert candidate["dimensions"]["parameter.strategyParameters.lookback"] == 10
  assert candidate["dimensions"]["initialCapital"] == "1000000"
  frozen = candidate["values"][0]
  assert frozen["dateScenarioOrder"] == 2
  assert frozen["adjustmentMode"] == "forward"
  assert frozen["datasetContentDigest"] == "c" * 64


def test_cost_stress_compares_independent_slippage_trial_results() -> None:
  base = _source(0, 0.2, 1.0)
  pressure = _source(1, 0.1, 0.8)
  pressure["dimension_projection"]["strategyParameters"]["lookback"] = 10
  base["dimension_projection"]["costScenario"] = {
    "scenarioId": "base", "slippage": "0",
  }
  pressure["dimension_projection"]["costScenario"] = {
    "scenarioId": "stress", "slippage": "0.001",
  }
  spec = _evaluation_spec(visualizations=[{
    "type": "cost_stress_v1",
    "metricId": "totalReturn",
    "baselineScenarioId": "base",
    "degradationThreshold": "0.25",
  }])

  output = evaluate(
    spec, catalog_snapshot(_experiment_spec()), [base, pressure], "c" * 64,
  )

  stress = output["visualizations"][0]
  assert stress["source"] == "independent_slippage_trials"
  assert stress["displayOnlyAdjustment"] is False
  assert stress["comparisons"][0]["delta"] == "-0.1"
  assert stress["comparisons"][0]["degradation"] == "0.5"
  assert stress["comparisons"][0]["crossedThreshold"] is True
  assert stress["comparisons"][0]["baselineRunId"] == base["run_id"]
  assert stress["comparisons"][0]["pressureRunId"] == pressure["run_id"]


def test_cost_stress_respects_lower_is_better_metric_direction() -> None:
  base = _source(0, 0.2, 1.0)
  pressure = _source(1, 0.1, 0.8)
  pressure["dimension_projection"]["strategyParameters"]["lookback"] = 10
  base["dimension_projection"]["costScenario"] = {
    "scenarioId": "base", "slippage": "0",
  }
  pressure["dimension_projection"]["costScenario"] = {
    "scenarioId": "stress", "slippage": "0.001",
  }
  base["result_json"]["summary"]["maxDrawdownRate"] = 0.1
  pressure["result_json"]["summary"]["maxDrawdownRate"] = 0.15
  base["expected_result_hash"] = stable_result_hash(base["result_json"])
  pressure["expected_result_hash"] = stable_result_hash(pressure["result_json"])
  spec = _evaluation_spec(visualizations=[{
    "type": "cost_stress_v1",
    "metricId": "maxDrawdownRate",
    "baselineScenarioId": "base",
    "degradationThreshold": "0.4",
  }])

  output = evaluate(
    spec, catalog_snapshot(_experiment_spec()), [base, pressure], "c" * 64,
  )

  comparison = output["visualizations"][0]["comparisons"][0]
  assert comparison["metricDirection"] == "lower"
  assert comparison["relativeChange"] == "0.5"
  assert comparison["degradation"] == "0.5"
  assert comparison["crossedThreshold"] is True


def test_grouped_output_rejects_unlisted_dimension_column() -> None:
  spec = _evaluation_spec(
    columns=["dateScenarioId", "meanReturn"],
    groupBy=["dateRole"],
    aggregates=[{
      "type": "metric_aggregate_v1",
      "aggregateId": "meanReturn",
      "metricId": "totalReturn",
      "function": "mean",
      "percentile": None,
    }],
    sort=[],
  )

  with pytest.raises(ExperimentError) as raised:
    validate_evaluation_spec(spec, catalog_snapshot(_experiment_spec()))

  assert raised.value.code == "experiment.evaluation_spec_invalid"


def test_heatmap_rejects_unrepresented_third_parameter_dimension() -> None:
  experiment_spec = _experiment_spec()
  inputs = experiment_spec["runTemplateSnapshot"]["tunableInputCatalog"]["inputs"]
  inputs.extend([
    {
      "inputId": "strategyParameters.threshold",
      "sourceName": "threshold",
      "type": "number",
      "group": "strategyParameters",
    },
    {
      "inputId": "strategyParameters.window",
      "sourceName": "window",
      "type": "integer",
      "group": "strategyParameters",
    },
  ])
  experiment_spec["plan"] = {"stages": [{
    "parameterGrid": [
      {"inputId": "strategyParameters.lookback", "values": [10, 20]},
      {"inputId": "strategyParameters.threshold", "values": ["0.1", "0.2"]},
      {"inputId": "strategyParameters.window", "values": [5, 8]},
    ],
  }]}
  sources = [_source(0, 0.1, 1.0), _source(1, 0.2, 1.1)]
  for index, source in enumerate(sources):
    source["dimension_projection"]["strategyParameters"].update({
      "threshold": "0.1", "window": 5 + index * 3,
    })
    source["parameter_dimensions"].update({
      "strategyParameters.threshold": "threshold",
      "strategyParameters.window": "window",
    })
  spec = _evaluation_spec(visualizations=[{
    "type": "parameter_heatmap_v1",
    "xDimensionId": "parameter.strategyParameters.lookback",
    "yDimensionId": "parameter.strategyParameters.threshold",
    "metricId": "totalReturn",
    "facetBy": [],
  }])

  with pytest.raises(ExperimentError) as raised:
    evaluate(spec, catalog_snapshot(experiment_spec), sources, "d" * 64)

  assert raised.value.code == "experiment.evaluation_visualization_ambiguous"
  assert raised.value.details == {
    "missingDimensions": ["parameter.strategyParameters.window"],
  }


def test_partial_snapshot_reports_all_terminal_and_missing_states() -> None:
  stage = {
    "stage_id": "stage-1",
    "stage_index": 0,
    "stage_status": "partially_completed",
    "stage_spec": {
      "dateScenarios": [
        {"scenarioId": "development", "role": "development"},
        {"scenarioId": "validation", "role": "validation"},
      ],
    },
    "planned_trial_count": 4,
    "cancelled_before_generation": 1,
  }
  succeeded = {
    **stage,
    "trial_id": "trial-0",
    "trial_index": 0,
    "summary_status": "succeeded",
    "selected_execution_id": "execution-0",
    "run_status": "succeeded",
    "result_hash": "a" * 64,
    "materialization_status": "materialized",
    "dimension_projection": {"dateScenario": {"scenarioId": "development"}},
    "latest_error_summary": None,
  }
  failed = {
    **stage,
    "trial_id": "trial-1",
    "trial_index": 1,
    "summary_status": "failed",
    "selected_execution_id": None,
    "run_status": "failed",
    "result_hash": None,
    "materialization_status": "materialized",
    "dimension_projection": {"dateScenario": {"scenarioId": "validation"}},
    "latest_error_summary": {"message": "运行失败"},
  }
  spec = _evaluation_spec(completeness={
    "allowPartial": True,
    "minimumCompletionRate": "0.2500",
    "requiredDimensionCoverage": [],
  })

  snapshot = _build_completeness_snapshot([stage], [succeeded, failed], spec)
  _enforce_completeness_policy(snapshot, spec["completeness"])

  assert snapshot["completionRate"] == "0.2500"
  assert snapshot["failedCount"] == 1
  assert snapshot["cancelledCount"] == 1
  assert snapshot["missingCount"] == 1
  assert snapshot["isPartial"] is True
  assert {item["status"] for item in snapshot["exceptions"]} == {"failed", "cancelled", "missing"}

  with pytest.raises(ExperimentError) as gate:
    _enforce_completeness_policy(snapshot, {
      "allowPartial": True,
      "minimumCompletionRate": "0.5000",
      "requiredDimensionCoverage": [],
    })
  assert gate.value.code == "experiment.evaluation_partial_gate_failed"

  coverage_spec = _evaluation_spec(completeness={
    "allowPartial": True,
    "minimumCompletionRate": "0.0000",
    "requiredDimensionCoverage": [{
      "dimensionId": "dateScenarioId",
      "minimumCoverageRate": "0.5000",
    }],
  })
  coverage_snapshot = _build_completeness_snapshot(
    [stage], [succeeded, failed], coverage_spec,
  )
  assert coverage_snapshot["dimensionCoverage"][0]["minimumObservedRate"] == "0.0000"
  with pytest.raises(ExperimentError) as coverage_gate:
    _enforce_completeness_policy(
      coverage_snapshot, coverage_spec["completeness"],
    )
  assert coverage_gate.value.code == "experiment.evaluation_dimension_coverage_failed"


def test_evaluation_migration_freezes_result_set_and_enforces_rls() -> None:
  migration = (
    ROOT / "db" / "migrations" / "047_backtest_experiment_evaluation.sql"
  ).read_text(encoding="utf-8")

  assert "CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_spec" in migration
  assert "CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_run" in migration
  assert "result_set_hash char(64) NOT NULL" in migration
  assert "execution_set jsonb NOT NULL" in migration
  assert "UNIQUE (revision_id, spec_hash)" in migration
  assert migration.count("ENABLE ROW LEVEL SECURITY") == 2
  assert "backtest_evaluation_run_owner" in migration
  assert 'TO :"app_db_user";' in migration
  assert "TO backtest_app;" not in migration

  advanced_migration = (
    ROOT / "db" / "migrations" / "049_experiment_advanced_evaluation.sql"
  ).read_text(encoding="utf-8")
  assert "completeness_snapshot jsonb NOT NULL" in advanced_migration
  assert "CHECK (execution_count >= 0)" in advanced_migration


def test_save_spec_route_requires_and_forwards_idempotency_key(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  payload = EvaluationSpecRequest.model_validate(_evaluation_spec())

  class FakeApplication:
    def save_spec(
      self,
      experiment_id: str,
      _payload: Any,
      key: str,
      request_id: str,
    ) -> tuple[dict[str, Any], int]:
      assert experiment_id == "60000000-0000-4000-8000-000000000001"
      assert key == "save-evaluation-spec-1"
      assert request_id == "req-evaluation"
      return {"evaluationSpecId": "spec-1", "replayed": False}, 201

  monkeypatch.setattr(app_module, "_evaluation_application", FakeApplication())
  request = Request({
    "type": "http",
    "method": "POST",
    "path": "/api/backtest/experiments/test/evaluation-specs",
    "headers": [(b"idempotency-key", b"save-evaluation-spec-1")],
  })
  request.state.request_id = "req-evaluation"

  response = app_module.save_experiment_evaluation_spec(
    UUID("60000000-0000-4000-8000-000000000001"), payload, request,
  )

  assert response.status_code == 201
