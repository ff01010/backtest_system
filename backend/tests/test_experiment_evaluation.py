from __future__ import annotations

from datetime import datetime, timezone
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
  catalog_snapshot,
  evaluate,
  output_hash,
  stable_result_hash,
  validate_evaluation_spec,
)


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


def test_advanced_evaluation_fields_remain_outside_ticket_nine() -> None:
  request = _evaluation_spec(groupBy=["dateScenarioId"])
  with pytest.raises(ValidationError):
    EvaluationSpecRequest.model_validate(request)


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
