# -*- coding: utf-8 -*-
"""
趋势网格信号可视化工作台 — FastAPI 后端
只读 API：instruments / dates / chart / strategies / health
写 API：data/sync（通过 subprocess 调同步脚本，避免直接 import xtquant）
"""

import sys
import os
import ast
from datetime import datetime, timedelta
import hashlib
import re

_viewer_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _viewer_dir not in sys.path:
  sys.path.insert(0, _viewer_dir)

from fastapi import Body, FastAPI, Query, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import settings
from backend import schemas
from backend.strategies.catalog import get_strategy_source_snapshot
from backend.strategies.scripted import validate_script_code
from backend.services import (
  backtest_runner,
  data_manager,
  eastmoney_client,
  market_data,
  data_repository,
  data_sync_tasks,
  state_store,
  strategy_adapters,
)
from backend.services import auth
from backend.services.user_context import get_current_user, reset_current_user, set_current_user

app = FastAPI(title="Trend Grid Viewer", version="0.1.0")

app.add_middleware(
  CORSMiddleware,
  allow_origins=[
    "http://localhost:5174", "http://127.0.0.1:5174",
    "http://localhost:5175", "http://127.0.0.1:5175",
    "http://localhost:5176", "http://127.0.0.1:5176",
    "http://localhost:5177", "http://127.0.0.1:5177",
  ],
  allow_credentials=True,
  allow_methods=["GET", "POST", "PUT", "DELETE"],
  allow_headers=["*"],
)


@app.middleware("http")
async def authenticate_api_request(request: Request, call_next):
  """所有业务 API 必须携带有效会话，用户身份由上下文供仓储层使用。"""
  path = request.url.path
  if request.method == "OPTIONS" or not path.startswith("/api/") or path in {
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
  }:
    return await call_next(request)
  user = auth.get_user_by_token(request.cookies.get(auth.SESSION_COOKIE))
  if not user:
    return JSONResponse(status_code=401, content={"detail": "登录已失效，请重新登录"})
  token = set_current_user(user)
  try:
    return await call_next(request)
  finally:
    reset_current_user(token)


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
  """通过东财补齐分钟缓存，兼容旧数据同步 API。"""
  end_date = request.get("endDate") or datetime.now().strftime("%Y%m%d")
  start_date = request.get("startDate") or (datetime.now() - timedelta(days=7)).strftime("%Y%m%d")
  try:
    result = market_data.ensure_cached(
      request["symbol"], request.get("timeframe", "5m"), start_date, end_date,
    )
  except market_data.MarketDataError as error:
    return {
      "status": "error",
      "symbol": request["symbol"],
      "timeframe": request.get("timeframe", "5m"),
      "message": str(error),
      "new_rows": 0,
      "new_dates": [],
    }
  return {
    "status": "ok", "symbol": request["symbol"], "timeframe": request.get("timeframe", "5m"),
    "from": start_date, "to": end_date, "new_rows": result["rowCount"],
    "new_dates": result["fetchedDates"], "cacheStatus": result["status"],
    "message": "数据库缓存命中" if result["status"] == "cache_hit" else "东财分钟行情已缓存",
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
  if payload.get("status") not in {"draft", "published", "disabled"}:
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

  if not request.get("configId"):
    raise HTTPException(422, "回测必须选择已保存的策略方案")
  if request.get("runDraft") and not get_current_user().is_admin:
    raise HTTPException(403, "只有管理员可以试跑未发布草稿")
  if request.get("configId") and not request.get("configVersionId"):
    latest_version = state_store.get_latest_strategy_config_version(str(request["configId"]))
    if latest_version and not request.get("runDraft"):
      request["configVersionId"] = latest_version["id"]
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
  else:
    if not get_current_user().is_admin:
      raise HTTPException(422, "策略方案尚未发布，普通用户不能回测")
    try:
      draft_config = state_store.get_strategy_config(str(request["configId"]))
    except KeyError as error:
      raise HTTPException(404, "策略方案不存在") from error
    script_id = draft_config.get("scriptId")
    if not script_id:
      raise HTTPException(422, "草稿方案必须绑定策略脚本")
    script = state_store.get_strategy_script(str(script_id))
    request["strategy"] = draft_config["strategy"]
    request["scriptId"] = script_id
    request["draftScriptSnapshot"] = {
      "id": script["id"], "name": script["name"], "code": script["code"], "entryType": script["entryType"],
    }
    request["versionScriptSnapshot"] = request["draftScriptSnapshot"]
    request["executionKind"] = "draft_test"

  if request["strategy"] != "user_script":
    raise HTTPException(422, "当前仅支持数据库中的用户脚本策略")

  try:
    for symbol in {request["symbol"], request.get("benchmarkSymbol") or request["symbol"]}:
      cache_result = market_data.ensure_cached(
        symbol, request["timeframe"], request["startDate"], request["endDate"],
      )
      state_store.log_data_sync_run({
        "symbol": symbol, "timeframe": request["timeframe"],
        "startDate": request["startDate"], "endDate": request["endDate"], "mode": "backtest_auto",
      }, {"status": "ok", "cacheStatus": cache_result["status"], "new_rows": cache_result["rowCount"],
          "new_dates": cache_result["fetchedDates"], "message": "回测自动补齐分钟行情"})
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


@app.post("/api/auth/login")
def login(payload: schemas.LoginRequest, request: Request, response: Response):
  """校验用户名密码并签发仅浏览器可见的 HttpOnly 会话 Cookie。"""
  try:
    token, user = auth.authenticate(
      payload.username,
      payload.password,
      request.client.host if request.client else None,
      request.headers.get("user-agent"),
    )
  except auth.AuthenticationError as error:
    raise HTTPException(401, str(error)) from error
  response.set_cookie(
    auth.SESSION_COOKIE,
    token,
    max_age=int(auth.SESSION_LIFETIME.total_seconds()),
    httponly=True,
    samesite="lax",
    secure=settings.AUTH_COOKIE_SECURE,
    path="/",
  )
  return {"user": {
    "id": user.id,
    "username": user.username,
    "displayName": user.display_name,
    "role": user.role,
  }}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
  """撤销服务端会话并清除浏览器 Cookie。"""
  auth.revoke_session(request.cookies.get(auth.SESSION_COOKIE))
  response.delete_cookie(auth.SESSION_COOKIE, path="/")
  return {"ok": True}


@app.get("/api/auth/me")
def current_user(request: Request):
  user = auth.get_user_by_token(request.cookies.get(auth.SESSION_COOKIE))
  if not user:
    raise HTTPException(401, "登录已失效，请重新登录")
  return {"user": {
    "id": user.id,
    "username": user.username,
    "displayName": user.display_name,
    "role": user.role,
  }}


def _require_admin(request: Request) -> None:
  user = auth.get_user_by_token(request.cookies.get(auth.SESSION_COOKIE))
  if not user or not user.is_admin:
    raise HTTPException(403, "仅管理员可执行此操作")


@app.get("/api/admin/users")
def admin_users(request: Request):
  _require_admin(request)
  return {"users": auth.list_users()}


@app.post("/api/admin/users")
def create_user(payload: schemas.UserCreate, request: Request):
  _require_admin(request)
  try:
    return {"user": auth.create_user(payload.username, payload.displayName, payload.password, payload.role)}
  except auth.UserManagementError as error:
    raise HTTPException(409, str(error)) from error


@app.put("/api/admin/users/{user_id}")
def update_user(user_id: str, payload: schemas.UserUpdate, request: Request):
  _require_admin(request)
  try:
    return {"user": auth.update_user(user_id, payload.displayName, payload.role, payload.status)}
  except KeyError as error:
    raise HTTPException(404, "用户不存在") from error
  except auth.UserManagementError as error:
    raise HTTPException(422, str(error)) from error


@app.post("/api/admin/users/{user_id}/reset-password")
def reset_user_password(user_id: str, payload: schemas.PasswordReset, request: Request):
  _require_admin(request)
  try:
    auth.reset_password(user_id, payload.password)
  except KeyError as error:
    raise HTTPException(404, "用户不存在") from error
  return {"ok": True}


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
  return {"instruments": data_repository.list_instruments()}


@app.get("/api/market-instruments/search")
def search_market_instruments(q: str = Query("", max_length=64), limit: int = Query(20, ge=1, le=50)):
  return {"instruments": state_store.search_market_instruments(q, limit)}


@app.post("/api/admin/market-instruments/refresh")
def refresh_market_instruments(request: Request):
  _require_admin(request)
  try:
    count = state_store.upsert_market_instruments(eastmoney_client.fetch_a_share_instruments())
  except eastmoney_client.EastmoneyError as error:
    raise HTTPException(502, str(error)) from error
  return {"count": count}


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
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
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
  if any(re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", item) is None for item in requested):
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
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
  timeframe: str = Query("5m"),
):
  if not settings.validate_timeframe(timeframe):
    raise HTTPException(422, f"不支持的周期: {timeframe}")
  items = data_repository.list_dates(symbol, timeframe)
  return {"symbol": symbol, "timeframe": timeframe, "dates": items}


@app.get("/api/chart")
def chart(
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
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
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
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
