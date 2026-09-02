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
import hmac
import json
import logging
import re
from typing import Any
from datetime import datetime, timezone
from uuid import uuid4

_viewer_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _viewer_dir not in sys.path:
  sys.path.insert(0, _viewer_dir)

_STRATEGY_GUIDE_PATH = os.path.join(_viewer_dir, "docs", "玄龄回测系统使用手册.md")

from fastapi import FastAPI, Query, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend import settings
from backend.execution import execution_algorithm_snapshot, validate_execution_request
from backend.date_codec import TradingDateError, format_legacy_date, normalize_date_range
from backend import schemas
from backend.time_utils import format_epoch_as_shanghai
from backend.strategies.catalog import get_strategy_source_snapshot
from backend.strategies.scripted import warmup_trading_days_for_script, validate_script_code
from backend.services import (
  backtest_preflight,
  backtest_runner,
  market_data,
  qmt_instrument_directory,
  data_repository,
  research_signal_snapshot,
  state_store,
  strategy_adapters,
)
from backend.services import auth
from backend.services.user_context import CurrentUser
from backend.services.backtest_errors import error_detail, error_summary
from backend.services.system_error_log import system_error_log_record
from backend.services.user_context import get_current_user, reset_current_user, set_current_user

app = FastAPI(title="Trend Grid Viewer", version="0.1.0")
logger = logging.getLogger(__name__)


def _backtest_error_summary(error: Exception, run_id: str | None) -> dict[str, Any]:
  return error_summary(error, run_id)


def _backtest_failure_response(error: Exception, run_id: str | None) -> HTTPException:
  detail = error_detail(error, run_id).to_dict()
  status_code = 404 if detail["code"] == "data.unavailable" else 422
  return HTTPException(status_code, {"errorDetail": detail})


def _sync_system_error_record(error: Exception, run_id: str) -> dict[str, Any] | None:
  record = system_error_log_record(error, "sync_runner", "sync.backtest_execute", run_id)
  return record.to_dict() if record else None


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
  """按 Cookie 或 API Key 认证业务接口，并将用户写入当前请求上下文。"""
  if request.url.path.startswith("/api/v1/"):
    request.state.request_id = f"req_{uuid4().hex}"
    if not settings.PUBLIC_API_ENABLED:
      return _public_api_error(request, 404, "api.disabled", "开放 API 未启用")
    authorization = request.headers.get("authorization", "")
    token_value = authorization[7:] if authorization.lower().startswith("bearer ") else None
    principal = auth.get_api_key_principal(token_value)
    if principal is None:
      return _public_api_error(request, 401, "api_key_invalid", "API Key 无效、已撤销或已过期")
    user, api_key = principal
    request.state.api_key = api_key
    token = set_current_user(user)
    try:
      return await call_next(request)
    finally:
      reset_current_user(token)
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


def _public_api_error(
  request: Request,
  status_code: int,
  code: str,
  message: str,
  retryable: bool = False,
) -> JSONResponse:
  return JSONResponse(
    status_code=status_code,
    content={
      "error": {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": getattr(request.state, "request_id", None),
      },
    },
  )


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, error: HTTPException) -> JSONResponse:
  if request.url.path.startswith("/api/v1/"):
    detail = error.detail if isinstance(error.detail, str) else "请求无法完成"
    code = "backtest.validation_failed" if error.status_code == 422 else "request.failed"
    if error.status_code == 403:
      code = "api_scope_denied"
    elif error.status_code == 404:
      code = "resource.not_found"
    elif error.status_code == 409:
      code = "idempotency.conflict"
    elif error.status_code == 429:
      code = "rate_limit.exceeded"
    return _public_api_error(request, error.status_code, code, detail, error.status_code >= 500)
  return JSONResponse(status_code=error.status_code, content={"detail": error.detail})


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
  if request.url.path.startswith("/api/v1/"):
    return _public_api_error(request, 422, "backtest.validation_failed", "请求参数不符合接口约束")
  return JSONResponse(status_code=422, content={"detail": error.errors()})


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, error: Exception) -> JSONResponse:
  logger.exception("未处理接口异常: %s", request.url.path, exc_info=error)
  if request.url.path.startswith("/api/v1/"):
    return _public_api_error(request, 503, "backtest.unavailable", "服务暂时不可用，请稍后重试", True)
  return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


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
      if execution_time[:8] == trade_date_key:
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
  "executionAlgorithm",
  "twapDurationSeconds",
  "twapIntervalSeconds",
  "twapMinChildQuantity",
}


def _validate_saved_params(request: dict, config: dict) -> None:
  """策略语义参数必须来自已保存的当前策略。"""
  saved_params = config.get("paramsJson", {}).get("params", {})
  if not isinstance(saved_params, dict):
    return
  request_params = request.get("strategyParams", {})
  if not isinstance(request_params, dict):
    raise HTTPException(422, "策略参数格式非法")
  mismatches: list[str] = []
  for key, value in saved_params.items():
    if key != "warmupTradingDays" and key not in _EXPERIMENT_PARAM_KEYS and request_params.get(key) != value:
      mismatches.append(key)
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
  try:
    warmup_days = warmup_trading_days_for_script(str(script["code"]))
  except ValueError as error:
    raise HTTPException(422, str(error)) from error
  if request.get("fillModel") == "l2_quote" and warmup_days != 0:
    raise HTTPException(422, "L2 盘口回测不支持策略声明预热交易日")
  _validate_saved_params(request, config)
  request["strategy"] = config["strategy"]
  request["scriptId"] = script["id"]
  request["warmupTradingDays"] = warmup_days
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
    "warmupTradingDays": warmup_days,
    "execution": execution_algorithm_snapshot(
      str(request["executionAlgorithm"]), dict(request.get("executionParameters") or {}),
    ),
  }


def _validate_backtest_request(request: dict) -> None:
  """校验回测请求，并为可选研究信号创建不可变快照。"""

  if not settings.validate_symbol(request["symbol"]):
    raise HTTPException(422, f"非法标的: {request['symbol']}")
  if request.get("benchmarkSymbol") and not settings.validate_symbol(request["benchmarkSymbol"]):
    raise HTTPException(422, f"非法对比标的: {request['benchmarkSymbol']}")
  try:
    request["startDate"], request["endDate"] = normalize_date_range(request["startDate"], request["endDate"])
  except TradingDateError as error:
    raise HTTPException(422, str(error)) from error
  fill_model = request.get("fillModel", "bar_next_open")
  try:
    request.update(validate_execution_request(
      str(fill_model), request.get("executionAlgorithm"), request.get("executionParameters"),
    ))
  except ValueError as error:
    raise HTTPException(422, str(error)) from error
  if not settings.validate_l2_backtest_timeframe(request["timeframe"]):
    raise HTTPException(422, f"当前回测引擎不支持 {request['timeframe']}")
  if request["strategy"] == "research_signal":
    raise HTTPException(422, "研究信号必须作为已保存策略的可选输入，不能单独运行")
  if not strategy_adapters.has_strategy(request["strategy"]):
    raise HTTPException(422, f"不支持的策略: {request['strategy']}")
  if not request.get("configId"):
    raise HTTPException(422, "回测必须先选择并保存策略")
  _prepare_saved_strategy_snapshot(request)
  if request["strategy"] != "user_script":
    raise HTTPException(422, "当前仅支持数据库中的用户脚本策略")
  if fill_model == "l2_quote":
    if request.get("signalDatasetId"):
      raise HTTPException(422, "研究信号联动首期仅支持标准 Bar 回放")
    request["fillModel"] = "l2_quote"
  else:
    request["fillModel"] = "bar_next_open"
  request["feeProfile"] = settings.default_fee_profile()
  request["commissionRate"] = request["feeProfile"]["commissionRate"]
  _require_preflight(request)
  if request.get("signalDatasetId"):
    _prepare_research_signal_attachment(request)


def _prepare_research_signal_attachment(request: dict[str, Any]) -> None:
  dataset_id = request.get("signalDatasetId")
  if not isinstance(dataset_id, str) or not dataset_id:
    raise HTTPException(422, "研究信号数据集无效")
  try:
    bars, warmup_bar_count, _ = backtest_runner._load_user_script_bars(request)
    bar_times = [str(row.time) for row in bars.iloc[warmup_bar_count:].itertuples(index=False)]
    snapshot = research_signal_snapshot.freeze_signal_snapshot(
      dataset_id, str(state_store.current_user_id()), request["symbol"], request["startDate"],
      request["endDate"], request["timeframe"], bar_times,
    )
  except research_signal_snapshot.ResearchSignalSnapshotError as error:
    raise HTTPException(422, str(error)) from error
  request["researchSignalSnapshot"] = snapshot
  saved_snapshot = dict(request.get("savedConfigSnapshot") or {})
  saved_snapshot["researchSignal"] = {
    "datasetId": snapshot["datasetId"],
    "datasetVersion": snapshot["manifest"].get("version"),
    "snapshotHash": snapshot["snapshotHash"],
    "sourceTimeframe": snapshot["sourceTimeframe"],
    "backtestTimeframe": snapshot["timeframe"],
    "rowCount": snapshot["rowCount"],
    "alignment": snapshot["alignment"],
  }
  request["savedConfigSnapshot"] = saved_snapshot

def _require_preflight(request: dict[str, Any]) -> None:
  try:
    snapshot = backtest_preflight.require_available(
      str(request["symbol"]), str(request["timeframe"]), str(request["startDate"]),
      str(request["endDate"]), str(request.get("fillModel", "bar_next_open")),
      int(request.get("warmupTradingDays", 0)),
    )
  except backtest_preflight.PreflightError as error:
    raise HTTPException(422, {"errorDetail": _preflight_error_detail(error.result)}) from error
  request["marketDataSnapshot"] = snapshot


def _preflight_error_detail(result: dict[str, Any]) -> dict[str, Any]:
  codes = list(result.get("missingDataTypes") or [])
  return {"category": "data", "code": codes[0] if codes else "data.unavailable",
          "message": result.get("message") or "当前日期范围数据不完整。",
          "hint": "请调整日期范围或补齐数据后重试。", "retryable": False,
          "taskId": None, "missingDates": result.get("missingDates") or []}


_PUBLIC_OVERRIDE_MAP = {
  "start_date": "startDate",
  "end_date": "endDate",
  "initial_capital": "initialCapital",
  "commission_rate": "commissionRate",
  "slippage": "slippage",
}


def _require_public_scope(request: Request, scope: str) -> auth.ApiKeyPrincipal:
  api_key = getattr(request.state, "api_key", None)
  if not isinstance(api_key, auth.ApiKeyPrincipal):
    raise HTTPException(401, "API Key 无效")
  if scope not in api_key.scopes:
    raise HTTPException(403, "当前 API Key 不具备所需权限")
  return api_key


def _build_public_backtest_request(payload: schemas.PublicBacktestCreateRequest) -> dict[str, Any]:
  """仅从已保存方案生成请求，开放接口只能覆盖少量运行参数。"""
  try:
    config = state_store.get_strategy_config(payload.config_id)
  except KeyError as error:
    raise HTTPException(404, "策略方案不存在") from error
  if not config.get("enabled") or not config.get("published", True):
    raise HTTPException(409, "策略方案未启用或尚未发布")
  params_json = config.get("paramsJson")
  saved_params = params_json.get("params", {}) if isinstance(params_json, dict) else {}
  if not isinstance(saved_params, dict):
    raise HTTPException(422, "策略方案缺少可运行参数")

  values = dict(saved_params)
  overrides = _model_dump(payload.overrides)
  for source_key, target_key in _PUBLIC_OVERRIDE_MAP.items():
    value = overrides.get(source_key)
    if value is not None:
      values[target_key] = value
  required_keys = {"startDate", "endDate", "initialCapital", "commissionRate", "slippage"}
  missing = sorted(key for key in required_keys if values.get(key) is None)
  if missing:
    raise HTTPException(422, f"策略方案缺少运行参数：{', '.join(missing)}")

  execution_algorithm = str(values.get("executionAlgorithm", "immediate"))
  execution_parameters: dict[str, Any] = {}
  if execution_algorithm == "twap":
    execution_parameters = {
      "durationSeconds": values.get("twapDurationSeconds"),
      "intervalSeconds": values.get("twapIntervalSeconds"),
      "minChildQuantity": values.get("twapMinChildQuantity"),
      "completionPolicy": "cancel_remaining",
    }
  request_model = schemas.BacktestRunRequest(
    configId=config["id"],
    scriptId=config.get("scriptId"),
    accountId=config.get("accountId"),
    account="default",
    mode="backtest",
    symbol=config["symbol"],
    benchmarkSymbol=config.get("benchmarkSymbol") or config["symbol"],
    strategy=config["strategy"],
    timeframe=config["timeframe"],
    startDate=values["startDate"],
    endDate=values["endDate"],
    initialCapital=values["initialCapital"],
    commissionRate=values["commissionRate"],
    slippage=values["slippage"],
    positionMode=str(values.get("positionMode", "full_available")),
    orderCash=values.get("orderCash"),
    orderPct=values.get("orderPct"),
    allowLong=values.get("allowLong", True),
    allowShort=values.get("allowShort", True),
    strategyCategory=values.get("strategyCategory"),
    holdingPolicy=values.get("holdingPolicy", "intraday"),
    fillModel=values.get("fillModel", "bar_next_open"),
    executionAlgorithm=execution_algorithm,
    executionParameters=execution_parameters,
    baseQuantity=values.get("baseQuantity", 0),
    baseCostPrice=values.get("baseCostPrice", 0),
    initialPositionEnabled=values.get("initialPositionEnabled", False),
    t0Enabled=values.get("t0Enabled", False),
    strategyParams={
      key: value for key, value in saved_params.items()
      if key not in _EXPERIMENT_PARAM_KEYS
    },
  )
  request = _model_dump(request_model)
  _validate_backtest_request(request)
  return request


def _public_request_hash(payload: schemas.PublicBacktestCreateRequest) -> str:
  canonical = json.dumps(_model_dump(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _public_run_payload(run: dict[str, Any]) -> dict[str, Any]:
  return {
    "run_id": run["id"],
    "display_no": run["displayNo"],
    "config_id": run.get("configId"),
    "symbol": run["symbol"],
    "timeframe": run["timeframe"],
    "fill_model": run["fillModel"],
    "status": run["status"],
    "start_date": run["startDate"],
    "end_date": run["endDate"],
    "summary": run.get("summaryJson") or {},
    "created_at": run["createdAt"],
    "finished_at": run["finishedAt"],
  }


@app.on_event("startup")
def startup():
  settings.validate_public_api_configuration()
  state_store.ensure_schema()


@app.get("/api/health")
def health():
  return {"status": "ok"}


@app.get("/api/research/signal-datasets")
def research_signal_datasets() -> dict[str, Any]:
  """供回测工作台选择当前用户可用的已发布研究信号。"""

  try:
    items = research_signal_snapshot.list_published_signal_datasets(str(state_store.current_user_id()))
  except research_signal_snapshot.ResearchSignalSnapshotError as error:
    raise HTTPException(422, str(error)) from error
  return {"items": items}


def _load_strategy_guide() -> str:
  """只读取固定策略手册，避免通用文件读取接口带来的路径穿越风险。"""
  try:
    with open(_STRATEGY_GUIDE_PATH, "r", encoding="utf-8") as guide_file:
      return guide_file.read()
  except OSError as error:
    raise HTTPException(503, "策略编写指南暂不可用，请稍后重试") from error


@app.get("/api/strategy-guide")
def strategy_guide():
  return {"title": "策略编写指南", "content": _load_strategy_guide()}


@app.get("/strategy-guide.md", include_in_schema=False)
def public_strategy_guide() -> Response:
  """提供固定的公开 Markdown 策略指南，不开放任意文件读取。"""
  return Response(content=_load_strategy_guide(), media_type="text/markdown")


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


@app.post("/api/research/authorization-codes")
def create_research_authorization_code(
  payload: schemas.ResearchAuthorizationCodeRequest,
):
  """为已登录用户签发跳转研究中心所需的一次性授权码。"""
  if not settings.RESEARCH_CENTER_CALLBACK_URL:
    raise HTTPException(503, "研究中心回调地址尚未配置")
  authorization_code = auth.create_research_authorization_code(
    get_current_user(), payload.state,
  )
  return {
    "authorizationCode": authorization_code,
    "expiresIn": settings.RESEARCH_AUTH_CODE_LIFETIME_SECONDS,
    "callbackUrl": settings.RESEARCH_CENTER_CALLBACK_URL,
  }


@app.post("/internal/v1/research-identity/exchange", include_in_schema=False)
def exchange_research_authorization_code(
  payload: schemas.ResearchAuthorizationCodeExchangeRequest,
  request: Request,
):
  """仅允许研究服务以内部凭据兑换浏览器持有的一次性授权码。"""
  expected_token = settings.RESEARCH_AUTH_SERVICE_TOKEN
  received_token = request.headers.get("X-Service-Token", "")
  if not expected_token or not hmac.compare_digest(received_token, expected_token):
    raise HTTPException(401, "研究服务身份无效")
  user = auth.exchange_research_authorization_code(
    payload.authorizationCode, payload.state,
  )
  if not user:
    raise HTTPException(401, "授权码无效、已使用或已过期")
  now = datetime.now(timezone.utc)
  issued_at = format_epoch_as_shanghai(now.timestamp())
  expires_at = format_epoch_as_shanghai(
    (now + auth.SESSION_LIFETIME).timestamp(),
  )
  return {
    "backtestUserId": user.id,
    "role": user.role,
    "active": True,
    "issuedAt": issued_at,
    "expiresAt": expires_at,
    "jti": uuid4().hex,
  }


@app.get("/api/api-keys")
def api_keys():
  return {"keys": auth.list_api_keys(get_current_user().id)}


@app.post("/api/api-keys")
def create_api_key(payload: schemas.ApiKeyCreateRequest):
  try:
    key, plain_text_key = auth.create_api_key(
      get_current_user().id,
      payload.name,
      list(payload.scopes),
      payload.expiresAt,
    )
  except auth.ApiKeyError as error:
    raise HTTPException(422, str(error)) from error
  return {"key": key, "plainTextKey": plain_text_key}


@app.delete("/api/api-keys/{key_id}")
def revoke_api_key(key_id: str):
  if not auth.revoke_api_key(get_current_user().id, key_id):
    raise HTTPException(404, "API Key 不存在")
  return {"revoked": True}


def _require_admin(request: Request) -> CurrentUser:
  user = auth.get_user_by_token(request.cookies.get(auth.SESSION_COOKIE))
  if not user or not user.is_admin:
    raise HTTPException(403, "仅管理员可执行此操作")
  return user


@app.get("/api/admin/users")
def admin_users(
  request: Request,
  page: int = Query(1, ge=1),
  page_size: int = Query(20, ge=1, le=100, alias="pageSize"),
):
  _require_admin(request)
  return auth.list_users(page, page_size)


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


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: str, request: Request):
  actor = _require_admin(request)
  try:
    return auth.delete_user(user_id, actor.id)
  except KeyError as error:
    raise HTTPException(404, "用户不存在") from error
  except auth.UserManagementError as error:
    raise HTTPException(422, str(error)) from error


@app.get("/api/admin/system-errors")
def admin_system_errors(
  request: Request,
  limit: int = Query(100, ge=1, le=200),
  user_id: str | None = Query(default=None, alias="userId"),
  run_id: str | None = Query(default=None, alias="runId"),
  error_code: str | None = Query(default=None, alias="errorCode"),
  from_time: str | None = Query(default=None, alias="fromTime"),
  to_time: str | None = Query(default=None, alias="toTime"),
):
  """返回管理员可见的安全系统错误摘要，不含原始堆栈和凭据。"""
  _require_admin(request)
  return {"items": state_store.list_system_error_logs(
    limit, user_id, run_id, error_code, from_time, to_time,
  )}


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
            mtime_str = format_epoch_as_shanghai(mtime)
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


@app.post("/api/complete-strategies")
def save_complete_strategy(payload: schemas.CompleteStrategySave):
  script_request = _model_dump(payload.script)
  config_request = _model_dump(payload.config)
  _validate_strategy_script(script_request)
  _apply_script_warmup_to_config(script_request, config_request)
  if not settings.validate_symbol(config_request["symbol"]):
    raise HTTPException(422, f"非法标的: {config_request['symbol']}")
  if config_request.get("benchmarkSymbol") and not settings.validate_symbol(config_request["benchmarkSymbol"]):
    raise HTTPException(422, f"非法买入持有基准: {config_request['benchmarkSymbol']}")
  _validate_strategy_config_timeframe(config_request)
  try:
    return state_store.save_complete_strategy(
      script_request, config_request, payload.scriptId, payload.configId,
    )
  except KeyError as error:
    raise HTTPException(404, "策略脚本或策略配置不存在") from error
  except state_store.ConflictError:
    raise HTTPException(409, f"策略名称已存在: {payload.script.name}")
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"PostgreSQL 保存完整策略失败: {error}") from error


def _apply_script_warmup_to_config(script: dict[str, Any], config: dict[str, Any]) -> None:
  """保存完整策略时，将代码声明写入与代码绑定的配置快照。"""
  params_json = config.get("paramsJson")
  params = params_json.get("params") if isinstance(params_json, dict) else None
  if not isinstance(params, dict):
    raise HTTPException(422, "完整策略必须包含回测参数")
  try:
    warmup_days = warmup_trading_days_for_script(str(script.get("code", "")))
  except ValueError as error:
    raise HTTPException(422, str(error)) from error
  if params.get("fillModel", "bar_next_open") == "l2_quote" and warmup_days != 0:
    raise HTTPException(422, "L2 盘口回测不支持策略声明预热交易日")
  params["warmupTradingDays"] = warmup_days


def _validate_strategy_config_timeframe(request: dict[str, Any]) -> None:
  """策略方案按保存的成交模式使用对应的周期白名单。"""
  params_json = request.get("paramsJson")
  params = params_json.get("params", {}) if isinstance(params_json, dict) else {}
  if isinstance(params, dict) and params.get("fillModel") == "bar_close":
    # 已废止的配置在再次保存时统一迁移为标准 Bar 回放，不能重新创建同 Bar 收盘成交任务。
    params["fillModel"] = "bar_next_open"
  fill_model = params.get("fillModel", "bar_next_open") if isinstance(params, dict) else "bar_next_open"
  timeframe = str(request["timeframe"])
  supported = (
    settings.validate_l2_backtest_timeframe(timeframe)
    if fill_model == "l2_quote"
    else settings.validate_backtest_timeframe(timeframe)
  )
  if not supported:
    engine_name = "L2 回测" if fill_model == "l2_quote" else "当前回测引擎"
    raise HTTPException(422, f"{engine_name}不支持 {timeframe}")
  if not isinstance(params, dict) or not params.get("startDate") or not params.get("endDate"):
    raise HTTPException(422, "完整策略必须包含回测开始日期和结束日期")
  try:
    backtest_preflight.require_available(
      str(request["symbol"]), timeframe, str(params["startDate"]), str(params["endDate"]), str(fill_model),
      0,
    )
  except backtest_preflight.PreflightError as error:
    raise HTTPException(422, {"errorDetail": _preflight_error_detail(error.result)}) from error


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
    "strategyCategory": run.get("strategyCategory") or "directional_holding",
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


@app.get("/api/backtest/preflight")
def backtest_preflight_check(
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
  timeframe: str = Query(...),
  start_date: str = Query(..., alias="startDate"),
  end_date: str = Query(..., alias="endDate"),
  fill_model: str = Query("bar_next_open", alias="fillModel"),
):
  try:
    start_date, end_date = normalize_date_range(start_date, end_date)
    return backtest_preflight.preview(
      symbol, start_date, end_date, fill_model, timeframe,
    )
  except ValueError as error:
    raise HTTPException(422, str(error)) from error


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


@app.post("/api/v1/backtests")
def public_create_backtest(payload: schemas.PublicBacktestCreateRequest, request: Request):
  api_key = _require_public_scope(request, "backtest:submit")
  idempotency_key = request.headers.get("idempotency-key", "").strip()
  if not 8 <= len(idempotency_key) <= 128:
    raise HTTPException(422, "Idempotency-Key 必须为 8 至 128 个字符")
  try:
    backtest_request = _build_public_backtest_request(payload)
    run_id, display_no, replayed, run_status, fill_model = state_store.create_api_backtest_job(
      backtest_request,
      api_key.id,
      idempotency_key,
      _public_request_hash(payload),
    )
  except state_store.ConflictError as error:
    raise HTTPException(409, str(error)) from error
  except ValueError as error:
    raise HTTPException(429, str(error)) from error
  return {
    "run_id": run_id,
    "display_no": display_no,
    "status": {"succeeded": "ok", "failed": "error"}.get(run_status, run_status),
    "fill_model": fill_model,
    "replayed": replayed,
    "request_id": request.state.request_id,
  }


@app.get("/api/v1/backtests")
def public_backtest_runs(
  request: Request,
  limit: int = Query(20, ge=1, le=100),
  cursor: int = Query(0, ge=0),
):
  _require_public_scope(request, "backtest:read")
  runs = state_store.list_backtest_runs(limit + 1, offset=cursor)
  page = runs[:limit]
  return {
    "items": [_public_run_payload(run) for run in page],
    "next_cursor": str(cursor + len(page)) if len(runs) > limit else None,
  }


@app.get("/api/v1/backtests/{run_id}")
def public_backtest_detail(run_id: str, request: Request):
  _require_public_scope(request, "backtest:read")
  try:
    return {"run": _public_run_payload(state_store.get_backtest_run(run_id))}
  except KeyError as error:
    raise HTTPException(404, "回测记录不存在") from error


@app.get("/api/v1/backtests/{run_id}/signals")
def public_backtest_signals(
  run_id: str,
  request: Request,
  cursor: int = Query(0, ge=0),
  limit: int = Query(100, ge=1, le=200),
):
  _require_public_scope(request, "backtest:read")
  try:
    page = state_store.get_backtest_result_page(run_id, "signals", cursor, limit)
    return {
      "items": page["items"],
      "next_cursor": page["nextCursor"],
      "total": page["total"],
    }
  except KeyError as error:
    raise HTTPException(404, "回测记录不存在") from error


@app.get("/api/v1/backtests/{run_id}/trades")
def public_backtest_trades(
  run_id: str,
  request: Request,
  cursor: int = Query(0, ge=0),
  limit: int = Query(100, ge=1, le=200),
):
  _require_public_scope(request, "backtest:read")
  try:
    page = state_store.get_backtest_result_page(run_id, "trades", cursor, limit)
    return {
      "items": page["items"],
      "next_cursor": page["nextCursor"],
      "total": page["total"],
    }
  except KeyError as error:
    raise HTTPException(404, "回测记录不存在") from error


@app.post("/api/v1/backtests/{run_id}/cancel")
def public_cancel_backtest(run_id: str, request: Request):
  _require_public_scope(request, "backtest:cancel")
  if not state_store.request_l2_backtest_cancel(run_id) and not state_store.request_bar_backtest_cancel(run_id):
    raise HTTPException(404, "未找到可取消的回测任务")
  return {"run_id": run_id, "status": "cancelling", "request_id": request.state.request_id}


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

  try:
    run_id, display_no = state_store.create_bar_backtest_job(request)
    return {
      "runId": run_id,
      "displayNo": display_no,
      "configId": request.get("configId"),
      "configVersionId": request.get("configVersionId"),
      "scriptId": request.get("scriptId"),
      "status": "queued",
      "fillModel": request["fillModel"],
      "message": "标准 Bar 回测已进入队列，等待 bar-runner 执行",
    }
  except ValueError as error:
    raise HTTPException(422, str(error)) from error


@app.post("/api/backtest/runs/{run_id}/cancel")
def cancel_backtest_run(run_id: str):
  """请求取消未结束的回测；Runner 会在当前可中断边界结束。"""
  if not state_store.request_l2_backtest_cancel(run_id) and not state_store.request_bar_backtest_cancel(run_id):
    raise HTTPException(404, "未找到可取消的回测任务")
  return {"runId": run_id, "status": "cancelling"}


@app.delete("/api/backtest/runs")
def delete_backtest_runs(payload: schemas.BacktestRunBatchDeleteRequest):
  try:
    deleted = state_store.delete_backtest_runs(payload.runIds)
  except KeyError as error:
    raise HTTPException(404, str(error)) from error
  except ValueError as error:
    raise HTTPException(409, str(error)) from error
  return {"deleted": deleted}


@app.delete("/api/backtest/runs/{run_id}")
def delete_backtest_run(run_id: str):
  try:
    deleted = state_store.delete_backtest_run(run_id)
  except KeyError as error:
    raise HTTPException(404, "回测记录不存在") from error
  except ValueError as error:
    raise HTTPException(409, str(error)) from error
  return {"deleted": deleted}


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
  try:
    return {"status": market_data.status(symbol, timeframe)}
  except market_data.MarketDataError as error:
    raise HTTPException(503, {
      "code": "data.source_unavailable",
      "message": "行情数据源暂时不可用，请稍后重试。",
      "retryable": True,
    }) from error


if __name__ == "__main__":
  import uvicorn
  uvicorn.run(app, host="0.0.0.0", port=8000)
