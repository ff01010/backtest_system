from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Any

import pytest

from backend import settings
from backend.experiments.application import ExperimentApplication
from backend.experiments.canonicalization import canonical_json, digest, with_version
from backend.experiments.contracts import ExperimentPreviewRequest, SubjectRef
from backend.experiments.errors import ExperimentError
from backend.experiments.generators import FiniteGridGenerator
from backend.experiments.generators import seed_bundle
from backend.experiments import generators
from backend.experiments.repository import PostgresPreviewRepository, validate_preview_bindings
from backend.experiments.resolvers import LegacySingleStrategyResolver, ResolvedRunTemplate
from backend.services import state_store


class StaticResolver:
  def __init__(self, template: ResolvedRunTemplate) -> None:
    self.template = template
    self.calls = 0

  def resolve(self, _subject_ref: object) -> ResolvedRunTemplate:
    self.calls += 1
    return self.template


class StaticDatasetResolver:
  def __init__(self, version: str = "v1") -> None:
    self.version = version

  def resolve(
    self,
    _template: ResolvedRunTemplate,
    scenarios: tuple[object, ...],
  ) -> tuple[dict[str, Any], ...]:
    return tuple(
      with_version({
        "datasetSnapshotSchemaVersion": "dataset_snapshot_v1",
        "scenario": scenario.model_dump(),  # type: ignore[attr-defined]
        "formalDateCount": 2,
        "warmupDateCount": 1,
        "contentDigest": digest({"index": index, "version": self.version}),
      })
      for index, scenario in enumerate(scenarios)
    )


class CapturingRepository:
  def __init__(self) -> None:
    self.previews: list[dict[str, Any]] = []

  def save_preview(self, preview: dict[str, Any]) -> None:
    self.previews.append(preview)


def _template() -> ResolvedRunTemplate:
  fixed = {
    "configId": "config-1",
    "configVersionId": "version-1",
    "strategyName": "均值回归",
    "scriptId": "script-1",
    "strategyVersionId": "script-version-1",
    "scriptCodeDigest": "a" * 64,
    "runtimeVersion": "runtime-v1",
    "runtimeCapabilityDigest": "b" * 64,
    "symbol": "688981.SH",
    "benchmarkSymbol": "688981.SH",
    "timeframe": "5m",
    "mode": "backtest",
    "account": {"accountId": None, "positionMode": "full_available"},
    "strategy": "user_script",
    "strategyCategory": "directional_holding",
    "holdingPolicy": "overnight",
    "overlaySettlementPolicy": "intraday_restore",
    "fillModel": "bar_next_open",
    "allowLong": True,
    "allowShort": True,
    "orderCash": None,
    "orderPct": None,
    "initialPositionEnabled": False,
    "baseQuantity": 0,
    "baseCostPrice": 0,
    "t0Enabled": False,
    "warmupTradingDays": 1,
    "signalDatasetId": None,
    "feeProfile": {
      "profileId": "default_a_share",
      "version": "fee-v1",
      "rates": {"commissionRate": "0.00008"},
      "contentDigest": "c" * 64,
    },
  }
  inputs = (
    {
      "inputId": "strategyParameters.window",
      "sourceName": "window",
      "type": "integer",
      "defaultValue": 20,
      "overridePolicy": "finite_candidates",
    },
    {
      "inputId": "strategyParameters.threshold",
      "sourceName": "threshold",
      "type": "number",
      "defaultValue": "1.5",
      "overridePolicy": "finite_candidates",
    },
  )
  snapshot = with_version({
    "templateType": "legacy_single_strategy_v1",
    "templateSchemaVersion": "run_template_v1",
    "fixedStructure": fixed,
    "tunableInputCatalog": {"inputs": list(inputs)},
    "assetManifestSnapshot": {"assets": []},
    "contracts": {"trialCompiler": "legacy_single_bar_v1"},
  })
  return ResolvedRunTemplate(
    snapshot=snapshot,
    digest=digest(snapshot),
    tunable_inputs=inputs,
    defaults={
      "strategyParameters": {"window": 20, "threshold": "1.5"},
      "dateScenario": {
        "scenarioId": "development-1",
        "name": "默认区间",
        "role": "development",
        "startDate": "2026-07-01",
        "endDate": "2026-07-02",
      },
      "initialCapital": "100000",
      "slippage": "0",
    },
  )


def _request(**stage_changes: Any) -> ExperimentPreviewRequest:
  stage = {
    "stageType": "finite_grid_v1",
    "generatorVersion": "1",
    "parameterGrid": [
      {"inputId": "strategyParameters.window", "values": [10, "10", 20]},
      {"inputId": "strategyParameters.threshold", "values": ["1.50", "2.0"]},
    ],
    "dateScenarios": [{
      "scenarioId": "development-1",
      "name": "开发集",
      "role": "development",
      "startDate": "2026-07-01",
      "endDate": "2026-07-02",
    }],
    "capitalScenarios": ["100000.00", "200000"],
    "costScenarios": [{"scenarioId": "zero", "slippage": "-0.000"}],
    "executionScenarios": [{
      "scenarioId": "immediate",
      "algorithm": "immediate",
      "parameters": {},
    }],
    **stage_changes,
  }
  return ExperimentPreviewRequest.model_validate({
    "schemaVersion": "experiment_create_v1",
    "name": "  网格预览  ",
    "description": "固定向量",
    "subjectRef": {
      "subjectType": "legacy_single_strategy",
      "subjectId": "config-1",
      "subjectVersion": "version-1",
    },
    "plan": {"planVersion": "experiment_plan_v1", "stages": [stage]},
  })


def _enable_preview(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_VALUES_PER_DIMENSION", 50)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_TRIALS", 1000)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_MAX_ESTIMATED_BARS", 10_000_000)
  monkeypatch.setattr(settings, "EXPERIMENT_PREVIEW_TTL_SECONDS", 900)


def test_canonical_json_fixed_vector_normalizes_unicode_decimal_and_negative_zero() -> None:
  decomposed = "e\u0301"
  value = {"z": Decimal("-0.000"), "a": decomposed, "items": [Decimal("1.500"), 2]}

  assert canonical_json(value) == '{"a":"é","items":["1.5",2],"z":"0"}'
  assert digest(value) == "1392849afe1f66c03cb627f14045ec241c8f85436cf2209cc2ef65f974fae1d6"
  assert digest(value) == digest({"items": ["1.5", 2], "a": "é", "z": "0"})
  assert canonical_json({"set": {"b", "a"}, "ordered": ["b", "a"]}) == (
    '{"ordered":["b","a"],"set":["a","b"]}'
  )


def test_generator_deduplicates_values_and_uses_stable_cartesian_order() -> None:
  grid = FiniteGridGenerator().normalize(_request().plan.stages[0], _template())

  assert grid.planned_trial_count == 8
  assert grid.dimension_counts == {
    "strategyParameters.threshold": 2,
    "strategyParameters.window": 2,
    "dateScenario": 1,
    "initialCapital": 2,
    "costScenario": 1,
    "executionScenario": 1,
  }
  assert grid.dimension_normalization["strategyParameters.window"] == {
    "submitted": 3,
    "normalized": 2,
  }
  assert grid.assignment_at(0)["strategyParameters"] == {"threshold": "1.5", "window": 10}
  assert grid.assignment_at(1)["initialCapital"] == "200000"
  assert grid.assignment_at(2)["strategyParameters"] == {"threshold": "1.5", "window": 20}
  assert grid.trial_fingerprint(0, grid.assignment_at(0)) == grid.trial_fingerprint(
    0, grid.assignment_at(0),
  )
  assert grid.stage_input_fingerprint == (
    "0b6b3a5bd3c1b4718ca22e929547a3f5c598cb8f20d5dc55c6b4c3fc6e9614e0"
  )
  assert grid.trial_fingerprint(0, grid.assignment_at(0)) == (
    "e1b289840d031323ee82dcf0b09e8f5f7c998f1926e9e15233ad3e5fd69d388c"
  )


def test_subject_resolver_requires_exact_published_version_and_normalizes_defaults(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  code = "def on_bar(bar, state):\n  return []\n"
  config = {
    "id": "config-1",
    "published": True,
    "enabled": True,
    "archivedAt": None,
    "publishedVersionId": "version-1",
  }
  version = {
    "id": "version-1",
    "name": "均值回归",
    "mode": "backtest",
    "symbol": "688981.SH",
    "benchmarkSymbol": None,
    "timeframe": "5m",
    "strategy": "user_script",
    "accountId": None,
    "paramsJson": {
      "params": {
        "fillModel": "bar_next_open",
        "startDate": "2026-07-01",
        "endDate": "2026-07-02",
        "initialCapital": 100000.0,
        "slippage": -0.0,
        "baseCostPrice": 0.0,
        "window": 20,
        "threshold": 1.5,
      },
      "strategyParams": {"window": 20, "threshold": 1.5},
      "scriptCodeHash": hashlib.sha256(code.encode("utf-8")).hexdigest(),
      "strategyParameterDefinitions": {
        "window": {"type": "integer", "default": 20},
        "threshold": {"type": "number", "default": 1.5},
      },
        "scriptSnapshot": {
          "id": "script-1",
          "versionId": "script-version-1",
          "name": "均值回归",
          "description": "",
          "code": code,
          "entryType": "on_bar",
          "signalRequired": False,
          "runtimeVersion": "runtime-v1",
        "capabilityDigest": "b" * 64,
      },
    },
  }
  monkeypatch.setattr(state_store, "get_strategy_config", lambda *_args, **_kwargs: config)
  monkeypatch.setattr(
    state_store,
    "get_strategy_config_version_for_config",
    lambda *_args: version,
  )
  subject_ref = SubjectRef(
    subjectType="legacy_single_strategy",
    subjectId="config-1",
    subjectVersion="version-1",
  )

  template = LegacySingleStrategyResolver().resolve(subject_ref)

  assert template.defaults["strategyParameters"] == {"window": 20, "threshold": "1.5"}
  assert template.defaults["initialCapital"] == "100000"
  assert template.defaults["slippage"] == "0"
  assert template.snapshot["fixedStructure"]["strategyName"] == "均值回归"

  config["publishedVersionId"] = "version-2"
  with pytest.raises(ExperimentError, match="不可用于") as stale:
    LegacySingleStrategyResolver().resolve(subject_ref)
  assert stale.value.status_code == 403


@pytest.mark.parametrize(
  ("stage_changes", "message"),
  [
    ({"parameterGrid": [{"inputId": "strategyParameters.unknown", "values": [1]}]}, "未声明"),
    ({"parameterGrid": [{"inputId": "strategyParameters.threshold", "values": ["NaN"]}]}, "有限值"),
    ({"capitalScenarios": [100000.0]}, "十进制定点字符串"),
    ({"costScenarios": [{"scenarioId": "bad", "slippage": "1"}]}, "必须小于 1"),
    ({"executionScenarios": [{"scenarioId": "twap", "algorithm": "twap", "parameters": {}}]}, "immediate"),
  ],
)
def test_generator_rejects_invalid_dimensions(
  stage_changes: dict[str, Any],
  message: str,
) -> None:
  with pytest.raises(ExperimentError, match=message):
    FiniteGridGenerator().normalize(_request(**stage_changes).plan.stages[0], _template())


def test_generator_enforces_catalog_range_constraints() -> None:
  template = _template()
  bounded_inputs = tuple(
    {**item, "min": "15", "max": "30"}
    if item["inputId"] == "strategyParameters.window" else item
    for item in template.tunable_inputs
  )
  bounded_template = replace(template, tunable_inputs=bounded_inputs)
  stage = _request(parameterGrid=[{
    "inputId": "strategyParameters.window",
    "values": [10, 20],
  }]).plan.stages[0]

  with pytest.raises(ExperimentError, match="小于声明的最小值"):
    FiniteGridGenerator().normalize(stage, bounded_template)


def test_generator_rejects_trial_count_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(generators, "MAX_PLANNED_TRIAL_COUNT", 4)

  with pytest.raises(ExperimentError, match="组合数量溢出"):
    FiniteGridGenerator().normalize(_request().plan.stages[0], _template())


def test_preview_is_reproducible_but_issues_a_fresh_owner_bound_token(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _enable_preview(monkeypatch)
  repository = CapturingRepository()
  application = ExperimentApplication(
    resolver=StaticResolver(_template()),
    dataset_resolver=StaticDatasetResolver(),
    repository=repository,
    clock=lambda: datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc),
  )

  first = application.preview(_request())
  second = application.preview(_request())

  assert first["specHash"] == second["specHash"]
  assert first["specHash"] == "58c35bc96a893a7e74ad8c25f2c6cc515b9e06a58438aae4170bd3dd4b394b11"
  assert first["plannedTrialCount"] == 8
  assert first["examples"] == second["examples"]
  assert first["examples"][0]["trialFingerprint"] == (
    "bd80d39a9cd9190fb6841f9d02c1a4428e10f65b58c19cb7bdec1225b9a42c39"
  )
  assert first["previewToken"] != second["previewToken"]
  assert first["expiresAt"].isoformat() == "2026-09-29T00:15:00+00:00"
  assert len(repository.previews) == 2
  assert all(item["previewSummary"]["specHash"] == first["specHash"] for item in repository.previews)


def test_preview_reports_limits_without_silently_truncating(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _enable_preview(monkeypatch)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_TRIALS", 4)
  repository = CapturingRepository()
  application = ExperimentApplication(
    resolver=StaticResolver(_template()),
    dataset_resolver=StaticDatasetResolver(),
    repository=repository,
  )

  result = application.preview(_request())

  assert result["plannedTrialCount"] == 8
  assert result["limitDecision"]["allowed"] is False
  assert result["limitDecision"]["blockingReasons"][0]["code"] == "trial_limit_exceeded"
  assert [item["trialIndex"] for item in result["examples"]] == [0, 1, 2, 5, 6, 7]


def test_dataset_snapshot_drift_changes_spec_and_trial_identity(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _enable_preview(monkeypatch)
  first = ExperimentApplication(
    resolver=StaticResolver(_template()),
    dataset_resolver=StaticDatasetResolver("release-1"),
    repository=CapturingRepository(),
  ).preview(_request())
  second = ExperimentApplication(
    resolver=StaticResolver(_template()),
    dataset_resolver=StaticDatasetResolver("release-2"),
    repository=CapturingRepository(),
  ).preview(_request())

  assert first["datasetSnapshotDigest"] != second["datasetSnapshotDigest"]
  assert first["runTemplateDigest"] != second["runTemplateDigest"]
  assert first["specHash"] != second["specHash"]
  assert first["examples"][0]["trialFingerprint"] != second["examples"][0]["trialFingerprint"]
  assert first["examples"][0]["compiledRequestDigest"] != second["examples"][0]["compiledRequestDigest"]


def test_preview_rejects_date_range_and_untrusted_trial_fields() -> None:
  payload = _request().model_dump()
  payload["plan"]["stages"][0]["dateScenarios"][0]["startDate"] = "2026-07-03"
  payload["plan"]["stages"][0]["plannedTrialCount"] = 1

  with pytest.raises(ValueError, match="开始日期|extra_forbidden"):
    ExperimentPreviewRequest.model_validate(payload)


def test_seed_bundle_uses_stable_unsigned_64_bit_derivation() -> None:
  assert seed_bundle("abc") == {
    "derivationVersion": "trial-seed-v1",
    "baseSeed": 435075266352232356,
  }
  assert seed_bundle("abc") == seed_bundle("abc")


def test_preview_binding_rejects_expiry_and_dataset_drift() -> None:
  now = datetime(2026, 9, 29, tzinfo=timezone.utc)
  row = {
    "expires_at": datetime(2026, 9, 29, 0, 15, tzinfo=timezone.utc),
    "spec_hash": "spec",
    "run_template_digest": "template",
    "dataset_snapshot_digest": "dataset-v1",
    "capability_digest": "capability",
    "budget_digest": "budget",
  }
  expected = {
    "specHash": "spec",
    "runTemplateDigest": "template",
    "datasetSnapshotDigest": "dataset-v1",
    "capabilityDigest": "capability",
    "budgetDigest": "budget",
  }

  validate_preview_bindings(row, expected, now)
  with pytest.raises(ExperimentError, match="运行环境已变化") as drift:
    validate_preview_bindings(row, {**expected, "datasetSnapshotDigest": "dataset-v2"}, now)
  assert drift.value.code == "experiment.preview_stale"
  assert drift.value.details == {"changedBindings": ["datasetSnapshotDigest"]}

  with pytest.raises(ExperimentError, match="已过期") as expired:
    validate_preview_bindings(row, expected, row["expires_at"])
  assert expired.value.status_code == 410


def test_preview_token_lookup_is_scoped_to_current_owner(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  executed: list[tuple[str, tuple[object, ...]]] = []

  class Cursor:
    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *_args: object) -> None:
      return None

    def execute(self, query: str, params: tuple[object, ...]) -> None:
      executed.append((query, params))

    def fetchone(self) -> None:
      return None

  class Connection:
    def cursor(self) -> Cursor:
      return Cursor()

  @contextmanager
  def database_session() -> Any:
    yield Connection()

  monkeypatch.setattr(state_store, "database_session", database_session)
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-a")

  assert PostgresPreviewRepository().load_preview("tampered-token") is None
  assert executed[0][1] == ("tampered-token", "user-a")
  assert "owner_user_id=%s" in executed[0][0]


def test_preview_migration_enforces_owner_isolation_and_ttl_columns() -> None:
  migration = Path("db/migrations/044_backtest_experiment_preview.sql").read_text(encoding="utf-8")

  assert "owner_user_id=app.current_user_id()" in migration
  assert "FORCE ROW LEVEL SECURITY" in migration
  assert "expires_at timestamptz NOT NULL" in migration
