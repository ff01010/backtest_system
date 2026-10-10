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
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, NoReturn
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import UUID, uuid4

_viewer_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _viewer_dir not in sys.path:
  sys.path.insert(0, _viewer_dir)

_STRATEGY_GUIDE_PATH = os.path.join(_viewer_dir, "docs", "玄龄回测系统使用手册.md")

from fastapi import FastAPI, Query, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from backend import settings
from backend.experiments import (
  EvaluationApplication,
  EvaluationExportApplication,
  EvaluationExportCreateRequest,
  EvaluationRunCreateRequest,
  EvaluationSpecRequest,
  ExperimentApplication,
  ExperimentCreateRequest,
  ExperimentLifecycleRequest,
  ExperimentPurgeRequest,
  ExperimentError,
  ExperimentPreviewRequest,
  ExperimentRetryFailedRequest,
  ExperimentRetryPreviewRequest,
  ExperimentRevisionCreateRequest,
  ExperimentResumeRequest,
  SubjectRef,
)
from backend.experiments.purge_repository import PostgresExperimentPurgeRepository
from backend.execution import execution_algorithm_snapshot, validate_execution_request
from backend.date_codec import TradingDateError, format_legacy_date, normalize_date_range
from backend import schemas
from backend.time_utils import format_epoch_as_shanghai
from backend.strategies.catalog import get_strategy_source_snapshot
from backend.strategies.scripted import (
  script_uses_research_signal,
  warmup_trading_days_for_script,
  validate_script_code,
)
from backend.strategies.runtime_contract import (
  RUNTIME_CAPABILITY_DIGEST,
  RuntimeDependencyLoadError,
  RuntimeDependencyMissingError,
  SCRIPT_RUNTIME_VERSION,
  StrategyModuleUnavailableError,
  StrategyRuntimePolicyError,
  declared_runtime_capabilities,
  run_runtime_deep_probe,
)
from backend.services import (
  backtest_preflight,
  backtest_runner,
  l2_quote_availability,
  market_data,
  qmt_instrument_directory,
  data_repository,
  loki_client,
  release_notifications,
  shared_backtest_admin,
  research_signal_snapshot,
  shared_backtest_artifacts,
  shared_backtest_lifecycle,
  shared_backtest_copy,
  shared_backtest_detail as shared_backtest_detail_service,
  shared_backtest_discovery,
  state_store,
  strategy_adapters,
  strategy_lifecycle,
  task_diagnostics,
)
from backend.services import auth
from backend.services.user_context import CurrentUser
from backend.services.backtest_errors import (
  StrategyExecutionError,
  error_detail,
  error_http_status,
  error_summary,
)
from backend.services.backtest_faults import BacktestFault
from backend.services.system_error_log import system_error_log_record
from backend.services.user_context import get_current_user, reset_current_user, set_current_user

app = FastAPI(title="Trend Grid Viewer", version="0.1.0")
logger = logging.getLogger(__name__)
_verified_runtime_capabilities = declared_runtime_capabilities()
_experiment_application = ExperimentApplication()
_evaluation_application = EvaluationApplication()
_evaluation_export_application = EvaluationExportApplication()
_experiment_purge_repository = PostgresExperimentPurgeRepository()


def _backtest_error_summary(error: Exception, run_id: str | None) -> dict[str, Any]:
  return error_summary(error, run_id)


def _backtest_failure_response(error: Exception, run_id: str | None) -> HTTPException:
  detail = error_detail(error, run_id).to_dict()
  return HTTPException(error_http_status(error), {"errorDetail": detail})


def _sync_system_error_record(error: Exception, run_id: str) -> dict[str, Any] | None:
  record = system_error_log_record(error, "sync_runner", "sync.backtest_execute", run_id)
  return record.to_dict() if record else None


@app.on_event("startup")
def sync_instrument_directory() -> None:
  """用统一证券主数据刷新 PostgreSQL 搜索目录。"""
  try:
    count = qmt_instrument_directory.sync_market_instruments()
  except Exception as error:
    logger.warning("统一证券目录同步跳过: %s", error)
  else:
    logger.info("统一证券目录已同步 %s 条", count)

app.add_middleware(
  CORSMiddleware,
  allow_origins=list(settings.CSRF_TRUSTED_ORIGINS),
  allow_credentials=True,
  allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
  allow_headers=["*"],
  expose_headers=["X-Request-Id"],
)

_COOKIE_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CSRF_LOGIN_PATH = "/api/auth/login"


def _normalized_url_origin(value: str, require_origin_only: bool = False) -> str | None:
  try:
    parsed = urlsplit(value.strip())
    port = parsed.port
  except ValueError:
    return None
  if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username
    or parsed.password
    or (require_origin_only and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment))
  ):
    return None
  hostname = parsed.hostname.lower()
  host = f"[{hostname}]" if ":" in hostname else hostname
  default_port = 80 if parsed.scheme == "http" else 443
  port_suffix = f":{port}" if port is not None and port != default_port else ""
  return f"{parsed.scheme}://{host}{port_suffix}"


def _requires_cookie_csrf(method: str, path: str) -> bool:
  is_api_path = path == "/api" or path.startswith("/api/")
  return (
    method.upper() in _COOKIE_WRITE_METHODS
    and is_api_path
    and not path.startswith("/api/v1/")
    and not (method.upper() == "POST" and path == _CSRF_LOGIN_PATH)
  )


def _csrf_origin_is_valid(request: Request) -> bool:
  origin = request.headers.get("origin")
  if origin:
    candidate = _normalized_url_origin(origin, require_origin_only=True)
  else:
    referer = request.headers.get("referer")
    candidate = _normalized_url_origin(referer) if referer else None
  if candidate is None:
    return False
  request_origin = _normalized_url_origin(str(request.url))
  trusted_origins = {*settings.CSRF_TRUSTED_ORIGINS}
  if request_origin:
    trusted_origins.add(request_origin)
  return candidate in trusted_origins


def _csrf_error(request: Request) -> JSONResponse:
  if _is_experiment_api_path(request.url.path):
    return _experiment_api_error(
      request,
      403,
      "auth.csrf_invalid",
      "请求安全验证失败，请刷新页面后重试",
    )
  return _legacy_api_error(request, 403, {
    "code": "auth.csrf_invalid",
    "message": "请求安全验证失败，请刷新页面后重试",
    "retryable": False,
  })


async def _call_as_user(
  request: Request,
  call_next: Callable[[Request], Awaitable[Response]],
  user: CurrentUser,
  auth_type: str,
) -> Response:
  request.state.auth_type = auth_type
  token = set_current_user(user)
  try:
    return await call_next(request)
  finally:
    reset_current_user(token)


async def _authenticate_public_api_request(
  request: Request,
  call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
  if not settings.PUBLIC_API_ENABLED:
    return _public_api_error(request, 404, "api.disabled", "开放 API 未启用")
  authorization = request.headers.get("authorization", "")
  token_value = authorization[7:] if authorization.lower().startswith("bearer ") else None
  principal = auth.get_api_key_principal(token_value)
  if principal is None:
    return _public_api_error(
      request, 401, "api_key_invalid", "API Key 无效、已撤销或已过期",
    )
  user, api_key = principal
  request.state.api_key = api_key
  return await _call_as_user(request, call_next, user, "api_key")


async def _dispatch_authenticated_request(
  request: Request,
  call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
  path = request.url.path
  if path.startswith("/api/v1/"):
    return await _authenticate_public_api_request(request, call_next)
  is_api_path = path == "/api" or path.startswith("/api/")
  is_public_route = (
    request.method == "GET" and path == "/api/health"
  ) or (
    request.method == "POST" and path == _CSRF_LOGIN_PATH
  )
  if request.method == "OPTIONS" or not is_api_path or is_public_route:
    return await call_next(request)
  session_token = request.cookies.get(auth.SESSION_COOKIE)
  user = auth.get_user_by_token(session_token)
  if not user:
    if _is_experiment_api_path(path):
      return _experiment_api_error(
        request, 401, "auth.unauthorized", "登录已失效，请重新登录",
      )
    return _legacy_api_error(request, 401, "登录已失效，请重新登录")
  request.state.auth_type = "cookie"
  if _requires_cookie_csrf(request.method, path) and not (
    _csrf_origin_is_valid(request)
    and auth.validate_csrf_token(session_token, request.headers.get("x-csrf-token"))
  ):
    return _csrf_error(request)
  return await _call_as_user(request, call_next, user, "cookie")


def _finish_api_request(
  request: Request,
  response: Response,
  started_at: float,
) -> Response:
  request_id = request.state.request_id
  response.headers["X-Request-Id"] = request_id
  logger.info("api_request %s", json.dumps({
    "requestId": request_id,
    "method": request.method,
    "path": request.url.path,
    "statusCode": response.status_code,
    "durationMs": round((perf_counter() - started_at) * 1000, 3),
    "authType": request.state.auth_type,
  }, ensure_ascii=False, separators=(",", ":")))
  return response


@app.middleware("http")
async def authenticate_api_request(
  request: Request,
  call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
  """按 Cookie 或 API Key 认证业务接口，并将用户写入当前请求上下文。"""
  path = request.url.path
  is_api_request = path == "/api" or path.startswith("/api/")
  started_at = perf_counter()
  if is_api_request:
    request.state.request_id = f"req_{uuid4().hex}"
  request.state.auth_type = "anonymous"
  try:
    response = await _dispatch_authenticated_request(request, call_next)
  except Exception as error:
    response = await handle_unexpected_exception(request, error)
  return _finish_api_request(request, response, started_at) if is_api_request else response


def _legacy_api_error(request: Request, status_code: int, detail: Any) -> JSONResponse:
  return JSONResponse(
    status_code=status_code,
    content={
      "detail": detail,
      "requestId": getattr(request.state, "request_id", None),
    },
  )


def _is_experiment_api_path(path: str) -> bool:
  return path.startswith((
    "/api/backtest/experiment",
    "/api/backtest/admin/experiments",
    "/api/backtest/evaluation-runs",
    "/api/backtest/evaluation-exports",
  ))


def _experiment_api_error(
  request: Request,
  status_code: int,
  code: str,
  message: str,
  *,
  retryable: bool = False,
  details: dict[str, Any] | None = None,
) -> JSONResponse:
  return JSONResponse(status_code=status_code, content={"error": {
    "code": code,
    "message": message,
    "retryable": retryable,
    "requestId": getattr(request.state, "request_id", None),
    "details": details or {},
  }})


def _public_api_error(
  request: Request,
  status_code: int,
  code: str,
  message: str,
  retryable: bool = False,
  details: dict[str, Any] | None = None,
) -> JSONResponse:
  return JSONResponse(
    status_code=status_code,
    content={
      "error": {
        "code": code,
        "message": message,
        "retryable": retryable,
        "requestId": getattr(request.state, "request_id", None),
        "request_id": getattr(request.state, "request_id", None),
        "details": details or {},
      },
    },
  )


@app.exception_handler(HTTPException)
async def handle_http_exception(request: Request, error: HTTPException) -> JSONResponse:
  if request.url.path.startswith("/api/v1/"):
    structured = error.detail if isinstance(error.detail, dict) else {}
    detail = (
      str(structured.get("message"))
      if structured.get("message")
      else error.detail if isinstance(error.detail, str) else "请求无法完成"
    )
    code = str(structured.get("code") or "")
    if not code:
      code = "backtest.validation_failed" if error.status_code == 422 else "request.failed"
      if error.status_code == 403:
        code = "api_scope_denied"
      elif error.status_code == 404:
        code = "resource.not_found"
      elif error.status_code == 409:
        code = "idempotency.conflict"
      elif error.status_code == 429:
        code = "rate_limit.exceeded"
    return _public_api_error(
      request,
      error.status_code,
      code,
      detail,
      bool(structured.get("retryable", error.status_code >= 500)),
      structured.get("details") if isinstance(structured.get("details"), dict) else None,
    )
  if _is_experiment_api_path(request.url.path):
    detail = error.detail if isinstance(error.detail, dict) else {}
    return _experiment_api_error(
      request,
      error.status_code,
      str(detail.get("code") or "experiment.command_invalid"),
      str(detail.get("message") or error.detail),
      retryable=bool(detail.get("retryable", False)),
      details=detail.get("details") if isinstance(detail.get("details"), dict) else None,
    )
  if request.url.path == "/api" or request.url.path.startswith("/api/"):
    return _legacy_api_error(request, error.status_code, error.detail)
  return JSONResponse(status_code=error.status_code, content={"detail": error.detail})


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
  if request.url.path.startswith("/api/v1/"):
    is_experiment = request.url.path.startswith("/api/v1/experiments")
    code = (
      "strategy.validation_failed"
      if request.url.path.startswith("/api/v1/strategies")
      else "experiment.validation_failed"
      if is_experiment
      else "backtest.validation_failed"
    )
    details = None
    if is_experiment:
      details = {
        "fields": [
          ".".join(str(part) for part in item["loc"][1:])
          for item in error.errors()
        ],
      }
    return _public_api_error(
      request, 422, code, "请求参数不符合接口约束", details=details,
    )
  if _is_experiment_api_path(request.url.path):
    fields = [".".join(str(part) for part in item["loc"][1:]) for item in error.errors()]
    return _experiment_api_error(
      request,
      422,
      "experiment.validation_failed",
      "实验预览参数不符合接口约束",
      details={"fields": fields},
    )
  if request.url.path == "/api" or request.url.path.startswith("/api/"):
    return _legacy_api_error(request, 422, "请求参数不符合接口约束")
  return JSONResponse(status_code=422, content={"detail": error.errors()})


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, error: Exception) -> JSONResponse:
  logger.exception(
    "未处理接口异常: path=%s request_id=%s",
    request.url.path,
    getattr(request.state, "request_id", None),
    exc_info=error,
  )
  if request.url.path.startswith("/api/v1/"):
    return _public_api_error(request, 503, "backtest.unavailable", "服务暂时不可用，请稍后重试", True)
  if _is_experiment_api_path(request.url.path):
    return _experiment_api_error(
      request, 500, "experiment.internal_error", "服务器内部错误",
    )
  if request.url.path == "/api" or request.url.path.startswith("/api/"):
    return _legacy_api_error(request, 500, "服务器内部错误")
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
  except StrategyRuntimePolicyError as error:
    detail = StrategyExecutionError.from_exception(error).detail.to_dict()
    raise HTTPException(422, {"errorDetail": detail}) from error
  except (
    RuntimeDependencyLoadError,
    RuntimeDependencyMissingError,
    StrategyModuleUnavailableError,
  ) as error:
    detail = StrategyExecutionError.from_exception(error).detail
    status_code = 503 if detail.category == "system_error" else 422
    raise HTTPException(status_code, {"errorDetail": detail.to_dict()}) from error
  except ValueError as error:
    raise HTTPException(422, str(error)) from error
  except SyntaxError as error:
    raise HTTPException(422, f"Python 语法错误: 第 {error.lineno} 行 {error.msg}")
  if payload.get("status") not in {"draft", "published", "disabled"}:
    raise HTTPException(422, f"非法脚本状态: {payload.get('status')}")
  if payload.get("signalRequired") and not script_uses_research_signal(str(payload.get("code", ""))):
    raise HTTPException(422, "研究信号驱动策略必须显式读取 bar['researchSignal']")


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
  params_json = config.get("paramsJson", {})
  saved_params = params_json.get("params", {}) if isinstance(params_json, dict) else {}
  if not isinstance(saved_params, dict):
    return
  definitions = (
    params_json.get("strategyParameterDefinitions", {})
    if isinstance(params_json, dict)
    else {}
  )
  declared_names = set(definitions) if isinstance(definitions, dict) else set()
  request_params = request.get("strategyParams", {})
  if not isinstance(request_params, dict):
    raise HTTPException(422, "策略参数格式非法")
  mismatches: list[str] = []
  for key, value in saved_params.items():
    if (
      key != "warmupTradingDays"
      and key not in _EXPERIMENT_PARAM_KEYS
      and key not in declared_names
      and request_params.get(key) != value
    ):
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
    strategy_version_id = config.get("strategyVersionId")
    script = (
      state_store.get_strategy_script_version(str(strategy_version_id), str(script_id))
      if strategy_version_id
      else state_store.get_strategy_script(str(script_id))
    )
  except KeyError as error:
    raise HTTPException(422, "策略保存的代码不存在") from error
  mismatches = []
  signal_required = bool(script.get("signalRequired", False))
  signal_selected = bool(request.get("signalDatasetId"))
  if signal_required and not signal_selected:
    raise HTTPException(422, "当前策略必须选择研究信号后才能运行")
  if signal_selected and not signal_required:
    raise HTTPException(422, "当前策略未声明为研究信号驱动策略，不能附加研究信号")

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
  params_json = config.get("paramsJson") if isinstance(config.get("paramsJson"), dict) else {}
  definitions = params_json.get("strategyParameterDefinitions")
  declared_names = set(definitions) if isinstance(definitions, dict) else set()
  overrides = request.get("strategyParamOverrides")
  if not isinstance(overrides, dict):
    request_params = request.get("strategyParams")
    overrides = {
      name: request_params[name]
      for name in declared_names
      if isinstance(request_params, dict) and name in request_params
    }
  try:
    saved_strategy_params, strategy_param_overrides, effective_strategy_params = (
      strategy_lifecycle.effective_strategy_parameters(config, overrides)
    )
  except strategy_lifecycle.StrategyLifecycleError as error:
    raise HTTPException(422, {
      "code": error.code,
      "message": str(error),
    }) from error
  request_params = request.get("strategyParams")
  structural_params = request_params if isinstance(request_params, dict) else {}
  request["strategyParams"] = {
    key: value
    for key, value in structural_params.items()
    if key not in declared_names
  } | effective_strategy_params
  request["savedStrategyParams"] = saved_strategy_params
  request["strategyParamOverrides"] = strategy_param_overrides
  request["effectiveStrategyParams"] = effective_strategy_params
  _validate_saved_params(request, config)
  request["strategy"] = config["strategy"]
  request["scriptId"] = script["id"]
  request["strategyVersionId"] = script.get("versionId") or strategy_version_id
  request["warmupTradingDays"] = warmup_days
  request["configVersionId"] = config.get("publishedVersionId")
  request["runDraft"] = False
  request["versionScriptSnapshot"] = {
    "id": script["id"], "name": script["name"], "description": script["description"],
    "code": script["code"], "entryType": script["entryType"],
    "versionId": script.get("versionId") or strategy_version_id,
    "signalRequired": signal_required,
    "codeHash": hashlib.sha256(script["code"].encode("utf-8")).hexdigest(),
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
  }
  request["savedScriptSnapshot"] = request["versionScriptSnapshot"]
  request["savedConfigSnapshot"] = {
    "name": config["name"], "strategy": config["strategy"], "mode": config["mode"],
    "symbol": config["symbol"], "timeframe": config["timeframe"],
    "benchmarkSymbol": config.get("benchmarkSymbol"), "accountId": config.get("accountId"),
    "params": config.get("paramsJson", {}).get("params", {}),
    "description": script["description"], "runParams": request.get("strategyParams", {}),
    "savedStrategyParams": request.get("savedStrategyParams", {}),
    "strategyParamOverrides": request.get("strategyParamOverrides", {}),
    "effectiveStrategyParams": request.get("effectiveStrategyParams", {}),
    "warmupTradingDays": warmup_days,
    "signalRequired": signal_required,
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
    "execution": execution_algorithm_snapshot(
      str(request["executionAlgorithm"]), dict(request.get("executionParameters") or {}),
    ),
    "sharedSource": config.get("sharedSource"),
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
  if request.get("signalDatasetId"):
    _prepare_research_signal_attachment(request)


def _prepare_research_signal_attachment(request: dict[str, Any]) -> None:
  dataset_id = request.get("signalDatasetId")
  if not isinstance(dataset_id, str) or not dataset_id:
    raise HTTPException(422, "研究信号数据集无效")
  try:
    imported = state_store.get_imported_research_signal_dataset(dataset_id)
    if imported:
      artifact_path = shared_backtest_artifacts.verified_artifact_path(
        str(imported["storage_key"]),
        str(imported["content_sha256"]),
        int(imported["byte_size"]),
      )
      imported["artifactFile"] = str(artifact_path)
      snapshot = research_signal_snapshot.freeze_imported_signal_snapshot(
        imported, request["symbol"], request["startDate"], request["endDate"],
        request["timeframe"], [],
      )
    else:
      snapshot = research_signal_snapshot.freeze_signal_snapshot(
        dataset_id, str(state_store.current_user_id()), request["symbol"], request["startDate"],
        request["endDate"], request["timeframe"], [],
      )
  except (
    research_signal_snapshot.ResearchSignalSnapshotError,
    shared_backtest_artifacts.SharedArtifactError,
  ) as error:
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
    raise HTTPException(409, {
      "code": "backtest.strategy_unavailable",
      "message": "策略方案未启用或尚未发布",
    })
  params_json = config.get("paramsJson")
  saved_params = params_json.get("params", {}) if isinstance(params_json, dict) else {}
  if not isinstance(saved_params, dict):
    raise HTTPException(422, "策略方案缺少可运行参数")

  try:
    saved_strategy_params, strategy_param_overrides, effective_strategy_params = (
      strategy_lifecycle.effective_strategy_parameters(
        config, dict(payload.overrides.strategy_params),
      )
    )
  except strategy_lifecycle.StrategyLifecycleError as error:
    raise HTTPException(422, {
      "code": error.code,
      "message": str(error),
    }) from error

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
    overlaySettlementPolicy=values.get("overlaySettlementPolicy", "intraday_restore"),
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
    } | effective_strategy_params,
  )
  request = _model_dump(request_model)
  request["savedStrategyParams"] = saved_strategy_params
  request["strategyParamOverrides"] = strategy_param_overrides
  request["effectiveStrategyParams"] = effective_strategy_params
  _validate_backtest_request(request)
  return request


def _public_request_hash(payload: schemas.PublicBacktestCreateRequest) -> str:
  canonical = json.dumps(_model_dump(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _public_write_hash(method: str, path: str, payload: Any) -> str:
  canonical = json.dumps({
    "method": method.upper(),
    "path": path,
    "body": _model_dump(payload),
  }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _public_idempotency_key(
  request: Request,
  validation_code: str = "strategy.validation_failed",
) -> str:
  value = request.headers.get("idempotency-key", "").strip()
  if not 8 <= len(value) <= 128:
    raise HTTPException(422, {
      "code": validation_code,
      "message": "Idempotency-Key 必须为 8 至 128 个字符",
    })
  return value


def _public_experiment_idempotency_key(
  request: Request,
  api_key: auth.ApiKeyPrincipal,
) -> str:
  """把调用方幂等键限定在当前 API Key 内，避免不同 Key 相互冲突。"""
  raw_key = _public_idempotency_key(request, "experiment.validation_failed")
  namespaced = hashlib.sha256(f"{api_key.id}:{raw_key}".encode("utf-8")).hexdigest()
  return f"public:{namespaced}"


def _audit_public_experiment_operation(
  api_key: auth.ApiKeyPrincipal,
  request: Request,
  operation_type: str,
  response: dict[str, Any],
  resource_id: str | None = None,
) -> None:
  """补充 API Key 归因审计；实验仓储审计仍是业务操作的原子记录。"""
  experiment_id = resource_id or response.get("experimentId")
  detail = {
    "operation": operation_type,
    "resourceId": str(experiment_id) if experiment_id else None,
    "replayed": bool(response.get("replayed", False)),
  }
  try:
    auth.audit_api_key_event(
      api_key.id,
      api_key.owner_user_id,
      operation_type,
      detail,
      getattr(request.state, "request_id", None),
    )
  except Exception:
    logger.exception(
      "记录公开实验 API 审计失败: operation=%s resource_id=%s",
      operation_type,
      experiment_id,
    )


def _public_experiment_command(
  request: Request,
  api_key: auth.ApiKeyPrincipal,
  operation_type: str,
  action: Callable[[str, str | None], tuple[dict[str, Any], int]],
  resource_id: str | None = None,
) -> JSONResponse:
  try:
    body, status_code = action(
      _public_experiment_idempotency_key(request, api_key),
      getattr(request.state, "request_id", None),
    )
  except ExperimentError as error:
    _raise_experiment_error(error)
  _audit_public_experiment_operation(
    api_key, request, operation_type, body, resource_id,
  )
  return JSONResponse(status_code=status_code, content=body)


def _public_write_context(
  request: Request,
  api_key: auth.ApiKeyPrincipal,
  operation_type: str,
  payload: Any,
) -> strategy_lifecycle.PublicWriteContext:
  return strategy_lifecycle.PublicWriteContext(
    api_key_id=api_key.id,
    idempotency_key=_public_idempotency_key(request),
    request_hash=_public_write_hash(request.method, request.url.path, payload),
    operation_type=operation_type,
    request_id=getattr(request.state, "request_id", None),
  )


def _public_backtest_params_to_internal(
  parameters: schemas.PublicStrategyBacktestParameters,
) -> dict[str, Any]:
  values = _model_dump(parameters)
  execution = values.get("execution_parameters") or {}
  return {
    "startDate": values["start_date"],
    "endDate": values["end_date"],
    "initialCapital": values["initial_capital"],
    "commissionRate": values["commission_rate"],
    "slippage": values["slippage"],
    "allowLong": values["allow_long"],
    "allowShort": values["allow_short"],
    "strategyCategory": values["strategy_category"],
    "holdingPolicy": values["holding_policy"],
    "overlaySettlementPolicy": values["overlay_settlement_policy"],
    "fillModel": values["fill_model"],
    "executionAlgorithm": values["execution_algorithm"],
    "twapDurationSeconds": execution.get("duration_seconds"),
    "twapIntervalSeconds": execution.get("interval_seconds"),
    "twapMinChildQuantity": execution.get("min_child_quantity"),
    "baseQuantity": values["base_quantity"],
    "baseCostPrice": values["base_cost_price"],
    "initialPositionEnabled": values["initial_position_enabled"],
    "t0Enabled": values["t0_enabled"],
  }


def _public_strategy_command(
  payload: schemas.PublicStrategyCreateRequest | schemas.PublicStrategyUpdateRequest,
  config_id: str | None = None,
) -> strategy_lifecycle.StrategySaveCommand:
  return strategy_lifecycle.StrategySaveCommand(
    name=payload.name,
    description=payload.description,
    code=payload.code,
    entry_type=payload.entry_type,
    signal_required=payload.signal_required,
    strategy="user_script",
    mode="backtest",
    symbol=payload.symbol,
    timeframe=payload.timeframe,
    benchmark_symbol=payload.benchmark_symbol or payload.symbol,
    account_id=None,
    params_json={"params": _public_backtest_params_to_internal(payload.backtest_parameters)},
    strategy_parameters=dict(payload.strategy_parameters),
    enabled=payload.enabled,
    config_id=config_id,
    expected_revision=(
      payload.expected_revision
      if isinstance(payload, schemas.PublicStrategyUpdateRequest)
      else None
    ),
  )


def _raise_public_strategy_error(error: Exception) -> None:
  if isinstance(error, strategy_lifecycle.StrategyLifecycleError):
    raise HTTPException(error.status_code, {
      "code": error.code,
      "message": str(error),
    }) from error
  if isinstance(error, state_store.RevisionConflictError):
    raise HTTPException(409, {
      "code": "strategy.revision_conflict",
      "message": str(error),
    }) from error
  if isinstance(error, state_store.NameConflictError):
    raise HTTPException(409, {
      "code": "strategy.name_conflict",
      "message": str(error),
    }) from error
  if isinstance(error, state_store.IdempotencyConflictError):
    raise HTTPException(409, {
      "code": "idempotency.conflict",
      "message": str(error),
    }) from error
  if isinstance(error, KeyError):
    raise HTTPException(404, {
      "code": "strategy.not_found",
      "message": "策略不存在",
    }) from error
  if isinstance(error, state_store.ConflictError):
    raise HTTPException(409, {
      "code": "strategy.state_conflict",
      "message": str(error),
    }) from error
  raise error


def _public_run_payload(run: dict[str, Any]) -> dict[str, Any]:
  result = run.get("resultJson") if isinstance(run.get("resultJson"), dict) else {}
  params = run.get("paramsJson") if isinstance(run.get("paramsJson"), dict) else {}
  manifest = result.get("experimentManifest") if isinstance(result.get("experimentManifest"), dict) else {}

  return {
    "run_id": run["id"],
    "display_no": run["displayNo"],
    "config_id": run.get("configId"),
    "symbol": run["symbol"],
    "timeframe": run["timeframe"],
    "fill_model": run["fillModel"],
    "strategy_category": result.get("strategyCategory") or params.get("strategyCategory"),
    "overlay_settlement_policy": manifest.get("overlaySettlementPolicy") or params.get("overlaySettlementPolicy"),
    "result_schema_version": result.get("resultSchemaVersion"),
    "status": run["status"],
    "start_date": run["startDate"],
    "end_date": run["endDate"],
    "summary": run.get("summaryJson") or {},
    "created_at": run["createdAt"],
    "finished_at": run["finishedAt"],
  }


@app.on_event("startup")
def startup():
  global _verified_runtime_capabilities
  _verified_runtime_capabilities = run_runtime_deep_probe()
  settings.validate_public_api_configuration()
  settings.validate_research_integration_configuration()
  settings.validate_application_build_configuration()
  settings.validate_observability_configuration()
  settings.validate_experiment_configuration()
  state_store.ensure_schema()
  state_store.prune_strategy_runtime_error_stats()


@app.get("/api/health")
def health():
  runner_health = state_store.get_backtest_runner_health()
  signal_export_healthy = state_store.signal_export_runner_available()
  evaluation_export_healthy = _evaluation_export_application.worker_available()
  return {
    "status": runner_health["status"],
    "backtest": "ok" if runner_health["status"] == "ok" else "unavailable",
    "strategyRuntimeVersion": SCRIPT_RUNTIME_VERSION,
    "applicationBuildId": settings.APPLICATION_BUILD_ID,
    "clientCompatibilityVersion": settings.CLIENT_COMPATIBILITY_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
    "runtimeCapabilities": _verified_runtime_capabilities,
    "signalExportRunner": {"healthy": signal_export_healthy},
    "evaluationExportWorker": {"healthy": evaluation_export_healthy},
    "observability": loki_client.health_status(),
    "experiments": {
      "enabled": settings.EXPERIMENTS_ENABLED,
      "barPreviewEnabled": settings.EXPERIMENT_BAR_ENABLED,
      "capabilityVersion": "experiment_bar_execution_v1",
    },
  }


def _release_cache_headers() -> dict[str, str]:
  return {
    "Cache-Control": "private, no-cache",
    "Vary": "Cookie",
  }


@app.get("/api/releases/status", response_model=schemas.ReleaseStatusResponse)
def release_status(
  request: Request,
  response: Response,
  clientBuildId: str = Query(pattern=r"^(dev|[0-9a-f]{40})$"),
  clientCompatibilityVersion: int = Query(ge=1),
):
  try:
    payload, etag = release_notifications.get_release_status(
      clientBuildId, clientCompatibilityVersion,
    )
  except release_notifications.ReleaseStateUnavailable:
    return JSONResponse(
      status_code=503,
      content={
        "detail": {
          "code": "release.state_unavailable",
          "message": "版本通知暂不可用",
        },
      },
      headers=_release_cache_headers(),
    )
  if request.headers.get("if-none-match") == etag:
    return Response(status_code=304, headers={**_release_cache_headers(), "ETag": etag})
  for name, value in _release_cache_headers().items():
    response.headers[name] = value
  response.headers["ETag"] = etag
  return payload


@app.post("/api/releases/{release_id}/acknowledge")
def release_acknowledge(release_id: str, payload: schemas.ReleaseAcknowledgeRequest):
  try:
    result = release_notifications.acknowledge_release(release_id, payload.clientBuildId)
  except state_store.ConflictError as error:
    if str(error) == "release.notice_stale":
      raise HTTPException(409, {"code": "release.notice_stale", "message": "公告已更新"}) from error
    raise
  return JSONResponse(content=result, headers=_release_cache_headers())


@app.get("/api/releases", response_model=schemas.ReleaseHistoryResponse)
def release_history(limit: int = Query(default=20, ge=1, le=50), cursor: str | None = None):
  try:
    result = release_notifications.list_published_releases(limit, cursor)
  except ValueError as error:
    raise HTTPException(422, str(error)) from error
  return JSONResponse(content=result, headers=_release_cache_headers())


def _require_backtest_runner(fill_model: str) -> None:
  job_kind = "l2" if fill_model == "l2_quote" else "bar"
  try:
    available = state_store.has_healthy_backtest_runner(job_kind)
  except state_store.DatabaseError as error:
    logger.exception("检查回测执行器可用性失败")
    raise HTTPException(503, "回测执行服务暂不可用，请稍后重试") from error
  if not available:
    raise HTTPException(503, "回测执行服务暂不可用，请稍后重试")


@app.get("/api/research/signal-datasets")
def research_signal_datasets() -> dict[str, Any]:
  """供回测工作台选择当前用户可用的已发布研究信号。"""

  imported_items = state_store.list_imported_research_signal_datasets()
  try:
    remote_items = research_signal_snapshot.list_published_signal_datasets(
      str(state_store.current_user_id()),
    )
  except research_signal_snapshot.ResearchSignalSnapshotError as error:
    if not imported_items:
      raise HTTPException(422, str(error)) from error
    remote_items = []
  return {"items": [*imported_items, *remote_items]}


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
def login(
  payload: schemas.LoginRequest,
  request: Request,
  response: Response,
) -> dict[str, Any]:
  """校验用户名密码并签发仅浏览器可见的 HttpOnly 会话 Cookie。"""
  try:
    token, user, csrf_token = auth.authenticate(
      payload.username,
      payload.password,
      request.client.host if request.client else None,
      request.headers.get("user-agent"),
      getattr(request.state, "request_id", None),
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
  return {
    "user": {
      "id": user.id,
      "username": user.username,
      "displayName": user.display_name,
      "role": user.role,
    },
    "csrfToken": csrf_token,
  }


@app.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, bool]:
  """撤销服务端会话并清除浏览器 Cookie。"""
  auth.revoke_session(
    request.cookies.get(auth.SESSION_COOKIE),
    getattr(request.state, "request_id", None),
    request.client.host if request.client else None,
  )
  response.delete_cookie(auth.SESSION_COOKIE, path="/")
  return {"ok": True}


@app.get("/api/auth/csrf")
def refresh_csrf_token(request: Request) -> dict[str, str]:
  """为有效 Cookie 会话签发或轮换仅本次响应可见的 CSRF Token。"""
  csrf_token = auth.rotate_csrf_token(
    request.cookies.get(auth.SESSION_COOKIE),
    getattr(request.state, "request_id", None),
  )
  if not csrf_token:
    raise HTTPException(401, "登录已失效，请重新登录")
  return {"csrfToken": csrf_token}


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
def create_api_key(
  payload: schemas.ApiKeyCreateRequest,
  request: Request,
) -> dict[str, Any]:
  try:
    key, plain_text_key = auth.create_api_key(
      get_current_user().id,
      payload.name,
      list(payload.scopes),
      payload.expiresAt,
      getattr(request.state, "request_id", None),
    )
  except auth.ApiKeyError as error:
    raise HTTPException(422, str(error)) from error
  return {"key": key, "plainTextKey": plain_text_key}


@app.delete("/api/api-keys/{key_id}")
def revoke_api_key(key_id: str, request: Request) -> dict[str, bool]:
  if not auth.revoke_api_key(
    get_current_user().id, key_id, getattr(request.state, "request_id", None),
  ):
    raise HTTPException(404, "API Key 不存在")
  return {"revoked": True}


def _require_admin(request: Request) -> CurrentUser:
  user = auth.get_user_by_token(request.cookies.get(auth.SESSION_COOKIE))
  if not user or not user.is_admin:
    raise HTTPException(403, "仅管理员可执行此操作")
  return user


def _require_experiment_admin(request: Request) -> CurrentUser:
  try:
    return _require_admin(request)
  except HTTPException as error:
    raise _stable_error(403, "experiment.forbidden", "仅管理员可执行此操作") from error


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
  run_id: UUID | None = Query(default=None, alias="runId"),
  error_code: str | None = Query(default=None, alias="errorCode"),
  from_time: str | None = Query(default=None, alias="fromTime"),
  to_time: str | None = Query(default=None, alias="toTime"),
):
  """返回管理员可见的安全系统错误摘要，不含原始堆栈和凭据。"""
  _require_admin(request)
  return {"items": state_store.list_system_error_logs(
    limit, user_id, str(run_id) if run_id else None, error_code, from_time, to_time,
  )}


@app.get(
  "/api/admin/backtest-runs/{run_id}/diagnostics",
  response_model=schemas.BacktestRunDiagnostics,
)
def admin_backtest_run_diagnostics(
  run_id: UUID,
  request: Request,
  attempt_token: UUID | None = Query(default=None, alias="attemptToken"),
):
  """按任务 UUID 返回安全 Attempt 时间线，不接受任意日志查询表达式。"""
  _require_admin(request)
  try:
    return task_diagnostics.get_admin_run_diagnostics(
      str(run_id), str(attempt_token) if attempt_token else None,
    )
  except KeyError as error:
    raise HTTPException(404, "回测任务不存在") from error


@app.get("/api/admin/strategy-runtime-errors")
def admin_strategy_runtime_errors(
  request: Request,
  limit: int = Query(100, ge=1, le=200),
  from_time: str | None = Query(default=None, alias="fromTime"),
  to_time: str | None = Query(default=None, alias="toTime"),
):
  """返回不可关联用户和任务的平台运行时错误聚合。"""
  _require_admin(request)
  return {"items": state_store.list_strategy_runtime_error_stats(from_time, to_time, limit)}


@app.get("/api/admin/backtest-capacity")
def admin_backtest_capacity(request: Request):
  _require_admin(request)
  return state_store.get_backtest_scheduler_capacity()


@app.put("/api/admin/backtest-capacity")
def update_admin_backtest_capacity(
  payload: schemas.BacktestCapacityOverrideRequest,
  request: Request,
):
  _require_admin(request)
  try:
    return state_store.set_backtest_scheduler_capacity(payload.limit, payload.ttlSeconds, payload.reason)
  except ValueError as error:
    raise HTTPException(422, str(error)) from error


@app.delete("/api/admin/backtest-capacity")
def clear_admin_backtest_capacity(
  request: Request,
  reason: str = Query("管理员提前结束临时扩容", min_length=1, max_length=500),
):
  _require_admin(request)
  return state_store.clear_backtest_scheduler_capacity(reason)


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
    return {"script": {
      **script,
      "runtimeVersion": SCRIPT_RUNTIME_VERSION,
      "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
    }}
  except state_store.ConflictError as error:
    raise HTTPException(409, str(error)) from error
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 写入策略脚本失败: {e}")


@app.put("/api/strategy-scripts/{script_id}")
def update_strategy_script(script_id: str, payload: schemas.StrategyScriptUpdate):
  request = _model_dump(payload)
  _validate_strategy_script(request)
  try:
    script = state_store.update_strategy_script(script_id, request)
    return {"script": {
      **script,
      "runtimeVersion": SCRIPT_RUNTIME_VERSION,
      "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
    }}
  except KeyError:
    raise HTTPException(404, f"策略脚本不存在: {script_id}")
  except state_store.ConflictError as error:
    raise HTTPException(409, str(error)) from error
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
  except state_store.ConflictError as error:
    raise HTTPException(409, str(error)) from error
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
  except state_store.ConflictError as error:
    raise HTTPException(409, str(error)) from error
  except state_store.DatabaseError as e:
    raise HTTPException(500, f"PostgreSQL 更新策略配置失败: {e}")


@app.post("/api/complete-strategies")
def save_complete_strategy(payload: schemas.CompleteStrategySave):
  script_request = _model_dump(payload.script)
  config_request = _model_dump(payload.config)
  params_json = config_request.get("paramsJson")
  strategy_parameters = (
    params_json.get("strategyParams")
    if isinstance(params_json, dict) and isinstance(params_json.get("strategyParams"), dict)
    else None
  )
  try:
    saved = strategy_lifecycle.save_strategy(
      strategy_lifecycle.StrategySaveCommand(
        name=str(config_request["name"]),
        description=str(script_request.get("description", "")),
        code=str(script_request["code"]),
        entry_type=str(script_request.get("entryType", "on_bar")),
        signal_required=bool(script_request.get("signalRequired", False)),
        strategy=str(config_request.get("strategy", "user_script")),
        mode=str(config_request.get("mode", "backtest")),
        symbol=str(config_request["symbol"]),
        timeframe=str(config_request.get("timeframe", "5m")),
        benchmark_symbol=config_request.get("benchmarkSymbol"),
        account_id=config_request.get("accountId"),
        params_json=dict(params_json) if isinstance(params_json, dict) else {},
        strategy_parameters=strategy_parameters,
        enabled=bool(config_request.get("enabled", True)),
        script_id=payload.scriptId,
        config_id=payload.configId,
        expected_revision=payload.expectedRevision,
      ),
    )
    return {
      **saved,
      "runtimeVersion": SCRIPT_RUNTIME_VERSION,
      "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
    }
  except strategy_lifecycle.StrategyLifecycleError as error:
    raise HTTPException(error.status_code, str(error)) from error
  except KeyError as error:
    raise HTTPException(404, "策略脚本或策略配置不存在") from error
  except state_store.ConflictError as error:
    raise HTTPException(409, str(error)) from error
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"PostgreSQL 保存完整策略失败: {error}") from error


@app.get("/api/complete-strategies/{config_id}")
def get_complete_strategy(config_id: str):
  try:
    return {"config": state_store.get_strategy_config(config_id)}
  except KeyError as error:
    raise HTTPException(404, f"策略不存在: {config_id}") from error
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"PostgreSQL 读取完整策略失败: {error}") from error


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


def _raise_experiment_error(error: ExperimentError) -> NoReturn:
  raise HTTPException(error.status_code, {
    "code": error.code,
    "message": str(error),
    "retryable": error.retryable,
    "details": error.details,
  }) from error


def _experiment_idempotency_key(request: Request) -> str:
  value = request.headers.get("idempotency-key", "").strip()
  if not value or len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
    raise ExperimentError(
      "experiment.validation_failed",
      "Idempotency-Key 缺失或格式无效",
      422,
    )
  return value


@app.get(
  "/api/backtest/experiment-subjects/{subject_type}/{subject_id}/versions/{subject_version}",
)
def experiment_subject(
  subject_type: Literal["legacy_single_strategy"],
  subject_id: str,
  subject_version: str,
) -> dict[str, Any]:
  try:
    return _experiment_application.resolve_subject(SubjectRef(
      subjectType=subject_type,
      subjectId=subject_id,
      subjectVersion=subject_version,
    ))
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/preview")
def preview_experiment(payload: ExperimentPreviewRequest) -> dict[str, Any]:
  try:
    return _experiment_application.preview(payload)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments")
def create_experiment(payload: ExperimentCreateRequest, request: Request) -> Response:
  try:
    body, status_code = _experiment_application.create(
      payload,
      _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments/by-idempotency-key/{idempotency_key}")
def find_created_experiment(idempotency_key: str) -> Response:
  try:
    if len(idempotency_key) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key):
      raise ExperimentError("experiment.not_found", "创建请求不存在", 404)
    body, status_code = _experiment_application.find_created(idempotency_key)
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments")
def list_experiments(
  limit: int = Query(50, ge=1, le=200),
  cursor: str | None = Query(default=None, max_length=2048),
  archived: bool = Query(False),
) -> dict[str, Any]:
  try:
    return _experiment_application.list_experiments(
      limit=limit, cursor=cursor, archived=archived,
    )
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/admin/experiments/archived")
def list_admin_archived_experiments(
  request: Request,
  limit: int = Query(50, ge=1, le=200),
  cursor: str | None = Query(default=None, max_length=2048),
) -> dict[str, Any]:
  _require_experiment_admin(request)
  try:
    return _experiment_application.list_admin_archived_experiments(
      limit=limit, cursor=cursor,
    )
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments/{experiment_id}")
def experiment_detail(experiment_id: UUID, request: Request) -> Response:
  try:
    body = _experiment_application.detail(str(experiment_id))
    etag = f'"{body["snapshotVersion"]}"'
    if request.headers.get("if-none-match") == etag:
      return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=body, headers={"ETag": etag})
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments/{experiment_id}/revisions")
def experiment_revisions(
  experiment_id: UUID,
  limit: int = Query(50, ge=1, le=200),
  cursor: str | None = Query(default=None, max_length=2048),
) -> dict[str, Any]:
  try:
    return _experiment_application.list_revisions(
      str(experiment_id), limit=limit, cursor=cursor,
    )
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/revisions")
def create_experiment_revision(
  experiment_id: UUID,
  payload: ExperimentRevisionCreateRequest,
  request: Request,
) -> Response:
  try:
    body, status_code = _experiment_application.create_revision(
      str(experiment_id),
      payload,
      _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments/{experiment_id}/trials")
def experiment_trials(
  experiment_id: UUID,
  limit: int = Query(50, ge=1, le=200),
  cursor: str | None = Query(default=None, max_length=2048),
  revision_id: UUID | None = Query(default=None, alias="revisionId"),
  status: Literal[
    "pending", "queued", "running", "cancelling", "succeeded", "failed", "cancelled"
  ] | None = Query(default=None),
) -> dict[str, Any]:
  try:
    return _experiment_application.list_trials(
      str(experiment_id),
      limit=limit,
      cursor=cursor,
      status=status,
      revision_id=str(revision_id) if revision_id else None,
    )
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments/{experiment_id}/backtest-runs")
def experiment_backtest_runs(
  experiment_id: UUID,
  limit: int = Query(50, ge=1, le=200),
  cursor: str | None = Query(default=None, max_length=2048),
  query: str = Query(default="", max_length=32),
) -> dict[str, Any]:
  try:
    return _experiment_application.list_related_runs(
      str(experiment_id), limit=limit, cursor=cursor, query=query,
    )
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments/{experiment_id}/evaluation-catalogs")
def experiment_evaluation_catalogs(experiment_id: UUID) -> dict[str, Any]:
  try:
    return _evaluation_application.catalogs(str(experiment_id))
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/evaluation-specs")
def save_experiment_evaluation_spec(
  experiment_id: UUID,
  payload: EvaluationSpecRequest,
  request: Request,
) -> Response:
  try:
    body, status_code = _evaluation_application.save_spec(
      str(experiment_id),
      payload,
      _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments/{experiment_id}/evaluation-specs")
def experiment_evaluation_specs(experiment_id: UUID) -> dict[str, Any]:
  try:
    return _evaluation_application.list_specs(str(experiment_id))
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/evaluation-runs")
def create_experiment_evaluation_run(
  experiment_id: UUID,
  payload: EvaluationRunCreateRequest,
  request: Request,
) -> Response:
  try:
    body, status_code = _evaluation_application.create_run(
      str(experiment_id),
      str(payload.evaluationSpecId),
      _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
      payload.forceRecompute,
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiments/{experiment_id}/evaluation-runs")
def experiment_evaluation_runs(experiment_id: UUID) -> dict[str, Any]:
  try:
    return _evaluation_application.list_runs(str(experiment_id))
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get(
  "/api/backtest/experiments/{experiment_id}/evaluation-runs/{evaluation_run_id}",
)
def experiment_evaluation_run_detail(
  experiment_id: UUID,
  evaluation_run_id: UUID,
) -> dict[str, Any]:
  try:
    return _evaluation_application.run_detail(
      str(experiment_id), str(evaluation_run_id),
    )
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post(
  "/api/backtest/experiments/{experiment_id}/evaluation-runs/{evaluation_run_id}/retry",
)
def retry_experiment_evaluation_run(
  experiment_id: UUID,
  evaluation_run_id: UUID,
  request: Request,
) -> Response:
  try:
    body, status_code = _evaluation_application.retry_run(
      str(experiment_id),
      str(evaluation_run_id),
      _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/evaluation-runs/{evaluation_run_id}/exports")
def create_evaluation_export(
  evaluation_run_id: UUID,
  payload: EvaluationExportCreateRequest,
  request: Request,
) -> Response:
  try:
    body, status_code = _evaluation_export_application.create(
      str(evaluation_run_id), payload, _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/evaluation-runs/{evaluation_run_id}/exports")
def evaluation_exports(evaluation_run_id: UUID) -> dict[str, Any]:
  try:
    return _evaluation_export_application.list_exports(str(evaluation_run_id))
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/evaluation-exports/{export_id}")
def evaluation_export_detail(export_id: UUID) -> dict[str, Any]:
  try:
    return _evaluation_export_application.detail(str(export_id))
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/evaluation-exports/{export_id}/cancel")
def cancel_evaluation_export(export_id: UUID, request: Request) -> Response:
  try:
    body, status_code = _evaluation_export_application.cancel(
      str(export_id), _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/evaluation-exports/{export_id}/retry")
def retry_evaluation_export(export_id: UUID, request: Request) -> Response:
  try:
    body, status_code = _evaluation_export_application.retry(
      str(export_id), _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/evaluation-exports/{export_id}/download")
def download_evaluation_export(export_id: UUID) -> FileResponse:
  try:
    export = _evaluation_export_application.download_source(str(export_id))
  except ExperimentError as error:
    _raise_experiment_error(error)
  if export["status"] == "expired":
    raise _stable_error(410, "experiment.export_expired", "评价导出制品已过期")
  if export["status"] != "succeeded":
    raise _stable_error(409, "experiment.export_state_conflict", "评价导出尚未完成")
  root = Path(settings.OUTPUT_ROOT).resolve()
  path = (root / str(export.get("storage_key") or "")).resolve()
  try:
    path.relative_to(root)
  except ValueError as error:
    logger.error("评价导出路径越界: export_id=%s", export_id)
    _evaluation_export_application.expire_artifact(str(export_id), "artifact_missing")
    raise _stable_error(410, "experiment.export_unavailable", "评价导出制品不可用") from error
  expected_size = int(export.get("byte_size") or -1)
  expected_hash = str(export.get("content_sha256") or "")
  if (
    not path.is_file()
    or path.stat().st_size != expected_size
    or _file_content_sha256(path) != expected_hash
  ):
    logger.error("评价导出文件缺失或校验失败: export_id=%s", export_id)
    _evaluation_export_application.expire_artifact(str(export_id), "artifact_missing")
    raise _stable_error(410, "experiment.export_unavailable", "评价导出制品不可用")
  _evaluation_export_application.mark_downloaded(str(export_id))
  return FileResponse(
    path,
    media_type=str(export.get("media_type") or "application/octet-stream"),
    filename=str(export.get("download_filename") or "evaluation-export"),
    headers={
      "Cache-Control": "private, no-store",
      "ETag": f'"{expected_hash}"',
      "Accept-Ranges": "bytes",
    },
  )


def _file_content_sha256(path: Path) -> str:
  content_hash = hashlib.sha256()
  with path.open("rb") as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
      content_hash.update(chunk)
  return content_hash.hexdigest()


@app.post("/api/backtest/experiments/{experiment_id}/cancel")
def cancel_experiment(experiment_id: UUID, request: Request) -> Response:
  try:
    body, status_code = _experiment_application.cancel(
      str(experiment_id),
      _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/archive")
def archive_experiment(
  experiment_id: UUID,
  payload: ExperimentLifecycleRequest,
  request: Request,
) -> Response:
  try:
    body, status_code = _experiment_application.archive(
      str(experiment_id), payload, _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/restore-archive")
def restore_archived_experiment(
  experiment_id: UUID,
  payload: ExperimentLifecycleRequest,
  request: Request,
) -> Response:
  try:
    body, status_code = _experiment_application.restore_archive(
      str(experiment_id), payload, _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/purge")
def purge_experiment(
  experiment_id: UUID,
  payload: ExperimentPurgeRequest,
  request: Request,
) -> Response:
  _require_experiment_admin(request)
  try:
    body, status_code = _experiment_application.purge(
      str(experiment_id), payload, _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/experiment-purges/{purge_id}")
def experiment_purge_detail(purge_id: UUID, request: Request) -> dict[str, Any]:
  _require_experiment_admin(request)
  try:
    return _experiment_purge_repository.get(str(purge_id))
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/resume")
def resume_experiment(
  experiment_id: UUID,
  payload: ExperimentResumeRequest,
  request: Request,
) -> Response:
  try:
    body, status_code = _experiment_application.resume(
      str(experiment_id),
      str(payload.expectedRevisionId),
      payload.reason,
      _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/retry-failed/preview")
def preview_experiment_retry_failed(
  experiment_id: UUID,
  payload: ExperimentRetryPreviewRequest,
) -> dict[str, Any]:
  try:
    return _experiment_application.preview_retry(str(experiment_id), payload)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/backtest/experiments/{experiment_id}/retry-failed")
def retry_experiment_failed(
  experiment_id: UUID,
  payload: ExperimentRetryFailedRequest,
  request: Request,
) -> Response:
  try:
    body, status_code = _experiment_application.retry_failed(
      str(experiment_id),
      payload,
      _experiment_idempotency_key(request),
      getattr(request.state, "request_id", None),
    )
    return JSONResponse(status_code=status_code, content=body)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/backtest/runs")
def backtest_runs(
  limit: int = Query(30, ge=1, le=100),
  offset: int = Query(0, ge=0),
  query: str = Query("", max_length=100),
  symbol: str | None = Query(default=None, pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
  config_id: str | None = Query(default=None, alias="configId"),
  favorites_only: bool = Query(False, alias="favoritesOnly"),
):
  return state_store.list_backtest_run_page(
    limit=limit,
    offset=offset,
    query=query,
    symbol=symbol,
    config_id=config_id,
    favorites_only=favorites_only,
  )


def _stable_error(status_code: int, code: str, message: str) -> HTTPException:
  return HTTPException(status_code, {"code": code, "message": message})


def _idempotency_key(request: Request) -> str:
  value = request.headers.get("idempotency-key", "").strip()
  try:
    return str(UUID(value))
  except (ValueError, AttributeError) as error:
    raise _stable_error(
      422, "signal_export.selection_invalid", "Idempotency-Key 必须是 UUID",
    ) from error


def _selection_payload(selection: Any) -> dict[str, Any]:
  return _model_dump(selection)


@app.post("/api/backtest/runs/selection-summary")
def backtest_run_selection_summary(
  payload: schemas.BacktestRunSelectionRequest,
) -> JSONResponse:
  try:
    summary = state_store.summarize_backtest_run_selection(
      _selection_payload(payload.selection),
    )
  except KeyError as error:
    raise _stable_error(404, "backtest.not_found", "部分回测记录不存在") from error
  except ValueError as error:
    raise _stable_error(422, "backtest.selection_invalid", str(error)) from error
  return JSONResponse(summary, headers={"Cache-Control": "private, no-store"})


@app.post("/api/backtest/signal-detail-exports")
def create_signal_detail_export(
  payload: schemas.SignalDetailExportCreateRequest,
  request: Request,
) -> JSONResponse:
  try:
    export = state_store.create_signal_detail_export(
      _selection_payload(payload.selection), _idempotency_key(request),
    )
  except KeyError as error:
    raise _stable_error(404, "signal_export.not_found", "部分回测记录不存在") from error
  except state_store.RunnerUnavailableError as error:
    raise _stable_error(503, "signal_export.runner_unavailable", str(error)) from error
  except state_store.ConflictError as error:
    raise _stable_error(409, "signal_export.idempotency_conflict", str(error)) from error
  except state_store.ExperimentRunRetainedError as error:
    raise _stable_error(409, "experiment.run_retained", str(error)) from error
  except ValueError as error:
    raise _stable_error(422, "signal_export.selection_invalid", str(error)) from error
  return JSONResponse(
    {"export": export}, headers={"Cache-Control": "private, no-store"},
  )


@app.get("/api/backtest/signal-detail-exports")
def signal_detail_exports(
  limit: int = Query(20, ge=1, le=100),
  cursor: str | None = None,
) -> JSONResponse:
  try:
    result = state_store.list_signal_detail_exports(limit, cursor)
  except ValueError as error:
    raise _stable_error(422, "signal_export.selection_invalid", str(error)) from error
  return JSONResponse(result, headers={"Cache-Control": "private, no-store"})


@app.get("/api/backtest/signal-detail-exports/{export_id}")
def signal_detail_export_detail(export_id: str) -> JSONResponse:
  try:
    export = state_store.get_signal_detail_export(export_id)
  except KeyError as error:
    raise _stable_error(404, "signal_export.not_found", "导出任务不存在") from error
  return JSONResponse(
    {"export": export}, headers={"Cache-Control": "private, no-store"},
  )


@app.post("/api/backtest/signal-detail-exports/{export_id}/cancel")
def cancel_signal_detail_export(export_id: str) -> JSONResponse:
  try:
    export = state_store.cancel_signal_detail_export(export_id)
  except KeyError as error:
    raise _stable_error(404, "signal_export.not_found", "导出任务不存在") from error
  return JSONResponse(
    {"export": export}, headers={"Cache-Control": "private, no-store"},
  )


@app.post("/api/backtest/signal-detail-exports/{export_id}/retry")
def retry_signal_detail_export(export_id: str, request: Request) -> JSONResponse:
  try:
    export = state_store.retry_signal_detail_export(
      export_id, _idempotency_key(request),
    )
  except KeyError as error:
    raise _stable_error(404, "signal_export.not_found", "导出任务不存在") from error
  except state_store.RunnerUnavailableError as error:
    raise _stable_error(503, "signal_export.runner_unavailable", str(error)) from error
  except state_store.ConflictError as error:
    raise _stable_error(409, "signal_export.idempotency_conflict", str(error)) from error
  except state_store.ExperimentRunRetainedError as error:
    raise _stable_error(409, "experiment.run_retained", str(error)) from error
  except ValueError as error:
    raise _stable_error(409, "signal_export.state_conflict", str(error)) from error
  return JSONResponse(
    {"export": export}, headers={"Cache-Control": "private, no-store"},
  )


@app.get("/api/backtest/signal-detail-exports/{export_id}/download")
def download_signal_detail_export(export_id: str) -> FileResponse:
  try:
    export = state_store.get_signal_detail_export_download(export_id)
  except KeyError as error:
    raise _stable_error(404, "signal_export.not_found", "导出任务不存在") from error
  if export["status"] == "expired":
    raise _stable_error(410, "signal_export.file_unavailable", "信号明细文件已过期")
  if export["status"] != "succeeded":
    raise _stable_error(409, "signal_export.state_conflict", "信号明细尚未生成完成")
  expires_at = export.get("expires_at")
  if expires_at is not None and expires_at <= datetime.now(timezone.utc):
    state_store.expire_signal_detail_export(export_id)
    raise _stable_error(410, "signal_export.file_unavailable", "信号明细文件已过期")
  artifact_root = Path(settings.OUTPUT_ROOT).resolve()
  storage_key = str(export.get("storage_key") or "")
  path = (artifact_root / storage_key).resolve()
  try:
    path.relative_to(artifact_root)
  except ValueError as error:
    logger.error("信号明细导出路径越界: export_id=%s", export_id)
    state_store.expire_signal_detail_export(export_id, "artifact_missing")
    raise _stable_error(410, "signal_export.file_unavailable", "信号明细文件不可用") from error
  if not path.is_file() or path.stat().st_size != int(export.get("byte_size") or -1):
    logger.error("信号明细导出文件缺失或大小不一致: export_id=%s", export_id)
    state_store.expire_signal_detail_export(export_id, "artifact_missing")
    raise _stable_error(410, "signal_export.file_unavailable", "信号明细文件不可用")
  state_store.mark_signal_detail_export_downloaded(export_id)
  return FileResponse(
    path,
    media_type="application/zip",
    filename=str(export.get("download_filename") or "signal-details.zip"),
    headers={
      "Cache-Control": "private, no-store",
      "ETag": f'"{export["content_sha256"]}"',
      "Accept-Ranges": "bytes",
    },
  )


@app.post("/api/backtest/runs/{run_id}/publication")
def publish_backtest_run(
  run_id: str,
  payload: schemas.SharedBacktestPublicationCreate,
) -> dict[str, Any]:
  try:
    publication, created = state_store.create_shared_backtest_publication(
      run_id,
      payload.title,
      payload.description,
      payload.tags,
    )
    return {"publication": publication, "created": created}
  except KeyError as error:
    raise HTTPException(404, "回测记录不存在或无权共享") from error
  except state_store.ExperimentRunRetainedError as error:
    raise _stable_error(409, "experiment.run_retained", str(error)) from error
  except ValueError as error:
    raise HTTPException(422, str(error)) from error
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"共享回测创建失败: {error}") from error


@app.get("/api/shared-backtests")
def shared_backtests(
  response: Response,
  limit: int = Query(20, ge=1, le=100),
  offset: int = Query(0, ge=0),
  query: str = Query("", max_length=100),
  strategy_category: str | None = Query(default=None, alias="strategyCategory", max_length=32),
  timeframe: str | None = Query(default=None, max_length=16),
  sort: Literal["latest", "copies"] = Query("latest"),
  favorites_only: bool = Query(False, alias="favoritesOnly"),
) -> dict[str, Any]:
  response.headers["Cache-Control"] = "private, no-store"
  return shared_backtest_discovery.list_shared_backtests(
    limit=limit,
    offset=offset,
    query=query,
    strategy_category=strategy_category,
    timeframe=timeframe,
    sort=sort,
    favorites_only=favorites_only,
  )


@app.post("/api/shared-backtests/{publication_id}/favorite")
def favorite_shared_backtest(publication_id: str) -> dict[str, bool]:
  try:
    created = shared_backtest_discovery.favorite_shared_backtest(publication_id)
  except KeyError as error:
    raise HTTPException(404, "该共享回测已不可用") from error
  return {"favorited": True, "created": created}


@app.delete("/api/shared-backtests/{publication_id}/favorite")
def unfavorite_shared_backtest(publication_id: str) -> dict[str, bool]:
  deleted = shared_backtest_discovery.unfavorite_shared_backtest(publication_id)
  return {"favorited": False, "deleted": deleted}


@app.get("/api/admin/shared-backtests")
def admin_shared_backtests(
  request: Request,
  limit: int = Query(20, ge=1, le=100),
  offset: int = Query(0, ge=0),
  status: str | None = Query(default=None, pattern=r"^(active|withdrawn|removed)$"),
) -> dict[str, Any]:
  _require_admin(request)
  return shared_backtest_admin.list_shared_backtests_for_admin(limit, offset, status)


@app.post("/api/admin/shared-backtests/{publication_id}/remove")
def admin_remove_shared_backtest(
  publication_id: str,
  payload: schemas.SharedBacktestRemovalRequest,
  request: Request,
) -> dict[str, Any]:
  _require_admin(request)
  return _admin_publication_transition(
    lambda: shared_backtest_admin.remove_publication(publication_id, payload.reason),
  )


@app.post("/api/admin/shared-backtests/{publication_id}/restore")
def admin_restore_shared_backtest(
  publication_id: str,
  request: Request,
) -> dict[str, Any]:
  _require_admin(request)
  return _admin_publication_transition(
    lambda: shared_backtest_admin.restore_publication(publication_id),
  )


def _admin_publication_transition(
  transition: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
  try:
    return {"publication": transition()}
  except KeyError as error:
    raise HTTPException(404, "共享回测不存在") from error
  except ValueError as error:
    raise HTTPException(409, str(error)) from error
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"共享回测管理员状态更新失败: {error}") from error


@app.get("/api/shared-backtests/mine")
def my_shared_backtests(
  limit: int = Query(20, ge=1, le=100),
  offset: int = Query(0, ge=0),
  status: str | None = Query(default=None, pattern=r"^(active|withdrawn|removed)$"),
) -> dict[str, Any]:
  return shared_backtest_lifecycle.list_my_shared_backtests(limit, offset, status)


@app.post("/api/shared-backtests/{publication_id}/copy")
def copy_shared_backtest(publication_id: str) -> dict[str, Any]:
  try:
    return shared_backtest_copy.copy_shared_backtest(publication_id)
  except KeyError as error:
    raise HTTPException(404, "该共享回测已不可用") from error
  except ValueError as error:
    raise HTTPException(409, str(error)) from error
  except state_store.ConflictError as error:
    raise HTTPException(409, str(error)) from error
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"复制共享回测失败: {error}") from error


@app.get("/api/shared-backtests/{publication_id}/chart")
def shared_backtest_chart(publication_id: str) -> dict[str, Any]:
  try:
    return shared_backtest_detail_service.get_shared_backtest_chart(publication_id)
  except KeyError as error:
    raise HTTPException(404, "该共享回测已不可用") from error
  except ValueError as error:
    raise HTTPException(409, str(error)) from error


@app.get("/api/shared-backtests/{publication_id}/{section}")
def shared_backtest_result_page(
  publication_id: str,
  section: Literal["holdings", "trades", "signals", "execution-plans", "cycles"],
  cursor: int = Query(0, ge=0),
  limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
  try:
    return shared_backtest_detail_service.get_shared_backtest_result_page(
      publication_id,
      section,
      cursor,
      limit,
    )
  except KeyError as error:
    raise HTTPException(404, "该共享回测已不可用") from error
  except ValueError as error:
    raise HTTPException(409, str(error)) from error


@app.get("/api/shared-backtests/{publication_id}")
def shared_backtest_detail(
  publication_id: str,
  response: Response,
) -> dict[str, Any]:
  response.headers["Cache-Control"] = "private, no-store"
  try:
    return {"publication": state_store.get_shared_backtest(publication_id)}
  except KeyError as error:
    raise HTTPException(404, "该共享回测已不可用") from error
  except ValueError as error:
    raise HTTPException(409, str(error)) from error


@app.get("/api/shared-backtests/{publication_id}/artifacts/{artifact_id}")
def download_shared_backtest_artifact(
  publication_id: str,
  artifact_id: str,
) -> FileResponse:
  """鉴权下载仍处于已上架状态的共享附件。"""
  try:
    artifact = state_store.get_shared_backtest_artifact(publication_id, artifact_id)
    path = shared_backtest_artifacts.verified_artifact_path(
      str(artifact["storage_key"]),
      str(artifact["content_sha256"]),
      int(artifact["byte_size"]),
    )
  except KeyError as error:
    raise HTTPException(404, "该共享回测已不可用") from error
  except shared_backtest_artifacts.SharedArtifactError as error:
    raise HTTPException(409, str(error)) from error
  return FileResponse(
    path,
    media_type=artifact.get("content_type") or "application/octet-stream",
    filename=shared_backtest_artifacts.safe_filename(artifact.get("display_name")),
  )


@app.patch("/api/shared-backtests/{publication_id}")
def update_shared_backtest_metadata(
  publication_id: str,
  payload: schemas.SharedBacktestMetaUpdate,
) -> dict[str, Any]:
  try:
    publication = shared_backtest_lifecycle.update_publication_metadata(
      publication_id,
      payload.title,
      payload.description,
      payload.tags,
    )
    return {"publication": publication}
  except KeyError as error:
    raise HTTPException(404, "共享回测不存在或无管理权限") from error
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"共享展示信息更新失败: {error}") from error


@app.post("/api/shared-backtests/{publication_id}/withdraw")
def withdraw_shared_backtest(publication_id: str) -> dict[str, Any]:
  return _author_publication_transition(
    publication_id,
    shared_backtest_lifecycle.withdraw_publication,
  )


@app.post("/api/shared-backtests/{publication_id}/relist")
def relist_shared_backtest(publication_id: str) -> dict[str, Any]:
  return _author_publication_transition(
    publication_id,
    shared_backtest_lifecycle.relist_publication,
  )


def _author_publication_transition(
  publication_id: str,
  transition: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
  try:
    return {"publication": transition(publication_id)}
  except KeyError as error:
    raise HTTPException(404, "共享回测不存在或无管理权限") from error
  except ValueError as error:
    raise HTTPException(409, str(error)) from error
  except state_store.DatabaseError as error:
    raise HTTPException(500, f"共享回测状态更新失败: {error}") from error


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
  if not settings.validate_backtest_timeframe(selected_timeframe):
    raise HTTPException(422, f"不支持的周期: {selected_timeframe}")
  try:
    bars = data_repository.load_chart_bars(run["symbol"], date, selected_timeframe)
  except BacktestFault as error:
    logger.exception("读取回测图表行情失败: %s %s", run_id, date)
    raise _backtest_failure_response(error, run_id) from error

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


@app.put("/api/backtest/runs/{run_id}/favorite")
def set_backtest_run_favorite(
  run_id: str,
  payload: schemas.BacktestRunFavoriteUpdate,
) -> dict[str, Any]:
  try:
    return state_store.set_backtest_run_favorite(run_id, payload.favorited)
  except KeyError as error:
    raise HTTPException(404, "回测记录不存在") from error


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
  except BacktestFault as error:
    raise _backtest_failure_response(error, None) from error
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


@app.get("/api/v1/strategies")
def public_strategies(
  request: Request,
  limit: int = Query(20, ge=1, le=100),
  cursor: int = Query(0, ge=0),
  archived: bool | None = None,
  enabled: bool | None = None,
):
  _require_public_scope(request, "strategy:read")
  return strategy_lifecycle.list_strategies(limit, cursor, archived, enabled)


@app.get("/api/v1/strategies/{config_id}")
def public_strategy_detail(config_id: str, request: Request):
  _require_public_scope(request, "strategy:read")
  try:
    return {"strategy": strategy_lifecycle.get_strategy(config_id)}
  except Exception as error:
    _raise_public_strategy_error(error)


@app.post("/api/v1/strategies")
def public_create_strategy(
  payload: schemas.PublicStrategyCreateRequest,
  request: Request,
):
  api_key = _require_public_scope(request, "strategy:write")
  try:
    result = strategy_lifecycle.save_strategy(
      _public_strategy_command(payload),
      _public_write_context(request, api_key, "strategy.create", payload),
    )
  except Exception as error:
    _raise_public_strategy_error(error)
  status_code = int(result.pop("response_status"))
  return JSONResponse(
    status_code=status_code,
    content={**result, "request_id": request.state.request_id},
  )


@app.put("/api/v1/strategies/{config_id}")
def public_update_strategy(
  config_id: str,
  payload: schemas.PublicStrategyUpdateRequest,
  request: Request,
):
  api_key = _require_public_scope(request, "strategy:write")
  try:
    result = strategy_lifecycle.save_strategy(
      _public_strategy_command(payload, config_id),
      _public_write_context(request, api_key, "strategy.update", payload),
    )
  except Exception as error:
    _raise_public_strategy_error(error)
  status_code = int(result.pop("response_status"))
  return JSONResponse(
    status_code=status_code,
    content={**result, "request_id": request.state.request_id},
  )


def _public_strategy_revision_action(
  config_id: str,
  payload: schemas.PublicStrategyRevisionRequest,
  request: Request,
  scope: str,
  operation_type: str,
  action: Any,
) -> JSONResponse:
  api_key = _require_public_scope(request, scope)
  try:
    result = action(
      config_id,
      payload.expected_revision,
      _public_write_context(request, api_key, operation_type, payload),
    )
  except Exception as error:
    _raise_public_strategy_error(error)
  status_code = int(result.pop("response_status"))
  return JSONResponse(
    status_code=status_code,
    content={**result, "request_id": request.state.request_id},
  )


@app.post("/api/v1/strategies/{config_id}/publish")
def public_publish_strategy(
  config_id: str,
  payload: schemas.PublicStrategyRevisionRequest,
  request: Request,
):
  return _public_strategy_revision_action(
    config_id, payload, request, "strategy:publish", "strategy.publish",
    strategy_lifecycle.publish_strategy,
  )


@app.post("/api/v1/strategies/{config_id}/archive")
def public_archive_strategy(
  config_id: str,
  payload: schemas.PublicStrategyRevisionRequest,
  request: Request,
):
  return _public_strategy_revision_action(
    config_id, payload, request, "strategy:delete", "strategy.archive",
    strategy_lifecycle.archive_strategy,
  )


@app.post("/api/v1/strategies/{config_id}/restore")
def public_restore_strategy(
  config_id: str,
  payload: schemas.PublicStrategyRevisionRequest,
  request: Request,
):
  return _public_strategy_revision_action(
    config_id, payload, request, "strategy:write", "strategy.restore",
    strategy_lifecycle.restore_strategy,
  )


@app.get("/api/v1/strategies/{config_id}/versions")
def public_strategy_versions(
  config_id: str,
  request: Request,
  limit: int = Query(20, ge=1, le=100),
  cursor: int = Query(0, ge=0),
):
  _require_public_scope(request, "strategy:read")
  try:
    return strategy_lifecycle.list_versions(config_id, limit, cursor)
  except Exception as error:
    _raise_public_strategy_error(error)


@app.get("/api/v1/strategies/{config_id}/versions/{version_id}")
def public_strategy_version_detail(
  config_id: str,
  version_id: str,
  request: Request,
):
  _require_public_scope(request, "strategy:read")
  try:
    return {"version": strategy_lifecycle.get_version(config_id, version_id)}
  except Exception as error:
    _raise_public_strategy_error(error)


@app.post("/api/v1/experiments/preview")
def public_preview_experiment(
  payload: ExperimentPreviewRequest,
  request: Request,
) -> dict[str, Any]:
  _require_public_scope(request, "experiment:submit")
  try:
    return _experiment_application.preview(payload)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/v1/experiments")
def public_create_experiment(
  payload: ExperimentCreateRequest,
  request: Request,
) -> Response:
  api_key = _require_public_scope(request, "experiment:submit")
  return _public_experiment_command(
    request,
    api_key,
    "experiment.create",
    lambda idempotency_key, request_id: _experiment_application.create(
      payload, idempotency_key, request_id,
    ),
  )


@app.get("/api/v1/experiments")
def public_experiments(
  request: Request,
  limit: int = Query(50, ge=1, le=200),
  cursor: str | None = Query(default=None, max_length=2048),
) -> dict[str, Any]:
  _require_public_scope(request, "experiment:read")
  try:
    return _experiment_application.list_experiments(
      limit=limit, cursor=cursor, archived=False,
    )
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.get("/api/v1/experiments/{experiment_id}")
def public_experiment_detail(experiment_id: UUID, request: Request) -> Response:
  _require_public_scope(request, "experiment:read")
  try:
    body = _experiment_application.detail(str(experiment_id))
  except ExperimentError as error:
    _raise_experiment_error(error)
  etag = f'"{body["snapshotVersion"]}"'
  if request.headers.get("if-none-match") == etag:
    return Response(status_code=304, headers={"ETag": etag})
  return JSONResponse(content=body, headers={"ETag": etag})


@app.get("/api/v1/experiments/{experiment_id}/trials")
def public_experiment_trials(
  experiment_id: UUID,
  request: Request,
  limit: int = Query(50, ge=1, le=200),
  cursor: str | None = Query(default=None, max_length=2048),
  revision_id: UUID | None = Query(default=None, alias="revisionId"),
  status: Literal[
    "pending", "queued", "running", "cancelling", "succeeded", "failed", "cancelled"
  ] | None = Query(default=None),
) -> dict[str, Any]:
  _require_public_scope(request, "experiment:read")
  try:
    return _experiment_application.list_trials(
      str(experiment_id),
      limit=limit,
      cursor=cursor,
      status=status,
      revision_id=str(revision_id) if revision_id else None,
    )
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/v1/experiments/{experiment_id}/retry-failed/preview")
def public_preview_experiment_retry_failed(
  experiment_id: UUID,
  payload: ExperimentRetryPreviewRequest,
  request: Request,
) -> dict[str, Any]:
  _require_public_scope(request, "experiment:submit")
  try:
    return _experiment_application.preview_retry(str(experiment_id), payload)
  except ExperimentError as error:
    _raise_experiment_error(error)


@app.post("/api/v1/experiments/{experiment_id}/retry-failed")
def public_retry_experiment_failed(
  experiment_id: UUID,
  payload: ExperimentRetryFailedRequest,
  request: Request,
) -> Response:
  api_key = _require_public_scope(request, "experiment:submit")
  experiment_id_value = str(experiment_id)
  return _public_experiment_command(
    request,
    api_key,
    "experiment.retry_failed",
    lambda idempotency_key, request_id: _experiment_application.retry_failed(
      experiment_id_value, payload, idempotency_key, request_id,
    ),
    experiment_id_value,
  )


@app.post("/api/v1/experiments/{experiment_id}/resume")
def public_resume_experiment(
  experiment_id: UUID,
  payload: ExperimentResumeRequest,
  request: Request,
) -> Response:
  api_key = _require_public_scope(request, "experiment:submit")
  experiment_id_value = str(experiment_id)
  return _public_experiment_command(
    request,
    api_key,
    "experiment.resume",
    lambda idempotency_key, request_id: _experiment_application.resume(
      experiment_id_value,
      str(payload.expectedRevisionId),
      payload.reason,
      idempotency_key,
      request_id,
    ),
    experiment_id_value,
  )


@app.post("/api/v1/experiments/{experiment_id}/cancel")
def public_cancel_experiment(experiment_id: UUID, request: Request) -> Response:
  api_key = _require_public_scope(request, "experiment:cancel")
  experiment_id_value = str(experiment_id)
  return _public_experiment_command(
    request,
    api_key,
    "experiment.cancel",
    lambda idempotency_key, request_id: _experiment_application.cancel(
      experiment_id_value, idempotency_key, request_id,
    ),
    experiment_id_value,
  )


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
      getattr(request.state, "request_id", None),
    )
  except state_store.ConflictError as error:
    raise HTTPException(409, str(error)) from error
  except state_store.RunnerUnavailableError as error:
    raise HTTPException(503, str(error)) from error
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
  status = state_store.request_backtest_cancel(run_id)
  if status is None:
    raise HTTPException(404, "未找到可取消的回测任务")
  return {"run_id": run_id, "status": status, "request_id": request.state.request_id}


@app.post("/api/backtest/run")
def run_backtest(payload: schemas.BacktestRunRequest, http_request: Request):
  request = _model_dump(payload)
  _validate_backtest_request(request)
  _require_backtest_runner(str(request.get("fillModel", "bar_next_open")))

  idempotency_key = http_request.headers.get("idempotency-key", "").strip()
  if idempotency_key:
    if not 8 <= len(idempotency_key) <= 128:
      raise HTTPException(422, "Idempotency-Key 必须为 8 至 128 个字符")
    request_hash = hashlib.sha256(json.dumps(
      request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    try:
      run_id, display_no, replayed, run_status, fill_model = (
        state_store.create_browser_backtest_job(
          request, idempotency_key, request_hash,
        )
      )
    except state_store.IdempotencyConflictError as error:
      raise HTTPException(409, str(error)) from error
    return {
      "runId": run_id,
      "displayNo": display_no,
      "configId": request.get("configId"),
      "configVersionId": request.get("configVersionId"),
      "scriptId": request.get("scriptId"),
      "status": run_status,
      "fillModel": fill_model,
      "replayed": replayed,
      "message": "回测幂等请求已返回原任务" if replayed else "回测已进入队列，等待执行",
    }

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
  """幂等请求取消，并返回数据库中的真实任务状态。"""
  status = state_store.request_backtest_cancel(run_id)
  if status is None:
    raise HTTPException(404, "未找到可取消的回测任务")
  return {"runId": run_id, "status": status}


@app.delete("/api/backtest/runs")
def delete_backtest_runs(payload: schemas.BacktestRunBatchDeleteRequest):
  try:
    if payload.selection is not None:
      deleted = state_store.delete_backtest_run_selection(
        _selection_payload(payload.selection),
        int(payload.expectedSelectedRunCount or 0),
        str(payload.expectedSelectionDigest or ""),
      )
    else:
      deleted = state_store.delete_backtest_runs(payload.runIds or [])
  except KeyError as error:
    raise _stable_error(404, "backtest.not_found", "部分回测记录不存在") from error
  except state_store.SelectionChangedError as error:
    raise _stable_error(409, "backtest.selection_changed", str(error)) from error
  except state_store.ExperimentRunRetainedError as error:
    raise _stable_error(409, "experiment.run_retained", str(error)) from error
  except ValueError as error:
    status_code = 422 if payload.selection is not None else 409
    raise _stable_error(status_code, "backtest.selection_invalid", str(error)) from error
  return {"deleted": deleted}


@app.delete("/api/backtest/runs/{run_id}")
def delete_backtest_run(run_id: str):
  try:
    deleted = state_store.delete_backtest_run(run_id)
  except KeyError as error:
    raise HTTPException(404, "回测记录不存在") from error
  except state_store.ExperimentRunRetainedError as error:
    raise _stable_error(409, "experiment.run_retained", str(error)) from error
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
  if not settings.validate_backtest_timeframe(timeframe):
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
  if not settings.validate_backtest_timeframe(timeframe):
    raise HTTPException(422, f"不支持的周期: {timeframe}")

  try:
    result = data_repository.load_chart_data(
      symbol, date, timeframe, strategy,
      signal_file=signal_file or None,
    )
  except BacktestFault as error:
    logger.exception("读取行情图表失败: %s %s %s", symbol, timeframe, date)
    raise _backtest_failure_response(error, None) from error
  if "error" in result:
    raise HTTPException(404, result["error"])
  return result


@app.get("/api/data/status")
def data_status(
  symbol: str = Query(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
  timeframe: str = Query("5m"),
  fill_model: str = Query("bar_next_open", alias="fillModel"),
):
  if not settings.validate_backtest_timeframe(timeframe):
    raise HTTPException(422, f"不支持的周期: {timeframe}")
  normalized_fill_model = fill_model if isinstance(fill_model, str) else "bar_next_open"
  if normalized_fill_model not in {"bar_next_open", "l2_quote"}:
    raise HTTPException(422, f"不支持的撮合方式: {normalized_fill_model}")
  try:
    status = (
      l2_quote_availability.status(symbol, timeframe)
      if normalized_fill_model == "l2_quote" else market_data.status(symbol, timeframe)
    )
    return {"status": {**status, "fillModel": normalized_fill_model}}
  except Exception as error:
    logger.exception(
      "读取可回测行情范围失败: %s %s %s", symbol, timeframe, normalized_fill_model,
    )
    raise HTTPException(503, {
      "code": "data.source_unavailable",
      "message": "行情数据异常，请联系管理员",
      "retryable": True,
    }) from error


if __name__ == "__main__":
  import uvicorn
  uvicorn.run(app, host="0.0.0.0", port=8000)
