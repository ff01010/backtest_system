"""QMT 原始历史行情的 ClickHouse 读取、覆盖检查和周期聚合服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import math
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from backend import settings
from backend.date_codec import TradingDateError, format_api_date, normalize_date_range, parse_trading_date
from backend.services import clickhouse_client, state_store


_LEGACY_SOURCE = "legacy"
_HISTORY_SOURCE = "history_dev"
_LEGACY_DATABASE = "raw_historical_data"
_HISTORY_DATABASE = "qmt_history_dev"
_HISTORY_RUNS_TABLE = "qmt_ingest_runs"
_HISTORY_UNIVERSE_TABLE = "qmt_universe_snapshot"
_HISTORY_ADJUSTMENT = "none"
_HISTORY_TABLES = {
  "1m": "qmt_kline_1m_raw",
  "5m": "qmt_kline_5m_raw",
  "15m": "qmt_kline_15m_raw",
  "30m": "qmt_kline_30m_raw",
  "1h": "qmt_kline_1h_raw",
  "1d": "qmt_kline_1d_raw",
}
_HISTORY_PERIODS = {
  "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "60m": "1h", "1d": "1d",
}
_LEGACY_DIRECT_PERIODS = {"1m", "5m", "60m", "1d"}
_LEGACY_DERIVED_PERIODS = {"3m": 3, "15m": 15, "30m": 30, "45m": 45}
_HISTORY_DERIVED_PERIODS = {"3m": 3, "45m": 45}
_TIMEZONE = ZoneInfo("Asia/Shanghai")


class MarketDataError(ValueError):
  """QMT 行情未导入、未覆盖或数据结构无效。"""


@dataclass(frozen=True)
class BacktestDataResolution:
  """标准 QMT Bar 回测使用的单一数据来源与实际加载范围。"""

  timeframe: str
  source_period: str
  derivation: str | None
  formal_dates: tuple[str, ...]
  warmup_dates: tuple[str, ...]
  available_warmup_days: int
  source: str = _LEGACY_SOURCE

  @property
  def all_dates(self) -> tuple[str, ...]:
    return self.warmup_dates + self.formal_dates


@dataclass(frozen=True)
class _SourceCandidate:
  source: str
  source_period: str
  derivation: str | None = None


def resolve_backtest_data(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  formal_dates: Sequence[str] | None = None,
  warmup_trading_days: int = 0,
) -> BacktestDataResolution:
  """为标准 QMT Bar 回测选择一个覆盖正式和预热区间的唯一来源。"""
  start_date, end_date = _normalized_range(start_date, end_date)
  _validate_request(symbol, timeframe, start_date, end_date)
  _validate_warmup_days(warmup_trading_days)
  candidates = _source_candidates(timeframe)
  calendar_dates = None if formal_dates is None else {format_api_date(item) for item in formal_dates}
  if calendar_dates is not None and not calendar_dates:
    raise MarketDataError(f"{symbol} {timeframe} 区间内未导入 QMT 行情")
  available_warmup_days = 0
  has_formal_bars = False
  for candidate in candidates:
    statuses = _coverage_for_source(symbol, candidate, start_date, end_date)
    candidate_dates = sorted({
      item["date"] for item in statuses
      if item["status"] == "complete" and (calendar_dates is None or item["date"] in calendar_dates)
    })
    if not candidate_dates:
      continue
    has_formal_bars = True
    warmup_dates = _complete_dates_before(symbol, candidate, start_date, warmup_trading_days)
    available_warmup_days = max(available_warmup_days, len(warmup_dates))
    if len(warmup_dates) == warmup_trading_days:
      return BacktestDataResolution(
        timeframe=timeframe,
        source_period=candidate.source_period,
        derivation=candidate.derivation,
        formal_dates=tuple(candidate_dates),
        warmup_dates=tuple(warmup_dates),
        available_warmup_days=len(warmup_dates),
        source=candidate.source,
      )
  if not has_formal_bars:
    raise MarketDataError(f"{symbol} {timeframe} 区间内无完整 QMT 行情")
  raise MarketDataError(
    f"{symbol} {timeframe} 预热数据不足: 策略声明需要 {warmup_trading_days} 个交易日，"
    f"实际可用数据只有 {available_warmup_days} 个"
  )


def preview_backtest_data(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  formal_dates: Sequence[str] | None = None,
  warmup_trading_days: int = 0,
) -> dict[str, Any]:
  """返回与实际加载共用的来源决策，供预检和日期可用性展示。"""
  start_date, end_date = _normalized_range(start_date, end_date)
  try:
    resolution = resolve_backtest_data(
      symbol, timeframe, start_date, end_date, formal_dates, warmup_trading_days,
    )
  except MarketDataError as error:
    try:
      available_warmup = _preview_warmup_days(symbol, timeframe, start_date, warmup_trading_days)
    except MarketDataError:
      available_warmup = 0
    codes = ["data.bar_missing"]
    if "预热数据不足" in str(error):
      codes = ["data.warmup_insufficient"]
    return {
      "available": False, "resolution": None, "missingDates": [],
      "missingDataTypes": codes, "availableWarmupDays": available_warmup, "reason": str(error),
    }
  return {
    "available": True, "resolution": resolution, "missingDates": [], "missingDataTypes": [],
    "availableWarmupDays": resolution.available_warmup_days,
    "reason": _resolution_reason(resolution),
  }


def load_backtest_bars(
  symbol: str,
  resolution: BacktestDataResolution,
) -> tuple[pd.DataFrame, int]:
  """按已解析的来源读取 Bar，并返回预热 Bar 数量。"""
  all_dates = resolution.all_dates
  if not all_dates:
    raise MarketDataError("回测数据范围为空")
  try:
    rows = _query_kline_rows(symbol, resolution, all_dates[0], all_dates[-1])
    frame = _rows_to_frame(rows)
    frame = _transform_source_frame(frame, resolution)
    if frame.empty:
      raise MarketDataError(f"{symbol} {resolution.timeframe} 区间内无可用 QMT 行情")
    allowed_dates = set(all_dates)
    frame_dates = frame["time"].str[:8].map(format_api_date)
    frame = frame[frame_dates.isin(allowed_dates)].reset_index(drop=True)
    if frame.empty:
      raise MarketDataError(f"{symbol} {resolution.timeframe} 区间内无可用 QMT 行情")
    warmup_dates = set(resolution.warmup_dates)
    warmup_bar_count = int(frame["time"].str[:8].map(format_api_date).isin(warmup_dates).sum())
  except MarketDataError:
    raise
  except Exception as error:
    raise MarketDataError(
      f"{symbol} {resolution.timeframe} QMT Bar 行数据格式无效",
    ) from error
  return frame[["time", "open", "high", "low", "close", "volume"]], warmup_bar_count


def snapshot_for_resolution(
  symbol: str,
  resolution: BacktestDataResolution,
  bar_count: int | None = None,
) -> dict[str, Any]:
  """将已选择的数据来源固化为分钟回测兼容的任务快照。"""
  all_dates = resolution.all_dates
  row = (
    _coverage_summary(symbol, resolution, all_dates[0], all_dates[-1])
    if resolution.source == _LEGACY_SOURCE else {}
  )
  identity = {
    "symbol": symbol,
    "timeframe": resolution.timeframe,
    "sourcePeriod": resolution.source_period,
    "derivation": resolution.derivation,
    "formalDates": list(resolution.formal_dates),
    "warmupDates": list(resolution.warmup_dates),
    "warmupTradingDays": len(resolution.warmup_dates),
    "barCount": int(bar_count if bar_count is not None else row.get("bar_count") or 0),
    "lastImportedAt": str(row.get("updated_at") or ""),
  }
  identity.update({
    "sourceDatabase": _source_database(resolution.source),
    "sourceTable": _source_table(resolution.source, resolution.source_period),
  })
  if resolution.source == _HISTORY_SOURCE:
    identity.update({
      "adjustment": _HISTORY_ADJUSTMENT,
      "manifestHash": str(row.get("manifest_hash") or ""),
    })
  identity["hash"] = hashlib.sha256(repr(sorted(identity.items())).encode("utf-8")).hexdigest()
  return identity


def ensure_cached(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  """兼容旧调用方：QMT 数据只检查覆盖，不在回测时联网抓取。"""
  resolve_backtest_data(symbol, timeframe, start_date, end_date)
  return {"status": "qmt_available", "fetchedDates": [], "rowCount": 0}


def load_bars(symbol: str, timeframe: str, start_date: str, end_date: str) -> pd.DataFrame:
  """从 ClickHouse 读取 QMT 原始 K 线，必要时由 1 分钟线派生周期。"""
  resolution = resolve_backtest_data(symbol, timeframe, start_date, end_date)
  frame, _ = load_backtest_bars(symbol, resolution)
  return frame


def timeframe_status(symbol: str, start_date: str | None, end_date: str | None) -> list[dict[str, Any]]:
  """返回引擎支持的周期；日期范围只由数据状态接口展示。"""
  _validate_symbol(symbol)
  if start_date and end_date:
    _normalized_range(start_date, end_date)
  return [{
    "timeframe": timeframe,
    "available": True,
    "reason": "平台支持",
    "dataStart": None,
    "dataEnd": None,
  } for timeframe in settings.BACKTEST_TIMEFRAMES]


def required_trade_dates(start_date: str, end_date: str) -> list[str]:
  """在 Worker 执行阶段读取请求区间应包含的交易日。"""
  start_date, end_date = _normalized_range(start_date, end_date)
  try:
    if not state_store.is_trade_calendar_range_covered(start_date, end_date):
      raise MarketDataError("交易日历未覆盖本次回测区间")
    dates = state_store.list_open_trade_dates(start_date, end_date)
  except state_store.DatabaseError as error:
    raise MarketDataError("交易日历暂不可用") from error
  if not dates:
    raise MarketDataError("回测区间内没有交易日")
  return dates


def coverage_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> list[dict[str, str]]:
  """公开给回测预检服务的轻量覆盖记录查询。"""
  return _coverage_status(symbol, timeframe, start_date, end_date)


def list_dates(symbol: str, timeframe: str) -> list[dict[str, Any]]:
  """返回已经导入且覆盖完整的 QMT 交易日。"""
  _validate_symbol(symbol)
  dates = {
    item["date"]
    for candidate in _source_candidates(timeframe)
    for item in _coverage_for_source(symbol, candidate, "20000101", "20991231")
    if item["status"] == "complete"
  }
  return [{"date": item, "hasKline": True, "hasSignal": False} for item in sorted(dates, reverse=True)]


def status(symbol: str, timeframe: str, preview_limit: int = 20) -> dict[str, Any]:
  """返回数据管理页所需的 QMT 数据概览。"""
  _validate_request(symbol, timeframe, "20000101", "20991231")
  _validate_symbol(symbol)
  candidate, coverage_range = _preferred_available_range(symbol, timeframe)
  if candidate is None:
    return _empty_status(symbol, timeframe)
  return {
    "symbol": symbol, "timeframe": timeframe,
    "path": coverage_range["path"],
    "exists": True,
    "firstDate": coverage_range["firstDate"],
    "lastDate": coverage_range["lastDate"],
    "barCount": coverage_range["barCount"],
    "fileSize": 0,
    "mtime": coverage_range["updatedAt"],
    "duplicateCount": 0,
    "nonTradingCount": 0, "expectedBarsPerDay": None, "gapDates": [], "preview": [],
  }


def _preferred_available_range(
  symbol: str,
  timeframe: str,
) -> tuple[_SourceCandidate | None, dict[str, Any]]:
  for candidate in _source_candidates(timeframe):
    coverage_range = _coverage_range_for_source(symbol, candidate)
    if coverage_range["firstDate"] and coverage_range["lastDate"]:
      path = (
        f"{_HISTORY_DATABASE}.{_HISTORY_RUNS_TABLE}+"
        f"{_HISTORY_DATABASE}.{_HISTORY_UNIVERSE_TABLE}"
        if candidate.source == _HISTORY_SOURCE
        else f"{_LEGACY_DATABASE}.qmt_kline_coverage"
      )
      return candidate, {**coverage_range, "path": f"clickhouse://{path}"}
  return None, {}


def _empty_status(symbol: str, timeframe: str) -> dict[str, Any]:
  return {
    "symbol": symbol, "timeframe": timeframe, "path": None, "exists": False,
    "firstDate": None, "lastDate": None, "barCount": 0, "fileSize": 0, "mtime": None,
    "duplicateCount": 0, "nonTradingCount": 0, "expectedBarsPerDay": None,
    "gapDates": [], "preview": [],
  }


def snapshot(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  """返回回测数据快照标识，记录读取的数据范围和最新导入时间。"""
  resolution = resolve_backtest_data(symbol, timeframe, start_date, end_date)
  return snapshot_for_resolution(symbol, resolution)


def _require_complete(symbol: str, timeframe: str, start_date: str, end_date: str) -> None:
  start_date, end_date = _normalized_range(start_date, end_date)
  _validate_request(symbol, timeframe, start_date, end_date)
  statuses = _coverage_status(symbol, timeframe, start_date, end_date)
  invalid = [item["date"] for item in statuses if item["status"] != "complete"]
  if invalid:
    raise MarketDataError(f"{symbol} {timeframe} QMT 行情不完整: {', '.join(invalid)}")
  if not statuses:
    raise MarketDataError(f"{symbol} {timeframe} 区间内未导入 QMT 行情")


def _source_candidates(timeframe: str) -> tuple[_SourceCandidate, ...]:
  candidates: list[_SourceCandidate] = []
  history_period = _HISTORY_PERIODS.get(timeframe)
  if history_period is not None:
    candidates.append(_SourceCandidate(_HISTORY_SOURCE, history_period))
  elif timeframe in _HISTORY_DERIVED_PERIODS:
    candidates.append(_SourceCandidate(_HISTORY_SOURCE, "1m", f"{timeframe}-from-1m-v1"))
  if timeframe in _LEGACY_DIRECT_PERIODS:
    candidates.append(_SourceCandidate(_LEGACY_SOURCE, timeframe))
  elif timeframe in _LEGACY_DERIVED_PERIODS:
    candidates.append(_SourceCandidate(_LEGACY_SOURCE, "1m", f"{timeframe}-from-1m-v1"))
  if timeframe == "1d":
    candidates.append(_SourceCandidate(_LEGACY_SOURCE, "1m", "daily-from-1m-v1"))
  return tuple(candidates)


def _complete_dates_before(symbol: str, candidate: _SourceCandidate, start_date: str, count: int) -> list[str]:
  if count == 0:
    return []
  expected_dates = _expected_warmup_dates(start_date, count)
  if not expected_dates:
    return []
  statuses = _coverage_for_source(symbol, candidate, expected_dates[0], expected_dates[-1])
  complete_dates = {item["date"] for item in statuses if item["status"] == "complete"}
  return [trade_date for trade_date in expected_dates if trade_date in complete_dates][-count:]


def _expected_warmup_dates(start_date: str, count: int) -> list[str]:
  """返回预热查找窗口内的开市日，停牌日不计入标的实际预热天数。"""
  end = date.fromisoformat(start_date) - timedelta(days=1)
  begin = end - timedelta(days=count * 3 + 60)
  try:
    if not state_store.is_trade_calendar_range_covered(begin.isoformat(), end.isoformat()):
      raise MarketDataError("交易日历暂不可用，无法计算预热日期")
    dates = state_store.list_open_trade_dates(begin.isoformat(), end.isoformat())
  except state_store.DatabaseError as error:
    raise MarketDataError("交易日历暂不可用，无法计算预热日期") from error
  return dates


def _preview_warmup_days(symbol: str, timeframe: str, start_date: str, count: int) -> int:
  if count == 0:
    return 0
  return max(
    (len(_complete_dates_before(symbol, candidate, start_date, count)) for candidate in _source_candidates(timeframe)),
    default=0,
  )


def _resolution_reason(resolution: BacktestDataResolution) -> str:
  if resolution.derivation == "daily-from-1m-v1":
    return "由完整 1m 数据派生日线"
  if resolution.derivation:
    return f"由完整 1m 数据派生 {resolution.timeframe}"
  if resolution.source == _HISTORY_SOURCE:
    return "QMT 优先行情可用"
  return "QMT 兼容行情可用"


def _transform_source_frame(frame: pd.DataFrame, resolution: BacktestDataResolution) -> pd.DataFrame:
  if resolution.derivation == "daily-from-1m-v1":
    return _aggregate_daily_minutes(frame)
  if resolution.derivation:
    return _aggregate_minutes(frame, int(resolution.timeframe.removesuffix("m")))
  return frame


def _cached_timeframe_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  preview = preview_backtest_data(symbol, timeframe, start_date, end_date)
  if not preview["available"]:
    return _unavailable_timeframe(timeframe, str(preview["reason"]))
  resolution = preview["resolution"]
  return {
    "timeframe": timeframe, "available": True, "reason": str(preview["reason"]),
    "dataStart": resolution.formal_dates[0], "dataEnd": resolution.formal_dates[-1],
  }


def _coverage_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> list[dict[str, str]]:
  start_date, end_date = _normalized_range(start_date, end_date)
  _validate_symbol(symbol)
  resolution = resolve_backtest_data(symbol, timeframe, start_date, end_date)
  return [{"date": item, "status": "complete"} for item in resolution.formal_dates]


def _coverage_for_source(
  symbol: str,
  candidate: _SourceCandidate,
  start_date: str,
  end_date: str,
) -> list[dict[str, str]]:
  _validate_symbol(symbol)
  if candidate.source == _HISTORY_SOURCE:
    query = _history_coverage_query(symbol, candidate.source_period, start_date, end_date)
  else:
    query = f"""
      SELECT trade_date, status
      FROM {_LEGACY_DATABASE}.qmt_kline_coverage FINAL
      WHERE symbol = '{symbol}' AND period = '{candidate.source_period}'
        AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
      ORDER BY trade_date
    """
  try:
    rows = clickhouse_client.query_rows(query)
  except clickhouse_client.ClickHouseError as error:
    raise MarketDataError(str(error)) from error
  return [{"date": format_api_date(str(row["trade_date"])), "status": str(row["status"])} for row in rows]


def _coverage_range_for_source(
  symbol: str,
  candidate: _SourceCandidate,
) -> dict[str, Any]:
  """只读取覆盖元数据聚合范围，避免把多年逐日记录拉回应用层。"""
  _validate_symbol(symbol)
  if candidate.source == _HISTORY_SOURCE:
    query = _history_coverage_range_query(symbol, candidate.source_period)
  else:
    query = f"""
      SELECT min(trade_date) AS first_date,
             max(trade_date) AS last_date,
             sum(valid_row_count) AS bar_count,
             max(updated_at) AS updated_at
      FROM {_LEGACY_DATABASE}.qmt_kline_coverage FINAL
      WHERE symbol = '{symbol}' AND period = '{candidate.source_period}'
        AND status = 'complete'
    """
  try:
    rows = clickhouse_client.query_rows_with_retry(query)
  except clickhouse_client.ClickHouseError as error:
    raise MarketDataError(str(error)) from error
  row = rows[0] if rows else {}
  first_date = row.get("first_date")
  last_date = row.get("last_date")
  return {
    "firstDate": format_api_date(str(first_date)) if first_date else None,
    "lastDate": format_api_date(str(last_date)) if last_date else None,
    "barCount": int(row["bar_count"]) if row.get("bar_count") is not None else None,
    "updatedAt": str(row["updated_at"]) if row.get("updated_at") else None,
  }


def _history_coverage_query(symbol: str, period: str, start_date: str, end_date: str) -> str:
  return f"""
    WITH latest_runs AS (
      SELECT runs.run_id AS run_id,
             argMax(runs.state, tuple(runs.state_version, runs.recorded_at)) AS state,
             argMax(runs.target_date, tuple(runs.state_version, runs.recorded_at)) AS target_date,
             argMax(runs.validation_result, tuple(runs.state_version, runs.recorded_at)) AS validation_result,
             argMax(runs.expected_route_count, tuple(runs.state_version, runs.recorded_at)) AS expected_route_count,
             argMax(runs.actual_route_count, tuple(runs.state_version, runs.recorded_at)) AS actual_route_count,
             argMax(runs.universe_snapshot_id, tuple(runs.state_version, runs.recorded_at)) AS universe_snapshot_id,
             argMax(runs.missing_symbol_count, tuple(runs.state_version, runs.recorded_at)) AS missing_symbol_count,
             argMax(runs.duplicate_key_count, tuple(runs.state_version, runs.recorded_at)) AS duplicate_key_count,
             argMax(runs.actual_routes, tuple(runs.state_version, runs.recorded_at)) AS actual_routes
      FROM {_HISTORY_DATABASE}.{_HISTORY_RUNS_TABLE} AS runs
      WHERE runs.target_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
      GROUP BY runs.run_id
    ), eligible_runs AS (
      SELECT latest_runs.target_date AS target_date,
             latest_runs.universe_snapshot_id AS universe_snapshot_id
      FROM latest_runs
      WHERE latest_runs.state = 'PUBLISHED'
        AND latest_runs.validation_result = 'PASS'
        AND latest_runs.expected_route_count = latest_runs.actual_route_count
        AND latest_runs.missing_symbol_count = 0
        AND latest_runs.duplicate_key_count = 0
        AND has(latest_runs.actual_routes, '{period}/{_HISTORY_ADJUSTMENT}')
    )
    SELECT eligible_runs.target_date AS trade_date, 'complete' AS status
    FROM eligible_runs
    INNER JOIN {_HISTORY_DATABASE}.{_HISTORY_UNIVERSE_TABLE} AS snapshots
      ON snapshots.universe_snapshot_id = eligible_runs.universe_snapshot_id
    WHERE snapshots.symbol = '{symbol}'
      AND snapshots.is_active = 1
    GROUP BY eligible_runs.target_date
    ORDER BY eligible_runs.target_date
  """


def _history_coverage_range_query(symbol: str, period: str) -> str:
  """组合发布批次与标的快照，直接聚合该标的声明的可用范围。"""
  return f"""
    WITH latest_runs AS (
      SELECT runs.run_id AS run_id,
             argMax(runs.state, tuple(runs.state_version, runs.recorded_at)) AS state,
             argMax(runs.target_date, tuple(runs.state_version, runs.recorded_at)) AS target_date,
             argMax(runs.validation_result, tuple(runs.state_version, runs.recorded_at)) AS validation_result,
             argMax(runs.expected_route_count, tuple(runs.state_version, runs.recorded_at)) AS expected_route_count,
             argMax(runs.actual_route_count, tuple(runs.state_version, runs.recorded_at)) AS actual_route_count,
             argMax(runs.universe_snapshot_id, tuple(runs.state_version, runs.recorded_at)) AS universe_snapshot_id,
             argMax(runs.missing_symbol_count, tuple(runs.state_version, runs.recorded_at)) AS missing_symbol_count,
             argMax(runs.duplicate_key_count, tuple(runs.state_version, runs.recorded_at)) AS duplicate_key_count,
             argMax(runs.actual_routes, tuple(runs.state_version, runs.recorded_at)) AS actual_routes
      FROM {_HISTORY_DATABASE}.{_HISTORY_RUNS_TABLE} AS runs
      GROUP BY runs.run_id
    ), eligible_runs AS (
      SELECT latest_runs.target_date AS target_date,
             latest_runs.universe_snapshot_id AS universe_snapshot_id
      FROM latest_runs
      WHERE latest_runs.state = 'PUBLISHED'
        AND latest_runs.validation_result = 'PASS'
        AND latest_runs.expected_route_count = latest_runs.actual_route_count
        AND latest_runs.missing_symbol_count = 0
        AND latest_runs.duplicate_key_count = 0
        AND has(latest_runs.actual_routes, '{period}/{_HISTORY_ADJUSTMENT}')
    )
    SELECT min(eligible_runs.target_date) AS first_date,
           max(eligible_runs.target_date) AS last_date
    FROM eligible_runs
    INNER JOIN {_HISTORY_DATABASE}.{_HISTORY_UNIVERSE_TABLE} AS snapshots
      ON snapshots.universe_snapshot_id = eligible_runs.universe_snapshot_id
    WHERE snapshots.symbol = '{symbol}'
      AND snapshots.is_active = 1
  """


def _coverage_summary(
  symbol: str,
  resolution: BacktestDataResolution,
  start_date: str,
  end_date: str,
) -> dict[str, Any]:
  query = f"""
    SELECT sum(valid_row_count) AS bar_count, max(updated_at) AS updated_at
    FROM {_LEGACY_DATABASE}.qmt_kline_coverage FINAL
    WHERE symbol = '{symbol}' AND period = '{resolution.source_period}'
      AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
      AND status = 'complete'
  """
  try:
    return clickhouse_client.query_rows(query)[0]
  except (IndexError, clickhouse_client.ClickHouseError) as error:
    raise MarketDataError(f"读取 QMT 数据快照失败: {error}") from error


def _query_kline_rows(
  symbol: str,
  resolution: BacktestDataResolution,
  start_date: str,
  end_date: str,
) -> list[dict[str, Any]]:
  _validate_symbol(symbol)
  if resolution.source == _HISTORY_SOURCE:
    query = _history_kline_query(symbol, resolution.source_period, start_date, end_date)
  else:
    start_time, end_time = _epoch_range(start_date, end_date)
    query = f"""
      SELECT qmt_index, time, open, high, low, close, volume
      FROM {_LEGACY_DATABASE}.qmt_kline FINAL
      WHERE symbol = '{symbol}' AND period = '{resolution.source_period}'
        AND time BETWEEN {start_time} AND {end_time}
      ORDER BY time
    """
  try:
    return clickhouse_client.query_rows(query)
  except clickhouse_client.ClickHouseError as error:
    raise MarketDataError(str(error)) from error


def _query_preview(
  symbol: str,
  candidate: _SourceCandidate,
  start_date: str,
  end_date: str,
  limit: int,
) -> list[dict[str, Any]]:
  _validate_symbol(symbol)
  row_limit = max(1, min(limit, 200))
  if candidate.source == _HISTORY_SOURCE:
    query = _history_kline_query(
      symbol, candidate.source_period, start_date, end_date, descending=True, limit=row_limit,
    )
  else:
    start_time, end_time = _epoch_range(start_date, end_date)
    query = f"""
      SELECT qmt_index, time, open, high, low, close, volume
      FROM {_LEGACY_DATABASE}.qmt_kline FINAL
      WHERE symbol = '{symbol}' AND period = '{candidate.source_period}'
        AND time BETWEEN {start_time} AND {end_time}
      ORDER BY time DESC LIMIT {row_limit}
    """
  rows = clickhouse_client.query_rows(query)
  return [_row_preview(row) for row in rows]


def _history_kline_query(
  symbol: str,
  period: str,
  start_date: str,
  end_date: str,
  descending: bool = False,
  limit: int | None = None,
) -> str:
  table = _source_table(_HISTORY_SOURCE, period)
  order = "DESC" if descending else "ASC"
  limit_clause = f" LIMIT {limit}" if limit is not None else ""
  return f"""
    SELECT qmt_index, time, open, high, low, close, volume
    FROM (
      SELECT bar_time,
             argMax(qmt_index, tuple(revision, ingested_at)) AS qmt_index,
             argMax(qmt_time_ms, tuple(revision, ingested_at)) AS time,
             argMax(open, tuple(revision, ingested_at)) AS open,
             argMax(high, tuple(revision, ingested_at)) AS high,
             argMax(low, tuple(revision, ingested_at)) AS low,
             argMax(close, tuple(revision, ingested_at)) AS close,
             argMax(volume, tuple(revision, ingested_at)) AS volume
      FROM {_HISTORY_DATABASE}.{table}
      WHERE symbol = '{symbol}' AND period = '{period}' AND adjustment = '{_HISTORY_ADJUSTMENT}'
        AND is_final = 1
        AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
      GROUP BY bar_time
    )
    ORDER BY bar_time {order}{limit_clause}
  """


def _rows_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
  frame = pd.DataFrame(rows)
  if frame.empty:
    return frame
  frame["_timestamp"] = pd.to_datetime(frame["time"].astype("int64"), unit="ms", utc=True).dt.tz_convert(_TIMEZONE)
  for column in ("open", "high", "low", "close", "volume"):
    frame[column] = pd.to_numeric(frame[column])
  frame["time"] = frame["_timestamp"].dt.strftime("%Y%m%d%H%M")
  return frame


def _aggregate_minutes(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
  if frame.empty:
    return frame
  frame = frame.copy()
  frame["_bucket"] = frame["_timestamp"].map(lambda item: _period_end(item.to_pydatetime(), minutes))
  grouped = frame.groupby("_bucket", sort=True)
  result = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).reset_index()
  result["time"] = result["_bucket"].dt.strftime("%Y%m%d%H%M")
  return result


def _aggregate_daily_minutes(frame: pd.DataFrame) -> pd.DataFrame:
  """将完整分钟数据按交易日聚合；午间休市不是日线边界。"""
  if frame.empty:
    return frame
  frame = frame.copy().sort_values("_timestamp")
  frame["_trade_date"] = frame["_timestamp"].dt.strftime("%Y%m%d")
  grouped = frame.groupby("_trade_date", sort=True)
  result = grouped.agg({
    "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "time": "last",
  }).reset_index(drop=True)
  return result[["time", "open", "high", "low", "close", "volume"]]


def _period_end(value: datetime, minutes: int) -> datetime:
  morning = value.replace(hour=9, minute=30, second=0, microsecond=0)
  afternoon = value.replace(hour=13, minute=0, second=0, microsecond=0)
  session_start = morning if morning <= value <= value.replace(hour=11, minute=30, second=0, microsecond=0) else afternoon
  offset = int((value - session_start).total_seconds() // 60)
  bucket_count = max(1, math.ceil(offset / minutes))
  return session_start + timedelta(minutes=bucket_count * minutes)


def _row_preview(row: dict[str, Any]) -> dict[str, Any]:
  timestamp = datetime.fromtimestamp(int(row["time"]) / 1000, tz=_TIMEZONE)
  return {
    "time": timestamp.strftime("%Y%m%d%H%M"), "open": float(row["open"]),
    "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]),
    "volume": float(row["volume"]),
  }


def _validate_request(symbol: str, timeframe: str, start_date: str, end_date: str) -> None:
  _validate_symbol(symbol)
  if timeframe not in settings.BAR_RUN_TIMEFRAMES or not _source_candidates(timeframe):
    raise MarketDataError(f"QMT 数据暂不支持周期: {timeframe}")
  _normalized_range(start_date, end_date)


def _validate_warmup_days(value: int) -> None:
  if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 250:
    raise MarketDataError("预热交易日数必须为 0 到 250 的整数")


def _validate_symbol(symbol: str) -> None:
  if not settings.validate_symbol(symbol):
    raise MarketDataError(f"标的格式无效: {symbol}")


def _source_database(source: str) -> str:
  if source == _HISTORY_SOURCE:
    return _HISTORY_DATABASE
  if source == _LEGACY_SOURCE:
    return _LEGACY_DATABASE
  raise MarketDataError(f"未知 QMT 行情来源: {source}")


def _source_table(source: str, source_period: str) -> str:
  if source == _HISTORY_SOURCE:
    table = _HISTORY_TABLES.get(source_period)
    if table is None:
      raise MarketDataError(f"新库不支持物理周期: {source_period}")
    return table
  if source == _LEGACY_SOURCE:
    return "qmt_kline"
  raise MarketDataError(f"未知 QMT 行情来源: {source}")


def _epoch_range(start_date: str, end_date: str) -> tuple[int, int]:
  start = datetime.combine(parse_trading_date(start_date), datetime.min.time(), tzinfo=_TIMEZONE)
  end = datetime.combine(parse_trading_date(end_date), datetime.max.time(), tzinfo=_TIMEZONE)
  return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _date_sql(value: str) -> str:
  return format_api_date(value)


def _normalized_range(start_date: str, end_date: str) -> tuple[str, str]:
  try:
    return normalize_date_range(start_date, end_date)
  except TradingDateError as error:
    raise MarketDataError(str(error)) from error


def _unavailable_timeframe(timeframe: str, reason: str) -> dict[str, Any]:
  return {"timeframe": timeframe, "available": False, "reason": reason, "dataStart": None, "dataEnd": None}
