"""标准 Bar 用户策略的单任务隔离子进程。"""

from __future__ import annotations

import logging
import multiprocessing
import os
import re
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Literal

import pandas as pd

from backend import settings
from backend.bar_market_data_transport import (
  BAR_DTYPE,
  AttachedBarMarketData,
  BarMarketDataRuntime,
  BarSequence,
  ParentBarMarketData,
  attach_bar_market_data,
)
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

logger = logging.getLogger(__name__)


class BarStrategyProcessError(RuntimeError):
  """Bar 策略子进程异常退出或 IPC 协议失效。"""

  def __init__(
    self,
    message: str,
    *,
    exit_code: int | None = None,
    termination_reason: str = "unknown_interruption",
  ) -> None:
    super().__init__(message)
    self.exit_code = exit_code
    self.termination_reason = termination_reason


class BarStrategySession:
  """每个 Bar 任务启动一个无服务凭据的策略子进程。"""

  _POLL_SECONDS = 0.2

  def __init__(
    self,
    abort_check: Callable[[], None] | None = None,
    cancel_grace_seconds: int = 5,
    *,
    ipc_mode: Literal["legacy", "auto"] | None = None,
    market_data_runtime: BarMarketDataRuntime | None = None,
    transport_override: Literal["shared_memory", "mmap"] | None = None,
  ) -> None:
    self._abort_check = abort_check
    self._cancel_grace_seconds = cancel_grace_seconds
    self._ipc_mode = ipc_mode or settings.BAR_IPC_MODE
    self._market_data_runtime = market_data_runtime
    self._transport_override = transport_override

  def run_script(
    self,
    payload: dict[str, Any],
    bars: pd.DataFrame,
  ) -> dict[str, Any]:
    self._check_abort()
    if self._uses_market_data_reference(payload):
      return self._run_with_market_data_reference(payload, bars)
    return self._run_process(_legacy_payload(payload, bars), None, "")

  def _uses_market_data_reference(self, payload: dict[str, Any]) -> bool:
    signals = (payload.get("params") or {}).get("_researchSignalByBarTime")
    return (
      self._ipc_mode == "auto"
      and payload.get("strategy", "user_script") == "user_script"
      and not signals
    )

  def _run_with_market_data_reference(
    self,
    payload: dict[str, Any],
    bars: pd.DataFrame,
  ) -> dict[str, Any]:
    owns_runtime = self._market_data_runtime is None
    runtime = self._market_data_runtime or BarMarketDataRuntime(
      settings.BAR_IPC_TEMP_ROOT,
      shared_memory_limit_bytes=settings.BAR_RUNNER_SHM_SIZE_BYTES,
    )
    task_uuid = uuid.uuid4().hex
    resource: ParentBarMarketData | None = None
    status = "failed"
    try:
      resource = runtime.create(
        bars,
        str(payload["symbol"]),
        int(payload.get("warmupBarCount", 0)),
        task_uuid=task_uuid,
        force_transport=self._transport_override,
      )
      child_payload = dict(payload)
      child_payload.pop("warmupBarCount", None)
      child_payload["marketDataRef"] = dict(resource.reference)
      result = self._run_process(child_payload, resource, str(runtime.root))
      status = "completed"
      return result
    finally:
      try:
        if resource is not None:
          resource.close()
          status = "cleaned" if status == "completed" else "failed_cleaned"
      finally:
        try:
          _log_market_data_lifecycle(
            task_uuid, resource, len(bars), status, self._transport_override,
          )
        finally:
          if owns_runtime:
            runtime.close()

  def _run_process(
    self,
    payload: dict[str, Any],
    resource: ParentBarMarketData | None,
    managed_root: str,
  ) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
      target=_bar_process_main,
      args=(child, payload, managed_root),
      daemon=True,
    )
    started = False
    try:
      process.start()
      started = True
      child.close()
      while True:
        while not parent.poll(self._POLL_SECONDS):
          try:
            self._check_abort()
          except Exception:
            _terminate(process, self._cancel_grace_seconds)
            raise
          if not process.is_alive():
            raise _process_exit_error(process, "Bar 策略子进程异常退出")
        message = parent.recv()
        if isinstance(message, dict) and message.get("status") == "market_data_attached":
          if resource is None:
            raise BarStrategyProcessError("Bar 策略子进程返回了意外的行情挂载确认")
          resource.mark_attached()
          continue
        return _parse_response(message)
    except (EOFError, OSError) as error:
      process.join(timeout=0.2)
      raise _process_exit_error(process, "Bar 策略子进程通信中断") from error
    finally:
      parent.close()
      child.close()
      if started:
        process.join(timeout=1)
        if process.is_alive():
          _terminate(process, self._cancel_grace_seconds)

  def _check_abort(self) -> None:
    if self._abort_check is not None:
      self._abort_check()


def _bar_process_main(
  connection: Connection,
  payload: dict[str, Any],
  managed_root: str,
) -> None:
  """只接收冻结脚本、参数和行情，不继承数据库或外部服务配置。"""
  attached: AttachedBarMarketData | None = None
  bars: Sequence[Bar] | None = None
  try:
    os.environ.clear()
    restore_numeric_thread_environment()
    if isinstance(payload.get("marketDataRef"), dict):
      attached = attach_bar_market_data(payload["marketDataRef"], managed_root)
      connection.send({"status": "market_data_attached"})
      bars = attached.bars
      warmup_bar_count = int(payload["marketDataRef"]["warmupBarCount"])
    else:
      bars = _legacy_bars(payload)
      warmup_bar_count = int(payload.get("warmupBarCount", 0))
    drop_strategy_process_privileges()
    if bars is None:
      raise RuntimeError("Bar 行情未挂载")
    connection.send({
      "status": "ok",
      "result": _execute_script(payload, bars, warmup_bar_count),
    })
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
    bars = None
    if attached is not None:
      attached.close()
    connection.close()


def _log_market_data_lifecycle(
  task_uuid: str,
  resource: ParentBarMarketData | None,
  row_count: int,
  status: str,
  requested_transport: Literal["shared_memory", "mmap"] | None,
) -> None:
  if resource is None:
    logger.info(
      "Bar IPC 资源生命周期: task=%s transport=%s rows=%s bytes=%s status=create_failed fallback=none",
      task_uuid,
      requested_transport or "auto",
      row_count,
      row_count * BAR_DTYPE.itemsize,
    )
    return
  logger.info(
    "Bar IPC 资源生命周期: task=%s transport=%s rows=%s bytes=%s status=%s fallback=%s",
    task_uuid,
    resource.reference["transport"],
    resource.reference["rowCount"],
    resource.reference["dataByteSize"],
    status,
    resource.fallback_reason or "none",
  )


def _legacy_bars(payload: dict[str, Any]) -> list[Bar]:
  return [Bar(
    time=str(item["time"]), symbol=str(item["symbol"]), open=float(item["open"]),
    high=float(item["high"]), low=float(item["low"]), close=float(item["close"]),
    volume=float(item.get("volume", 0)),
  ) for item in payload["bars"]]


def _execute_script(
  payload: dict[str, Any],
  bars: Sequence[Bar],
  warmup_bar_count: int,
) -> dict[str, Any]:
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
  result = engine.run(strategy, bars, warmup_bar_count)
  return {
    "fills": [asdict(fill) for fill in result.fills],
    "trades": [fill_to_api_trade(fill) for fill in result.fills],
    "portfolioSnapshots": [_snapshot_to_dict(item) for item in result.portfolio_snapshots],
    "skippedSignals": strategy.skipped_signals,
    "unexecutedOrders": [_unexecuted_order_to_api(item) for item in result.unexecuted_orders],
    "warnings": strategy.direction_warnings,
  }


def _legacy_payload(payload: dict[str, Any], bars: pd.DataFrame) -> dict[str, Any]:
  legacy = dict(payload)
  symbol = str(payload["symbol"])
  legacy["bars"] = [
    {
      "time": str(row.time), "symbol": symbol, "open": float(row.open),
      "high": float(row.high), "low": float(row.low), "close": float(row.close),
      "volume": float(row.volume),
    }
    for row in bars.itertuples(index=False)
  ]
  return legacy


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


def _process_exit_error(
  process: multiprocessing.Process,
  message: str,
) -> BarStrategyProcessError:
  exit_code = process.exitcode
  if exit_code is None:
    reason = "unknown_interruption"
  elif exit_code < 0:
    reason = "strategy_signal"
  elif exit_code > 0:
    reason = "strategy_exit_nonzero"
  else:
    reason = "unknown_interruption"
  return BarStrategyProcessError(
    message, exit_code=exit_code, termination_reason=reason,
  )


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
    "__internal_raw_message": str(cause),
    "__internal_raw_traceback": "".join(
      traceback.format_exception(type(cause), cause, cause.__traceback__),
    ),
  }
  name = missing_name(cause)
  line = strategy_source_line(cause)
  if name:
    diagnostic["missingName"] = name
  if line:
    diagnostic["strategyLine"] = str(line)
  return diagnostic
