"""L2 runner 内的单任务回测会话子进程。"""

from __future__ import annotations

import multiprocessing
import os
import re
import time
import traceback
from collections.abc import Callable, Sequence
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from backend.backtesting.fees import FeeProfile
from backend.l2.engine import L2ExecutionEngine
from backend.l2.models import InstrumentRules, NormalizedBar, QuoteLevel, QuoteSnapshot, TradeEvent
from backend.services.backtest_errors import RemoteBacktestError, error_detail
from backend.strategies.scripted import ScriptedBarStrategy


class StrategySessionProcessError(RuntimeError):
  """策略会话子进程异常退出或 IPC 协议失效。"""


class L2StrategySession:
  """将一个任务的策略与撮合放在独立子进程中批量执行。"""

  def __init__(
    self,
    payload: dict[str, Any],
    abort_check: Callable[[], None] | None = None,
  ) -> None:
    self._session = _EngineProcessProxy(_session_payload(payload), abort_check)
    self._trade_date: str | None = None
    self._closed = False

  def begin_day(self, trade_date: str) -> None:
    if self._trade_date is not None:
      raise ValueError("上一交易日尚未结束")
    self._session.call("begin_day", {"tradeDate": trade_date})
    self._trade_date = trade_date

  def process_bars(self, bars: Sequence[NormalizedBar]) -> dict[str, Any]:
    self._require_day()
    return _response(self._session.call("process_bars", {"bars": list(bars)}))

  def process_quote(self, quote: QuoteSnapshot) -> dict[str, Any]:
    self._require_day()
    return _response(self._session.call("process_quote", {"quote": quote}))

  def process_chunk(
    self,
    bars: Sequence[NormalizedBar],
    quotes: Sequence[QuoteSnapshot],
    trades: Sequence[TradeEvent],
  ) -> None:
    self._require_day()
    self._session.call("process_chunk", {
      "bars": list(bars), "quotes": list(quotes), "trades": list(trades),
    })

  def process_chunk_payload(self, payload: dict[str, Any]) -> None:
    self._require_day()
    self._session.call("process_chunk_payload", {"chunk": payload})

  def finish_day(self, trade_date: str) -> None:
    if self._trade_date != trade_date:
      raise ValueError("L2 交易日不一致")
    self._session.call("finish_day", {"tradeDate": trade_date})
    self._trade_date = None

  def result(self) -> dict[str, Any]:
    if self._trade_date is not None:
      raise ValueError("交易日尚未结束")
    return _response(self._session.call("result", {}))

  def close(self) -> None:
    if self._closed:
      return
    self._closed = True
    self._session.close()

  @property
  def callback_calls(self) -> int:
    return self._session.calls

  @property
  def callback_elapsed_ms(self) -> float:
    return self._session.elapsed_ms

  def _require_day(self) -> None:
    if self._trade_date is None:
      raise ValueError("L2 交易日尚未开始")


class _EngineProcessProxy:
  """父 runner 到单任务撮合会话的受控 Pipe 协议。"""

  _POLL_SECONDS = 0.2

  def __init__(
    self,
    payload: dict[str, Any],
    abort_check: Callable[[], None] | None,
  ) -> None:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    self._connection = parent
    self._process = context.Process(target=_engine_process_main, args=(child, payload), daemon=True)
    self._abort_check = abort_check
    self._closed = False
    self._calls = 0
    self._elapsed_ms = 0.0
    self._process.start()
    child.close()
    self._receive("initialize")

  def call(self, operation: str, payload: dict[str, Any]) -> Any:
    if self._closed:
      raise StrategySessionProcessError("策略会话已关闭")
    self._check_abort()
    started = time.perf_counter()
    try:
      self._connection.send({"operation": operation, "payload": payload})
      return self._receive(operation)
    except (BrokenPipeError, EOFError, OSError) as error:
      raise StrategySessionProcessError("策略会话进程通信中断") from error
    finally:
      self._elapsed_ms += (time.perf_counter() - started) * 1000
      self._calls += 1

  def close(self) -> None:
    if self._closed:
      return
    self._closed = True
    if self._process.is_alive():
      try:
        self._connection.send({"operation": "close", "payload": {}})
      except (BrokenPipeError, EOFError, OSError):
        pass
      self._join()
    self._connection.close()

  @property
  def calls(self) -> int:
    return self._calls

  @property
  def elapsed_ms(self) -> float:
    return self._elapsed_ms

  def _receive(self, operation: str) -> Any:
    while not self._connection.poll(self._POLL_SECONDS):
      self._check_abort()
      if not self._process.is_alive():
        raise StrategySessionProcessError(f"策略会话在 {operation} 阶段异常退出")
    message = self._connection.recv()
    if not isinstance(message, dict):
      raise StrategySessionProcessError("策略会话返回了无效消息")
    if message.get("status") == "error":
      detail = message.get("detail")
      if isinstance(detail, dict):
        diagnostic = message.get("diagnostic")
        safe_diagnostic = {
          key: value for key, value in diagnostic.items()
          if isinstance(key, str) and isinstance(value, str)
        } if isinstance(diagnostic, dict) else None
        raise RemoteBacktestError.from_dict(detail, safe_diagnostic)
      raise StrategySessionProcessError("策略会话未返回结构化错误")
    if message.get("status") != "ok":
      raise StrategySessionProcessError("策略会话返回了未知状态")
    return message.get("result")

  def _check_abort(self) -> None:
    if self._abort_check is None:
      return
    try:
      self._abort_check()
    except Exception:
      self._terminate()
      raise

  def _join(self) -> None:
    self._process.join(timeout=1)
    if self._process.is_alive():
      self._terminate()

  def _terminate(self) -> None:
    if self._process.is_alive():
      self._process.terminate()
      self._process.join(timeout=2)


def _engine_process_main(connection: Connection, payload: dict[str, Any]) -> None:
  """策略子进程入口；不继承服务配置，只处理父进程提供的数据块。"""
  try:
    os.environ.clear()
    strategy = ScriptedBarStrategy(
      str(payload["scriptId"]), str(payload["code"]), dict(payload.get("params") or {}),
      str(payload.get("quoteMode") or "order_driven"),
    )
    engine = _build_engine(payload, strategy)
    engine.initialize_base_position(str(payload["symbol"]))
    connection.send({"status": "ok", "result": None})
    _serve_engine_session(connection, engine, strategy)
  except Exception as error:
    _send_error(connection, error)
  finally:
    connection.close()


def _serve_engine_session(
  connection: Connection,
  engine: L2ExecutionEngine,
  strategy: ScriptedBarStrategy,
) -> None:
  while True:
    message = connection.recv()
    if not isinstance(message, dict):
      raise ValueError("策略会话请求格式无效")
    operation = message.get("operation")
    payload = message.get("payload")
    if not isinstance(payload, dict):
      raise ValueError("策略会话请求缺少内容")
    if operation == "close":
      return
    result = _run_engine_operation(engine, strategy, str(operation), payload)
    connection.send({"status": "ok", "result": result})


def _run_engine_operation(
  engine: L2ExecutionEngine,
  strategy: ScriptedBarStrategy,
  operation: str,
  payload: dict[str, Any],
) -> Any:
  if operation == "begin_day":
    engine.begin_day(str(payload["tradeDate"]))
    return None
  if operation == "process_bars":
    return _process_bars(engine, _normalized_bars(payload, "bars"))
  if operation == "process_quote":
    quote = payload.get("quote")
    if not isinstance(quote, QuoteSnapshot):
      raise ValueError("策略会话缺少有效 Quote")
    engine.process_quote(quote)
    return _quote_demand(engine)
  if operation == "process_chunk":
    engine.process_chunk(_normalized_bars(payload, "bars"), _quotes(payload, "quotes"), _trades(payload, "trades"))
    return None
  if operation == "process_chunk_payload":
    chunk = payload.get("chunk")
    if not isinstance(chunk, dict):
      raise ValueError("L2 数据块格式无效")
    engine.process_chunk(_normalized_bars(chunk, "bars"), _quotes(chunk, "quotes"), _trades(chunk, "trades"))
    return None
  if operation == "finish_day":
    engine.finish_day(str(payload["tradeDate"]))
    return None
  if operation == "result":
    result = engine.result()
    result["skippedSignals"] = strategy.skipped_signals
    result["warnings"] = result["warnings"] + strategy.direction_warnings
    return result
  raise ValueError("策略会话操作不支持")


def _process_bars(engine: L2ExecutionEngine, bars: Sequence[NormalizedBar]) -> dict[str, Any]:
  processed = 0
  for bar in bars:
    engine.process_bar(bar)
    processed += 1
    if engine.has_pending_orders():
      break
  return {"processedBars": processed, **_quote_demand(engine)}


def _quote_demand(engine: L2ExecutionEngine) -> dict[str, Any]:
  return {
    "quoteDemand": "waiting_quote" if engine.has_pending_orders() else "idle",
    "nextQuoteAfter": engine.next_quote_after(),
    "dayEndQuoteRequired": engine.requires_day_end_quote(),
  }


def _response(value: Any) -> dict[str, Any]:
  if not isinstance(value, dict):
    raise StrategySessionProcessError("策略会话返回格式无效")
  return value


def _send_error(connection: Connection, error: Exception) -> None:
  try:
    connection.send({
      "status": "error",
      "detail": error_detail(error).to_dict(),
      "diagnostic": _error_diagnostic(error),
    })
  except (BrokenPipeError, EOFError, OSError):
    return


def _error_diagnostic(error: Exception) -> dict[str, str]:
  cause = _root_error(error)
  return {
    "exceptionType": type(cause).__name__,
    "reason": _redact_diagnostic_text(str(cause))[:500],
    "trace": _trace_summary(cause),
    "__internal_raw_message": str(cause),
    "__internal_raw_traceback": "".join(traceback.format_exception(type(cause), cause, cause.__traceback__)),
  }


def _root_error(error: Exception) -> Exception:
  current = error
  visited: set[int] = set()
  while id(current) not in visited:
    visited.add(id(current))
    next_error = current.__cause__ or current.__context__
    if not isinstance(next_error, Exception):
      return current
    current = next_error
  return current


def _trace_summary(error: Exception) -> str:
  frames = traceback.extract_tb(error.__traceback__)[-8:]
  return " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)


def _redact_diagnostic_text(value: str) -> str:
  without_credentials = re.sub(r"([a-z]+://[^\s:/]+:)[^@\s]+@", r"\1***@", value, flags=re.IGNORECASE)
  return re.sub(r"(?i)\b(password|token|secret|authorization)\b\s*[:=]\s*\S+", r"\1=***", without_credentials)


def _session_payload(payload: dict[str, Any]) -> dict[str, Any]:
  return {
    "scriptId": str(payload["scriptId"]),
    "code": str(payload["code"]),
    "params": dict(payload.get("params") or {}),
    "quoteMode": str(payload.get("quoteMode") or "order_driven"),
    "symbol": str(payload["symbol"]),
    "initialCapital": float(payload["initialCapital"]),
    "commissionRate": float(payload["commissionRate"]),
    "instrumentRules": dict(payload.get("instrumentRules") or {}),
    "latencyMs": int(payload.get("latencyMs", 0)),
    "passiveParticipationRate": float(payload.get("passiveParticipationRate", 0.1)),
    "holdingPolicy": str(payload.get("holdingPolicy") or "intraday"),
    "strategyCategory": str(payload.get("strategyCategory") or "directional_holding"),
    "overlaySettlementPolicy": str(payload.get("overlaySettlementPolicy") or "intraday_restore"),
    "baseQuantity": int(payload.get("baseQuantity", 0)),
    "baseCostPrice": float(payload.get("baseCostPrice", 0)),
    "initialPositionEnabled": bool(payload.get("initialPositionEnabled", False)),
    "t0Enabled": bool(payload.get("t0Enabled", False)),
    "feeProfile": dict(payload.get("feeProfile") or {}),
    "executionAlgorithm": str(payload.get("executionAlgorithm") or "immediate"),
    "executionParameters": dict(payload.get("executionParameters") or {}),
  }


def _items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
  value = payload.get(key, [])
  # dataclasses.asdict() 保留盘口 tuple；HTTP 历史数据块会反序列化为 list。
  if not isinstance(value, (list, tuple)) or not all(isinstance(item, dict) for item in value):
    raise ValueError(f"L2 数据块 {key} 格式无效")
  return list(value)


def _normalized_bars(payload: dict[str, Any], key: str) -> list[NormalizedBar]:
  value = payload.get(key, [])
  if isinstance(value, list) and all(isinstance(item, NormalizedBar) for item in value):
    return value
  return [NormalizedBar(**item) for item in _items(payload, key)]


def _quotes(payload: dict[str, Any], key: str) -> list[QuoteSnapshot]:
  value = payload.get(key, [])
  if isinstance(value, list) and all(isinstance(item, QuoteSnapshot) for item in value):
    return value
  return [_quote(item) for item in _items(payload, key)]


def _trades(payload: dict[str, Any], key: str) -> list[TradeEvent]:
  value = payload.get(key, [])
  if isinstance(value, list) and all(isinstance(item, TradeEvent) for item in value):
    return value
  return [TradeEvent(**item) for item in _items(payload, key)]


def _quote(payload: dict[str, Any]) -> QuoteSnapshot:
  bids = _items(payload, "bids")
  asks = _items(payload, "asks")
  return QuoteSnapshot(
    symbol=str(payload["symbol"]),
    trade_date=str(payload["trade_date"]),
    time=str(payload["time"]),
    sequence=int(payload["sequence"]),
    bids=tuple(QuoteLevel(**item) for item in bids),
    asks=tuple(QuoteLevel(**item) for item in asks),
    last_price=payload.get("last_price"),
    source_version=str(payload.get("source_version", "")),
  )


def _build_engine(payload: dict[str, Any], strategy: ScriptedBarStrategy) -> L2ExecutionEngine:
  fee_profile = dict(payload.get("feeProfile") or {})
  commission_rate = float(payload["commissionRate"])
  return L2ExecutionEngine(
    strategy,
    float(payload["initialCapital"]),
    commission_rate,
    InstrumentRules(**dict(payload.get("instrumentRules") or {})),
    int(payload.get("latencyMs", 0)),
    float(payload.get("passiveParticipationRate", 0.1)),
    str(payload.get("holdingPolicy") or "intraday"),
    int(payload.get("baseQuantity", 0)),
    float(payload.get("baseCostPrice", 0)),
    bool(payload.get("t0Enabled", False)),
    FeeProfile(
      commission_rate=float(fee_profile.get("commissionRate", commission_rate)),
      transfer_fee_rate=float(fee_profile.get("transferFeeRate", 0.00001)),
      stamp_duty_rate=float(fee_profile.get("stampDutyRate", 0.0005)),
      minimum_commission=float(fee_profile.get("minimumCommission", 5)),
    ),
    str(payload.get("executionAlgorithm") or "immediate"),
    dict(payload.get("executionParameters") or {}),
    str(payload.get("strategyCategory") or "directional_holding"),
    str(payload.get("overlaySettlementPolicy") or "intraday_restore"),
  )
