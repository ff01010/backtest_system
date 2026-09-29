# -*- coding: utf-8 -*-
"""测试策略适配器注册和回测分发。"""

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi import HTTPException

from backend import app as api_app
from backend import schemas
from backend.services import backtest_preflight, backtest_runner, research_signal_snapshot
from backend.services import state_store
from backend.services import strategy_adapters
from shared.cost import buy_net, sell_net


class FakeAdapter:
  strategy_id = "fake"
  display_name = "测试策略"

  def get_parameter_schema(self) -> dict[str, Any]:
    return {}

  def run(self, payload: dict[str, Any]) -> dict[str, Any]:
    return {
      "strategy": payload["strategy"],
      "ok": True,
    }


def test_registered_strategy_ids_match_current_viewer_options() -> None:
  assert strategy_adapters.list_strategy_ids() == ["user_script"]


def test_strategy_summaries_have_stable_shape() -> None:
  summaries = strategy_adapters.list_strategy_summaries()

  assert summaries == [{"id": "user_script", "name": "自定义脚本"}]


def test_unknown_strategy_adapter_raises_key_error() -> None:
  with pytest.raises(KeyError):
    strategy_adapters.get_strategy_adapter("missing")


def test_parameter_schema_returns_user_script_fields() -> None:
  schema = strategy_adapters.get_parameter_schema("user_script")

  assert schema == {}


def test_removed_builtin_strategy_has_no_adapter() -> None:
  with pytest.raises(KeyError):
    strategy_adapters.get_strategy_adapter("trend_grid")


def test_midcap_strategy_params_override_runtime_config() -> None:
  config = {
    "universe": {
      "min_market_cap": 10_000_000_000,
      "max_market_cap": 80_000_000_000,
    },
    "fundamental": {
      "min_revenue_yoy": 0.10,
      "min_net_profit_yoy": 0.40,
    },
    "scoring": {
      "max_candidates": 25,
    },
    "position": {
      "max_positions": 25,
      "single_weight": 0.04,
      "max_total_pct": 0.95,
    },
    "rebalance": {
      "rebalance_months": [5, 8, 11],
      "min_holding_days": 40,
    },
    "exit": {
      "stop_loss": -0.20,
    },
    "risk": {
      "index_trend_filter": True,
    },
  }

  backtest_runner._apply_midcap_strategy_params(config, {
    "minMarketCap": "12000000000",
    "maxPositions": "20",
    "singleWeight": "",
    "rebalanceMonths": "4,7,10",
    "stopLoss": "-0.15",
    "indexTrendFilter": False,
  })

  assert config["universe"]["min_market_cap"] == 12_000_000_000
  assert config["position"]["max_positions"] == 20
  assert config["position"]["single_weight"] == 0.05
  assert config["rebalance"]["rebalance_months"] == [4, 7, 10]
  assert config["exit"]["stop_loss"] == -0.15
  assert config["risk"]["index_trend_filter"] is False


def test_strategy_parameters_endpoint_returns_schema() -> None:
  result = api_app.strategy_parameters("user_script")

  assert result["strategy"] == "user_script"
  assert result["parameters"] == {}


def test_backtest_request_preserves_strategy_params() -> None:
  request = schemas.BacktestRunRequest(
    symbol="000001.SZ",
    benchmarkSymbol="000001.SZ",
    strategy="user_script",
    timeframe="5m",
    startDate="20260101",
    endDate="20260102",
    initialCapital=1_000_000,
    commissionRate=0.0003,
    slippage=0,
    strategyParams={"allowLong": False, "window": "20"},
  )

  assert request.model_dump()["strategyParams"] == {"allowLong": False, "window": "20"}


def test_strategy_config_uses_backend_generated_code_hash(monkeypatch: pytest.MonkeyPatch) -> None:
  code = "def on_bar(bar, state):\n  return []\n"
  monkeypatch.setattr(state_store, "get_strategy_script", lambda _: {
    "id": "00000000-0000-0000-0000-000000000011",
    "name": "测试脚本",
    "code": code,
    "entryType": "on_bar",
  })

  normalized = state_store._with_script_code_hash({
    "scriptId": "00000000-0000-0000-0000-000000000011",
    "paramsJson": {"params": {"window": "20"}},
  })

  assert normalized["paramsJson"]["scriptCodeHash"] == hashlib.sha256(
    code.encode("utf-8")
  ).hexdigest()


def test_backtest_requires_saved_strategy_params(monkeypatch: pytest.MonkeyPatch) -> None:
  code = "def on_bar(bar, state):\n  return []\n"
  config = {
    "id": "00000000-0000-0000-0000-000000000007",
    "name": "测试策略",
    "timeframe": "5m",
    "strategy": "user_script",
    "scriptId": "00000000-0000-0000-0000-000000000011",
    "mode": "backtest",
    "symbol": "000001.SZ",
    "benchmarkSymbol": "000001.SZ",
    "accountId": None,
    "paramsJson": {
      "params": {
        "allowLong": False,
        "allowShort": True,
        "holdingPolicy": "intraday",
        "executionAlgorithm": "twap",
        "twapDurationSeconds": "60",
        "twapIntervalSeconds": "3",
        "twapMinChildQuantity": "100",
        "window": "20",
      },
    },
  }
  monkeypatch.setattr(api_app.state_store, "get_strategy_config", lambda _: config)
  monkeypatch.setattr(api_app.state_store, "get_strategy_script", lambda _: {
    "id": config["scriptId"], "name": "测试策略", "description": "测试说明",
    "code": code, "entryType": "on_bar",
  })
  monkeypatch.setattr(api_app.market_data, "ensure_cached", lambda *_: {"status": "cache_hit", "rowCount": 0, "fetchedDates": []})
  monkeypatch.setattr(api_app.market_data, "snapshot", lambda *args: {"hash": args[0]})
  from backend.services import bar_data
  monkeypatch.setattr(bar_data, "ensure_available", lambda *args: {"hash": args[0], "provider": "qmt_kline"})
  monkeypatch.setattr(api_app.data_repository, "validate_backtest_coverage", lambda *args: None)
  monkeypatch.setattr(
    backtest_preflight,
    "require_available",
    lambda *_: pytest.fail("提交回测不应执行行情预检"),
  )
  request = {
    "configId": "00000000-0000-0000-0000-000000000003",
    "configVersionId": None,
    "scriptId": "00000000-0000-0000-0000-000000000011",
    "symbol": "000001.SZ",
    "benchmarkSymbol": "000001.SZ",
    "strategy": "user_script",
    "timeframe": "5m",
    "startDate": "20260101",
    "endDate": "20260102",
    "allowLong": False,
    "allowShort": True,
    "holdingPolicy": "intraday",
    "strategyParams": {
      "allowLong": False,
      "allowShort": True,
      "holdingPolicy": "intraday",
      "window": "20",
    },
  }

  api_app._validate_backtest_request(request)
  assert request["configVersionId"] is None
  assert request["savedScriptSnapshot"]["code"] == code
  assert request["savedConfigSnapshot"]["name"] == "测试策略"

  request["strategyParams"]["window"] = "10"
  with pytest.raises(HTTPException, match="window"):
    api_app._validate_backtest_request(request)


def test_saved_initial_position_setting_must_match_run_request() -> None:
  config = {"paramsJson": {"params": {"initialPositionEnabled": False}}}
  request = {"strategyParams": {"initialPositionEnabled": False}}

  api_app._validate_saved_params(request, config)

  request["strategyParams"]["initialPositionEnabled"] = True
  with pytest.raises(HTTPException, match="initialPositionEnabled"):
    api_app._validate_saved_params(request, config)


def test_bar_quality_error_preserves_structured_failure(monkeypatch: pytest.MonkeyPatch) -> None:
  from backend.l2.providers import L2DataError
  from backend.services import bar_data

  monkeypatch.setattr(
    bar_data,
    "load_bars_with_snapshot",
    lambda *_: (_ for _ in ()).throw(L2DataError(
      "临时 L2 Bar 无有效价格: 2025-03-07",
      code="data.l2_bar_price_invalid",
      context={"symbol": "688981.SH", "tradeDate": "2025-03-07"},
    )),
  )

  with pytest.raises(L2DataError) as raised:
    bar_data.ensure_available("688981.SH", "5m", "20250307", "20250307")

  assert raised.value.code == "data.l2_bar_price_invalid"


def test_trend_grid_direction_flags_block_opening_orders(tmp_path: Any) -> None:
  config = backtest_runner._build_config({
    "strategy": "trend_grid",
    "symbol": "000001.SZ",
    "startDate": "20260101",
    "endDate": "20260131",
    "initialCapital": 1_000_000,
    "allowLong": False,
    "allowShort": False,
  })
  config["backtest"]["output_dir"] = str(tmp_path)
  engine = backtest_runner.BacktestEngine(config)

  assert engine._a_sell(10, 10) is False
  assert engine._a_buy_open(10, 10) is False
  engine._b_open_short(10, 10)
  engine._b_open_long(10, 10)

  assert engine.trades_a == []
  assert engine.trades_b == []


def test_cost_model_uses_the_requested_commission_rate() -> None:
  default_buy = buy_net(10, 100)
  custom_buy = buy_net(10, 100, commission_rate=0.0003)
  default_sell = sell_net(10, 100)
  custom_sell = sell_net(10, 100, commission_rate=0.0003)

  assert custom_buy > default_buy
  assert custom_sell < default_sell


def test_legacy_backtest_request_defaults_to_allowing_long() -> None:
  request = schemas.BacktestRunRequest(
    symbol="000001.SZ",
    startDate="20260101",
    endDate="20260131",
    initialCapital=1_000_000,
    commissionRate=0.0003,
    slippage=0,
    allowShort=True,
  )

  assert request.allowLong is True
  assert request.strategyCategory == "directional_holding"
  assert request.holdingPolicy == "overnight"
  assert request.initialPositionEnabled is False


def test_strategy_category_enforces_its_execution_semantics() -> None:
  overlay = schemas.BacktestRunRequest(
    symbol="688981.SH", startDate="20260101", endDate="20260131",
    initialCapital=1_000_000, commissionRate=0.0003, slippage=0,
    strategyCategory="core_position_overlay", holdingPolicy="intraday", fillModel="l2_quote",
    baseQuantity=1_000, baseCostPrice=10, initialPositionEnabled=True, t0Enabled=True,
  )
  directional_with_initial_position = schemas.BacktestRunRequest(
    symbol="688981.SH", startDate="20260101", endDate="20260131",
    initialCapital=1_000_000, commissionRate=0.0003, slippage=0,
    strategyCategory="directional_holding", holdingPolicy="overnight",
    baseQuantity=1_000, baseCostPrice=10, initialPositionEnabled=True,
  )

  assert overlay.strategyCategory == "core_position_overlay"
  assert directional_with_initial_position.strategyCategory == "directional_holding"
  assert directional_with_initial_position.t0Enabled is False
  with pytest.raises(ValueError, match="未启用初始底仓"):
    schemas.BacktestRunRequest(
      symbol="688981.SH", startDate="20260101", endDate="20260131",
      initialCapital=1_000_000, commissionRate=0.0003, slippage=0,
      strategyCategory="directional_holding", baseQuantity=1_000, baseCostPrice=10,
    )
  with pytest.raises(ValueError, match="不能启用底仓做 T"):
    schemas.BacktestRunRequest(
      symbol="688981.SH", startDate="20260101", endDate="20260131",
      initialCapital=1_000_000, commissionRate=0.0003, slippage=0,
      strategyCategory="directional_holding", holdingPolicy="overnight",
      baseQuantity=1_000, baseCostPrice=10, initialPositionEnabled=True, t0Enabled=True,
    )


def test_intraday_holding_policy_locks_entries_and_settles_positions(tmp_path: Any) -> None:
  config = backtest_runner._build_config({
    "strategy": "trend_grid",
    "symbol": "000001.SZ",
    "startDate": "20260101",
    "endDate": "20260131",
    "initialCapital": 1_000_000,
    "holdingPolicy": "intraday",
  })
  config["backtest"]["output_dir"] = str(tmp_path)
  config["backtest"]["signal_only"] = False
  config["strategy"]["slip_base_vol"] = 0
  engine = backtest_runner.BacktestEngine(config)
  engine._entry_locked = True

  assert engine._a_sell(10, 10) is False
  engine._b_open_short(10, 10)
  assert engine.trades_a == []
  assert engine.trades_b == []

  engine._date = "20260101"
  engine.a_short_holds.append((10, 10, 200))
  engine.b_pos = -200
  engine.b_entry_price = 10
  engine._force_close_all(11)

  assert engine.a_short_holds == []
  assert engine.b_pos == 0
  assert {trade["direction"] for trade in engine.trades_a + engine.trades_b} == {
    "FORCE_BUY",
  }


def test_intraday_holding_policy_settles_on_the_close_bar(tmp_path: Any) -> None:
  config = backtest_runner._build_config({
    "strategy": "trend_grid",
    "symbol": "000001.SZ",
    "startDate": "20260101",
    "endDate": "20260101",
    "initialCapital": 1_000_000,
    "holdingPolicy": "intraday",
  })
  config["backtest"]["output_dir"] = str(tmp_path)
  config["backtest"]["signal_only"] = False
  config["strategy"]["slip_base_vol"] = 0
  engine = backtest_runner.BacktestEngine(config)
  engine.b_pos = -200
  engine.b_entry_price = 10

  engine.run(pd.DataFrame([{
    "time": "202601011455",
    "open": 11,
    "high": 11,
    "low": 11,
    "close": 11,
    "volume": 100,
  }]))

  assert engine.b_pos == 0
  assert engine.trades_b[-1]["direction"] == "FORCE_BUY"


def test_trend_grid_realized_pnl_uses_the_requested_commission_rate(tmp_path: Any) -> None:
  def realized_pnl(commission_rate: float) -> float:
    config = backtest_runner._build_config({
      "strategy": "trend_grid",
      "symbol": "000001.SZ",
      "startDate": "20260101",
      "endDate": "20260131",
      "initialCapital": 1_000_000,
      "commissionRate": commission_rate,
    })
    config["backtest"]["output_dir"] = str(tmp_path / str(commission_rate))
    config["strategy"]["slip_base_vol"] = 0
    config["strategy"]["close_slippage"] = 0
    engine = backtest_runner.BacktestEngine(config)

    assert engine._a_buy_open(10, 10) is True
    engine._a_reduce_long_one(11)
    return float(engine.trades_a[-1]["pnl"])

  assert realized_pnl(0.0003) < realized_pnl(0.00008)


def test_experiment_manifest_is_deterministic(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Any,
) -> None:
  data_path = tmp_path / "000001.SZ_5m.csv"
  data_path.write_text(
    "time,open,high,low,close,volume\n202601010930,10,10,10,10,100\n",
    encoding="utf-8",
  )
  monkeypatch.setattr(
    backtest_runner.settings,
    "resolve_kline_path",
    lambda *_args: str(data_path),
  )
  database_snapshot = {
    "provider": "mapped_l2_bar",
    "hash": "database-cache-hash",
    "barCount": 1,
  }
  payload = {
    "strategy": "trend_grid",
    "timeframe": "1m",
    "symbol": "000001.SZ",
    "startDate": "20260101",
    "endDate": "20260131",
    "initialCapital": 1_000_000,
    "commissionRate": 0.0003,
    "slippage": 0.0001,
    "allowLong": True,
    "allowShort": True,
  }
  bars = pd.read_csv(data_path)
  first = backtest_runner._build_experiment_manifest(
    payload,
    backtest_runner._build_config(copy.deepcopy(payload)),
    bars,
    database_snapshot,
  )
  second = backtest_runner._build_experiment_manifest(
    payload,
    backtest_runner._build_config(copy.deepcopy(payload)),
    bars,
    database_snapshot,
  )

  assert first["experimentHash"] == second["experimentHash"]
  assert first["randomSeed"] == second["randomSeed"]
  assert first["costModel"]["commissionRate"] == 0.0003
  assert first["barRange"]["timeframe"] == "1m"
  assert first["databaseSnapshot"]["provider"] == "mapped_l2_bar"


def test_script_experiment_manifest_includes_display_fields(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    backtest_runner.market_data,
    "snapshot",
    lambda *_args: {"hash": "qmt-history-hash", "barCount": 2},
  )
  payload = {
    "strategyParams": {"window": 20},
    "symbol": "688981.SH",
    "timeframe": "1m",
    "startDate": "20260601",
    "endDate": "20260601",
    "initialCapital": 1_000_000,
    "commissionRate": 0.0003,
    "slippage": 0.0001,
    "allowLong": True,
    "allowShort": False,
    "holdingPolicy": "intraday",
  }
  bars = pd.DataFrame({"time": ["202606010930", "202606011500"]})

  manifest = backtest_runner._build_script_experiment_manifest(
    payload, bars, "script-1", "测试策略", "script-code-hash",
  )

  assert manifest["experimentHash"]
  assert manifest["barRange"]["timeframe"] == "1m"
  assert manifest["costModel"] == {"commissionRate": 0.0003, "slippage": 0.0001}
  assert manifest["holdingPolicy"] == "intraday"


def test_script_experiment_manifest_identifies_research_signal_snapshot(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    backtest_runner.market_data,
    "snapshot",
    lambda *_args: {"hash": "qmt-history-hash", "barCount": 2},
  )
  payload = {
    "strategyParams": {}, "symbol": "688981.SH", "timeframe": "5m",
    "startDate": "20260601", "endDate": "20260601",
    "initialCapital": 1_000_000, "commissionRate": 0.0003, "slippage": 0.0001,
    "researchSignalSnapshot": {"snapshotHash": "a" * 64},
  }
  bars = pd.DataFrame({"time": ["202606010930", "202606011500"]})

  first = backtest_runner._build_script_experiment_manifest(
    payload, bars, "script-1", "测试策略", "script-code-hash",
  )
  payload["researchSignalSnapshot"] = {"snapshotHash": "b" * 64}
  second = backtest_runner._build_script_experiment_manifest(
    payload, bars, "script-1", "测试策略", "script-code-hash",
  )

  assert first["configHash"] != second["configHash"]
  assert first["experimentHash"] != second["experimentHash"]


def test_strategy_parameters_endpoint_rejects_unknown_strategy() -> None:
  with pytest.raises(HTTPException) as exc_info:
    api_app.strategy_parameters("missing")

  assert exc_info.value.status_code == 404
  assert exc_info.value.detail == "策略不存在: missing"


def test_backtest_runner_dispatches_by_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setitem(strategy_adapters._ADAPTERS, "fake", FakeAdapter())

  result = backtest_runner.run_backtest({"strategy": "fake"})

  assert result == {"strategy": "fake", "ok": True}


def test_research_signal_lookup_normalizes_and_forward_fills(tmp_path: Path) -> None:
  artifact_file = tmp_path / "signals.ndjson"
  artifact_file.write_text(
    '{"signalId":"sig-long","action":"long"}\n'
    '{"signalId":"sig-flat","action":"flat"}\n',
    encoding="utf-8",
  )
  snapshot = {
    "artifactFile": str(artifact_file),
    "datasetId": "dataset-1",
    "manifest": {"version": 1},
    "alignmentPlan": [
      {"signalId": "sig-long", "executionBarTime": "202606020935"},
      {"signalId": "sig-flat", "executionBarTime": "202606021000"},
    ],
  }

  active = backtest_runner._research_signal_by_bar_time(
    snapshot, ["202606020930", "202606020935", "202606020940", "202606021000", "202606021005"],
  )

  assert "20260602093000" not in active
  assert active["20260602093500"]["action"] == "long"
  assert active["20260602094000"]["signalId"] == "sig-long"
  assert active["20260602100000"]["action"] == "flat"
  assert active["20260602100500"]["signalId"] == "sig-flat"


def test_user_script_aligns_frozen_signal_after_loading_formal_bars(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  row = research_signal_snapshot._canonical_signal({
    "signalId": "signal-1", "instrument": "688981.SH", "timeframe": "5m",
    "signalTime": "2026-06-02T09:30:00+08:00",
    "availableAt": "2026-06-02T09:30:00+08:00",
    "effectiveAt": "2026-06-02T09:30:00+08:00", "sequence": 1,
    "predictedLabelId": "UP", "action": "long", "modelVersionId": "model-1",
    "sourceHash": "a" * 64, "diagnostic": {},
  })
  artifact_file = tmp_path / "signals.ndjson"
  artifact_file.write_text(
    json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8",
  )
  bars = pd.DataFrame([
    {"time": "20260602093000", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
    {"time": "20260602093500", "open": 10, "high": 10, "low": 10, "close": 10, "volume": 1},
  ])
  monkeypatch.setattr(
    backtest_runner,
    "_load_user_script_bars",
    lambda _payload: (bars, 1, {"hash": "data-hash", "barCount": 2}),
  )
  monkeypatch.setattr(backtest_runner, "_build_benchmark_equity", lambda *_args: ([], []))
  captured: dict[str, Any] = {}

  def execute(script_payload: dict[str, Any]) -> dict[str, Any]:
    captured.update(script_payload)
    return {
      "trades": [], "fills": [], "skippedSignals": [], "unexecutedOrders": [], "warnings": [],
      "portfolioSnapshots": [{
        "longPositions": {}, "shortPositions": {}, "cash": 1_000_000,
        "equity": 1_000_000, "sellableQuantity": 0,
      }],
    }

  payload = {
    "strategy": "user_script", "scriptId": "script-1",
    "versionScriptSnapshot": {"name": "信号策略", "entryType": "on_bar", "code": "def on_bar(bar, state):\n  return []"},
    "symbol": "688981.SH", "timeframe": "5m", "startDate": "2026-06-02", "endDate": "2026-06-02",
    "initialCapital": 1_000_000, "commissionRate": 0.0003, "feeProfile": {}, "slippage": 0,
    "strategyParams": {}, "fillModel": "bar_next_open", "executionAlgorithm": "immediate",
    "holdingPolicy": "intraday", "allowLong": True, "allowShort": False,
    "researchSignalSnapshot": {
      "datasetId": "dataset-1", "manifest": {"version": 1}, "symbol": "688981.SH",
      "sourceTimeframe": "5m", "timeframe": "5m", "startDate": "2026-06-02",
      "endDate": "2026-06-02", "rowCount": 1,
      "snapshotHash": research_signal_snapshot._snapshot_hash([row]),
      "artifactFile": str(artifact_file), "alignmentPlan": [],
    },
  }

  result = backtest_runner.run_user_script_backtest(payload, execute)

  signals = captured["params"]["_researchSignalByBarTime"]
  assert list(signals) == ["20260602093500"]
  assert result["researchSignalSnapshot"]["alignmentPlan"][0]["executionBarTime"] == "20260602093500"


def test_research_signal_lookup_uses_last_transition_from_legacy_same_bar_plan(tmp_path: Path) -> None:
  artifact_file = tmp_path / "signals.ndjson"
  artifact_file.write_text(
    '{"signalId":"sig-long","action":"long"}\n'
    '{"signalId":"sig-flat","action":"flat"}\n',
    encoding="utf-8",
  )
  snapshot = {
    "artifactFile": str(artifact_file),
    "datasetId": "dataset-1",
    "manifest": {"version": 1},
    "alignmentPlan": [
      {"signalId": "sig-long", "executionBarTime": "202606020935"},
      {"signalId": "sig-flat", "executionBarTime": "202606020935"},
    ],
  }

  active = backtest_runner._research_signal_by_bar_time(
    snapshot, ["202606020935", "202606020940"],
  )

  assert active["20260602093500"]["signalId"] == "sig-flat"
  assert active["20260602094000"]["action"] == "flat"


@pytest.mark.parametrize(
  ("signal_required", "dataset_id", "message"),
  [
    (True, None, "当前策略必须选择研究信号后才能运行"),
    (False, "dataset-1", "当前策略未声明为研究信号驱动策略"),
  ],
)
def test_backtest_research_signal_selection_matches_script_declaration(
  monkeypatch: pytest.MonkeyPatch,
  signal_required: bool,
  dataset_id: str | None,
  message: str,
) -> None:
  monkeypatch.setattr(api_app.state_store, "get_strategy_config", lambda _: {"scriptId": "script-1"})
  monkeypatch.setattr(api_app.state_store, "get_strategy_script", lambda _: {
    "id": "script-1", "signalRequired": signal_required,
  })

  with pytest.raises(HTTPException, match=message):
    api_app._prepare_saved_strategy_snapshot({
      "configId": "config-1", "signalDatasetId": dataset_id,
    })


def test_signal_required_script_must_read_research_signal() -> None:
  payload = {
    "code": "def on_bar(bar, state):\n  return []\n",
    "entryType": "on_bar", "signalRequired": True, "status": "draft",
  }

  with pytest.raises(HTTPException, match="必须显式读取"):
    api_app._validate_strategy_script(payload)

  payload["code"] = "def on_bar(bar, state):\n  return bar.get('researchSignal') or []\n"
  api_app._validate_strategy_script(payload)
