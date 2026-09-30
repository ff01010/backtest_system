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
from backend.experiments.repository import _decode_cursor, _encode_cursor
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
