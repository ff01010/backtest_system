# -*- coding: utf-8 -*-
"""viewer 回测封装：复用现有 BacktestEngine，并归一化为前端响应。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import numbers
import os
import sys
from contextlib import redirect_stdout
from io import StringIO
from typing import Any, Sequence

import pandas as pd
import yaml

from backend.backtesting import Bar, BarBacktestEngine, FeeProfile, NextOpenExecutionModel, RecordedPriceExecutionModel
from backend.backtesting.legacy import fill_to_api_trade
from backend.strategies.catalog import create_bar_strategy, get_strategy_source_snapshot
from backend.strategies.external_engine import load_external_engine
from backend.services import bar_data, market_data, state_store, strategy_worker

_BACKTEST_SYSTEM_ROOT = os.path.dirname(
  os.path.dirname(os.path.dirname(__file__))
)
_PROJECT_ROOT = os.getenv(
  "TREND_GRID_CODE_ROOT",
  os.path.join(os.path.dirname(_BACKTEST_SYSTEM_ROOT), "code"),
)
_STRATEGY_DIR = os.path.join(_PROJECT_ROOT, "strategies", "trend_grid")
if _PROJECT_ROOT not in sys.path:
  sys.path.insert(0, _PROJECT_ROOT)

from backend import settings
_ENGINE_VERSION = "bar-portfolio-v1"
_RANDOM_SEED_VERSION = "bar-signal-v1"
_TRADING_DAYS_PER_YEAR = 242
_ANNUAL_RISK_FREE_RATE = 0.0


def is_trading_time(hhmm: int) -> bool:
  """A 股分钟 K 线仅保留上午与下午连续竞价时段。"""
  return 930 <= hhmm <= 1130 or 1300 <= hhmm <= 1500


def __getattr__(name: str) -> Any:
  """兼容仍从本模块读取趋势网格引擎的旧调用方。"""
  if name == "BacktestEngine":
    return load_external_engine("trend_grid")
  raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def run_backtest(payload: dict[str, Any]) -> dict[str, Any]:
  """按策略 ID 分发到对应适配器。"""
  from backend.services.strategy_adapters import get_strategy_adapter

  adapter = get_strategy_adapter(payload["strategy"])
  return _sanitize_json_value(adapter.run(payload))


def _sanitize_json_value(value: Any) -> Any:
  """递归清理 JSON 不支持的非有限浮点数。"""
  if isinstance(value, numbers.Real) and not isinstance(value, bool):
    if not math.isfinite(float(value)):
      return None
    return int(value) if isinstance(value, numbers.Integral) else float(value)
  if isinstance(value, list):
    return [_sanitize_json_value(item) for item in value]
  if isinstance(value, dict):
    return {key: _sanitize_json_value(item) for key, item in value.items()}
  return value


def run_trend_grid_backtest(payload: dict[str, Any]) -> dict[str, Any]:
  """运行单标的同步回测，返回 summary/equity/trades。"""
  config = _build_config(payload)
  bars = _load_bars(
    payload["symbol"], payload.get("timeframe", "5m"), payload["startDate"], payload["endDate"],
  )
  experiment_manifest = _build_experiment_manifest(payload, config, bars)
  strategy_source = get_strategy_source_snapshot(payload["strategy"])
  experiment_manifest["strategySource"] = {
    "id": strategy_source["id"],
    "name": strategy_source["name"],
    "codeHash": strategy_source["codeHash"],
    "paths": [source["path"] for source in strategy_source["sources"]],
  }
  htf_df = _load_htf(config)
  core_bars = _to_backtest_bars(bars, payload["symbol"])
  strategy = create_bar_strategy(
    payload["strategy"],
    config,
    htf_df,
    _day_end_times(core_bars),
  )
  core_engine = BarBacktestEngine(
    initial_cash=float(payload["initialCapital"]),
    commission_rate=float(payload["commissionRate"]),
    execution_model=RecordedPriceExecutionModel(),
    holding_policy=str(config["backtest"]["holding_policy"]),
  )
  with redirect_stdout(StringIO()):
    core_result = core_engine.run(strategy, core_bars)
  response = _build_bar_backtest_response(
    payload,
    config,
    bars,
    core_result,
    experiment_manifest,
  )
  response["warnings"] += list(getattr(strategy, "warnings", []))
  return response


def run_user_script_backtest(payload: dict[str, Any]) -> dict[str, Any]:
  """将脚本快照交给隔离容器运行，后端只持有数据和结果。"""
  script_id = payload.get("scriptId")
  if not isinstance(script_id, str) or not script_id:
    raise ValueError("自定义脚本回测必须选择策略代码")
  version_snapshot = payload.get("versionScriptSnapshot")
  if isinstance(version_snapshot, dict) and isinstance(version_snapshot.get("code"), str):
    script = {
      "id": script_id,
      "name": version_snapshot.get("name", f"版本脚本 {script_id}"),
      "code": version_snapshot["code"],
      "entryType": version_snapshot.get("entryType", "on_bar"),
    }
  else:
    script = state_store.get_strategy_script(script_id)
  if script["entryType"] != "on_bar":
    raise ValueError("所选策略代码不是可执行的 on_bar 脚本")
  bars, warmup_bar_count, data_snapshot = _load_user_script_bars(payload)
  formal_bars = bars.iloc[warmup_bar_count:].reset_index(drop=True)
  code_hash = hashlib.sha256(script["code"].encode("utf-8")).hexdigest()
  manifest = _build_script_experiment_manifest(
    payload, formal_bars, script_id, script["name"], code_hash, data_snapshot,
  )
  manifest["strategySource"] = {
    "id": f"script:{script_id}",
    "name": script["name"],
    "codeHash": code_hash,
    "entryType": "on_bar",
    "sources": [{
      "path": f"database:user_strategy/{script_id}",
      "code": script["code"],
    }],
  }
  worker_result = strategy_worker.run_script({
    "scriptId": script_id,
    "code": script["code"],
    "params": {
      **payload.get("strategyParams", {}),
      "allowLong": payload.get("allowLong", True),
      "allowShort": payload.get("allowShort", True),
      "corePositionOverlay": _strategy_category(payload) == "core_position_overlay",
    },
    "bars": [
      {"time": str(row.time), "symbol": payload["symbol"], "open": float(row.open),
       "high": float(row.high), "low": float(row.low), "close": float(row.close),
       "volume": float(row.volume)}
      for row in bars.itertuples(index=False)
    ],
    "initialCapital": float(payload["initialCapital"]),
    "commissionRate": float(payload["commissionRate"]),
    "feeProfile": dict(payload.get("feeProfile") or {}),
    "slippage": float(payload["slippage"]),
    "holdingPolicy": _holding_policy(payload),
    "fillModel": str(payload.get("fillModel", "bar_next_open")),
    "executionAlgorithm": str(payload.get("executionAlgorithm", "immediate")),
    "executionParameters": dict(payload.get("executionParameters") or {}),
    "baseQuantity": int(payload.get("baseQuantity", 0)),
    "baseCostPrice": float(payload.get("baseCostPrice", 0)),
    "t0Enabled": bool(payload.get("t0Enabled", False)),
    "warmupBarCount": warmup_bar_count,
  })
  initial_cash = float(payload["initialCapital"])
  base_quantity = int(payload.get("baseQuantity", 0))
  base_cost_price = float(payload.get("baseCostPrice", 0))
  initial_equity = _initial_total_equity(initial_cash, base_quantity, base_cost_price)
  daily_holdings = _build_worker_daily_holdings(
    formal_bars, worker_result["portfolioSnapshots"], payload["symbol"], initial_equity,
  )
  summary = _build_worker_summary(
    initial_cash, base_quantity, base_cost_price, bool(payload.get("t0Enabled", False)),
    worker_result["trades"], daily_holdings,
  )
  benchmark_symbol = payload.get("benchmarkSymbol") or payload["symbol"]
  benchmark_equity, benchmark_warnings = _build_benchmark_equity(
    benchmark_symbol, payload.get("timeframe", "5m"), payload["startDate"], payload["endDate"],
    float(payload["initialCapital"]),
  )
  benchmark_return = _compute_benchmark_return(benchmark_equity)
  return {
    "strategyCategory": _strategy_category(payload),
    "resultSchemaVersion": 2,
    "strategyMetrics": _strategy_metrics(_strategy_category(payload), summary),
    "outputDir": _script_output_dir(payload),
    "summary": summary,
    "equity": _build_portfolio_equity(daily_holdings),
    "benchmarkSymbol": benchmark_symbol,
    "benchmarkEquity": benchmark_equity,
    "benchmarkReturn": benchmark_return,
    "excessReturn": round(summary["totalReturn"] - benchmark_return, 6),
    "dailyHoldings": daily_holdings,
    "trades": worker_result["trades"],
    "skippedSignals": worker_result["skippedSignals"] + worker_result.get("unexecutedOrders", []),
    "experimentManifest": manifest,
    "warnings": _build_warnings(payload) + benchmark_warnings + worker_result["warnings"],
  }


def _load_user_script_bars(payload: dict[str, Any]) -> tuple[pd.DataFrame, int, dict[str, Any]]:
  """标准自定义策略固定走与预检一致的 QMT 来源解析器。"""
  timeframe = str(payload.get("timeframe", "5m"))
  saved = dict(payload.get("marketDataSnapshot") or {}).get("barSnapshot") or {}
  formal_dates = saved.get("formalDates") if isinstance(saved.get("formalDates"), list) else None
  resolution = market_data.resolve_backtest_data(
    payload["symbol"], timeframe, payload["startDate"], payload["endDate"], formal_dates,
    int(payload.get("warmupTradingDays", 0)),
  )
  frame, warmup_bar_count = market_data.load_backtest_bars(payload["symbol"], resolution)
  return frame, warmup_bar_count, {"provider": "qmt_kline", **market_data.snapshot_for_resolution(payload["symbol"], resolution)}


def _script_output_dir(payload: dict[str, Any]) -> str:
  root = os.getenv("TREND_GRID_OUTPUT_ROOT", "./runtime/artifacts")
  return os.path.join(root, f"user_script_{payload['symbol']}_{payload['startDate']}_{payload['endDate']}")


def _build_script_experiment_manifest(
  payload: dict[str, Any], bars: pd.DataFrame, script_id: str, script_name: str, code_hash: str,
  data_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
  timeframe = str(payload.get("timeframe", "5m"))
  holding_policy = _holding_policy(payload)
  data_snapshot = data_snapshot or dict(payload.get("marketDataSnapshot", {}).get(payload["symbol"]) or market_data.snapshot(
    payload["symbol"], timeframe, payload["startDate"], payload["endDate"],
  ))
  config_hash = _stable_hash({
    "scriptId": script_id,
    "strategyParams": payload.get("strategyParams", {}),
    "symbol": payload["symbol"],
    "timeframe": timeframe,
    "startDate": payload["startDate"],
    "endDate": payload["endDate"],
    "initialCapital": payload["initialCapital"],
    "commissionRate": payload["commissionRate"],
    "slippage": payload["slippage"],
    "fillModel": payload.get("fillModel", "bar_next_open"),
    "executionAlgorithm": payload.get("executionAlgorithm", "immediate"),
    "executionParameters": payload.get("executionParameters", {}),
    "feeProfile": payload.get("feeProfile", {}),
    "allowLong": payload.get("allowLong", True),
    "allowShort": payload.get("allowShort", True),
    "holdingPolicy": holding_policy,
    "strategyCategory": _strategy_category(payload),
    "resultSchemaVersion": 2,
  })
  experiment_hash = _stable_hash({
    "engineVersion": "isolated-script-worker-v1",
    "strategyCodeHash": code_hash,
    "dataHash": data_snapshot["hash"],
    "configHash": config_hash,
  })
  return {
    "experimentHash": experiment_hash,
    "engineVersion": "isolated-script-worker-v1",
    "signalModel": "on_bar_script",
    "fillModel": str(payload.get("fillModel", "bar_next_open")),
    "executionAlgorithm": str(payload.get("executionAlgorithm", "immediate")),
    "executionParameters": dict(payload.get("executionParameters") or {}),
    "randomSeed": 0,
    "strategyCodeHash": code_hash,
    "configHash": config_hash,
    "strategySource": {"id": f"script:{script_id}", "name": script_name, "codeHash": code_hash},
    "dataHash": data_snapshot["hash"],
    "databaseSnapshot": data_snapshot,
    "barCount": int(len(bars)),
    "barRange": {
      "start": str(bars.iloc[0]["time"]) if not bars.empty else "",
      "end": str(bars.iloc[-1]["time"]) if not bars.empty else "",
      "timeframe": timeframe,
    },
    "costModel": {
      "commissionRate": float(payload["commissionRate"]),
      "slippage": float(payload["slippage"]),
      **dict(payload.get("feeProfile") or {}),
    },
    "holdingPolicy": holding_policy,
    "strategyCategory": _strategy_category(payload),
    "resultSchemaVersion": 2,
    "executionEnvironment": "strategy-worker",
  }


def _build_worker_daily_holdings(
  bars: pd.DataFrame, snapshots: list[dict[str, Any]], symbol: str, initial_equity: float,
) -> list[dict[str, Any]]:
  if len(bars) != len(snapshots):
    raise ValueError("策略执行容器返回的账本快照数量异常")
  items: list[dict[str, Any]] = []
  previous_total = initial_equity
  for index, row in enumerate(bars.itertuples(index=False)):
    date = str(row.time)[:8]
    if index + 1 < len(bars) and str(bars.iloc[index + 1]["time"])[:8] == date:
      continue
    snapshot = snapshots[index]
    long_position = snapshot["longPositions"].get(symbol, {})
    short_position = snapshot["shortPositions"].get(symbol, {})
    long_qty = int(long_position.get("quantity", 0))
    short_qty = int(short_position.get("quantity", 0))
    total_equity = float(snapshot["equity"])
    items.append({
      "date": date, "close": round(float(row.close), 4), "longQty": long_qty, "shortQty": short_qty,
      "cash": round(float(snapshot["cash"]), 2), "longMarketValue": round(long_qty * float(row.close), 2),
      "shortMarketValue": round(short_qty * float(row.close), 2),
      "marketValue": round((long_qty - short_qty) * float(row.close), 2),
      "totalEquity": round(total_equity, 2), "dailyPnl": round(total_equity - previous_total, 2),
    })
    previous_total = total_equity
  return items


def _build_worker_summary(
  initial_cash: float,
  base_quantity: int,
  base_cost_price: float,
  t0_enabled: bool,
  trades: list[dict[str, Any]],
  daily_holdings: list[dict[str, Any]],
) -> dict[str, Any]:
  initial_equity = _initial_total_equity(initial_cash, base_quantity, base_cost_price)
  realized_pnl = [float(item.get("pnl", 0)) for item in trades if abs(float(item.get("pnl", 0))) > 1e-8]
  wins = [value for value in realized_pnl if value > 0]
  losses = [value for value in realized_pnl if value < 0]
  daily_pnl = {item["date"]: float(item["dailyPnl"]) for item in daily_holdings}
  performance = _compute_performance_stats(initial_equity, daily_pnl)
  final_equity = daily_holdings[-1]["totalEquity"] if daily_holdings else initial_equity
  return {
    "initialEquity": round(initial_equity, 2),
    "totalPnl": round(final_equity - initial_equity, 2),
    "totalReturn": round(final_equity / initial_equity - 1, 6) if initial_equity > 0 else 0.0,
    "maxDrawdown": round(_max_drawdown_amount(initial_equity, daily_holdings), 2), **performance,
    "winRate": round(len(wins) / len(realized_pnl), 4) if realized_pnl else 0.0,
    "dailyWinRate": round(sum(value > 0 for value in daily_pnl.values()) / len(daily_pnl), 4) if daily_pnl else 0.0,
    "tradeCount": len(trades), "orderCount": len(trades), "fillCount": len(trades),
    "winningTrades": len(wins), "losingTrades": len(losses),
    "avgWin": round(sum(wins) / len(wins), 2) if wins else None,
    "avgLoss": round(sum(losses) / len(losses), 2) if losses else None,
    "profitLossRatio": round(abs(sum(wins) / sum(losses)), 4) if wins and losses else None,
    "finalEquity": round(final_equity, 2),
    **_build_worker_base_metrics(initial_cash, base_quantity, base_cost_price, t0_enabled, daily_holdings),
  }


def _initial_total_equity(initial_cash: float, base_quantity: int, base_cost_price: float) -> float:
  return initial_cash + max(0, base_quantity) * max(0.0, base_cost_price)


def _build_worker_base_metrics(
  initial_cash: float,
  base_quantity: int,
  base_cost_price: float,
  t0_enabled: bool,
  daily_holdings: list[dict[str, Any]],
) -> dict[str, float | None]:
  if base_quantity <= 0 or base_cost_price <= 0:
    return {"baseHoldingPnl": None, "baseHoldingReturn": None, "t0ExcessPnl": None, "t0ExcessReturn": None}
  initial_equity = _initial_total_equity(initial_cash, base_quantity, base_cost_price)
  final_equity = float(daily_holdings[-1]["totalEquity"]) if daily_holdings else initial_equity
  final_close = float(daily_holdings[-1]["close"]) if daily_holdings else base_cost_price
  base_final_equity = initial_cash + base_quantity * final_close
  base_pnl = base_final_equity - initial_equity
  excess_pnl = final_equity - base_final_equity
  return {
    "baseHoldingPnl": round(base_pnl, 2),
    "baseHoldingReturn": round(base_pnl / initial_equity, 6) if initial_equity > 0 else None,
    "t0ExcessPnl": round(excess_pnl, 2) if t0_enabled else None,
    "t0ExcessReturn": round(excess_pnl / (base_quantity * base_cost_price), 6) if t0_enabled else None,
  }


def _build_bar_backtest_response(
  payload: dict[str, Any],
  config: dict[str, Any],
  bars: pd.DataFrame,
  core_result: Any,
  manifest: dict[str, Any],
) -> dict[str, Any]:
  """将通用 Bar 内核结果归一化为统一 API 响应。"""
  daily_holdings = _build_portfolio_daily_holdings(
    bars=bars,
    portfolio_snapshots=core_result.portfolio_snapshots,
    symbol=payload["symbol"],
    initial_capital=float(payload["initialCapital"]),
  )
  summary = _build_portfolio_summary(float(payload["initialCapital"]), core_result.fills, daily_holdings)
  benchmark_symbol = payload.get("benchmarkSymbol") or payload["symbol"]
  benchmark_equity, benchmark_warnings = _build_benchmark_equity(
    symbol=benchmark_symbol,
    timeframe=payload.get("timeframe", "5m"),
    start_date=payload["startDate"],
    end_date=payload["endDate"],
    initial_capital=float(payload["initialCapital"]),
  )
  benchmark_return = _compute_benchmark_return(benchmark_equity)
  return {
    "strategyCategory": _strategy_category(payload),
    "resultSchemaVersion": 2,
    "strategyMetrics": _strategy_metrics(_strategy_category(payload), summary),
    "outputDir": config["backtest"]["output_dir"],
    "summary": summary,
    "equity": _build_portfolio_equity(daily_holdings),
    "benchmarkSymbol": benchmark_symbol,
    "benchmarkEquity": benchmark_equity,
    "benchmarkReturn": benchmark_return,
    "excessReturn": round(summary["totalReturn"] - benchmark_return, 6),
    "dailyHoldings": daily_holdings,
    "trades": [fill_to_api_trade(fill) for fill in core_result.fills],
    "experimentManifest": manifest,
    "warnings": _build_warnings(payload) + benchmark_warnings,
  }


def _day_end_times(bars: Sequence[Bar]) -> set[str]:
  """返回每个交易日最后一根实际存在的 K 线时间。"""
  return {
    bar.time
    for index, bar in enumerate(bars)
    if index == len(bars) - 1 or bars[index + 1].date != bar.date
  }


def run_midcap_earnings_backtest(payload: dict[str, Any]) -> dict[str, Any]:
  """运行中盘盈利增长组合回测，返回 viewer 统一结果。"""
  try:
    MidcapEarningsEngine = load_external_engine("midcap_earnings")
  except ModuleNotFoundError as error:
    raise ValueError("中盘盈利增长策略运行代码不可用，请检查策略目录配置。") from error

  strategy_dir = os.path.join(_PROJECT_ROOT, "strategies", "midcap_earnings")
  config_path = os.path.join(strategy_dir, "config.yaml")
  with open(config_path, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

  start_date = str(payload["startDate"])
  end_date = str(payload["endDate"])
  initial_capital = float(payload["initialCapital"])
  data_dir = config["backtest"]["data_dir"]
  run_name = f"midcap_earnings_{start_date}_{end_date}"

  config["backtest"]["start_date"] = start_date
  config["backtest"]["end_date"] = end_date
  config["backtest"]["initial_capital"] = initial_capital
  config["backtest"]["output_dir"] = os.path.join(strategy_dir, "viewer_output", run_name)
  _apply_midcap_strategy_params(config, payload.get("strategyParams"))

  daily = pd.read_csv(
    os.path.join(data_dir, "stocks_daily_enriched.csv.gz"),
    dtype={"trade_date": str, "stock_code": str},
  )
  daily = daily[(daily["trade_date"] >= start_date) & (daily["trade_date"] <= end_date)]

  financials = pd.read_csv(
    os.path.join(data_dir, "income_quarterly.csv"),
    dtype={"stock_code": str, "m_anntime": str},
  )

  indices_path = os.path.join(data_dir, "indices_daily.csv")
  if os.path.exists(indices_path):
    indices = pd.read_csv(indices_path, dtype={"trade_date": str})
    indices = indices[(indices["trade_date"] >= start_date) & (indices["trade_date"] <= end_date)]
  else:
    indices = pd.DataFrame()
    config["risk"]["index_trend_filter"] = False

  engine = MidcapEarningsEngine(config)
  with redirect_stdout(StringIO()):
    engine.load_data(daily, financials, indices)
    raw_summary = engine.run()

  equity = _build_midcap_equity(initial_capital, dict(engine._daily_nav))
  trades = [_normalize_midcap_trade(t) for t in engine._trades]
  summary = _build_midcap_summary(initial_capital, dict(engine._daily_nav), raw_summary, trades)
  daily_holdings = _build_midcap_daily_holdings(
    initial_capital=initial_capital,
    daily_nav=dict(engine._daily_nav),
    positions_log=list(engine._daily_positions_log),
  )
  benchmark_symbol = config["backtest"].get("benchmark_index")
  benchmark_equity = _build_midcap_benchmark_equity(
    indices=indices,
    benchmark_symbol=str(benchmark_symbol or ""),
    initial_capital=initial_capital,
  )
  benchmark_return = _compute_benchmark_return(benchmark_equity)

  return {
    "strategyCategory": "directional_holding",
    "resultSchemaVersion": 2,
    "strategyMetrics": _strategy_metrics("directional_holding", summary),
    "outputDir": config["backtest"]["output_dir"],
    "summary": summary,
    "equity": equity,
    "benchmarkSymbol": benchmark_symbol,
    "benchmarkEquity": benchmark_equity,
    "benchmarkReturn": benchmark_return,
    "excessReturn": round(summary["totalReturn"] - benchmark_return, 6),
    "dailyHoldings": daily_holdings,
    "trades": trades,
    "warnings": [
      "中盘盈利增长为全市场组合策略，前端标的参数仅用于回测记录，不参与选股。",
    ],
  }


def _load_base_config() -> dict[str, Any]:
  config_path = os.path.join(_STRATEGY_DIR, "config.yaml")
  with open(config_path, "r", encoding="utf-8") as f:
    return yaml.safe_load(f)


def _stable_hash(value: Any) -> str:
  encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_fingerprint(path: str | None) -> dict[str, Any] | None:
  if not path or not os.path.exists(path):
    return None
  digest = hashlib.sha256()
  with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
      digest.update(chunk)
  stat = os.stat(path)
  return {
    "path": os.path.relpath(path, _PROJECT_ROOT),
    "sha256": digest.hexdigest(),
    "size": stat.st_size,
    "modifiedAt": int(stat.st_mtime),
  }


def _fingerprint_hash(files: list[dict[str, Any] | None]) -> str:
  identity = [
    {"path": item["path"], "sha256": item["sha256"], "size": item["size"]}
    for item in files
    if item is not None
  ]
  return _stable_hash(identity)


def _build_experiment_manifest(
  payload: dict[str, Any],
  config: dict[str, Any],
  bars: pd.DataFrame,
) -> dict[str, Any]:
  timeframe = str(payload.get("timeframe", "5m"))
  code_files = [
    _file_fingerprint(os.path.join(_STRATEGY_DIR, "engine.py")),
    _file_fingerprint(os.path.join(_STRATEGY_DIR, "config.yaml")),
  ]
  data_files = [
    None,
    _file_fingerprint(config["backtest"].get("htf_data_path")),
  ]
  strategy_hash = _fingerprint_hash(code_files)
  database_snapshot = market_data.snapshot(
    payload["symbol"], timeframe, payload["startDate"], payload["endDate"],
  )
  data_hash = database_snapshot["hash"]
  seed_material = {
    "payload": payload,
    "strategyHash": strategy_hash,
    "dataHash": data_hash,
    "seedVersion": _RANDOM_SEED_VERSION,
  }
  random_seed = int(_stable_hash(seed_material)[:8], 16)
  config["strategy"]["random_seed"] = random_seed
  config_hash = _stable_hash(config)
  experiment_hash = _stable_hash({
    "strategyHash": strategy_hash,
    "dataHash": data_hash,
    "configHash": config_hash,
    "engineVersion": _ENGINE_VERSION,
  })
  return {
    "experimentHash": experiment_hash,
    "engineVersion": _ENGINE_VERSION,
    "signalModel": "bar_close_signal",
    "fillModel": "bar_close_signal_fill",
    "randomSeed": random_seed,
    "strategyCodeHash": strategy_hash,
    "configHash": config_hash,
    "dataHash": data_hash,
    "dataFiles": [item for item in data_files if item is not None],
    "databaseSnapshot": database_snapshot,
    "strategyFiles": [item for item in code_files if item is not None],
    "barCount": int(len(bars)),
    "barRange": {
      "start": str(bars.iloc[0]["time"]) if not bars.empty else "",
      "end": str(bars.iloc[-1]["time"]) if not bars.empty else "",
      "timeframe": timeframe,
    },
    "costModel": {
      "commissionRate": float(payload["commissionRate"]),
      "slippage": float(payload["slippage"]),
    },
    "holdingPolicy": config["backtest"]["holding_policy"],
    "strategyCategory": _strategy_category(payload),
    "resultSchemaVersion": 2,
  }


def _build_config(payload: dict[str, Any]) -> dict[str, Any]:
  config = copy.deepcopy(_load_base_config())
  bc = config["backtest"]
  strategy = config["strategy"]

  bc["stock"] = payload["symbol"]
  bc["data_start"] = payload["startDate"]
  bc["data_end"] = payload["endDate"]
  bc["initial_capital"] = float(payload["initialCapital"])
  bc["commission_rate"] = float(payload.get("commissionRate", 0.00008))
  bc["allow_long"] = bool(payload.get("allowLong", True))
  bc["allow_short"] = bool(payload.get("allowShort", True))
  bc["signal_only"] = True
  bc["holding_policy"] = _holding_policy(payload)
  run_name = (
    f"{payload['strategy']}_{payload['symbol']}_"
    f"{payload['startDate']}_{payload['endDate']}"
  )
  output_root = os.getenv(
    "TREND_GRID_OUTPUT_ROOT",
    os.path.join(_STRATEGY_DIR, "output"),
  )
  bc["output_dir"] = os.path.join(output_root, run_name)

  slippage = float(payload.get("slippage") or 0)
  if slippage > 0:
    strategy["close_slippage"] = slippage
    strategy["slip_base_vol"] = slippage

  _apply_position_params(strategy, payload)
  return config


def _strategy_category(payload: dict[str, Any]) -> str:
  category = str(payload.get("strategyCategory") or "")
  if category in {"core_position_overlay", "directional_holding"}:
    return category
  return "core_position_overlay" if int(payload.get("baseQuantity", 0)) > 0 or bool(payload.get("t0Enabled", False)) else "directional_holding"


def _holding_policy(payload: dict[str, Any]) -> str:
  if payload.get("strategyCategory") not in {"core_position_overlay", "directional_holding"}:
    return "overnight" if payload.get("holdingPolicy") == "overnight" else "intraday"
  return "intraday" if _strategy_category(payload) == "core_position_overlay" else "overnight"


def _strategy_metrics(category: str, summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
  if category == "core_position_overlay":
    return {"t0Metrics": {
      "baseHoldingPnl": summary.get("baseHoldingPnl"),
      "baseHoldingReturn": summary.get("baseHoldingReturn"),
      "t0ExcessPnl": summary.get("t0ExcessPnl"),
      "t0ExcessReturn": summary.get("t0ExcessReturn"),
    }}
  return {"holdingMetrics": {
    "totalPnl": summary.get("totalPnl"),
    "totalReturn": summary.get("totalReturn"),
    "maxDrawdown": summary.get("maxDrawdown"),
    "maxDrawdownRate": summary.get("maxDrawdownRate"),
    "finalEquity": summary.get("finalEquity"),
  }}


def _apply_position_params(strategy: dict[str, Any], payload: dict[str, Any]) -> None:
  initial_capital = float(payload["initialCapital"])
  if initial_capital <= 0:
    return

  # 当前回测没有资金占用和撮合模型，统一以初始资金作为策略资金上限。
  strategy["max_long_capital"] = max(float(strategy.get("max_long_capital", 0)), initial_capital)
  strategy["max_short_capital"] = max(float(strategy.get("max_short_capital", 0)), initial_capital)


def _load_bars(symbol: str, timeframe: str, start_date: str, end_date: str) -> pd.DataFrame:
  df, _ = bar_data.load_bars_with_snapshot(symbol, timeframe, start_date, end_date)
  df["hhmm"] = df["time"].str[8:12].astype(int)
  df = df[df["hhmm"].apply(is_trading_time)]
  return df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def _to_backtest_bars(bars: pd.DataFrame, symbol: str) -> list[Bar]:
  """把数据读取层的 DataFrame 转换为通用 K 线事件。"""
  return [
    Bar(
      time=str(row.time),
      symbol=symbol,
      open=float(row.open),
      high=float(row.high),
      low=float(row.low),
      close=float(row.close),
      volume=float(row.volume),
    )
    for row in bars.itertuples(index=False)
  ]


def _load_htf(config: dict[str, Any]) -> pd.DataFrame | None:
  path = config["backtest"].get("htf_data_path")
  if not path or not os.path.exists(path):
    return None
  return pd.read_csv(path)


def _build_portfolio_daily_holdings(
  bars: pd.DataFrame,
  portfolio_snapshots: list[Any],
  symbol: str,
  initial_capital: float,
) -> list[dict[str, Any]]:
  """从内核账本快照生成每日持仓，避免再次回放交易。"""
  if bars.empty or len(bars) != len(portfolio_snapshots):
    raise ValueError("账本快照与 K 线数量不一致")

  snapshots: list[dict[str, Any]] = []
  previous_total = initial_capital
  for index, row in enumerate(bars.itertuples(index=False)):
    date = str(row.time)[:8]
    is_day_end = index == len(bars) - 1 or str(bars.iloc[index + 1]["time"])[:8] != date
    if not is_day_end:
      continue
    snapshot = portfolio_snapshots[index]
    long_position = snapshot.long_positions.get(symbol)
    short_position = snapshot.short_positions.get(symbol)
    close_price = float(row.close)
    long_qty = int(long_position.quantity) if long_position else 0
    short_qty = int(short_position.quantity) if short_position else 0
    long_value = long_qty * close_price
    short_value = short_qty * close_price
    total_equity = float(snapshot.equity)
    daily_pnl = total_equity - previous_total
    snapshots.append({
      "date": date,
      "close": round(close_price, 4),
      "longQty": long_qty,
      "shortQty": short_qty,
      "cash": round(float(snapshot.cash), 2),
      "longMarketValue": round(long_value, 2),
      "shortMarketValue": round(short_value, 2),
      "marketValue": round(long_value - short_value, 2),
      "totalEquity": round(total_equity, 2),
      "dailyPnl": round(daily_pnl, 2),
    })
    previous_total = total_equity
  return snapshots


def _build_portfolio_equity(daily_holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
  return [{
    "date": item["date"],
    "pnl": item["dailyPnl"],
    "value": item["totalEquity"],
  } for item in daily_holdings]


def _build_portfolio_summary(
  initial_capital: float,
  fills: list[Any],
  daily_holdings: list[dict[str, Any]],
) -> dict[str, Any]:
  daily_pnl = {item["date"]: float(item["dailyPnl"]) for item in daily_holdings}
  realized_pnl = [float(fill.realized_pnl) for fill in fills if abs(float(fill.realized_pnl)) > 1e-8]
  wins = [value for value in realized_pnl if value > 0]
  losses = [value for value in realized_pnl if value < 0]
  performance = _compute_performance_stats(initial_capital, daily_pnl)
  final_equity = daily_holdings[-1]["totalEquity"] if daily_holdings else initial_capital
  max_drawdown = _max_drawdown_amount(initial_capital, daily_holdings)
  return {
    "totalPnl": round(float(final_equity) - initial_capital, 2),
    "totalReturn": round(float(final_equity) / initial_capital - 1, 6),
    "maxDrawdown": round(max_drawdown, 2),
    "maxDrawdownRate": performance["maxDrawdownRate"],
    "annualVolatility": performance["annualVolatility"],
    "winRate": round(len(wins) / len(realized_pnl), 4) if realized_pnl else 0.0,
    "dailyWinRate": round(sum(value > 0 for value in daily_pnl.values()) / len(daily_pnl), 4) if daily_pnl else 0.0,
    "tradeCount": len(fills),
    "orderCount": len(fills),
    "fillCount": len(fills),
    "winningTrades": len(wins),
    "losingTrades": len(losses),
    "avgWin": round(sum(wins) / len(wins), 2) if wins else 0.0,
    "avgLoss": round(sum(losses) / len(losses), 2) if losses else 0.0,
    "profitLossRatio": round(sum(wins) / abs(sum(losses)), 4) if losses else 0.0,
    "finalEquity": round(float(final_equity), 2),
    "annualReturn": performance["annualReturn"],
    "annualSharpe": performance["annualSharpe"],
    "sortino": performance["sortino"],
  }


def _max_drawdown_amount(
  initial_capital: float,
  daily_holdings: list[dict[str, Any]],
) -> float:
  peak = initial_capital
  max_drawdown = 0.0
  for item in daily_holdings:
    value = float(item["totalEquity"])
    peak = max(peak, value)
    max_drawdown = max(max_drawdown, peak - value)
  return max_drawdown


def _compute_performance_stats(
  initial_capital: float,
  daily_pnl: dict[str, float],
) -> dict[str, float]:
  if not daily_pnl or initial_capital <= 0:
    return {
      "annualReturn": 0.0,
      "annualVolatility": 0.0,
      "annualSharpe": 0.0,
      "sortino": 0.0,
      "maxDrawdownRate": 0.0,
    }

  equity = initial_capital
  peak = initial_capital
  max_drawdown_rate = 0.0
  returns = []
  for _, pnl in sorted(daily_pnl.items()):
    prev_equity = equity
    equity += float(pnl)
    if prev_equity > 0:
      returns.append(float(pnl) / prev_equity)
    peak = max(peak, equity)
    if peak > 0:
      max_drawdown_rate = max(max_drawdown_rate, (peak - equity) / peak)

  n = len(returns)
  total_return = equity / initial_capital - 1
  annual_return = (1 + total_return) ** (_TRADING_DAYS_PER_YEAR / n) - 1 if n > 0 and equity > 0 else 0.0
  avg_return = sum(returns) / n if n else 0.0
  daily_risk_free_rate = (1 + _ANNUAL_RISK_FREE_RATE) ** (1 / _TRADING_DAYS_PER_YEAR) - 1
  avg_excess_return = sum(value - daily_risk_free_rate for value in returns) / n if n else 0.0
  std_return = _sample_std(returns, avg_return)
  downside = [min(value, 0.0) for value in returns]
  downside_std = _std(downside, 0.0)
  annual_volatility = std_return * math.sqrt(_TRADING_DAYS_PER_YEAR)
  annual_sharpe = avg_excess_return / std_return * math.sqrt(_TRADING_DAYS_PER_YEAR) if std_return > 0 else 0.0
  sortino = avg_return / downside_std * math.sqrt(_TRADING_DAYS_PER_YEAR) if downside_std > 0 else 0.0

  return {
    "annualReturn": round(annual_return, 6),
    "annualVolatility": round(annual_volatility, 6),
    "annualSharpe": round(annual_sharpe, 4),
    "sortino": round(sortino, 4),
    "maxDrawdownRate": round(max_drawdown_rate, 6),
  }


def _std(values: list[float], avg: float) -> float:
  if not values:
    return 0.0
  return (sum((value - avg) ** 2 for value in values) / len(values)) ** 0.5


def _sample_std(values: list[float], avg: float) -> float:
  if len(values) <= 1:
    return 0.0
  return (sum((value - avg) ** 2 for value in values) / (len(values) - 1)) ** 0.5


def _build_midcap_equity(
  initial_capital: float,
  daily_nav: dict[str, float],
) -> list[dict[str, Any]]:
  points = []
  previous = float(initial_capital)
  for date, nav in sorted(daily_nav.items()):
    value = float(nav)
    points.append({
      "date": str(date),
      "pnl": round(value - previous, 2),
      "value": round(value, 2),
    })
    previous = value
  return points


def _apply_midcap_strategy_params(config: dict[str, Any], raw_params: Any) -> None:
  """把前端策略参数覆盖到中盘策略配置。"""
  if not isinstance(raw_params, dict):
    return

  max_positions = _read_int(raw_params, "maxPositions", config["position"]["max_positions"])
  max_total_pct = _read_float(raw_params, "maxTotalPct", config["position"]["max_total_pct"])
  single_weight = _read_optional_float(raw_params, "singleWeight")

  config["universe"]["min_market_cap"] = _read_float(
    raw_params, "minMarketCap", config["universe"]["min_market_cap"]
  )
  config["universe"]["max_market_cap"] = _read_float(
    raw_params, "maxMarketCap", config["universe"]["max_market_cap"]
  )
  config["fundamental"]["min_revenue_yoy"] = _read_float(
    raw_params, "minRevenueYoy", config["fundamental"]["min_revenue_yoy"]
  )
  config["fundamental"]["min_net_profit_yoy"] = _read_float(
    raw_params, "minNetProfitYoy", config["fundamental"]["min_net_profit_yoy"]
  )
  config["scoring"]["max_candidates"] = _read_int(
    raw_params, "maxCandidates", config["scoring"]["max_candidates"]
  )
  config["position"]["max_positions"] = max_positions
  config["position"]["single_weight"] = (
    single_weight if single_weight is not None else 1 / max(max_positions, 1)
  )
  config["position"]["max_total_pct"] = max_total_pct
  config["rebalance"]["rebalance_months"] = _read_months(
    raw_params, "rebalanceMonths", config["rebalance"]["rebalance_months"]
  )
  config["rebalance"]["min_holding_days"] = _read_int(
    raw_params, "minHoldingDays", config["rebalance"]["min_holding_days"]
  )
  config["exit"]["stop_loss"] = _read_float(
    raw_params, "stopLoss", config["exit"]["stop_loss"]
  )
  config["risk"]["index_trend_filter"] = _read_bool(
    raw_params, "indexTrendFilter", config["risk"]["index_trend_filter"]
  )


def _read_optional_float(params: dict[str, Any], key: str) -> float | None:
  value = params.get(key)
  if value in (None, ""):
    return None
  try:
    parsed = float(value)
  except (TypeError, ValueError):
    return None
  return parsed if math.isfinite(parsed) else None


def _read_float(params: dict[str, Any], key: str, fallback: float) -> float:
  value = _read_optional_float(params, key)
  return fallback if value is None else value


def _read_int(params: dict[str, Any], key: str, fallback: int) -> int:
  value = params.get(key)
  if value in (None, ""):
    return fallback
  try:
    parsed = int(float(value))
  except (TypeError, ValueError):
    return fallback
  return max(parsed, 0)


def _read_bool(params: dict[str, Any], key: str, fallback: bool) -> bool:
  value = params.get(key)
  if isinstance(value, bool):
    return value
  if isinstance(value, str):
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
      return True
    if normalized in {"false", "0", "no", "n"}:
      return False
  return fallback


def _read_months(params: dict[str, Any], key: str, fallback: list[int]) -> list[int]:
  value = params.get(key)
  if value in (None, ""):
    return fallback
  if isinstance(value, list):
    try:
      months = [int(item) for item in value]
    except (TypeError, ValueError):
      return fallback
    months = [month for month in months if 1 <= month <= 12]
    return months or fallback
  try:
    months = [
      int(item.strip())
      for item in str(value).replace("，", ",").split(",")
      if item.strip()
    ]
  except ValueError:
    return fallback
  months = [month for month in months if 1 <= month <= 12]
  return months or fallback


def _build_midcap_summary(
  initial_capital: float,
  daily_nav: dict[str, float],
  raw_summary: dict[str, Any],
  trades: list[dict[str, Any]],
) -> dict[str, Any]:
  nav_series = pd.Series(daily_nav).sort_index()
  final_equity = float(nav_series.iloc[-1]) if len(nav_series) else float(initial_capital)
  total_pnl = final_equity - float(initial_capital)
  returns = nav_series.pct_change().dropna() if len(nav_series) else pd.Series(dtype=float)
  daily_win_rate = float((returns > 0).mean()) if len(returns) else 0.0
  peak = nav_series.cummax() if len(nav_series) else pd.Series(dtype=float)
  drawdown_amount = (peak - nav_series).max() if len(nav_series) else 0.0

  sell_trades = [t for t in trades if t["side"] == "SELL"]
  winning = [t for t in sell_trades if float(t.get("pnl", 0)) > 0]
  losing = [t for t in sell_trades if float(t.get("pnl", 0)) <= 0]
  avg_win = sum(float(t["pnl"]) for t in winning) / len(winning) if winning else 0.0
  avg_loss = sum(float(t["pnl"]) for t in losing) / len(losing) if losing else 0.0

  return {
    "totalPnl": round(total_pnl, 2),
    "totalReturn": round(total_pnl / float(initial_capital), 6) if initial_capital > 0 else 0.0,
    "maxDrawdown": round(float(drawdown_amount), 2),
    "maxDrawdownRate": round(abs(float(raw_summary.get("max_drawdown_pct", 0))) / 100, 6),
    "annualVolatility": round(float(raw_summary.get("annual_vol_pct", 0)) / 100, 6),
    "winRate": round(float(raw_summary.get("win_rate_pct", 0)) / 100, 6),
    "dailyWinRate": round(daily_win_rate, 6),
    "tradeCount": int(raw_summary.get("total_trades", len(trades))),
    "orderCount": int(raw_summary.get("total_trades", len(trades))),
    "fillCount": int(raw_summary.get("total_trades", len(trades))),
    "winningTrades": len(winning),
    "losingTrades": len(losing),
    "avgWin": round(float(avg_win), 2),
    "avgLoss": round(float(avg_loss), 2),
    "profitLossRatio": float(raw_summary.get("profit_factor", 0.0)),
    "finalEquity": round(final_equity, 2),
    "annualReturn": round(float(raw_summary.get("annual_return_pct", 0)) / 100, 6),
    "annualSharpe": float(raw_summary.get("sharpe_ratio", 0.0)),
    "sortino": 0.0,
  }


def _build_midcap_daily_holdings(
  initial_capital: float,
  daily_nav: dict[str, float],
  positions_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
  if not daily_nav:
    return []

  positions_df = pd.DataFrame(positions_log)
  if positions_df.empty:
    market_value_by_date: dict[str, float] = {}
    qty_by_date: dict[str, int] = {}
  else:
    positions_df["trade_date"] = positions_df["trade_date"].astype(str)
    market_value_by_date = positions_df.groupby("trade_date")["market_value"].sum().to_dict()
    qty_by_date = positions_df.groupby("trade_date")["stock_code"].nunique().astype(int).to_dict()

  snapshots = []
  previous_total = float(initial_capital)
  for date, nav in sorted(daily_nav.items()):
    date_text = str(date)
    total_equity = float(nav)
    long_value = float(market_value_by_date.get(date_text, 0.0))
    long_qty = int(qty_by_date.get(date_text, 0))
    cash = total_equity - long_value
    snapshots.append({
      "date": date_text,
      "close": 0.0,
      "longQty": long_qty,
      "shortQty": 0,
      "cash": round(cash, 2),
      "longMarketValue": round(long_value, 2),
      "shortMarketValue": 0.0,
      "marketValue": round(long_value, 2),
      "totalEquity": round(total_equity, 2),
      "dailyPnl": round(total_equity - previous_total, 2),
    })
    previous_total = total_equity
  return snapshots


def _build_benchmark_equity(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  initial_capital: float,
) -> tuple[list[dict[str, Any]], list[str]]:
  try:
    bars = _load_bars(symbol, timeframe, start_date, end_date)
  except (FileNotFoundError, ValueError) as e:
    return [], [f"对比标的 {symbol} {timeframe} 数据不可用：{e}"]

  daily_close = (
    bars.assign(date=bars["time"].str[:8])
    .groupby("date", as_index=False)
    .tail(1)[["date", "close"]]
    .reset_index(drop=True)
  )
  if daily_close.empty:
    return [], [f"对比标的 {symbol} 在区间内无可用收盘价"]

  base_close = float(daily_close.iloc[0]["close"])
  if base_close <= 0:
    return [], [f"对比标的 {symbol} 首日收盘价无效"]

  points = []
  for row in daily_close.itertuples(index=False):
    close = float(row.close)
    return_rate = close / base_close - 1
    points.append({
      "date": str(row.date),
      "close": round(close, 4),
      "value": round(initial_capital * (1 + return_rate), 2),
      "returnRate": round(return_rate, 6),
    })
  return points, []


def _build_midcap_benchmark_equity(
  indices: pd.DataFrame,
  benchmark_symbol: str,
  initial_capital: float,
) -> list[dict[str, Any]]:
  if indices.empty or not benchmark_symbol:
    return []
  bench = indices[indices["index_code"] == benchmark_symbol].copy()
  if bench.empty:
    return []
  bench = bench.sort_values("trade_date")
  base_close = float(bench.iloc[0]["index_close"])
  if base_close <= 0:
    return []
  points = []
  for row in bench.itertuples(index=False):
    close = float(getattr(row, "index_close"))
    return_rate = close / base_close - 1
    points.append({
      "date": str(getattr(row, "trade_date")),
      "close": round(close, 4),
      "value": round(initial_capital * (1 + return_rate), 2),
      "returnRate": round(return_rate, 6),
    })
  return points


def _compute_benchmark_return(points: list[dict[str, Any]]) -> float:
  if not points:
    return 0.0
  return round(float(points[-1]["returnRate"]), 6)


def _normalize_trade(trade: dict[str, Any]) -> dict[str, Any]:
  direction = str(trade.get("direction", ""))
  side = "BUY" if "BUY" in direction else "SELL" if "SELL" in direction else ""
  return {
    "time": str(trade.get("time", "")),
    "direction": direction,
    "side": side,
    "anchor": float(trade.get("anchor", 0) or 0),
    "price": float(trade.get("price", 0) or 0),
    "quantity": int(trade.get("qty", 0) or 0),
    "pnl": float(trade.get("pnl", 0) or 0),
    "note": str(trade.get("note", "")),
  }


def _normalize_midcap_trade(trade: dict[str, Any]) -> dict[str, Any]:
  direction = str(trade.get("direction", ""))
  side = "BUY" if direction == "BUY" else "SELL" if direction == "SELL" else direction
  code = str(trade.get("stock_code", ""))
  name = str(trade.get("stock_name", ""))
  reason = str(trade.get("reason", ""))
  note_parts = [part for part in [code, name, reason] if part]
  return {
    "time": str(trade.get("trade_date", "")),
    "direction": direction,
    "side": side,
    "anchor": 0.0,
    "price": float(trade.get("price", 0) or 0),
    "quantity": int(trade.get("shares", 0) or 0),
    "pnl": float(trade.get("pnl", 0) or 0),
    "note": " · ".join(note_parts),
  }


def _build_warnings(payload: dict[str, Any]) -> list[str]:
  return []
