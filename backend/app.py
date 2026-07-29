# -*- coding: utf-8 -*-
"""
趋势网格信号可视化工作台 — FastAPI 后端
只读 API：instruments / dates / chart / strategies / health
写 API：data/sync（通过 subprocess 调同步脚本，避免直接 import xtquant）
"""

import sys
import os
import ast
import hashlib
import re

_viewer_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _viewer_dir not in sys.path:
  sys.path.insert(0, _viewer_dir)

from fastapi import Body, FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend import settings
from backend import schemas
from backend.strategies.catalog import get_strategy_source_snapshot
from backend.strategies.scripted import validate_script_code
from backend.services import (
  backtest_runner,
  data_manager,
  data_repository,
  data_sync_tasks,
  state_store,
  strategy_adapters,
)

app = FastAPI(title="Trend Grid Viewer", version="0.1.0")

app.add_middleware(
  CORSMiddleware,
  allow_origins=[
    "http://localhost:5174", "http://127.0.0.1:5174",
    "http://localhost:5175", "http://127.0.0.1:5175",
    "http://localhost:5176", "http://127.0.0.1:5176",
    "http://localhost:5177", "http://127.0.0.1:5177",
  ],
  allow_methods=["GET", "POST", "PUT", "DELETE"],
  allow_headers=["*"],
)


def _model_dump(model):
  if hasattr(model, "model_dump"):
    return model.model_dump()
  return model.dict()


def _merge_instruments(
  scanned: list[dict],
  stored: list[dict],
) -> list[dict]:
  """数据库自定义标的和文件扫描结果合并，扫描状态优先。"""
  merged = {item["symbol"]: item for item in stored}
  for item in scanned:
    previous = merged.get(item["symbol"], {})
    merged[item["symbol"]] = {
      **previous,
      **item,
      "name": previous.get("name"),
      "market": previous.get("market") or item["symbol"].split(".")[1],
      "enabled": previous.get("enabled", True),
      "note": previous.get("note"),
    }
  return sorted(merged.values(), key=lambda item: item["symbol"])


def _run_sync_script(request: dict) -> dict:
  import json
  import subprocess
  import time

  sync_script = os.path.join(
    os.path.dirname(_viewer_dir), "scripts", "sync_kline.py",
  )
  cmd = [
    sys.executable,
    sync_script,
    request["symbol"],
    "--timeframe",
    request.get("timeframe", "5m"),
    "--mode",
    request.get("mode", "incremental"),
    "--json",
  ]
  if request.get("startDate"):
    cmd.extend(["--start-date", request["startDate"]])
  if request.get("endDate"):
    cmd.extend(["--end-date", request["endDate"]])

  started = time.perf_counter()
  try:
    proc = subprocess.run(
      cmd,
      capture_output=True, text=True, timeout=180,
    )
  except subprocess.TimeoutExpired:
    return {
      "status": "error",
      "symbol": request["symbol"],
      "timeframe": request.get("timeframe", "5m"),
      "message": "同步超时，请检查 QMT 是否运行",
      "new_rows": 0,
      "new_dates": [],
    }
  output_lines = proc.stdout.strip().splitlines()
  if not output_lines:
    return {
      "status": "error",
      "symbol": request["symbol"],
      "timeframe": request.get("timeframe", "5m"),
      "message": proc.stderr or "同步脚本无输出",
    }
  try:
    result = json.loads(output_lines[-1])
    result["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    return result
  except json.JSONDecodeError:
    return {
      "status": "error",
      "symbol": request["symbol"],
      "timeframe": request.get("timeframe", "5m"),
      "message": f"同步脚本输出异常: {proc.stderr or proc.stdout[-500:]}",
    }


def _validate_sync_request(request: dict) -> None:
  if not settings.validate_symbol(request["symbol"]):
    raise HTTPException(422, f"非法标的: {request['symbol']}")
  if not settings.validate_timeframe(request.get("timeframe", "5m")):
    raise HTTPException(422, f"不支持的周期: {request.get('timeframe')}")
  start_date = request.get("startDate")
  end_date = request.get("endDate")
  if start_date and not settings.validate_date(start_date):
    raise HTTPException(422, f"非法开始日期: {start_date}")
  if end_date and not settings.validate_date(end_date):
    raise HTTPException(422, f"非法结束日期: {end_date}")
  if start_date and end_date and start_date > end_date:
    raise HTTPException(422, "开始日期不能晚于结束日期")


def _sync_many(request: dict) -> list[dict]:
  results = []
  for symbol in request["symbols"]:
    item = {
      "symbol": symbol,
      "timeframe": request.get("timeframe", "5m"),
      "startDate": request.get("startDate"),
      "endDate": request.get("endDate"),
      "mode": request.get("mode", "incremental"),
    }
    try:
      _validate_sync_request(item)
      result = _run_sync_script(item)
      state_store.log_data_sync_run(item, result)
    except Exception as e:
      result = {
        "status": "error",
        "symbol": symbol,
        "timeframe": item["timeframe"],
        "message": str(e),
        "new_rows": 0,
        "new_dates": [],
      }
      state_store.log_data_sync_run(item, result)
    results.append(result)
  return results


def _sync_gap_dates(request: dict) -> list[dict]:
  results = []
  for date in request["dates"]:
    item = {
      "symbol": request["symbol"],
      "timeframe": request.get("timeframe", "5m"),
      "startDate": date,
      "endDate": date,
      "mode": "range",
    }
    try:
      _validate_sync_request(item)
      result = _run_sync_script(item)
      state_store.log_data_sync_run(item, result)
    except Exception as e:
      result = {
        "status": "error",
        "symbol": request["symbol"],
        "timeframe": item["timeframe"],
        "from": date,
        "to": date,
        "message": str(e),
        "new_rows": 0,
        "new_dates": [],
      }
      state_store.log_data_sync_run(item, result)
    results.append(result)
  return results


def _validate_strategy_script(payload: dict) -> None:
  try:
    if payload.get("entryType", "on_bar") == "on_bar":
      validate_script_code(payload.get("code", ""))
    else:
      ast.parse(payload.get("code", ""))
  except ValueError as error:
    raise HTTPException(422, str(error)) from error
  except SyntaxError as error:
    raise HTTPException(422, f"Python 语法错误: 第 {error.lineno} 行 {error.msg}")
  if payload.get("status") not in {"draft", "enabled", "disabled"}:
    raise HTTPException(422, f"非法脚本状态: {payload.get('status')}")


_EXPERIMENT_PARAM_KEYS = {
  "startDate",
  "endDate",
  "initialCapital",
  "commissionRate",
  "slippage",
}


def _validate_versioned_script(version: dict) -> None:
  """优先使用版本内的脚本快照，兼容旧版本的代码指纹校验。"""
  expected_hash = version.get("paramsJson", {}).get("scriptCodeHash")
  script_snapshot = version.get("paramsJson", {}).get("scriptSnapshot")
  if isinstance(script_snapshot, dict) and isinstance(script_snapshot.get("code"), str):
    snapshot_hash = hashlib.sha256(script_snapshot["code"].encode("utf-8")).hexdigest()
    if expected_hash and snapshot_hash != expected_hash:
      raise HTTPException(422, "策略方案保存的代码快照已损坏")
    return
  script_id = version.get("scriptId")
  if not expected_hash or script_id is None:
    return
  try:
    script = state_store.get_strategy_script(str(script_id))
  except KeyError as error:
    raise HTTPException(422, "策略方案绑定的脚本不存在") from error
  actual_hash = hashlib.sha256(script["code"].encode("utf-8")).hexdigest()
  if actual_hash != expected_hash:
    raise HTTPException(422, "策略脚本已修改，请保存策略方案生成新版本后再回测")


def _validate_versioned_params(request: dict, version: dict) -> None:
  """只锁定策略语义参数，日期、资金和成本属于本次实验输入。"""
  saved_params = version.get("paramsJson", {}).get("params", {})
  if not isinstance(saved_params, dict):
    return
  request_params = request.get("strategyParams", {})
  if not isinstance(request_params, dict):
    raise HTTPException(422, "策略参数格式非法")
  mismatches = [
    key
    for key, value in saved_params.items()
    if key not in _EXPERIMENT_PARAM_KEYS and request_params.get(key) != value
  ]
  if mismatches:
    raise HTTPException(422, f"回测参数必须与方案版本一致：{', '.join(sorted(mismatches))}")


def _validate_backtest_request(request: dict) -> None:
  """校验回测请求并确认策略方案版本与实际执行内容一致。"""
  if not settings.validate_symbol(request["symbol"]):
    raise HTTPException(422, f"非法标的: {request['symbol']}")
  if request.get("benchmarkSymbol") and not settings.validate_symbol(request["benchmarkSymbol"]):
    raise HTTPException(422, f"非法对比标的: {request['benchmarkSymbol']}")
  if not settings.validate_date(request["startDate"]) or not settings.validate_date(request["endDate"]):
    raise HTTPException(422, "日期格式必须为 YYYYMMDD")
  if request["startDate"] > request["endDate"]:
    raise HTTPException(422, "开始日期不能晚于结束日期")
  if not settings.validate_backtest_timeframe(request["timeframe"]):
    raise HTTPException(422, f"当前回测引擎不支持 {request['timeframe']}")
  if not strategy_adapters.has_strategy(request["strategy"]):
    raise HTTPException(422, f"不支持的策略: {request['strategy']}")

  if request.get("configId") and not request.get("configVersionId"):
    latest_version = state_store.get_latest_strategy_config_version(str(request["configId"]))
    request["configVersionId"] = latest_version["id"] if latest_version else None
  if request.get("configVersionId"):
    try:
      version = state_store.get_strategy_config_version(str(request["configVersionId"]))
    except KeyError as error:
      raise HTTPException(404, "策略方案版本不存在") from error
    if version["timeframe"] != request["timeframe"]:
      raise HTTPException(422, f"回测周期必须与方案版本一致：{version['timeframe']}")
    if version["strategy"] != request["strategy"]:
      raise HTTPException(422, f"回测策略必须与方案版本一致：{version['strategy']}")
    if version.get("scriptId") != request.get("scriptId"):
      raise HTTPException(422, "回测脚本必须与方案版本一致")
    _validate_versioned_params(request, version)
    _validate_versioned_script(version)
    script_snapshot = version.get("paramsJson", {}).get("scriptSnapshot")
    if isinstance(script_snapshot, dict) and isinstance(script_snapshot.get("code"), str):
      request["versionScriptSnapshot"] = script_snapshot

  try:
    data_repository.validate_backtest_coverage(
      request["symbol"], request["timeframe"], request["startDate"], request["endDate"],
    )
    benchmark_symbol = request.get("benchmarkSymbol") or request["symbol"]
    data_repository.validate_backtest_coverage(
      benchmark_symbol, request["timeframe"], request["startDate"], request["endDate"],
    )
  except ValueError as error:
    raise HTTPException(422, str(error)) from error


@app.on_event("startup")
def startup():
  state_store.ensure_schema()


@app.get("/api/health")
def health():
  return {"status": "ok"}


@app.get("/api/strategies")
def strategies():
  return {"strategies": strategy_adapters.list_strategy_ids()}


@app.get("/api/strategies/{strategy_id}/parameters")
def strategy_parameters(strategy_id: str):
  if not strategy_adapters.has_strategy(strategy_id):
    raise HTTPException(404, f"策略不存在: {strategy_id}")
  return {
    "strategy": strategy_id,
    "parameters": strategy_adapters.get_parameter_schema(strategy_id),
  }


@app.get("/api/strategies/{strategy_id}/source")
def strategy_source(strategy_id: str):
  try:
    return get_strategy_source_snapshot(strategy_id)
  except ValueError as exc:
    raise HTTPException(404, str(exc))


@app.get("/api/signal-files")
def signal_files():
  """列出 SIGNAL_ROOT 及其子目录下所有可用的信号 CSV 文件"""
  import os as _os
  import time as _time
  root = settings.SIGNAL_ROOT
  files = []
  if _os.path.isdir(root):
    for dirpath, _dirnames, filenames in _os.walk(root):
      for fname in filenames:
        if fname.endswith(".csv"):
          fpath = _os.path.join(dirpath, fname)
          rel = _os.path.relpath(fpath, root)
          try:
            mtime = _os.path.getmtime(fpath)
            mtime_str = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(mtime))
          except OSError:
            mtime_str = ""
          files.append({"name": rel.replace("\\", "/"), "mtime": mtime_str})
  files.sort(key=lambda f: f["mtime"], reverse=True)
  return {"files": files}


@app.get("/api/instruments")
def instruments():
  items = _merge_instruments(
    data_repository.list_instruments(),
    state_store.list_instruments(),
  )
  return {"instruments": items}


@app.post("/api/instruments")
def create_instrument(payload: schemas.InstrumentCreate):
  if not settings.validate_symbol(payload.symbol):
    raise HTTPException(422, f"非法标的: {payload.symbol}")
  try:
    item = state_store.upsert_instrument(_model_dump(payload))
    return {"instrument": item}
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 写入标的失败: {e}")


@app.put("/api/instruments/{symbol}")
def update_instrument(
  symbol: str,
  payload: schemas.InstrumentUpdate,
):
  if not settings.validate_symbol(symbol):
    raise HTTPException(422, f"非法标的: {symbol}")
  try:
    item = state_store.update_instrument(symbol, _model_dump(payload))
    return {"instrument": item}
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 更新标的失败: {e}")


@app.delete("/api/instruments/{symbol}")
def delete_instrument(symbol: str):
  if not settings.validate_symbol(symbol):
    raise HTTPException(422, f"非法标的: {symbol}")
  deleted = state_store.delete_instrument(symbol)
  return {"deleted": deleted}


@app.get("/api/accounts")
def accounts():
  return {"accounts": state_store.list_accounts()}


@app.post("/api/accounts")
def create_account(payload: schemas.BacktestAccountCreate):
  try:
    account = state_store.create_account(_model_dump(payload))
    return {"account": account}
  except state_store.ConflictError:
    raise HTTPException(409, f"账号名称已存在: {payload.name}")
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 写入账号失败: {e}")


@app.put("/api/accounts/{account_id}")
def update_account(account_id: int, payload: schemas.BacktestAccountUpdate):
  try:
    account = state_store.update_account(account_id, _model_dump(payload))
    return {"account": account}
  except KeyError:
    raise HTTPException(404, f"账号不存在: {account_id}")
  except state_store.ConflictError:
    raise HTTPException(409, f"账号名称已存在: {payload.name}")
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 更新账号失败: {e}")


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int):
  deleted = state_store.delete_account(account_id)
  return {"deleted": deleted}


@app.get("/api/strategy-scripts")
def strategy_scripts():
  return {"scripts": state_store.list_strategy_scripts()}


@app.post("/api/strategy-scripts")
def create_strategy_script(payload: schemas.StrategyScriptCreate):
  request = _model_dump(payload)
  _validate_strategy_script(request)
  try:
    script = state_store.create_strategy_script(request)
    return {"script": script}
  except state_store.ConflictError:
    raise HTTPException(409, f"策略脚本名称已存在: {payload.name}")
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 写入策略脚本失败: {e}")


@app.put("/api/strategy-scripts/{script_id}")
def update_strategy_script(script_id: str, payload: schemas.StrategyScriptUpdate):
  request = _model_dump(payload)
  _validate_strategy_script(request)
  try:
    script = state_store.update_strategy_script(script_id, request)
    return {"script": script}
  except KeyError:
    raise HTTPException(404, f"策略脚本不存在: {script_id}")
  except state_store.ConflictError:
    raise HTTPException(409, f"策略脚本名称已存在: {payload.name}")
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 更新策略脚本失败: {e}")


@app.delete("/api/strategy-scripts/{script_id}")
def delete_strategy_script(script_id: str):
  deleted = state_store.delete_strategy_script(script_id)
  return {"deleted": deleted}


@app.get("/api/strategy-configs")
def strategy_configs():
  return {"configs": state_store.list_strategy_configs()}


@app.post("/api/strategy-configs")
def create_strategy_config(payload: schemas.StrategyConfigCreate):
  request = _model_dump(payload)
  if not settings.validate_symbol(request["symbol"]):
    raise HTTPException(422, f"非法标的: {request['symbol']}")
  if request.get("benchmarkSymbol") and not settings.validate_symbol(request["benchmarkSymbol"]):
    raise HTTPException(422, f"非法买入持有基准: {request['benchmarkSymbol']}")
  if not settings.validate_backtest_timeframe(request["timeframe"]):
    raise HTTPException(422, f"当前回测引擎不支持 {request['timeframe']}")
  try:
    config = state_store.create_strategy_config(request)
    return {"config": config}
  except state_store.ConflictError:
    raise HTTPException(409, f"策略配置名称已存在: {payload.name}")
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 写入策略配置失败: {e}")


@app.put("/api/strategy-configs/{config_id}")
def update_strategy_config(config_id: str, payload: schemas.StrategyConfigUpdate):
  request = _model_dump(payload)
  if not settings.validate_symbol(request["symbol"]):
    raise HTTPException(422, f"非法标的: {request['symbol']}")
  if request.get("benchmarkSymbol") and not settings.validate_symbol(request["benchmarkSymbol"]):
    raise HTTPException(422, f"非法买入持有基准: {request['benchmarkSymbol']}")
  if not settings.validate_backtest_timeframe(request["timeframe"]):
    raise HTTPException(422, f"当前回测引擎不支持 {request['timeframe']}")
  try:
    config = state_store.update_strategy_config(config_id, request)
    return {"config": config}
  except KeyError:
    raise HTTPException(404, f"策略配置不存在: {config_id}")
  except state_store.ConflictError:
    raise HTTPException(409, f"策略配置名称已存在: {payload.name}")
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 更新策略配置失败: {e}")


@app.post("/api/strategy-configs/{config_id}/publish")
def publish_strategy_config(config_id: str):
  try:
    version, created = state_store.publish_strategy_config(config_id)
    return {"version": version, "created": created}
  except KeyError as error:
    raise HTTPException(404, "策略配置不存在") from error
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"PostgreSQL 发布策略版本失败: {error}") from error


@app.delete("/api/strategy-configs/{config_id}")
def delete_strategy_config(config_id: str):
  deleted = state_store.delete_strategy_config(config_id)
  return {"deleted": deleted}


@app.get("/api/strategy-configs/{config_id}/versions")
def strategy_config_versions(config_id: str):
  try:
    state_store.get_strategy_config(config_id)
  except KeyError:
    raise HTTPException(404, f"策略配置不存在: {config_id}")
  return {"versions": state_store.list_strategy_config_versions(config_id)}


@app.get("/api/backtest/runs")
def backtest_runs(
  limit: int = Query(30, ge=1, le=100),
  config_id: str | None = Query(default=None, alias="configId"),
):
  return {"runs": state_store.list_backtest_runs(limit, config_id)}


@app.get("/api/backtest/runs/{run_id}")
def backtest_run_detail(run_id: str):
  try:
    return {"run": state_store.get_backtest_run(run_id)}
  except KeyError:
    raise HTTPException(404, f"回测记录不存在: {run_id}")


@app.put("/api/backtest/runs/{run_id}")
def update_backtest_run_meta(run_id: str, payload: schemas.BacktestRunMetaUpdate):
  try:
    run = state_store.update_backtest_run_meta(run_id, payload.note.strip(), payload.tags)
    return {"run": run}
  except KeyError:
    raise HTTPException(404, f"回测记录不存在: {run_id}")


@app.get("/api/backtest/timeframes")
def backtest_timeframes(
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ)$"),
  start_date: str | None = Query(default=None, alias="startDate"),
  end_date: str | None = Query(default=None, alias="endDate"),
):
  if start_date and not settings.validate_date(start_date):
    raise HTTPException(422, f"非法开始日期: {start_date}")
  if end_date and not settings.validate_date(end_date):
    raise HTTPException(422, f"非法结束日期: {end_date}")
  if start_date and end_date and start_date > end_date:
    raise HTTPException(422, "开始日期不能晚于结束日期")
  return {"timeframes": data_repository.get_backtest_timeframes(symbol, start_date, end_date)}


@app.get("/api/backtest/timeframes/batch")
def backtest_timeframes_batch(
  symbols: str = Query(...),
  start_date: str = Query(..., alias="startDate"),
  end_date: str = Query(..., alias="endDate"),
):
  if not settings.validate_date(start_date):
    raise HTTPException(422, f"非法开始日期: {start_date}")
  if not settings.validate_date(end_date):
    raise HTTPException(422, f"非法结束日期: {end_date}")
  if start_date > end_date:
    raise HTTPException(422, "开始日期不能晚于结束日期")
  requested = list(dict.fromkeys(item.strip() for item in symbols.split(",") if item.strip()))
  if not requested or len(requested) > 100:
    raise HTTPException(422, "标的数量必须在 1 至 100 之间")
  if any(re.fullmatch(r"\d{6}\.(SH|SZ)", item) is None for item in requested):
    raise HTTPException(422, "标的格式必须为 6 位代码.交易所")
  return {
    "timeframesBySymbol": {
      item: data_repository.get_backtest_timeframes(item, start_date, end_date)
      for item in requested
    },
  }


@app.post("/api/backtest/run")
def run_backtest(payload: schemas.BacktestRunRequest):
  request = _model_dump(payload)
  _validate_backtest_request(request)

  run_id = None
  try:
    run_id = state_store.create_backtest_run(request)
    result = backtest_runner.run_backtest(request)
    state_store.finish_backtest_run(
      run_id,
      "ok",
      result["summary"],
      result["outputDir"],
      result,
    )
    return {
      "runId": run_id,
      "configId": request.get("configId"),
      "configVersionId": request.get("configVersionId"),
      "scriptId": request.get("scriptId"),
      **result,
    }
  except FileNotFoundError as e:
    if run_id is not None:
      state_store.finish_backtest_run(run_id, "error", {"error": str(e)}, "")
    raise HTTPException(404, str(e))
  except ValueError as e:
    if run_id is not None:
      state_store.finish_backtest_run(run_id, "error", {"error": str(e)}, "")
    raise HTTPException(422, str(e))
  except Exception as e:
    if run_id is not None:
      state_store.finish_backtest_run(run_id, "error", {"error": str(e)}, "")
    raise HTTPException(500, f"回测失败: {e}")


@app.post("/api/backtest/live")
def create_live_run(payload: schemas.BacktestRunRequest):
  request = _model_dump(payload)
  _validate_backtest_request(request)

  run_id = state_store.create_backtest_run(request, status="queued")
  return {
    "runId": run_id,
    "configId": request.get("configId"),
    "configVersionId": request.get("configVersionId"),
    "scriptId": request.get("scriptId"),
    "status": "queued",
    "message": "运行模式已记录，后续实盘执行入口待接入",
  }


@app.get("/api/dates")
def dates(
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ)$"),
  timeframe: str = Query("5m"),
):
  if not settings.validate_timeframe(timeframe):
    raise HTTPException(422, f"不支持的周期: {timeframe}")
  items = data_repository.list_dates(symbol, timeframe)
  return {"symbol": symbol, "timeframe": timeframe, "dates": items}


@app.get("/api/chart")
def chart(
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ)$"),
  date: str = Query(..., pattern=r"^\d{8}$"),
  timeframe: str = Query("5m"),
  strategy: str = Query("trend_grid"),
  signal_file: str = Query(""),
):
  if not settings.validate_timeframe(timeframe):
    raise HTTPException(422, f"不支持的周期: {timeframe}")

  result = data_repository.load_chart_data(
    symbol, date, timeframe, strategy,
    signal_file=signal_file or None,
  )
  if "error" in result:
    raise HTTPException(404, result["error"])
  return result


@app.get("/api/data/status")
def data_status(
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ)$"),
  timeframe: str = Query("5m"),
):
  if not settings.validate_timeframe(timeframe):
    raise HTTPException(422, f"不支持的周期: {timeframe}")
  return {"status": data_manager.get_status(symbol, timeframe)}


@app.get("/api/data/sync-runs")
def data_sync_runs(limit: int = Query(20, ge=1, le=100)):
  return {"runs": state_store.list_data_sync_runs(limit)}


@app.post("/api/data/sync")
def sync_kline(
  payload: schemas.DataSyncRequest | None = Body(None),
  symbol: str | None = Query(None),
):
  """同步指定标的 K 线；兼容旧 query 参数和新 JSON 请求体。"""
  request = _model_dump(payload) if payload else {
    "symbol": symbol,
    "timeframe": "5m",
    "startDate": None,
    "endDate": None,
    "mode": "incremental",
  }
  if not request.get("symbol"):
    raise HTTPException(422, "缺少标的")
  _validate_sync_request(request)
  result = _run_sync_script(request)
  state_store.log_data_sync_run(request, result)
  if result["status"] == "error":
    raise HTTPException(500, result.get("message", "同步失败"))
  return result


@app.post("/api/data/sync/batch")
def sync_kline_batch(payload: schemas.DataSyncBatchRequest):
  request = _model_dump(payload)
  return {"results": _sync_many(request)}


@app.post("/api/data/sync/start")
def start_sync_task(payload: schemas.DataSyncBatchRequest):
  request = _model_dump(payload)
  if not request["symbols"]:
    raise HTTPException(422, "缺少标的")
  for symbol in request["symbols"]:
    _validate_sync_request({
      "symbol": symbol,
      "timeframe": request.get("timeframe", "5m"),
      "startDate": request.get("startDate"),
      "endDate": request.get("endDate"),
      "mode": request.get("mode", "incremental"),
    })
  return {"task": data_sync_tasks.start_task(request, _sync_many)}


@app.get("/api/data/sync/tasks/{task_id}")
def get_sync_task(task_id: str):
  task = data_sync_tasks.get_task(task_id)
  if task is None:
    raise HTTPException(404, f"同步任务不存在: {task_id}")
  return {"task": task}


@app.post("/api/data/refill")
def refill_data_gaps(payload: schemas.DataRefillRequest):
  request = _model_dump(payload)
  if not settings.validate_symbol(request["symbol"]):
    raise HTTPException(422, f"非法标的: {request['symbol']}")
  if not settings.validate_timeframe(request.get("timeframe", "5m")):
    raise HTTPException(422, f"不支持的周期: {request.get('timeframe')}")
  if not request["dates"]:
    raise HTTPException(422, "缺少补齐日期")
  for date in request["dates"]:
    if not settings.validate_date(date):
      raise HTTPException(422, f"非法日期: {date}")
  return {"task": data_sync_tasks.start_task(request, _sync_gap_dates)}


if __name__ == "__main__":
  import uvicorn
  uvicorn.run(app, host="0.0.0.0", port=8000)
