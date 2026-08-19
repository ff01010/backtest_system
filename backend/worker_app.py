# -*- coding: utf-8 -*-
"""隔离策略执行容器的内部 HTTP 接口。"""

import json
import os
import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from backend.backtesting import Bar, BarBacktestEngine, FeeProfile, NextOpenExecutionModel, RecordedPriceExecutionModel
from backend.backtesting.legacy import fill_to_api_trade
from backend.date_codec import TradingDateError, format_api_date
from backend.l2.engine import L2ExecutionEngine
from backend.l2.models import InstrumentRules, NormalizedBar, QuoteLevel, QuoteSnapshot, TradeEvent
from backend.services.backtest_errors import error_detail
from backend.strategies.scripted import ScriptedBarStrategy


class WorkerRunRequest(BaseModel):
  scriptId: str = Field(min_length=1)
  code: str = Field(min_length=1)
  params: dict[str, Any] = Field(default_factory=dict)
  bars: list[dict[str, Any]]
  initialCapital: float = Field(gt=0)
  commissionRate: float = Field(ge=0)
  feeProfile: dict[str, float] = Field(default_factory=dict)
  fillModel: str = "bar_next_open"
  slippage: float = Field(default=0, ge=0, lt=1)
  holdingPolicy: str = "intraday"
  baseQuantity: int = Field(default=0, ge=0)
  baseCostPrice: float = Field(default=0, ge=0)
  t0Enabled: bool = False


class WorkerL2SessionCreateRequest(BaseModel):
  scriptId: str = Field(min_length=1)
  code: str = Field(min_length=1)
  params: dict[str, Any] = Field(default_factory=dict)
  initialCapital: float = Field(gt=0)
  commissionRate: float = Field(ge=0)
  feeProfile: dict[str, float] = Field(default_factory=dict)
  latencyMs: int = Field(default=0, ge=0)
  passiveParticipationRate: float = Field(default=0.1, gt=0, le=1)
  holdingPolicy: str = "intraday"
  strategyCategory: str = "directional_holding"
  symbol: str = ""
  baseQuantity: int = Field(default=0, ge=0)
  baseCostPrice: float = Field(default=0, ge=0)
  t0Enabled: bool = False
  quoteMode: str = "order_driven"
  instrumentRules: dict[str, Any] = Field(default_factory=dict)


class WorkerL2DayRequest(BaseModel):
  tradeDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
  expectedChunks: int | None = Field(default=None, ge=0)

  @field_validator("tradeDate", mode="before")
  @classmethod
  def normalize_trade_date(cls, value: object) -> str:
    try:
      return format_api_date(value)  # type: ignore[arg-type]
    except TradingDateError as error:
      raise ValueError(str(error)) from error


class WorkerL2ChunkRequest(BaseModel):
  tradeDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
  chunkNo: int = Field(ge=0)
  bars: list[dict[str, Any]] = Field(default_factory=list)
  quotes: list[dict[str, Any]] = Field(default_factory=list)
  trades: list[dict[str, Any]] = Field(default_factory=list)

  @field_validator("tradeDate", mode="before")
  @classmethod
  def normalize_trade_date(cls, value: object) -> str:
    try:
      return format_api_date(value)  # type: ignore[arg-type]
    except TradingDateError as error:
      raise ValueError(str(error)) from error


class WorkerL2BarsRequest(BaseModel):
  tradeDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
  bars: list[dict[str, Any]] = Field(min_length=1)

  @field_validator("tradeDate", mode="before")
  @classmethod
  def normalize_trade_date(cls, value: object) -> str:
    try:
      return format_api_date(value)  # type: ignore[arg-type]
    except TradingDateError as error:
      raise ValueError(str(error)) from error


class WorkerL2QuoteRequest(BaseModel):
  tradeDate: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
  quote: dict[str, Any]

  @field_validator("tradeDate", mode="before")
  @classmethod
  def normalize_trade_date(cls, value: object) -> str:
    try:
      return format_api_date(value)  # type: ignore[arg-type]
    except TradingDateError as error:
      raise ValueError(str(error)) from error


class L2StreamDecoder:
  """按行解析 runner 传入的有界 L2 NDJSON 数据。"""

  _MAX_LINE_BYTES = 16 * 1024 * 1024

  def __init__(self) -> None:
    self._buffer = b""

  def feed(self, chunk: bytes) -> list[WorkerL2ChunkRequest]:
    self._buffer += chunk
    if len(self._buffer) > self._MAX_LINE_BYTES:
      raise ValueError("L2 流式数据块超过大小限制")
    records: list[WorkerL2ChunkRequest] = []
    while b"\n" in self._buffer:
      line, self._buffer = self._buffer.split(b"\n", 1)
      if line:
        records.append(WorkerL2ChunkRequest.model_validate_json(line))
    return records

  def finish(self) -> None:
    if self._buffer.strip():
      raise ValueError("L2 流式数据末尾不完整")


app = FastAPI(title="Quant Backtest Strategy Worker")
_L2_SESSION_LIMIT = int(os.getenv("TREND_GRID_L2_WORKER_SESSION_LIMIT", "2"))
_l2_sessions: dict[str, "L2WorkerSession"] = {}


def _worker_http_error(
  error: Exception,
  symbol: str | None = None,
  event_time: str | None = None,
) -> HTTPException:
  """worker 只向调度端返回已脱敏的结构化错误。"""
  detail = error_detail(error).with_event_context(symbol, event_time)
  return HTTPException(422, {
    "errorDetail": detail.to_dict(),
    "diagnostic": {
      "exceptionType": type(error).__name__,
      "reason": str(error)[:500],
    },
  })


class L2WorkerSession:
  """worker 内存态会话，保持脚本状态、账户和待成交订单。"""

  def __init__(self, payload: WorkerL2SessionCreateRequest) -> None:
    self.strategy = ScriptedBarStrategy(payload.scriptId, payload.code, payload.params, payload.quoteMode)
    fee_profile = FeeProfile(
      commission_rate=float(payload.feeProfile.get("commissionRate", payload.commissionRate)),
      transfer_fee_rate=float(payload.feeProfile.get("transferFeeRate", 0.00001)),
      stamp_duty_rate=float(payload.feeProfile.get("stampDutyRate", 0.0005)),
      minimum_commission=float(payload.feeProfile.get("minimumCommission", 5)),
    )
    self.engine = L2ExecutionEngine(
      self.strategy, payload.initialCapital, payload.commissionRate,
      InstrumentRules(**payload.instrumentRules), payload.latencyMs,
      payload.passiveParticipationRate, payload.holdingPolicy, payload.baseQuantity,
      payload.baseCostPrice, payload.t0Enabled, fee_profile,
    )
    self.engine.initialize_base_position(payload.symbol)
    self.trade_date: str | None = None
    self.expected_chunks: int | None = None
    self.next_chunk = 0

  def begin_day(self, payload: WorkerL2DayRequest) -> None:
    if self.trade_date is not None:
      raise ValueError("上一交易日尚未结束")
    self.trade_date = payload.tradeDate
    self.expected_chunks = payload.expectedChunks
    self.next_chunk = 0
    self.engine.begin_day(payload.tradeDate)

  def process_chunk(self, payload: WorkerL2ChunkRequest) -> None:
    if self.trade_date != payload.tradeDate or payload.chunkNo != self.next_chunk:
      raise ValueError("L2 数据块日期或序号不连续")
    self.engine.process_chunk(_bars(payload.bars), _quotes(payload.quotes), _trades(payload.trades))
    self.next_chunk += 1

  def process_bars(self, payload: WorkerL2BarsRequest) -> dict[str, Any]:
    self._require_trade_date(payload.tradeDate)
    processed = 0
    for bar in _bars(payload.bars):
      self.engine.process_bar(bar)
      processed += 1
      if self.engine.has_pending_orders():
        break
    return {"processedBars": processed, **self._quote_demand()}

  def process_quote(self, payload: WorkerL2QuoteRequest) -> dict[str, Any]:
    self._require_trade_date(payload.tradeDate)
    self.engine.process_quote(_quote(payload.quote))
    return self._quote_demand()

  def finish_day(self, trade_date: str) -> None:
    trade_date = format_api_date(trade_date)
    if self.trade_date != trade_date:
      raise ValueError("L2 交易日不一致")
    if self.expected_chunks is not None and self.next_chunk != self.expected_chunks:
      raise ValueError("L2 交易日数据块不完整")
    self.engine.finish_day(trade_date)
    self.trade_date = None

  def _quote_demand(self) -> dict[str, Any]:
    return {
      "quoteDemand": "waiting_quote" if self.engine.has_pending_orders() else "idle",
      "nextQuoteAfter": self.engine.next_quote_after(),
      "dayEndQuoteRequired": self.engine.requires_day_end_quote(),
    }

  def _require_trade_date(self, trade_date: str) -> None:
    if self.trade_date != trade_date:
      raise ValueError("L2 交易日不一致")

  def result(self) -> dict[str, Any]:
    result = self.engine.result()
    result["skippedSignals"] = self.strategy.skipped_signals
    result["warnings"] = result["warnings"] + self.strategy.direction_warnings
    return result


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
  }


@app.get("/health")
def health() -> dict[str, str]:
  return {"status": "ok"}


@app.post("/run")
def run(payload: WorkerRunRequest) -> dict[str, Any]:
  """在无数据库凭据和无宿主机挂载的容器内执行单次策略脚本。"""
  try:
    bars = [Bar(
      time=str(item["time"]),
      symbol=str(item["symbol"]),
      open=float(item["open"]),
      high=float(item["high"]),
      low=float(item["low"]),
      close=float(item["close"]),
      volume=float(item.get("volume", 0)),
    ) for item in payload.bars]
    strategy = ScriptedBarStrategy(payload.scriptId, payload.code, payload.params)
    fee_profile = FeeProfile(
      commission_rate=float(payload.feeProfile.get("commissionRate", payload.commissionRate)),
      transfer_fee_rate=float(payload.feeProfile.get("transferFeeRate", 0.00001)),
      stamp_duty_rate=float(payload.feeProfile.get("stampDutyRate", 0.0005)),
      minimum_commission=float(payload.feeProfile.get("minimumCommission", 5)),
    )
    engine = BarBacktestEngine(
      initial_cash=payload.initialCapital,
      commission_rate=payload.commissionRate,
      fee_profile=fee_profile,
      execution_model=(
        RecordedPriceExecutionModel(slippage=payload.slippage)
        if payload.fillModel == "bar_close"
        else NextOpenExecutionModel(slippage=payload.slippage)
      ),
      holding_policy=payload.holdingPolicy,
      execute_on_next_bar_open=payload.fillModel == "bar_next_open",
      base_quantity=payload.baseQuantity,
      base_cost_price=payload.baseCostPrice,
      t0_enabled=payload.t0Enabled,
    )
    result = engine.run(strategy, bars)
  except ValueError as error:
    raise _worker_http_error(error) from error
  return {
    "trades": [fill_to_api_trade(fill) for fill in result.fills],
    "portfolioSnapshots": [_snapshot_to_dict(item) for item in result.portfolio_snapshots],
    "skippedSignals": strategy.skipped_signals,
    "warnings": strategy.direction_warnings,
  }


@app.post("/l2/sessions")
def create_l2_session(payload: WorkerL2SessionCreateRequest) -> dict[str, str]:
  """创建有上限的 L2 脚本会话。"""
  if len(_l2_sessions) >= _L2_SESSION_LIMIT:
    raise HTTPException(429, "L2 策略会话已达到并发上限")
  try:
    session_id = str(uuid4())
    _l2_sessions[session_id] = L2WorkerSession(payload)
    return {"sessionId": session_id}
  except ValueError as error:
    raise _worker_http_error(error) from error


@app.post("/l2/sessions/{session_id}/days")
def begin_l2_day(session_id: str, payload: WorkerL2DayRequest) -> dict[str, str]:
  session = _session(session_id)
  try:
    session.begin_day(payload)
    return {"status": "ok"}
  except ValueError as error:
    raise _worker_http_error(error) from error


@app.post("/l2/sessions/{session_id}/chunks")
def process_l2_chunk(session_id: str, payload: WorkerL2ChunkRequest) -> dict[str, str]:
  session = _session(session_id)
  try:
    session.process_chunk(payload)
    return {"status": "ok"}
  except ValueError as error:
    raise _worker_http_error(error) from error


@app.post("/l2/sessions/{session_id}/stream")
async def process_l2_stream(session_id: str, request: Request) -> dict[str, float | int | str]:
  """在单次 HTTP 请求中顺序处理一个交易日的有界数据块。"""
  session = _session(session_id)
  decoder = L2StreamDecoder()
  processed_chunks = 0
  processing_ms = 0.0
  try:
    async for raw_chunk in request.stream():
      for payload in decoder.feed(raw_chunk):
        started = time.perf_counter()
        session.process_chunk(payload)
        processing_ms += (time.perf_counter() - started) * 1000
        processed_chunks += 1
    decoder.finish()
    return {
      "status": "ok",
      "processedChunks": processed_chunks,
      "workerProcessingMs": round(processing_ms, 3),
    }
  except (ValueError, json.JSONDecodeError) as error:
    raise _worker_http_error(error) from error


@app.post("/l2/sessions/{session_id}/bars")
def process_l2_bars(session_id: str, payload: WorkerL2BarsRequest) -> dict[str, Any]:
  session = _session(session_id)
  try:
    return session.process_bars(payload)
  except ValueError as error:
    raise _worker_http_error(error) from error


@app.post("/l2/sessions/{session_id}/quotes")
def process_l2_quote(session_id: str, payload: WorkerL2QuoteRequest) -> dict[str, Any]:
  session = _session(session_id)
  try:
    return session.process_quote(payload)
  except ValueError as error:
    quote = payload.quote
    symbol = quote.get("symbol")
    event_time = quote.get("time")
    raise _worker_http_error(
      error,
      symbol if isinstance(symbol, str) else None,
      event_time if isinstance(event_time, str) else None,
    ) from error


@app.post("/l2/sessions/{session_id}/days/{trade_date}/finish")
def finish_l2_day(session_id: str, trade_date: str) -> dict[str, str]:
  session = _session(session_id)
  try:
    session.finish_day(format_api_date(trade_date))
    return {"status": "ok"}
  except (TradingDateError, ValueError) as error:
    raise _worker_http_error(error) from error


@app.post("/l2/sessions/{session_id}/finish")
def finish_l2_session(session_id: str) -> dict[str, Any]:
  session = _session(session_id)
  if session.trade_date is not None:
    raise _worker_http_error(ValueError("交易日尚未结束"))
  result = session.result()
  del _l2_sessions[session_id]
  return result


@app.delete("/l2/sessions/{session_id}")
def discard_l2_session(session_id: str) -> dict[str, str]:
  _l2_sessions.pop(session_id, None)
  return {"status": "discarded"}


def _session(session_id: str) -> L2WorkerSession:
  session = _l2_sessions.get(session_id)
  if session is None:
    raise HTTPException(404, "L2 策略会话不存在")
  return session


def _bars(items: list[dict[str, Any]]) -> list[NormalizedBar]:
  return [NormalizedBar(**item) for item in items]


def _quotes(items: list[dict[str, Any]]) -> list[QuoteSnapshot]:
  return [_quote(item) for item in items]


def _quote(item: dict[str, Any]) -> QuoteSnapshot:
  return QuoteSnapshot(
    symbol=str(item["symbol"]), trade_date=str(item["trade_date"]), time=str(item["time"]),
    sequence=int(item["sequence"]), bids=tuple(QuoteLevel(**level) for level in item["bids"]),
    asks=tuple(QuoteLevel(**level) for level in item["asks"]), last_price=item.get("last_price"),
    source_version=str(item.get("source_version", "")),
  )


def _trades(items: list[dict[str, Any]]) -> list[TradeEvent]:
  return [TradeEvent(**item) for item in items]
