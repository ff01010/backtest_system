"""标准 Bar 用户策略的单任务隔离子进程。"""

from __future__ import annotations

import multiprocessing
import os
import re
import time
import traceback
from collections.abc import Callable
from dataclasses import asdict
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from backend.backtesting import Bar, BarBacktestEngine, FeeProfile, NextOpenExecutionModel
from backend.backtesting.legacy import fill_to_api_trade
from backend.services.backtest_errors import RemoteBacktestError, error_detail
from backend.services.system_error_catalog import root_cause
from backend.strategies.runtime_contract import (
  RUNTIME_CAPABILITY_DIGEST,
  SCRIPT_RUNTIME_VERSION,
  drop_strategy_process_privileges,
  missing_name,
  restore_numeric_thread_environment,
  strategy_source_line,
)
from backend.strategies.scripted import ScriptedBarStrategy


class BarStrategyProcessError(RuntimeError):
  """Bar 策略子进程异常退出或 IPC 协议失效。"""


class BarStrategySession:
  """每个 Bar 任务启动一个无服务凭据的策略子进程。"""

  _POLL_SECONDS = 0.2

  def __init__(self, abort_check: Callable[[], None] | None = None, cancel_grace_seconds: int = 5) -> None:
    self._abort_check = abort_check
    self._cancel_grace_seconds = cancel_grace_seconds

  def run_script(self, payload: dict[str, Any]) -> dict[str, Any]:
    self._check_abort()
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_bar_process_main, args=(child, payload), daemon=True)
    process.start()
    child.close()
    try:
      while not parent.poll(self._POLL_SECONDS):
        try:
          self._check_abort()
        except Exception:
          _terminate(process, self._cancel_grace_seconds)
          raise
        if not process.is_alive():
          raise BarStrategyProcessError("Bar 策略子进程异常退出")
      message = parent.recv()
      return _parse_response(message)
    except (EOFError, OSError) as error:
      raise BarStrategyProcessError("Bar 策略子进程通信中断") from error
    finally:
      parent.close()
      process.join(timeout=1)
      if process.is_alive():
        _terminate(process, self._cancel_grace_seconds)

  def _check_abort(self) -> None:
    if self._abort_check is not None:
      self._abort_check()


def _bar_process_main(connection: Connection, payload: dict[str, Any]) -> None:
  """只接收冻结脚本、参数和行情，不继承数据库或外部服务配置。"""
  try:
    os.environ.clear()
    restore_numeric_thread_environment()
    drop_strategy_process_privileges()
    connection.send({"status": "ok", "result": _execute_script(payload)})
  except Exception as error:
    try:
      connection.send({
        "status": "error",
        "detail": error_detail(error).to_dict(),
        "diagnostic": _error_diagnostic(error),
      })
    except (BrokenPipeError, EOFError, OSError):
      pass
  finally:
    connection.close()


def _execute_script(payload: dict[str, Any]) -> dict[str, Any]:
  bars = [Bar(
    time=str(item["time"]), symbol=str(item["symbol"]), open=float(item["open"]),
    high=float(item["high"]), low=float(item["low"]), close=float(item["close"]),
    volume=float(item.get("volume", 0)),
  ) for item in payload["bars"]]
  strategy = ScriptedBarStrategy(
    str(payload["scriptId"]), str(payload["code"]), dict(payload.get("params") or {}),
    runtime_version=str(payload.get("runtimeVersion") or "legacy"),
    capability_digest=str(payload.get("capabilityDigest") or "legacy"),
  )
  commission_rate = float(payload["commissionRate"])
  fee_profile = dict(payload.get("feeProfile") or {})
  engine = BarBacktestEngine(
    initial_cash=float(payload["initialCapital"]),
    commission_rate=commission_rate,
    fee_profile=FeeProfile(
      commission_rate=float(fee_profile.get("commissionRate", commission_rate)),
      transfer_fee_rate=float(fee_profile.get("transferFeeRate", 0.00001)),
      stamp_duty_rate=float(fee_profile.get("stampDutyRate", 0.0005)),
      minimum_commission=float(fee_profile.get("minimumCommission", 5)),
    ),
    execution_model=NextOpenExecutionModel(slippage=float(payload.get("slippage", 0))),
    holding_policy=str(payload.get("holdingPolicy") or "intraday"),
    execute_on_next_bar_open=True,
    base_quantity=int(payload.get("baseQuantity", 0)),
    base_cost_price=float(payload.get("baseCostPrice", 0)),
    t0_enabled=bool(payload.get("t0Enabled", False)),
    strategy_category=str(payload.get("strategyCategory") or "directional_holding"),
    overlay_settlement_policy=str(payload.get("overlaySettlementPolicy") or "intraday_restore"),
  )
  result = engine.run(strategy, bars, int(payload.get("warmupBarCount", 0)))
  return {
    "fills": [asdict(fill) for fill in result.fills],
    "trades": [fill_to_api_trade(fill) for fill in result.fills],
    "portfolioSnapshots": [_snapshot_to_dict(item) for item in result.portfolio_snapshots],
    "skippedSignals": strategy.skipped_signals,
    "unexecutedOrders": [_unexecuted_order_to_api(item) for item in result.unexecuted_orders],
    "warnings": strategy.direction_warnings,
  }


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
  return {
    "cash": float(snapshot.cash),
    "equity": float(snapshot.equity),
    "longPositions": {
      symbol: {"quantity": position.quantity, "averagePrice": position.average_price}
      for symbol, position in snapshot.long_positions.items()
    },
    "shortPositions": {
      symbol: {"quantity": position.quantity, "averagePrice": position.average_price}
      for symbol, position in snapshot.short_positions.items()
    },
    "basePosition": int(snapshot.base_position),
    "overlayQuantity": snapshot.overlay_quantity,
    "sellableQuantity": snapshot.sellable_quantity,
  }


def _unexecuted_order_to_api(order: Any) -> dict[str, Any]:
  metadata = dict(order.metadata)
  return {
    "time": order.time, "signalTime": order.time, "executionTime": None,
    "direction": str(metadata.get("direction") or order.side.value),
    "action": str(metadata.get("direction") or order.side.value),
    "side": order.side.value, "anchor": 0.0,
    "price": float(metadata.get("recorded_price") or 0.0), "quantity": order.quantity,
    "pnl": 0.0, "note": "回测区间结束，下一开盘委托未执行",
    "priceSource": "当前 Bar 收盘价（未成交）", "status": "unexecuted",
    "strategyModule": "用户脚本",
  }


def _parse_response(message: object) -> dict[str, Any]:
  if not isinstance(message, dict):
    raise BarStrategyProcessError("Bar 策略子进程返回格式无效")
  if message.get("status") == "error":
    detail = message.get("detail")
    diagnostic = message.get("diagnostic")
    if isinstance(detail, dict):
      raise RemoteBacktestError.from_dict(detail, diagnostic if isinstance(diagnostic, dict) else None)
    raise BarStrategyProcessError("Bar 策略子进程未返回结构化错误")
  result = message.get("result")
  if message.get("status") != "ok" or not isinstance(result, dict):
    raise BarStrategyProcessError("Bar 策略子进程返回了未知状态")
  return result


def _terminate(process: multiprocessing.Process, grace_seconds: int) -> None:
  if process.is_alive():
    process.terminate()
    process.join(timeout=grace_seconds)
  if process.is_alive() and hasattr(process, "kill"):
    process.kill()
    process.join(timeout=2)


def _error_diagnostic(error: Exception) -> dict[str, str]:
  cause = root_cause(error)
  trace = traceback.extract_tb(cause.__traceback__)[-8:]
  reason = re.sub(
    r"(?i)\b(password|token|secret|authorization)\b\s*[:=]\s*\S+", r"\1=***", str(cause),
  )
  diagnostic = {
    "exceptionType": type(cause).__name__,
    "reason": reason[:500],
    "trace": " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in trace),
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
  }
  name = missing_name(cause)
  line = strategy_source_line(cause)
  if name:
    diagnostic["missingName"] = name
  if line:
    diagnostic["strategyLine"] = str(line)
  return diagnostic
