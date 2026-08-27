"""QMT 原始历史行情的 ClickHouse 读取、覆盖检查和周期聚合服务。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import math
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from backend import settings
from backend.date_codec import TradingDateError, format_api_date, normalize_date_range, parse_trading_date
from backend.services import clickhouse_client


_DATABASE = "raw_historical_data"
_DIRECT_PERIODS = {"1m", "5m", "60m", "1d"}
_DERIVED_PERIODS = {"15m": 15, "30m": 30}
_TIMEZONE = ZoneInfo("Asia/Shanghai")


class MarketDataError(ValueError):
  """QMT 行情未导入、未覆盖或数据结构无效。"""


def ensure_cached(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  """兼容旧调用方：QMT 数据只检查覆盖，不在回测时联网抓取。"""
  start_date, end_date = _normalized_range(start_date, end_date)
  _require_complete(symbol, timeframe, start_date, end_date)
  return {"status": "qmt_available", "fetchedDates": [], "rowCount": 0}


def load_bars(symbol: str, timeframe: str, start_date: str, end_date: str) -> pd.DataFrame:
  """从 ClickHouse 读取 QMT 原始 K 线，必要时由 1 分钟线派生周期。"""
  start_date, end_date = _normalized_range(start_date, end_date)
  _require_complete(symbol, timeframe, start_date, end_date)
  source_period = "1m" if timeframe in _DERIVED_PERIODS else timeframe
  rows = _query_kline_rows(symbol, source_period, start_date, end_date)
  frame = _rows_to_frame(rows)
  if timeframe in _DERIVED_PERIODS:
    frame = _aggregate_minutes(frame, _DERIVED_PERIODS[timeframe])
  if frame.empty:
    raise MarketDataError(f"{symbol} {timeframe} 区间内无可用 QMT 行情")
  return frame[["time", "open", "high", "low", "close", "volume"]]


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
  _validate_request(timeframe, "20000101", "20991231")
  _validate_symbol(symbol)
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


def snapshot(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  """返回回测数据快照标识，记录读取的数据范围和最新导入时间。"""
  start_date, end_date = _normalized_range(start_date, end_date)
  _require_complete(symbol, timeframe, start_date, end_date)
  source_period = "1m" if timeframe in _DERIVED_PERIODS else timeframe
  query = f"""
    SELECT sum(valid_row_count) AS bar_count, min(first_bar_time) AS first_time,
           max(last_bar_time) AS last_time, max(updated_at) AS updated_at
    FROM {_DATABASE}.qmt_kline_coverage FINAL
    WHERE symbol = '{symbol}' AND period = '{source_period}'
      AND trade_date BETWEEN toDate('{_date_sql(start_date)}') AND toDate('{_date_sql(end_date)}')
      AND status = 'complete'
  """
  try:
    row = clickhouse_client.query_rows(query)[0]
  except (IndexError, clickhouse_client.ClickHouseError) as error:
    raise MarketDataError(f"读取 QMT 数据快照失败: {error}") from error
  identity = {
    "symbol": symbol, "timeframe": timeframe, "sourcePeriod": source_period,
    "startDate": start_date, "endDate": end_date, "barCount": int(row.get("bar_count") or 0),
    "lastImportedAt": str(row.get("updated_at") or ""),
  }
  identity["hash"] = hashlib.sha256(repr(sorted(identity.items())).encode("utf-8")).hexdigest()
  return identity


def _require_complete(symbol: str, timeframe: str, start_date: str, end_date: str) -> None:
  start_date, end_date = _normalized_range(start_date, end_date)
  _validate_request(timeframe, start_date, end_date)
  statuses = _coverage_status(symbol, timeframe, start_date, end_date)
  invalid = [item["date"] for item in statuses if item["status"] != "complete"]
  if invalid:
    raise MarketDataError(f"{symbol} {timeframe} QMT 行情不完整: {', '.join(invalid)}")
  if not statuses:
    raise MarketDataError(f"{symbol} {timeframe} 区间内未导入 QMT 行情")


def _cached_timeframe_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  try:
    statuses = _coverage_status(symbol, timeframe, start_date, end_date)
  except MarketDataError as error:
    return _unavailable_timeframe(timeframe, str(error))
  invalid = [item["date"] for item in statuses if item["status"] != "complete"]
  if invalid:
    return _unavailable_timeframe(timeframe, f"QMT 数据不完整: {', '.join(invalid)}")
  if not statuses:
    return _unavailable_timeframe(timeframe, "未导入该标的的 QMT 行情")
  reason = "由 QMT 1 分钟数据聚合" if timeframe in _DERIVED_PERIODS else "QMT 原始数据可用"
  return {"timeframe": timeframe, "available": True, "reason": reason, "dataStart": statuses[0]["date"], "dataEnd": statuses[-1]["date"]}


def _coverage_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> list[dict[str, str]]:
  start_date, end_date = _normalized_range(start_date, end_date)
  _validate_symbol(symbol)
  source_period = "1m" if timeframe in _DERIVED_PERIODS else timeframe
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


def _query_kline_rows(symbol: str, period: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
  _validate_symbol(symbol)
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
  query = f"""
    SELECT qmt_index, time, open, high, low, close, volume
    FROM {_DATABASE}.qmt_kline FINAL
    WHERE symbol = '{symbol}' AND period = '{period}' ORDER BY time DESC LIMIT {max(1, min(limit, 200))}
  """
  rows = clickhouse_client.query_rows(query)
  return [_row_preview(row) for row in rows]


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


def _validate_request(timeframe: str, start_date: str, end_date: str) -> None:
  supported = _DIRECT_PERIODS | set(_DERIVED_PERIODS)
  if timeframe not in supported:
    raise MarketDataError(f"QMT 数据暂不支持周期: {timeframe}")
  _normalized_range(start_date, end_date)


def _validate_symbol(symbol: str) -> None:
  if not settings.validate_symbol(symbol):
    raise MarketDataError(f"标的格式无效: {symbol}")


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
