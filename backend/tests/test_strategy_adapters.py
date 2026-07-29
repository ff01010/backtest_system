# -*- coding: utf-8 -*-
"""测试策略适配器注册和回测分发。"""

import copy
import hashlib
from typing import Any

import pandas as pd
import pytest
from fastapi import HTTPException

from backend import app as api_app
from backend import schemas
from backend.services import backtest_runner
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
  assert strategy_adapters.list_strategy_ids() == [
    "trend_follow",
    "grid_mean_reversion",
    "trend_grid",
    "user_script",
    "midcap_earnings",
  ]


def test_strategy_summaries_have_stable_shape() -> None:
  summaries = strategy_adapters.list_strategy_summaries()

  assert summaries[0] == {"id": "trend_follow", "name": "趋势跟踪"}
  assert summaries[1] == {"id": "grid_mean_reversion", "name": "网格均值回归"}
  assert summaries[2] == {"id": "trend_grid", "name": "趋势网格组合"}
  assert summaries[3] == {"id": "user_script", "name": "自定义脚本"}
  assert summaries[4] == {"id": "midcap_earnings", "name": "中盘盈利增长"}


def test_unknown_strategy_adapter_raises_key_error() -> None:
  with pytest.raises(KeyError):
    strategy_adapters.get_strategy_adapter("missing")


def test_parameter_schema_returns_trend_grid_fields() -> None:
  schema = strategy_adapters.get_parameter_schema("trend_grid")

  assert schema == {}


def test_parameter_schema_returns_midcap_editable_fields() -> None:
  schema = strategy_adapters.get_parameter_schema("midcap_earnings")

  assert schema["maxPositions"]["label"] == "最大持仓数"
  assert schema["minMarketCap"]["type"] == "number"
  assert schema["rebalanceMonths"]["type"] == "text"
  assert schema["indexTrendFilter"]["type"] == "boolean"


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
  result = api_app.strategy_parameters("trend_grid")

  assert result["strategy"] == "trend_grid"
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


def test_backtest_version_rejects_changed_locked_params(monkeypatch: pytest.MonkeyPatch) -> None:
  code = "def on_bar(bar, state):\n  return []\n"
  code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
  version = {
    "id": "00000000-0000-0000-0000-000000000007",
    "timeframe": "5m",
    "strategy": "user_script",
    "scriptId": "00000000-0000-0000-0000-000000000011",
    "paramsJson": {
      "params": {
        "allowLong": False,
        "allowShort": True,
        "holdingPolicy": "intraday",
        "window": "20",
      },
      "scriptCodeHash": code_hash,
    },
  }
  monkeypatch.setattr(api_app.state_store, "get_latest_strategy_config_version", lambda _: version)
  monkeypatch.setattr(api_app.state_store, "get_strategy_config_version", lambda _: version)
  monkeypatch.setattr(api_app.state_store, "get_strategy_script", lambda _: {"code": code})
  monkeypatch.setattr(api_app.data_repository, "validate_backtest_coverage", lambda *args: None)
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
  assert request["configVersionId"] == "00000000-0000-0000-0000-000000000007"

  request["strategyParams"]["window"] = "10"
  with pytest.raises(HTTPException, match="window"):
    api_app._validate_backtest_request(request)


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
  assert request.holdingPolicy == "intraday"


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
  )
  second = backtest_runner._build_experiment_manifest(
    payload,
    backtest_runner._build_config(copy.deepcopy(payload)),
    bars,
  )

  assert first["experimentHash"] == second["experimentHash"]
  assert first["randomSeed"] == second["randomSeed"]
  assert first["costModel"]["commissionRate"] == 0.0003
  assert first["barRange"]["timeframe"] == "1m"


def test_strategy_parameters_endpoint_rejects_unknown_strategy() -> None:
  with pytest.raises(HTTPException) as exc_info:
    api_app.strategy_parameters("missing")

  assert exc_info.value.status_code == 404
  assert exc_info.value.detail == "策略不存在: missing"


def test_backtest_runner_dispatches_by_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setitem(strategy_adapters._ADAPTERS, "fake", FakeAdapter())

  result = backtest_runner.run_backtest({"strategy": "fake"})

  assert result == {"strategy": "fake", "ok": True}
