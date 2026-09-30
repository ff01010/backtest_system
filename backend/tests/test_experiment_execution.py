from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from backend import schemas, settings
from backend.experiments.canonicalization import digest, with_version
from backend.experiments.compiler import LegacySingleBarTrialCompiler
from backend.experiments.coordinator import ExperimentCoordinator
from backend.experiments.errors import ExperimentError
from backend.experiments import repository as experiment_repository
from backend.experiments.repository import (
  _budget_capacity_gate,
  _decode_cursor,
  _encode_cursor,
  _manual_capacity_gate,
)
from backend.experiments.recovery import (
  AggregateFacts,
  allows_automatic_retry,
  default_failure_policy,
  derive_aggregate_status,
  normalize_failure,
  should_open_circuit,
  validate_status_transition,
)
from backend.experiments.resolvers import ResolvedRunTemplate
from backend.services import state_store
from backend.strategies.runtime_contract import (
  RUNTIME_CAPABILITY_DIGEST,
  SCRIPT_RUNTIME_VERSION,
)


def _execution_template() -> ResolvedRunTemplate:
  code = "def on_bar(bar, state):\n  return []\n"
  code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
  fixed = {
    "configId": None,
    "configVersionId": None,
    "strategyName": "冻结策略",
    "scriptId": None,
    "strategyVersionId": None,
    "scriptCodeDigest": code_hash,
    "signalRequired": False,
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "runtimeCapabilityDigest": RUNTIME_CAPABILITY_DIGEST,
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
    "baseCostPrice": "0",
    "t0Enabled": False,
    "warmupTradingDays": 1,
    "signalDatasetId": None,
    "feeProfile": {
      "profileId": "default_a_share",
      "version": "fee-v1",
      "rates": {
        "commissionRate": "0.00008",
        "minimumCommission": "5",
      },
      "contentDigest": "c" * 64,
    },
  }
  inputs = ({
    "inputId": "strategyParameters.window",
    "sourceName": "window",
    "type": "integer",
    "group": "strategyParameters",
    "defaultValue": 20,
    "overridePolicy": "finite_candidates",
  },)
  snapshot = with_version({
    "templateType": "legacy_single_strategy_v1",
    "templateSchemaVersion": "run_template_v1",
    "runPath": "strategy",
    "scopeType": "single",
    "fixedStructure": fixed,
    "executionSnapshot": {
      "script": {
        "id": None,
        "versionId": None,
        "name": "冻结策略",
        "description": "",
        "code": code,
        "entryType": "on_bar",
        "signalRequired": False,
        "codeHash": code_hash,
        "runtimeVersion": SCRIPT_RUNTIME_VERSION,
        "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
      },
      "publishedConfig": {
        "name": "冻结策略",
        "strategy": "user_script",
        "mode": "backtest",
        "symbol": "688981.SH",
        "timeframe": "5m",
        "savedStrategyParams": {"window": 20},
      },
    },
    "tunableInputCatalog": {"inputs": list(inputs)},
    "assetManifestSnapshot": {"assets": []},
    "contracts": {"trialCompiler": "legacy_single_bar_v1"},
  })
  return ResolvedRunTemplate(
    snapshot=snapshot,
    digest=digest(snapshot),
    tunable_inputs=inputs,
    defaults={"strategyParameters": {"window": 20}},
  )


def _assignment() -> dict[str, Any]:
  return {
    "strategyParameters": {"window": 30},
    "dateScenario": {
      "scenarioId": "development-1",
      "name": "开发集",
      "role": "development",
      "startDate": "2026-07-01",
      "endDate": "2026-07-02",
    },
    "initialCapital": "100000",
    "costScenario": {"scenarioId": "cost-1", "slippage": "0.01"},
    "executionScenario": {
      "scenarioId": "immediate-1",
      "algorithm": "immediate",
      "parameters": {},
    },
  }


def _dataset() -> dict[str, Any]:
  return {
    "provider": "qmt_kline",
    "symbol": "688981.SH",
    "timeframe": "5m",
    "sourceDatabase": "qmt_history_dev",
    "sourceTable": "qmt_kline_5m_raw",
    "sourcePeriod": "5m",
    "derivationRuleVersion": None,
    "formalDates": ["2026-07-01", "2026-07-02"],
    "warmupDates": ["2026-06-30"],
    "adjustmentMode": "none",
    "releaseMetadataDigest": "d" * 64,
    "contentDigest": "e" * 64,
  }


def test_compiler_builds_frozen_standard_bar_request() -> None:
  compiler = LegacySingleBarTrialCompiler()

  request = compiler.compile_request(
    _execution_template(),
    _assignment(),
    _dataset(),
    {"derivationVersion": "trial-seed-v1", "baseSeed": 42},
  )

  assert request["savedScriptSnapshot"]["code"].startswith("def on_bar")
  assert request["strategyParams"] == {"window": 30}
  assert request["savedStrategyParams"] == {"window": 20}
  assert request["strategyParamOverrides"] == {"window": 30}
  assert request["feeProfile"]["commissionRate"] == 0.00008
  assert request["marketDataSnapshot"]["barSnapshot"]["formalDates"] == [
    "2026-07-01", "2026-07-02",
  ]
  assert request["seedBundle"]["baseSeed"] == 42


def test_compiler_matches_manual_standard_bar_request_contract() -> None:
  compiled = LegacySingleBarTrialCompiler().compile_request(
    _execution_template(),
    _assignment(),
    _dataset(),
    {"derivationVersion": "trial-seed-v1", "baseSeed": 42},
  )
  manual = schemas.BacktestRunRequest(
    mode="backtest",
    symbol="688981.SH",
    benchmarkSymbol="688981.SH",
    strategy="user_script",
    timeframe="5m",
    startDate="2026-07-01",
    endDate="2026-07-02",
    initialCapital=100000,
    commissionRate=0.00008,
    slippage=0.01,
    positionMode="full_available",
    allowLong=True,
    allowShort=True,
    strategyCategory="directional_holding",
    holdingPolicy="overnight",
    overlaySettlementPolicy="intraday_restore",
    fillModel="bar_next_open",
    executionAlgorithm="immediate",
    executionParameters={},
    baseQuantity=0,
    baseCostPrice="0",
    initialPositionEnabled=False,
    t0Enabled=False,
    warmupTradingDays=1,
    strategyParams={"window": 30},
    strategyParamOverrides={"window": 30},
  ).model_dump()

  for field, value in manual.items():
    assert compiled[field] == value


def test_compiler_rejects_tampered_frozen_script() -> None:
  template = _execution_template()
  template.snapshot["executionSnapshot"]["script"]["code"] = "tampered"

  with pytest.raises(ExperimentError, match="摘要不一致"):
    LegacySingleBarTrialCompiler().compile_request(
      template, _assignment(), _dataset(), {"baseSeed": 1},
    )


def test_transactional_job_creation_forwards_dispatch_identity(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  captured: dict[str, Any] = {}

  def create(*args: Any, **kwargs: Any) -> tuple[str, int]:
    captured["args"] = args
    captured["kwargs"] = kwargs
    return "run-1", 1

  monkeypatch.setattr(state_store, "_create_queued_backtest_job", create)
  origin = {
    "type": "experiment",
    "experimentId": "experiment-1",
    "stageId": "stage-1",
    "trialExecutionId": "execution-1",
  }

  result = state_store.create_queued_backtest_job_in_transaction(
    object(),
    {"fillModel": "bar_next_open"},
    "user-1",
    request_key="experiment:execution-1",
    origin=origin,
  )

  assert result == ("run-1", 1)
  assert captured["args"][3] == "bar_backtest_job"
  assert captured["kwargs"] == {
    "request_key": "experiment:execution-1",
    "origin": origin,
  }


def test_coordinator_runs_reconcile_before_bounded_materialization(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls: list[str] = []

  class Repository:
    def reconcile_next_execution(self) -> bool:
      calls.append("reconcile")
      return True

    def claim_generation_stage(self, *_args: Any) -> None:
      calls.append("generation")
      return None

    def materialize_next_trial(self, _compiler: Any) -> bool:
      calls.append("materialize")
      return True

  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)

  assert ExperimentCoordinator(repository=Repository()).run_once()
  assert calls == ["reconcile", "generation", "materialize"]


def test_coordinator_refreshes_capacity_blocks_before_materialization(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  calls: list[str] = []

  class Repository:
    def reconcile_next_execution(self) -> bool:
      calls.append("reconcile")
      return False

    def claim_generation_stage(self, *_args: Any) -> None:
      calls.append("generation")
      return None

    def repair_next_aggregate(self) -> bool:
      calls.append("repair")
      return False

    def refresh_next_capacity_block(self) -> bool:
      calls.append("refresh")
      return True

    def materialize_next_trial(self, _compiler: Any) -> bool:
      calls.append("materialize")
      return False

  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)

  assert ExperimentCoordinator(repository=Repository()).run_once()
  assert calls == ["reconcile", "repair", "generation", "refresh", "materialize"]


def test_capacity_tightening_blocks_without_changing_frozen_budget(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_TRIALS", 100)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_USER_PLANNED_TRIALS", 100)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_MAX_ESTIMATED_BARS", 100_000)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_USER", 2)
  locked = {
    "revision": {
      "planned_trial_count": 10,
      "resource_budget": {
        "estimate": {"estimatedBarCount": 10_000},
        "capacityPolicy": {
          "maxTrials": 100,
          "maxUserPlannedTrials": 100,
          "maxEstimatedBars": 100_000,
          "maxInFlightPerExperiment": 1,
          "maxInFlightPerUser": 2,
          "schedulerCapacity": 16,
        },
      },
    },
    "quota": {"reserved_planned_trial_count": 10},
  }

  gate = _budget_capacity_gate(locked, effective_capacity=8)

  assert not gate.allowed
  assert gate.code == "experiment.capacity_blocked"
  assert locked["revision"]["resource_budget"]["capacityPolicy"]["schedulerCapacity"] == 16


def test_manual_wait_gate_stops_new_experiment_materialization(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    experiment_repository, "_manual_queue_state", lambda *_args: (1, 61.0),
  )
  monkeypatch.setattr(settings, "EXPERIMENT_MANUAL_MAX_WAIT_SECONDS", 60)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_USER", 3)
  locked = {
    "quota": {
      "owner_user_id": "user-1",
      "manual_reserved_permits": 1,
      "experiment_in_flight_count": 0,
    },
  }

  gate = _manual_capacity_gate(object(), locked)

  assert not gate.allowed
  assert gate.code == "experiment.manual_wait_blocked"


def test_signed_cursor_binds_owner_filters_and_position(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENT_CURSOR_SIGNING_SECRET", "s" * 32)
  cursor = _encode_cursor(
    "trial-list", "user-1", {"experimentId": "e1"}, {"trialIndex": 10},
  )

  assert _decode_cursor(
    cursor, "trial-list", "user-1", {"experimentId": "e1"},
  ) == {"trialIndex": 10}
  with pytest.raises(ExperimentError, match="游标无效"):
    _decode_cursor(cursor, "trial-list", "user-2", {"experimentId": "e1"})


def test_execution_migration_enforces_atomic_identity_and_retention() -> None:
  sql = Path("db/migrations/046_backtest_experiment_execution.sql").read_text(
    encoding="utf-8",
  )

  assert "backtest_experiment_trial_execution" in sql
  assert "backtest_experiment_dispatch_outbox" in sql
  assert "UNIQUE (dispatch_key)" in sql
  assert "UNIQUE (run_id)" in sql
  assert "origin_trial_execution_id" in sql
  assert "DEFERRABLE INITIALLY DEFERRED" in sql
  assert sql.count("FORCE ROW LEVEL SECURITY") == 2


def test_fair_capacity_migration_persists_round_robin_state() -> None:
  sql = Path("db/migrations/048_experiment_fair_capacity.sql").read_text(
    encoding="utf-8",
  )

  assert "materialization_sequence" in sql
  assert "last_materialized_sequence" in sql
  assert "ix_backtest_experiment_stage_fair_materialization" in sql
  assert "IF NOT EXISTS" in sql


@pytest.mark.parametrize(("facts", "expected"), [
  (AggregateFacts(3, 3, 0, 0, 0, 0, 3, 0, 0, False), "completed"),
  (AggregateFacts(3, 3, 0, 0, 0, 0, 1, 2, 0, False), "partially_completed"),
  (AggregateFacts(3, 3, 0, 0, 0, 0, 0, 3, 0, False), "failed"),
  (AggregateFacts(3, 2, 0, 0, 0, 0, 0, 0, 3, True), "cancelled"),
  (AggregateFacts(3, 3, 0, 0, 1, 0, 1, 0, 0, True), "cancelling"),
  (AggregateFacts(3, 3, 1, 0, 0, 0, 1, 0, 1, False), "queued"),
])
def test_aggregate_status_uses_persisted_fact_priority(
  facts: AggregateFacts,
  expected: str,
) -> None:
  assert derive_aggregate_status(facts) == expected


def test_terminal_status_cannot_be_overwritten() -> None:
  with pytest.raises(ExperimentError) as error:
    validate_status_transition("cancelled", "completed")
  assert error.value.code == "experiment.invalid_status_transition"


def test_failure_fingerprint_ignores_dynamic_error_text() -> None:
  context = {
    "stage_type": "finite_grid_v1",
    "template_type": "legacy_single_strategy_v1",
    "run_path": "strategy",
    "scope_type": "single",
    "failure_stage": "bar.market_replay",
    "component": "bar_runner",
  }
  first = normalize_failure({
    "error": "动态文本 1",
    "errorDetail": {
      "category": "system_error",
      "code": "runtime.version_mismatch",
      "message": "公开消息 1",
      "retryable": True,
    },
  }, **context)
  second = normalize_failure({
    "error": "动态文本 2",
    "errorDetail": {
      "category": "system_error",
      "code": "runtime.version_mismatch",
      "message": "公开消息 2",
      "retryable": True,
    },
  }, **context)

  assert first["fingerprint"] == second["fingerprint"]
  assert first["message"] != second["message"]


def test_failure_policy_bounds_retry_and_circuit_breaker() -> None:
  policy = default_failure_policy()
  failure = {"category": "system_error", "retryable": True}

  assert allows_automatic_retry(
    policy, failure, automatic_retry_count=0, retry_count_in_window=0,
  )
  assert not allows_automatic_retry(
    policy, failure, automatic_retry_count=1, retry_count_in_window=1,
  )
  assert should_open_circuit(
    policy, consecutive_count=3, window_failures=3, window_size=3,
  )
  assert not should_open_circuit(
    policy, consecutive_count=2, window_failures=2, window_size=3,
  )


def test_recovery_migration_adds_retry_and_circuit_state() -> None:
  sql = Path("db/migrations/050_experiment_recovery.sql").read_text(
    encoding="utf-8",
  )

  assert "plan_quota_released_at" in sql
  assert "automatic_retry_count" in sql
  assert "terminal_status" in sql
  assert "error_fingerprint" in sql
  assert "automatic_retry" in sql
  assert "effective_backtest_scheduler_capacity" in sql
  assert "SECURITY DEFINER" in sql
  assert "IF NOT EXISTS" in sql
