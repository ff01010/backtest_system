# -*- coding: utf-8 -*-
"""
趋势网格信号可视化工作台 — FastAPI 后端
只读 API：instruments / dates / chart / strategies / health
写 API：策略、账号和回测记录管理
"""

import sys
import os
import ast
import hashlib
import logging
import re
from typing import Any

_viewer_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _viewer_dir not in sys.path:
  sys.path.insert(0, _viewer_dir)

from fastapi import FastAPI, Query, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import settings
from backend.date_codec import TradingDateError, format_legacy_date, normalize_date_range
from backend import schemas
from backend.strategies.catalog import get_strategy_source_snapshot
from backend.strategies.scripted import validate_script_code
from backend.services import (
  backtest_runner,
  market_data,
  qmt_instrument_directory,
  data_repository,
  state_store,
  strategy_adapters,
)
from backend.services import auth
from backend.services.backtest_errors import error_detail, error_summary
from backend.services.user_context import get_current_user, reset_current_user, set_current_user

app = FastAPI(title="Trend Grid Viewer", version="0.1.0")
logger = logging.getLogger(__name__)


def _backtest_error_summary(error: Exception, run_id: str | None) -> dict[str, Any]:
  return error_summary(error, run_id)


def _backtest_failure_response(error: Exception, run_id: str | None) -> HTTPException:
  detail = error_detail(error, run_id).to_dict()
  status_code = 404 if detail["code"] == "data.unavailable" else 422
  return HTTPException(status_code, {"errorDetail": detail})


@app.on_event("startup")
def sync_qmt_instrument_directory() -> None:
  """用已预置的 QMT 快照刷新 PostgreSQL 搜索目录。"""
  try:
    count = qmt_instrument_directory.sync_market_instruments()
  except Exception as error:
    logger.warning("QMT 证券目录同步跳过: %s", error)
  else:
    logger.info("QMT 证券目录已同步 %s 条", count)

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


def _compact_trade_time(value: object) -> str:
  """统一回测成交时间，兼容历史记录中的 ISO 与紧凑格式。"""
  digits = re.sub(r"\D", "", str(value or ""))
  return digits[:12] if len(digits) >= 12 else ""


def _backtest_chart_trades(
  run: dict[str, Any],
  date: str,
) -> tuple[list[dict[str, Any]], int]:
  """筛选指定交易日的成交记录，不猜测缺失的历史字段。"""
  trade_date_key = format_legacy_date(date)
  result = run.get("resultJson") or {}
  trades: list[dict[str, Any]] = []
  unsupported_count = 0
  for index, trade in enumerate(result.get("trades") or []):
    execution_time = _compact_trade_time(trade.get("executionTime") or trade.get("time"))
    signal_time = _compact_trade_time(trade.get("signalTime") or trade.get("time"))
    action = str(trade.get("action") or "").lower()
    side = str(trade.get("side") or "").upper()
    price = trade.get("price")
    quantity = trade.get("quantity")
    if action not in {"buy_open", "sell_close", "sell_open", "buy_close"} and side in {"BUY", "SELL"}:
      action = side.lower()
    if (
      not execution_time
      or execution_time[:8] != trade_date_key
      or action not in {"buy_open", "sell_close", "sell_open", "buy_close", "buy", "sell"}
      or side not in {"BUY", "SELL"}
      or not isinstance(price, (int, float))
      or not isinstance(quantity, int)
    ):
      if execution_time[:8] == date:
        unsupported_count += 1
      continue
    trades.append({
      "id": f"{run['id']}-{index}",
      "executionTime": execution_time,
      "signalTime": signal_time or execution_time,
      "action": action,
      "side": side,
      "price": float(price),
      "quantity": quantity,
      "note": str(trade.get("note") or ""),
    })
  return trades, unsupported_count


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


def _validate_saved_params(request: dict, config: dict) -> None:
  """策略语义参数必须来自已保存的当前策略。"""
  saved_params = config.get("paramsJson", {}).get("params", {})
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
    raise HTTPException(422, f"策略内容已修改，请先保存策略：{', '.join(sorted(mismatches))}")


def _prepare_saved_strategy_snapshot(request: dict) -> None:
  try:
    config = state_store.get_strategy_config(str(request["configId"]))
  except KeyError as error:
    raise HTTPException(404, "策略不存在") from error
  script_id = config.get("scriptId")
  if not script_id:
    raise HTTPException(422, "策略必须先保存代码")
  try:
    script = state_store.get_strategy_script(str(script_id))
  except KeyError as error:
    raise HTTPException(422, "策略保存的代码不存在") from error
  mismatches = []
  for key, saved_value, request_value in (
    ("标的", config["symbol"], request["symbol"]),
    ("周期", config["timeframe"], request["timeframe"]),
    ("策略", config["strategy"], request["strategy"]),
    ("基准标的", config.get("benchmarkSymbol") or config["symbol"], request.get("benchmarkSymbol") or request["symbol"]),
  ):
    if saved_value != request_value:
      mismatches.append(key)
  if config.get("accountId") != request.get("accountId"):
    mismatches.append("回测账户")
  if mismatches:
    raise HTTPException(422, f"策略内容已修改，请先保存策略：{', '.join(mismatches)}")
  _validate_saved_params(request, config)
  request["strategy"] = config["strategy"]
  request["scriptId"] = script["id"]
  request["configVersionId"] = None
  request["runDraft"] = False
  request["versionScriptSnapshot"] = {
    "id": script["id"], "name": script["name"], "description": script["description"],
    "code": script["code"], "entryType": script["entryType"],
    "codeHash": hashlib.sha256(script["code"].encode("utf-8")).hexdigest(),
  }
  request["savedScriptSnapshot"] = request["versionScriptSnapshot"]
  request["savedConfigSnapshot"] = {
    "name": config["name"], "strategy": config["strategy"], "mode": config["mode"],
    "symbol": config["symbol"], "timeframe": config["timeframe"],
    "benchmarkSymbol": config.get("benchmarkSymbol"), "accountId": config.get("accountId"),
    "params": config.get("paramsJson", {}).get("params", {}),
    "description": script["description"], "runParams": request.get("strategyParams", {}),
  }


def _validate_backtest_request(request: dict) -> None:
  """校验回测请求并确认使用已保存的当前策略。"""
  if not settings.validate_symbol(request["symbol"]):
    raise HTTPException(422, f"非法标的: {request['symbol']}")
  if request.get("benchmarkSymbol") and not settings.validate_symbol(request["benchmarkSymbol"]):
    raise HTTPException(422, f"非法对比标的: {request['benchmarkSymbol']}")
  try:
    request["startDate"], request["endDate"] = normalize_date_range(request["startDate"], request["endDate"])
  except TradingDateError as error:
    raise HTTPException(422, str(error)) from error
  fill_model = request.get("fillModel", "bar_close")
  if fill_model == "l2_quote":
    if not settings.validate_l2_backtest_timeframe(request["timeframe"]):
      raise HTTPException(422, f"L2 回测不支持 {request['timeframe']}")
  elif not settings.validate_backtest_timeframe(request["timeframe"]):
    raise HTTPException(422, f"当前回测引擎不支持 {request['timeframe']}")
  if not strategy_adapters.has_strategy(request["strategy"]):
    raise HTTPException(422, f"不支持的策略: {request['strategy']}")

  if not request.get("configId"):
    raise HTTPException(422, "回测必须先选择并保存策略")
  _prepare_saved_strategy_snapshot(request)

  if request["strategy"] != "user_script":
    raise HTTPException(422, "当前仅支持数据库中的用户脚本策略")

  if fill_model == "l2_quote":
    request["fillModel"] = "l2_quote"
    return

  try:
    market_snapshots = {}
    for symbol in {request["symbol"], request.get("benchmarkSymbol") or request["symbol"]}:
      market_data.ensure_cached(
        symbol, request["timeframe"], request["startDate"], request["endDate"],
      )
      market_snapshots[symbol] = market_data.snapshot(
        symbol, request["timeframe"], request["startDate"], request["endDate"],
      )
    request["marketDataSnapshot"] = market_snapshots
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
  _validate_strategy_config_timeframe(request)
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
  _validate_strategy_config_timeframe(request)
  try:
    config = state_store.update_strategy_config(config_id, request)
    return {"config": config}
  except KeyError:
    raise HTTPException(404, f"策略配置不存在: {config_id}")
  except state_store.ConflictError:
    raise HTTPException(409, f"策略配置名称已存在: {payload.name}")
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 更新策略配置失败: {e}")


def _validate_strategy_config_timeframe(request: dict[str, Any]) -> None:
  """策略方案按保存的成交模式使用对应的周期白名单。"""
  params_json = request.get("paramsJson")
  params = params_json.get("params", {}) if isinstance(params_json, dict) else {}
  fill_model = params.get("fillModel", "bar_close") if isinstance(params, dict) else "bar_close"
  timeframe = str(request["timeframe"])
  supported = (
    settings.validate_l2_backtest_timeframe(timeframe)
    if fill_model == "l2_quote"
    else settings.validate_backtest_timeframe(timeframe)
  )
  if not supported:
    engine_name = "L2 回测" if fill_model == "l2_quote" else "当前回测引擎"
    raise HTTPException(422, f"{engine_name}不支持 {timeframe}")


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


@app.get("/api/backtest/runs/{run_id}/chart", response_model=schemas.BacktestChartResponse)
def backtest_run_chart(
  run_id: str,
  date: str = Query(...),
  timeframe: str | None = Query(default=None),
):
  """返回当前用户指定回测的一日行情与已成交买卖点。"""
  try:
    run = state_store.get_backtest_run(run_id)
  except KeyError as error:
    raise HTTPException(404, "回测记录不存在") from error

  try:
    date, _ = normalize_date_range(date, date)
  except TradingDateError as error:
    raise HTTPException(422, str(error)) from error
  selected_timeframe = timeframe or str(run["timeframe"])
  if not settings.validate_timeframe(selected_timeframe):
    raise HTTPException(422, f"不支持的周期: {selected_timeframe}")
  try:
    bars = data_repository.load_chart_bars(run["symbol"], date, selected_timeframe)
  except ValueError as error:
    raise HTTPException(404, str(error)) from error

  trades, unsupported_count = _backtest_chart_trades(run, date)
  if unsupported_count:
    raise HTTPException(422, "该历史回测缺少可用于交易点复盘的成交字段")
  return {
    "runId": run["id"],
    "displayNo": run["displayNo"],
    "symbol": run["symbol"],
    "timeframe": selected_timeframe,
    "date": date,
    "bars": bars,
    "trades": trades,
  }


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
  if bool(start_date) != bool(end_date):
    raise HTTPException(422, "开始日期和结束日期必须同时提供")
  if start_date and end_date:
    try:
      start_date, end_date = normalize_date_range(start_date, end_date)
    except TradingDateError as error:
      raise HTTPException(422, str(error)) from error
  return {"timeframes": data_repository.get_backtest_timeframes(symbol, start_date, end_date)}


@app.get("/api/backtest/timeframes/batch")
def backtest_timeframes_batch(
  symbols: str = Query(...),
  start_date: str = Query(..., alias="startDate"),
  end_date: str = Query(..., alias="endDate"),
):
  try:
    start_date, end_date = normalize_date_range(start_date, end_date)
  except TradingDateError as error:
    raise HTTPException(422, str(error)) from error
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

  if request.get("fillModel") == "l2_quote":
    try:
      run_id, display_no = state_store.create_l2_backtest_job(request)
      return {
        "runId": run_id,
        "displayNo": display_no,
        "configId": request.get("configId"),
        "configVersionId": request.get("configVersionId"),
        "scriptId": request.get("scriptId"),
        "status": "queued",
        "fillModel": "l2_quote",
        "message": "L2 回测已进入队列，等待 l2-runner 执行",
      }
    except ValueError as error:
      raise HTTPException(422, str(error)) from error

  run_id = None
  try:
    run_id, display_no = state_store.create_backtest_run(request)
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
      "displayNo": display_no,
      "configId": request.get("configId"),
      "configVersionId": request.get("configVersionId"),
      "scriptId": request.get("scriptId"),
      **result,
    }
  except FileNotFoundError as e:
    logger.exception("同步回测任务失败: %s", run_id)
    if run_id is not None:
      summary = _backtest_error_summary(e, run_id)
      state_store.finish_backtest_run(run_id, "error", summary, "")
    raise _backtest_failure_response(e, run_id)
  except ValueError as e:
    logger.exception("同步回测任务失败: %s", run_id)
    if run_id is not None:
      summary = _backtest_error_summary(e, run_id)
      state_store.finish_backtest_run(run_id, "error", summary, "")
    raise _backtest_failure_response(e, run_id)
  except Exception as e:
    logger.exception("同步回测任务失败: %s", run_id)
    if run_id is not None:
      summary = _backtest_error_summary(e, run_id)
      state_store.finish_backtest_run(run_id, "error", summary, "")
    raise _backtest_failure_response(e, run_id)


@app.post("/api/backtest/runs/{run_id}/cancel")
def cancel_l2_backtest_run(run_id: str):
  """请求取消尚未结束的 L2 回测；Runner 在当前块结束后清理会话。"""
  if not state_store.request_l2_backtest_cancel(run_id):
    raise HTTPException(404, "未找到可取消的 L2 回测任务")
  return {"runId": run_id, "status": "cancelling"}


@app.post("/api/backtest/live")
def create_live_run(payload: schemas.BacktestRunRequest):
  request = _model_dump(payload)
  _validate_backtest_request(request)

  run_id, display_no = state_store.create_backtest_run(request, status="queued")
  return {
    "runId": run_id,
    "displayNo": display_no,
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
  date: str = Query(...),
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
  return {"status": market_data.status(symbol, timeframe)}


if __name__ == "__main__":
  import uvicorn
  uvicorn.run(app, host="0.0.0.0", port=8000)
