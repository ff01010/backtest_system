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


_DATABASE = "raw_historical_data"
_BJ_DATABASE = "qmt_history_dev"
_BJ_RAW_TABLE = "qmt_kline_1m_raw"
_BJ_RUNS_TABLE = "qmt_ingest_runs"
_BJ_ADJUSTMENT = "none"
_DIRECT_PERIODS = {"1m", "5m", "60m", "1d"}
_DERIVED_PERIODS = {"15m": 15, "30m": 30}
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

  @property
  def all_dates(self) -> tuple[str, ...]:
    return self.warmup_dates + self.formal_dates


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
  required_dates = _required_formal_dates(symbol, timeframe, start_date, end_date, formal_dates)
  if not required_dates:
    raise MarketDataError(f"{symbol} {timeframe} 区间内未导入 QMT 行情")
  failures: list[tuple[str, list[str], int]] = []
  for source_period in _source_candidates(symbol, timeframe):
    missing_dates = _missing_dates(symbol, source_period, required_dates)
    warmup_dates = _complete_dates_before(symbol, source_period, start_date, warmup_trading_days)
    if not missing_dates and len(warmup_dates) == warmup_trading_days:
      return BacktestDataResolution(
        timeframe=timeframe,
        source_period=source_period,
        derivation=_derivation(timeframe, source_period),
        formal_dates=tuple(required_dates),
        warmup_dates=tuple(warmup_dates),
        available_warmup_days=len(warmup_dates),
      )
    failures.append((source_period, missing_dates, len(warmup_dates)))
  raise _resolution_error(symbol, timeframe, failures, warmup_trading_days)


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
      required_dates = _required_formal_dates(symbol, timeframe, start_date, end_date, formal_dates)
      missing_dates = _preview_missing_dates(symbol, timeframe, required_dates)
      available_warmup = _preview_warmup_days(symbol, timeframe, start_date, warmup_trading_days)
    except MarketDataError:
      required_dates, missing_dates, available_warmup = [], [], 0
    codes = ["data.bar_missing"]
    if timeframe == "1d" and missing_dates:
      codes.append("data.minute_incomplete")
    if not missing_dates and available_warmup < warmup_trading_days:
      codes = ["data.warmup_insufficient"]
    return {
      "available": False, "resolution": None, "missingDates": missing_dates,
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
  rows = _query_kline_rows(symbol, resolution.source_period, all_dates[0], all_dates[-1])
  frame = _rows_to_frame(rows)
  frame = _transform_source_frame(frame, resolution)
  allowed_dates = set(all_dates)
  frame = frame[frame["time"].str[:8].map(format_api_date).isin(allowed_dates)].reset_index(drop=True)
  if frame.empty:
    raise MarketDataError(f"{symbol} {resolution.timeframe} 区间内无可用 QMT 行情")
  warmup_dates = set(resolution.warmup_dates)
  warmup_bar_count = int(frame["time"].str[:8].map(format_api_date).isin(warmup_dates).sum())
  return frame[["time", "open", "high", "low", "close", "volume"]], warmup_bar_count


def snapshot_for_resolution(
  symbol: str,
  resolution: BacktestDataResolution,
) -> dict[str, Any]:
  """将已选择的数据来源固化为分钟回测兼容的任务快照。"""
  all_dates = resolution.all_dates
  row = _coverage_summary(symbol, resolution.source_period, all_dates[0], all_dates[-1])
  identity = {
    "symbol": symbol,
    "timeframe": resolution.timeframe,
    "sourcePeriod": resolution.source_period,
    "derivation": resolution.derivation,
    "formalDates": list(resolution.formal_dates),
    "warmupDates": list(resolution.warmup_dates),
    "warmupTradingDays": len(resolution.warmup_dates),
    "barCount": int(row.get("bar_count") or 0),
    "lastImportedAt": str(row.get("updated_at") or ""),
  }
  if _is_bj_symbol(symbol):
    identity.update({
      "sourceDatabase": _BJ_DATABASE,
      "sourceTable": _BJ_RAW_TABLE,
      "adjustment": _BJ_ADJUSTMENT,
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
  """返回回测可用周期与 QMT 覆盖情况，不触发任何上游请求。"""
  if start_date and end_date:
    start_date, end_date = _normalized_range(start_date, end_date)
  items: list[dict[str, Any]] = []
  for timeframe in settings.BACKTEST_TIMEFRAMES:
    if not start_date or not end_date:
      items.append(_unavailable_timeframe(timeframe, "请选择回测日期"))
      continue
    items.append(_cached_timeframe_status(symbol, timeframe, start_date, end_date))
  return items


def coverage_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> list[dict[str, str]]:
  """公开给回测预检服务的轻量覆盖记录查询。"""
  return _coverage_status(symbol, timeframe, start_date, end_date)


def list_dates(symbol: str, timeframe: str) -> list[dict[str, Any]]:
  """返回已经导入且覆盖完整的 QMT 交易日。"""
  _validate_symbol(symbol)
  if _is_bj_symbol(symbol):
    rows = _coverage_for_period(symbol, "1m", "20000101", "20991231")
    return [{
      "date": item["date"], "hasKline": True, "hasSignal": False,
    } for item in reversed(rows) if item["status"] == "complete"]
  source_period = "1m" if timeframe in _DERIVED_PERIODS else timeframe
  query = f"""
    SELECT trade_date
    FROM {_DATABASE}.qmt_kline_coverage FINAL
    WHERE symbol = '{symbol}' AND period = '{source_period}' AND status = 'complete'
    ORDER BY trade_date DESC
  """
  try:
    rows = clickhouse_client.query_rows(query)
  except clickhouse_client.ClickHouseError as error:
    raise MarketDataError(str(error)) from error
  return [{"date": format_api_date(str(row["trade_date"])), "hasKline": True, "hasSignal": False} for row in rows]


def status(symbol: str, timeframe: str, preview_limit: int = 20) -> dict[str, Any]:
  """返回数据管理页所需的 QMT 数据概览。"""
  _validate_request(symbol, timeframe, "20000101", "20991231")
  _validate_symbol(symbol)
  if _is_bj_symbol(symbol):
    return _bj_status(symbol, timeframe, preview_limit)
  source_period = "1m" if timeframe in _DERIVED_PERIODS else timeframe
  query = f"""
    SELECT min(trade_date) AS first_date, max(trade_date) AS last_date,
           sum(valid_row_count) AS bar_count, max(updated_at) AS updated_at
    FROM {_DATABASE}.qmt_kline_coverage FINAL
    WHERE symbol = '{symbol}' AND period = '{source_period}' AND status = 'complete'
  """
  try:
    summary = clickhouse_client.query_rows(query)[0]
    preview = _query_preview(symbol, source_period, preview_limit)
  except (IndexError, clickhouse_client.ClickHouseError) as error:
    raise MarketDataError(f"读取 QMT 数据状态失败: {error}") from error
  return {
    "symbol": symbol, "timeframe": timeframe, "path": f"clickhouse://{_DATABASE}.qmt_kline",
    "exists": int(summary.get("bar_count") or 0) > 0, "firstDate": _api_date_or_none(summary.get("first_date")),
    "lastDate": _api_date_or_none(summary.get("last_date")), "barCount": int(summary.get("bar_count") or 0),
    "fileSize": 0, "mtime": str(summary.get("updated_at") or "") or None, "duplicateCount": 0,
    "nonTradingCount": 0, "expectedBarsPerDay": None, "gapDates": [], "preview": preview,
  }


def _bj_status(symbol: str, timeframe: str, preview_limit: int) -> dict[str, Any]:
  statuses = _coverage_for_period(symbol, "1m", "20000101", "20991231")
  complete_dates = [item["date"] for item in statuses if item["status"] == "complete"]
  if complete_dates:
    summary = _coverage_summary(symbol, "1m", complete_dates[0], complete_dates[-1])
    preview = _query_preview(symbol, "1m", preview_limit)
  else:
    summary = {"bar_count": 0, "updated_at": None}
    preview = []
  return {
    "symbol": symbol, "timeframe": timeframe,
    "path": f"clickhouse://{_BJ_DATABASE}.{_BJ_RAW_TABLE}",
    "exists": bool(complete_dates), "firstDate": complete_dates[0] if complete_dates else None,
    "lastDate": complete_dates[-1] if complete_dates else None,
    "barCount": int(summary.get("bar_count") or 0), "fileSize": 0,
    "mtime": str(summary.get("updated_at") or "") or None, "duplicateCount": 0,
    "nonTradingCount": 0, "expectedBarsPerDay": None, "gapDates": [], "preview": preview,
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


def _required_formal_dates(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  formal_dates: Sequence[str] | None,
) -> list[str]:
  if formal_dates is not None:
    return sorted({format_api_date(item) for item in formal_dates})
  dates: set[str] = set()
  for source_period in _source_candidates(symbol, timeframe):
    dates.update(item["date"] for item in _coverage_for_period(symbol, source_period, start_date, end_date))
  return sorted(dates)


def _source_candidates(symbol: str, timeframe: str) -> tuple[str, ...]:
  if _is_bj_symbol(symbol):
    return ("1m",)
  if timeframe == "1d":
    return ("1d", "1m")
  return ("1m",) if timeframe in _DERIVED_PERIODS else (timeframe,)


def _derivation(timeframe: str, source_period: str) -> str | None:
  if timeframe == "1d" and source_period == "1m":
    return "daily-from-1m-v1"
  if source_period == "1m" and timeframe != "1m":
    return f"{timeframe}-from-1m-v1"
  return None


def _missing_dates(symbol: str, source_period: str, required_dates: Sequence[str]) -> list[str]:
  if not required_dates:
    return []
  statuses = _coverage_for_period(symbol, source_period, required_dates[0], required_dates[-1])
  complete_dates = {item["date"] for item in statuses if item["status"] == "complete"}
  return sorted(set(required_dates) - complete_dates)


def _complete_dates_before(symbol: str, source_period: str, start_date: str, count: int) -> list[str]:
  if count == 0:
    return []
  expected_dates = _expected_warmup_dates(start_date, count)
  if not expected_dates:
    return []
  statuses = _coverage_for_period(symbol, source_period, expected_dates[0], expected_dates[-1])
  complete_dates = {item["date"] for item in statuses if item["status"] == "complete"}
  contiguous_dates: list[str] = []
  for trade_date in reversed(expected_dates):
    if trade_date not in complete_dates:
      break
    contiguous_dates.append(trade_date)
  return sorted(contiguous_dates)


def _expected_warmup_dates(start_date: str, count: int) -> list[str]:
  """返回紧邻正式开始日前的预期交易日序列。"""
  end = date.fromisoformat(start_date) - timedelta(days=1)
  begin = end - timedelta(days=count * 3 + 60)
  try:
    if not state_store.is_trade_calendar_range_covered(begin.isoformat(), end.isoformat()):
      raise MarketDataError("交易日历暂不可用，无法校验连续预热数据")
    dates = state_store.list_open_trade_dates(begin.isoformat(), end.isoformat())
  except state_store.DatabaseError as error:
    raise MarketDataError("交易日历暂不可用，无法校验连续预热数据") from error
  return dates[-count:]


def _resolution_error(
  symbol: str,
  timeframe: str,
  failures: Sequence[tuple[str, list[str], int]],
  warmup_days: int,
) -> MarketDataError:
  missing_dates = sorted({item for _, dates, _ in failures for item in dates})
  if missing_dates:
    detail = ", ".join(missing_dates)
    return MarketDataError(f"{symbol} {timeframe} QMT 行情不完整: {detail}")
  available_days = max((days for _, _, days in failures), default=0)
  return MarketDataError(
    f"{symbol} {timeframe} 预热数据不足: 策略声明需要 {warmup_days} 个交易日，"
    f"实际连续完整数据只有 {available_days} 个"
  )


def _preview_missing_dates(symbol: str, timeframe: str, required_dates: Sequence[str]) -> list[str]:
  if not required_dates:
    return []
  candidate_missing = [
    _missing_dates(symbol, source_period, required_dates)
    for source_period in _source_candidates(symbol, timeframe)
  ]
  return sorted(set.intersection(*(set(item) for item in candidate_missing))) if candidate_missing else []


def _preview_warmup_days(symbol: str, timeframe: str, start_date: str, count: int) -> int:
  if count == 0:
    return 0
  return max(
    (len(_complete_dates_before(symbol, source_period, start_date, count)) for source_period in _source_candidates(symbol, timeframe)),
    default=0,
  )


def _resolution_reason(resolution: BacktestDataResolution) -> str:
  if resolution.derivation == "daily-from-1m-v1":
    return "由完整 1m 数据派生日线"
  if resolution.derivation:
    return f"由完整 1m 数据派生 {resolution.timeframe}"
  return "QMT 原始数据可用"


def _transform_source_frame(frame: pd.DataFrame, resolution: BacktestDataResolution) -> pd.DataFrame:
  if resolution.derivation == "daily-from-1m-v1":
    return _aggregate_daily_minutes(frame)
  if resolution.source_period == "1m" and resolution.timeframe != "1m":
    return _aggregate_minutes(frame, int(resolution.timeframe.removesuffix("m")))
  return frame


def _cached_timeframe_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  if timeframe == "1d":
    preview = preview_backtest_data(symbol, timeframe, start_date, end_date)
    if not preview["available"]:
      return _unavailable_timeframe(timeframe, str(preview["reason"]))
    resolution = preview["resolution"]
    return {
      "timeframe": timeframe, "available": True, "reason": str(preview["reason"]),
      "dataStart": resolution.formal_dates[0], "dataEnd": resolution.formal_dates[-1],
    }
  try:
    statuses = _coverage_status(symbol, timeframe, start_date, end_date)
  except MarketDataError as error:
    return _unavailable_timeframe(timeframe, str(error))
  invalid = [item["date"] for item in statuses if item["status"] != "complete"]
  if invalid:
    return _unavailable_timeframe(timeframe, f"QMT 数据不完整: {', '.join(invalid)}")
  if not statuses:
    return _unavailable_timeframe(timeframe, "未导入该标的的 QMT 行情")
  derived_from_minutes = timeframe in _DERIVED_PERIODS or (_is_bj_symbol(symbol) and timeframe != "1m")
  reason = "由 QMT 1 分钟数据聚合" if derived_from_minutes else "QMT 原始数据可用"
  return {"timeframe": timeframe, "available": True, "reason": reason, "dataStart": statuses[0]["date"], "dataEnd": statuses[-1]["date"]}


def _coverage_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> list[dict[str, str]]:
  start_date, end_date = _normalized_range(start_date, end_date)
  _validate_symbol(symbol)
  source_period = "1m" if _is_bj_symbol(symbol) or timeframe in _DERIVED_PERIODS else timeframe
  return _coverage_for_period(symbol, source_period, start_date, end_date)


def _coverage_for_period(symbol: str, source_period: str, start_date: str, end_date: str) -> list[dict[str, str]]:
  _validate_symbol(symbol)
  if _is_bj_symbol(symbol):
    if source_period != "1m":
      return []
    query = _bj_coverage_query(symbol, start_date, end_date)
  else:
    query = f"""
      SELECT trade_date, status
      FROM {_DATABASE}.qmt_kline_coverage FINAL
      WHERE symbol = '{symbol}' AND period = '{source_period}'
        AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
      ORDER BY trade_date
    """
  try:
    rows = clickhouse_client.query_rows(query)
  except clickhouse_client.ClickHouseError as error:
    raise MarketDataError(str(error)) from error
  return [{"date": format_api_date(str(row["trade_date"])), "status": str(row["status"])} for row in rows]


def _bj_coverage_query(symbol: str, start_date: str, end_date: str) -> str:
  return f"""
    WITH latest_runs AS (
      SELECT run_id,
             argMax(state, tuple(state_version, recorded_at)) AS state,
             argMax(target_date, tuple(state_version, recorded_at)) AS target_date,
             argMax(validation_result, tuple(state_version, recorded_at)) AS validation_result,
             argMax(expected_route_count, tuple(state_version, recorded_at)) AS expected_route_count,
             argMax(actual_route_count, tuple(state_version, recorded_at)) AS actual_route_count,
             argMax(missing_symbol_count, tuple(state_version, recorded_at)) AS missing_symbol_count,
             argMax(duplicate_key_count, tuple(state_version, recorded_at)) AS duplicate_key_count,
             argMax(actual_routes, tuple(state_version, recorded_at)) AS actual_routes
      FROM {_BJ_DATABASE}.{_BJ_RUNS_TABLE}
      GROUP BY run_id
    ), symbol_dates AS (
      SELECT trade_date, argMax(run_id, tuple(revision, ingested_at)) AS run_id
      FROM {_BJ_DATABASE}.{_BJ_RAW_TABLE}
      WHERE symbol = '{symbol}' AND period = '1m' AND adjustment = '{_BJ_ADJUSTMENT}'
        AND quality_status = 'valid' AND is_final = 1
        AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
      GROUP BY trade_date
    )
    SELECT symbol_dates.trade_date AS trade_date,
           if(latest_runs.state = 'PUBLISHED' AND latest_runs.validation_result = 'PASS'
              AND latest_runs.target_date = symbol_dates.trade_date
              AND latest_runs.expected_route_count = latest_runs.actual_route_count
              AND latest_runs.missing_symbol_count = 0 AND latest_runs.duplicate_key_count = 0
              AND has(latest_runs.actual_routes, '1m/none'), 'complete', 'partial') AS status
    FROM symbol_dates
    LEFT JOIN latest_runs ON latest_runs.run_id = symbol_dates.run_id
    ORDER BY trade_date
  """


def _coverage_summary(symbol: str, source_period: str, start_date: str, end_date: str) -> dict[str, Any]:
  if _is_bj_symbol(symbol):
    query = f"""
      SELECT uniqExact(bar_time) AS bar_count, max(ingested_at) AS updated_at,
             max(toString(manifest_sha256)) AS manifest_hash
      FROM {_BJ_DATABASE}.{_BJ_RAW_TABLE}
      WHERE symbol = '{symbol}' AND period = '1m' AND adjustment = '{_BJ_ADJUSTMENT}'
        AND quality_status = 'valid' AND is_final = 1
        AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
    """
  else:
    query = f"""
      SELECT sum(valid_row_count) AS bar_count, max(updated_at) AS updated_at
      FROM {_DATABASE}.qmt_kline_coverage FINAL
      WHERE symbol = '{symbol}' AND period = '{source_period}'
        AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
        AND status = 'complete'
    """
  try:
    return clickhouse_client.query_rows(query)[0]
  except (IndexError, clickhouse_client.ClickHouseError) as error:
    raise MarketDataError(f"读取 QMT 数据快照失败: {error}") from error


def _query_kline_rows(symbol: str, period: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
  _validate_symbol(symbol)
  if _is_bj_symbol(symbol):
    query = _bj_kline_query(symbol, start_date, end_date)
  else:
    start_time, end_time = _epoch_range(start_date, end_date)
    query = f"""
      SELECT qmt_index, time, open, high, low, close, volume
      FROM {_DATABASE}.qmt_kline FINAL
      WHERE symbol = '{symbol}' AND period = '{period}' AND time BETWEEN {start_time} AND {end_time}
      ORDER BY time
    """
  try:
    return clickhouse_client.query_rows(query)
  except clickhouse_client.ClickHouseError as error:
    raise MarketDataError(str(error)) from error


def _query_preview(symbol: str, period: str, limit: int) -> list[dict[str, Any]]:
  _validate_symbol(symbol)
  row_limit = max(1, min(limit, 200))
  if _is_bj_symbol(symbol):
    query = _bj_kline_query(symbol, "20000101", "20991231", descending=True, limit=row_limit)
  else:
    query = f"""
      SELECT qmt_index, time, open, high, low, close, volume
      FROM {_DATABASE}.qmt_kline FINAL
      WHERE symbol = '{symbol}' AND period = '{period}' ORDER BY time DESC LIMIT {row_limit}
    """
  rows = clickhouse_client.query_rows(query)
  return [_row_preview(row) for row in rows]


def _bj_kline_query(
  symbol: str,
  start_date: str,
  end_date: str,
  descending: bool = False,
  limit: int | None = None,
) -> str:
  order = "DESC" if descending else "ASC"
  limit_clause = f" LIMIT {limit}" if limit is not None else ""
  return f"""
    SELECT formatDateTime(bar_time, '%Y%m%d%H%i%S', 'Asia/Shanghai') AS qmt_index,
           toUInt64(toUnixTimestamp(bar_time)) * 1000 AS time, open, high, low, close, volume
    FROM {_BJ_DATABASE}.{_BJ_RAW_TABLE}
    WHERE symbol = '{symbol}' AND period = '1m' AND adjustment = '{_BJ_ADJUSTMENT}'
      AND quality_status = 'valid' AND is_final = 1
      AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
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
  supported = _DIRECT_PERIODS | set(_DERIVED_PERIODS)
  if timeframe not in supported and not (_is_bj_symbol(symbol) and timeframe in settings.BAR_RUN_TIMEFRAMES):
    raise MarketDataError(f"QMT 数据暂不支持周期: {timeframe}")
  _normalized_range(start_date, end_date)


def _validate_warmup_days(value: int) -> None:
  if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 250:
    raise MarketDataError("预热交易日数必须为 0 到 250 的整数")


def _validate_symbol(symbol: str) -> None:
  if not settings.validate_symbol(symbol):
    raise MarketDataError(f"标的格式无效: {symbol}")


def _is_bj_symbol(symbol: str) -> bool:
  return symbol.endswith(".BJ")


def _epoch_range(start_date: str, end_date: str) -> tuple[int, int]:
  start = datetime.combine(parse_trading_date(start_date), datetime.min.time(), tzinfo=_TIMEZONE)
  end = datetime.combine(parse_trading_date(end_date), datetime.max.time(), tzinfo=_TIMEZONE)
  return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _date_sql(value: str) -> str:
  return format_api_date(value)


def _api_date_or_none(value: Any) -> str | None:
  if value is None:
    return None
  return format_api_date(str(value))


def _normalized_range(start_date: str, end_date: str) -> tuple[str, str]:
  try:
    return normalize_date_range(start_date, end_date)
  except TradingDateError as error:
    raise MarketDataError(str(error)) from error


def _unavailable_timeframe(timeframe: str, reason: str) -> dict[str, Any]:
  return {"timeframe": timeframe, "available": False, "reason": reason, "dataStart": None, "dataEnd": None}
