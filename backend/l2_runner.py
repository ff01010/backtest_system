"""独立 L2 任务 runner：读取行情，不向策略容器暴露任何数据源凭据。"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections import deque
from dataclasses import asdict
from datetime import datetime
from itertools import chain
from typing import Any, Iterator

from backend import settings
from backend.date_codec import format_api_date
from backend.l2.config import has_profile, load_profile, load_temporary_quote_profile
from backend.l2.models import NormalizedBar, QuoteSnapshot, TradeEvent, parse_timestamp
from backend.l2.providers import ClickHouseL2Adapter, L2DataCoverageError, L2DataError
from backend.l2.result_adapter import normalize_l2_result
from backend.l2.strategy_session import L2StrategySession
from backend.services.backtest_errors import error_detail, error_summary
from backend.services import bar_data, state_store
from backend.services.system_error_log import system_error_log_record
from backend.strategies.scripted import quote_mode_for_script

logger = logging.getLogger(__name__)


class L2Runner:
  """以 PostgreSQL 租约领取任务，并以受限块回放 L2 数据。"""

  def __init__(self, worker_id: str | None = None) -> None:
    self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

  def run_forever(self) -> None:
    """持续领取任务；空队列时短暂轮询。"""
    while True:
      job = state_store.claim_l2_backtest_job(self._worker_id, settings.L2_JOB_LEASE_SECONDS)
      if job is None:
        time.sleep(settings.L2_RUNNER_POLL_SECONDS)
        continue
      self._run_job(job)

  def run_once(self) -> bool:
    """处理一个任务，便于容器健康检查和单元测试。"""
    job = state_store.claim_l2_backtest_job(self._worker_id, settings.L2_JOB_LEASE_SECONDS)
    if job is None:
      return False
    self._run_job(job)
    return True

  def _run_job(self, job: dict[str, Any]) -> None:
    session: L2StrategySession | None = None
    lease = _JobLeaseKeeper(job)
    completed = False
    stage = "l2.initialize"
    try:
      lease.start()
      started_at = time.perf_counter()
      payload = dict(job["payload"])
      quote_provider = ClickHouseL2Adapter(load_profile("quote") if has_profile("quote") else load_temporary_quote_profile())
      bar_read_started = time.perf_counter()
      stage = "l2.bar_load"
      bars, bar_snapshot = bar_data.load_normalized_bars_with_snapshot(
        payload["symbol"], payload["timeframe"], payload["startDate"], payload["endDate"],
      )
      stage = "l2.quote_coverage"
      _validate_quote_coverage(quote_provider, payload["symbol"], bars)
      stage = "l2.strategy_session"
      session = L2StrategySession(self._session_payload(payload), lease.ensure_active)
      quote_mode = _quote_mode(payload)
      performance = _new_performance(quote_mode)
      performance["barReadMs"] = _elapsed_ms(bar_read_started)
      stage = "l2.market_replay"
      for trade_date, day_bars in _bars_by_day(bars).items():
        if quote_mode == "continuous":
          _replay_day_continuous(
            session, day_bars, quote_provider, payload["symbol"], trade_date,
            job, performance,
          )
        else:
          _replay_day(
            session, day_bars, quote_provider, payload["symbol"], trade_date,
            job, performance,
          )
      stage = "l2.result_finalize"
      raw_result = session.result()
      session.close()
      session = None
      raw_result["dataSnapshot"] = _data_snapshot(bar_snapshot, quote_provider, payload, quote_mode)
      performance["totalElapsedMs"] = _elapsed_ms(started_at)
      raw_result["performance"] = performance
      result = normalize_l2_result(raw_result, payload)
      summary = _summary(result)
      lease.ensure_active()
      completed = state_store.finish_l2_backtest_job(job["runId"], job["attemptToken"], "succeeded", summary, result)
      if not completed:
        logger.warning("L2 任务 %s 已由新的执行尝试接管", job["runId"])
    except Exception as error:
      logger.exception("L2 回测任务失败: %s", job["runId"])
      status = "cancelled" if "租约丢失或已取消" in str(error) else "failed"
      system_error = None
      if status == "failed" and error_detail(error, job["runId"]).category == "system_error":
        system_error = system_error_log_record(error, "l2_runner", stage, job["runId"])
      state_store.finish_l2_backtest_job(
        job["runId"], job["attemptToken"], status, error_summary(error, job["runId"]),
        system_error=system_error.to_dict() if system_error else None,
        owner_user_id=job["ownerUserId"],
      )
    finally:
      if session is not None:
        session.close()
      lease.stop()

  @staticmethod
  def _session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    script = payload.get("savedScriptSnapshot") or payload.get("versionScriptSnapshot")
    if not isinstance(script, dict) or not isinstance(script.get("code"), str):
      raise L2DataError("L2 回测缺少已保存的策略脚本快照")
    params = dict(payload.get("strategyParams") or {})
    instrument_rules = dict(params.get("l2InstrumentRules") or {})
    instrument_rules["allow_short"] = bool(payload.get("allowShort", False))
    if str(payload.get("symbol", "")).startswith(("688", "689")):
      instrument_rules.setdefault("min_order_quantity", 200)
      instrument_rules.setdefault("quantity_step", 1)
    return {
      "scriptId": str(script.get("id") or payload.get("scriptId")),
      "code": script["code"],
      "params": {**params, "allowLong": payload.get("allowLong", True), "allowShort": payload.get("allowShort", False)},
      "initialCapital": float(payload["initialCapital"]),
      "commissionRate": float(payload["commissionRate"]),
      "feeProfile": dict(payload.get("feeProfile") or {}),
      "latencyMs": int(params.get("l2LatencyMs", 0)),
      "passiveParticipationRate": float(params.get("l2PassiveParticipationRate", 0.1)),
      "holdingPolicy": _holding_policy(payload),
      "strategyCategory": _strategy_category(payload),
      "symbol": str(payload["symbol"]),
      "baseQuantity": int(payload.get("baseQuantity", 0)),
      "baseCostPrice": float(payload.get("baseCostPrice", 0)),
      "t0Enabled": bool(payload.get("t0Enabled", False)),
      "quoteMode": _quote_mode(payload),
      "instrumentRules": instrument_rules,
    }


def _strategy_category(payload: dict[str, Any]) -> str:
  if payload.get("strategyCategory") in {"core_position_overlay", "directional_holding"}:
    return str(payload["strategyCategory"])
  return "core_position_overlay" if int(payload.get("baseQuantity", 0)) > 0 or bool(payload.get("t0Enabled", False)) else "directional_holding"


def _holding_policy(payload: dict[str, Any]) -> str:
  if payload.get("strategyCategory") not in {"core_position_overlay", "directional_holding"}:
    return "overnight" if payload.get("holdingPolicy") == "overnight" else "intraday"
  return "intraday" if _strategy_category(payload) == "core_position_overlay" else "overnight"


def _validate_quote_coverage(
  quote_provider: ClickHouseL2Adapter,
  symbol: str,
  bars: list[NormalizedBar],
) -> None:
  missing_dates = quote_provider.missing_quote_dates(symbol, [bar.trade_date for bar in bars])
  if missing_dates:
    raise L2DataCoverageError(symbol, missing_dates)


def _quote_mode(payload: dict[str, Any]) -> str:
  """从不可变脚本快照推导回放方式，防止运行时读取已编辑的脚本。"""
  script = payload.get("savedScriptSnapshot") or payload.get("versionScriptSnapshot")
  if not isinstance(script, dict) or not isinstance(script.get("code"), str):
    raise L2DataError("L2 回测缺少已保存的策略脚本快照")
  params = dict(payload.get("strategyParams") or {})
  configured = params.get("quoteMode")
  return quote_mode_for_script(script["code"], configured if isinstance(configured, str) else None)


def _bars_by_day(bars: list[NormalizedBar]) -> dict[str, list[NormalizedBar]]:
  grouped: dict[str, list[NormalizedBar]] = {}
  for bar in bars:
    grouped.setdefault(format_api_date(bar.trade_date), []).append(bar)
  return grouped


def _replay_day(
  session: L2StrategySession,
  bars: list[NormalizedBar],
  quote_provider: ClickHouseL2Adapter,
  symbol: str,
  trade_date: str,
  job: dict[str, Any],
  performance: dict[str, Any],
) -> None:
  """以 Bar 为主时钟，只有待成交订单才按时间游标读取盘口。"""
  trade_date = format_api_date(trade_date)
  session.begin_day(trade_date)
  bar_index = 0
  quote_cursor: str | None = None
  quote_reader = _QuotePrefetchBuffer(quote_provider, symbol, trade_date, performance)
  demand: dict[str, Any] = {"quoteDemand": "idle", "dayEndQuoteRequired": False}
  try:
    while bar_index < len(bars):
      next_bar = bars[bar_index]
      if _waiting_quote(demand):
        quote = quote_reader.next(quote_cursor, demand, next_bar.end_time)
        if quote is not None:
          demand = _process_quote(session, quote, job, performance)
          quote_cursor = quote.time
          continue
      batch = bars[bar_index:] if not _waiting_quote(demand) else [next_bar]
      started = time.perf_counter()
      response = session.process_bars(batch)
      performance["workerMs"] += _elapsed_ms(started)
      processed = int(response.get("processedBars", 0))
      if processed <= 0 or processed > len(batch):
        raise L2DataError("L2 策略会话未返回有效的 Bar 处理数量")
      performance["barCount"] += processed
      bar_index += processed
      demand = response
      _heartbeat(job)

    session_end = f"{trade_date} 14:57:00"
    while _waiting_quote(demand):
      quote = quote_reader.next(quote_cursor, demand, session_end)
      if quote is None:
        break
      demand = _process_quote(session, quote, job, performance)
      quote_cursor = quote.time

    if bool(demand.get("dayEndQuoteRequired")):
      # 尾盘结算必须使用真实最后快照，不能使用任意预读窗口中的旧快照。
      quote_reader.close()
      started = time.perf_counter()
      final_quote = quote_provider.last_quote_before(symbol, trade_date, session_end)
      performance["quoteQueries"] += 1
      performance["quoteBufferMisses"] += 1
      performance["sourceReadMs"] += _elapsed_ms(started)
      if final_quote is not None and (quote_cursor is None or parse_timestamp(final_quote.time) > parse_timestamp(quote_cursor)):
        demand = _process_quote(session, final_quote, job, performance)
        quote_cursor = final_quote.time
    session.finish_day(trade_date)
    _record_session_performance(session, performance)
  finally:
    quote_reader.close()


def _replay_day_continuous(
  session: L2StrategySession,
  bars: list[NormalizedBar],
  quote_provider: ClickHouseL2Adapter,
  symbol: str,
  trade_date: str,
  job: dict[str, Any],
  performance: dict[str, Any],
) -> None:
  """连续模式只为声明 on_quote 的策略回放完整 3 秒快照。"""
  trade_date = format_api_date(trade_date)
  session.begin_day(trade_date)
  started = time.perf_counter()
  for chunk in _continuous_stream_chunks(bars, quote_provider, symbol, trade_date, job, performance):
    session.process_chunk_payload(chunk)
  performance["workerMs"] += _elapsed_ms(started)
  session.finish_day(trade_date)
  _record_session_performance(session, performance)


def _continuous_stream_chunks(
  bars: list[NormalizedBar], quote_provider: ClickHouseL2Adapter, symbol: str,
  trade_date: str, job: dict[str, Any], performance: dict[str, Any],
) -> Iterator[dict[str, Any]]:
  """惰性产生 NDJSON 记录，维持旧块边界和事件顺序。"""
  chunks = iter(_day_chunks(bars, quote_provider, symbol, trade_date, settings.L2_PAGE_SIZE))
  chunk_no = 0
  while True:
    source_started = time.perf_counter()
    try:
      chunk = next(chunks)
    except StopIteration:
      return
    performance["sourceReadMs"] += _elapsed_ms(source_started)
    performance["quoteReadBatches"] += 1
    performance["chunkCount"] += 1
    performance["barCount"] += len(chunk["bars"])
    performance["quotesFetched"] += len(chunk["quotes"])
    _heartbeat(job)
    yield {"tradeDate": trade_date, "chunkNo": chunk_no, **chunk}
    chunk_no += 1


class _QuotePrefetchBuffer:
  """只在有待成交订单时预取有界的后续连续竞价快照。"""

  def __init__(
    self,
    quote_provider: ClickHouseL2Adapter,
    symbol: str,
    trade_date: str,
    performance: dict[str, Any],
  ) -> None:
    self._quote_provider = quote_provider
    self._symbol = symbol
    self._trade_date = trade_date
    self._performance = performance
    self._quotes: deque[QuoteSnapshot] = deque()

  def next(self, quote_cursor: str | None, demand: dict[str, Any], before_time: str) -> QuoteSnapshot | None:
    after_time = _later_timestamp(quote_cursor, _required_timestamp(demand))
    self._discard_before(after_time)
    reused_buffer = bool(self._quotes)
    if not self._quotes:
      self._prefetch(after_time)
    if not self._quotes or parse_timestamp(self._quotes[0].time) >= parse_timestamp(before_time):
      return None
    if reused_buffer:
      self._performance["quoteBufferHits"] += 1
    return self._quotes.popleft()

  def close(self) -> None:
    """预读缓冲不持有外部资源。"""

  def _discard_before(self, after_time: str) -> None:
    while self._quotes and parse_timestamp(self._quotes[0].time) <= parse_timestamp(after_time):
      self._quotes.popleft()

  def _prefetch(self, after_time: str) -> None:
    started = time.perf_counter()
    quotes = self._quote_provider.quotes_after(
      self._symbol, self._trade_date, after_time, None, settings.L2_QUOTE_PREFETCH_SIZE,
    )
    self._performance["quoteQueries"] += 1
    self._performance["quoteBufferMisses"] += 1
    self._performance["quoteRowsPrefetched"] += len(quotes)
    self._performance["sourceReadMs"] += _elapsed_ms(started)
    self._quotes.extend(quotes)
    self._performance["maxQuoteBufferRows"] = max(self._performance["maxQuoteBufferRows"], len(self._quotes))


def _process_quote(
  session: L2StrategySession,
  quote: QuoteSnapshot,
  job: dict[str, Any],
  performance: dict[str, Any],
) -> dict[str, Any]:
  started = time.perf_counter()
  response = session.process_quote(quote)
  performance["workerMs"] += _elapsed_ms(started)
  performance["quotesFetched"] += 1
  _heartbeat(job)
  return response


def _record_session_performance(session: L2StrategySession, performance: dict[str, Any]) -> None:
  performance["workerCalls"] = int(getattr(session, "callback_calls", performance["workerCalls"]))


def _waiting_quote(demand: dict[str, Any]) -> bool:
  return demand.get("quoteDemand") == "waiting_quote"


def _required_timestamp(demand: dict[str, Any]) -> str:
  value = demand.get("nextQuoteAfter")
  if not isinstance(value, str) or not value:
    raise L2DataError("L2 worker 未返回待成交订单的时间边界")
  return value


def _later_timestamp(left: str | None, right: str) -> str:
  if left is None or parse_timestamp(right) > parse_timestamp(left):
    return right
  return left


def _heartbeat(job: dict[str, Any]) -> None:
  if not state_store.heartbeat_l2_backtest_job(job["runId"], job["attemptToken"], settings.L2_JOB_LEASE_SECONDS):
    raise L2DataError("L2 任务租约丢失或已取消")


class _JobLeaseKeeper:
  """在策略回调阻塞时仍由父 runner 持续续租。"""

  def __init__(self, job: dict[str, Any]) -> None:
    self._job = job
    self._stop_event = threading.Event()
    self._lock = threading.Lock()
    self._failure: Exception | None = None
    self._thread = threading.Thread(target=self._run, name="l2-job-lease", daemon=True)

  def start(self) -> None:
    self._thread.start()

  def stop(self) -> None:
    self._stop_event.set()
    self._thread.join(timeout=2)

  def ensure_active(self) -> None:
    with self._lock:
      failure = self._failure
    if failure is not None:
      raise failure

  def _run(self) -> None:
    interval = max(1.0, min(10.0, settings.L2_JOB_LEASE_SECONDS / 3))
    while not self._stop_event.wait(interval):
      try:
        _heartbeat(self._job)
      except Exception as error:
        with self._lock:
          self._failure = error
        return


def _new_performance(quote_mode: str = "order_driven") -> dict[str, Any]:
  return {
    "executionMode": f"{quote_mode}_quote",
    "quoteMode": quote_mode,
    "barCount": 0,
    "quotesFetched": 0,
    "quoteRowsPrefetched": 0,
    "quoteQueries": 0,
    "quoteBufferHits": 0,
    "quoteBufferMisses": 0,
    "maxQuoteBufferRows": 0,
    "workerCalls": 0,
    "chunkCount": 0,
    "quoteReadBatches": 0,
    "sourceReadMs": 0.0,
    "barReadMs": 0.0,
    "workerMs": 0.0,
    "transportMs": 0.0,
    "totalElapsedMs": 0.0,
  }


def _elapsed_ms(started: float) -> float:
  return round((time.perf_counter() - started) * 1000, 3)


def _expected_chunk_count(
  bars: list[NormalizedBar], quote_provider: ClickHouseL2Adapter, symbol: str, trade_date: str, page_size: int,
) -> int:
  total = len(bars) + quote_provider.count_rows(symbol, trade_date)
  return (total + page_size - 1) // page_size


def _day_chunks(
  bars: list[NormalizedBar], quote_provider: ClickHouseL2Adapter,
  symbol: str, trade_date: str, page_size: int,
) -> Iterator[dict[str, list[dict[str, Any]]]]:
  sources = [
    _tagged_items("bar", iter(bars)),
    _tagged_items("quote", _flatten(quote_provider.iter_quotes(symbol, trade_date, page_size))),
  ]
  current = [next(source, None) for source in sources]
  chunk: dict[str, list[dict[str, Any]]] = {"bars": [], "quotes": [], "trades": []}
  count = 0
  while any(item is not None for item in current):
    source_index = min((index for index, item in enumerate(current) if item is not None), key=lambda index: _event_key(current[index]))
    kind, item = current[source_index]
    chunk[{"bar": "bars", "quote": "quotes"}[kind]].append(asdict(item))
    count += 1
    current[source_index] = next(sources[source_index], None)
    if count == page_size:
      yield chunk
      chunk = {"bars": [], "quotes": [], "trades": []}
      count = 0
  if count:
    yield chunk


def _tagged_items(kind: str, items: Iterator[Any]) -> Iterator[tuple[str, Any]]:
  yield from ((kind, item) for item in items)


def _flatten(pages: Iterator[list[Any]]) -> Iterator[Any]:
  yield from chain.from_iterable(pages)


def _event_key(item: tuple[str, Any] | None) -> tuple[datetime, int, int]:
  if item is None:
    raise ValueError("空 L2 事件不能排序")
  kind, value = item
  if kind == "bar":
    return parse_timestamp(value.end_time), 0, 0
  if kind == "quote":
    return parse_timestamp(value.time), 1, value.sequence
  return parse_timestamp(value.time), 2, value.sequence


def _data_snapshot(
  bar_snapshot: dict[str, Any],
  quotes: ClickHouseL2Adapter,
  payload: dict[str, Any],
  quote_mode: str,
) -> dict[str, Any]:
  snapshot = {
    "executionMode": f"{quote_mode}_quote",
    "quoteMode": quote_mode,
    "quoteGranularitySeconds": 3,
    "eventProtocolVersion": "l2-script-events-v1",
    "bar": bar_snapshot,
    "quote": asdict(quotes.coverage(payload["symbol"], payload["startDate"], payload["endDate"])),
    "event": {
      "enabled": quote_mode == "continuous",
      "reason": "连续 3 秒盘口策略回放" if quote_mode == "continuous" else "基础 L2 主动盘口撮合不回放逐笔成交",
    },
  }
  if bar_snapshot.get("provider") == "temporary_tick_quote_aggregation":
    snapshot["temporaryBarAggregation"] = bar_snapshot
  return snapshot


def _summary(result: dict[str, Any]) -> dict[str, Any]:
  summary = dict(result["summary"])
  summary["warningCount"] = len(result.get("warnings") or [])
  summary["fillModel"] = "l2_quote"
  return summary


def main() -> None:
  logging.basicConfig(level=os.getenv("TREND_GRID_LOG_LEVEL", "INFO"))
  L2Runner().run_forever()


if __name__ == "__main__":
  main()
