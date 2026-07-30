# -*- coding: utf-8 -*-
"""分钟行情缓存、覆盖检查和 PostgreSQL 读取服务。"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
import hashlib
from typing import Any

import pandas as pd
from psycopg.types.json import Jsonb

from backend import settings
from backend.services import eastmoney_client, state_store

_EXPECTED_BARS = {"1m": 240, "5m": 48}
_ADJUSTMENT = "none"
_SOURCE = "eastmoney"


class MarketDataError(ValueError):
  """行情缓存未就绪或上游数据不可用。"""


def ensure_cached(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  """确保所需日期已缓存，只有缺失日期会访问东财。"""
  _validate_request(timeframe, start_date, end_date)
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{symbol}:{timeframe}",))
      instrument_id = state_store._instrument_id(cur, symbol)
      dates = _date_range(start_date, end_date)
      covered = _coverage_status(cur, instrument_id, timeframe, dates)
      missing = [
        item for item in dates
        if covered.get(item) not in {"complete", "empty"}
      ]
      if not missing:
        _log_fetch(cur, instrument_id, timeframe, start_date, end_date, "cache_hit", 0, None)
        return {"status": "cache_hit", "fetchedDates": [], "rowCount": 0}
      try:
        bars = eastmoney_client.fetch_minute_bars(symbol, timeframe, start_date, end_date)
      except eastmoney_client.EastmoneyError as error:
        _mark_failed(cur, instrument_id, timeframe, missing, str(error))
        _log_fetch(cur, instrument_id, timeframe, start_date, end_date, "failed", 0, str(error))
        raise MarketDataError(str(error)) from error
      inserted = _upsert_bars(cur, instrument_id, timeframe, bars)
      _update_coverage(cur, instrument_id, timeframe, missing, bars)
      _log_fetch(cur, instrument_id, timeframe, start_date, end_date, "fetched", inserted, None)
  _require_complete(symbol, timeframe, start_date, end_date)
  return {"status": "fetched", "fetchedDates": [item.strftime("%Y%m%d") for item in missing], "rowCount": inserted}


def load_bars(symbol: str, timeframe: str, start_date: str, end_date: str) -> pd.DataFrame:
  """从 PostgreSQL 读取已验证的分钟 K 线。"""
  _require_complete(symbol, timeframe, start_date, end_date)
  with state_store._db() as conn:
    with conn.cursor() as cur:
      instrument_id = state_store._instrument_id(cur, symbol)
      cur.execute("""
        SELECT bar_time, open, high, low, close, volume
        FROM market_minute_bar
        WHERE instrument_id=%s AND period=%s AND adjustment=%s
          AND trade_date BETWEEN %s AND %s
        ORDER BY bar_time
      """, (instrument_id, timeframe, _ADJUSTMENT, _date(start_date), _date(end_date)))
      rows = cur.fetchall()
  if not rows:
    raise MarketDataError(f"{symbol} {timeframe} 区间内无可用分钟行情")
  return pd.DataFrame([{
    "time": row["bar_time"].strftime("%Y%m%d%H%M"),
    "open": float(row["open"]), "high": float(row["high"]),
    "low": float(row["low"]), "close": float(row["close"]),
    "volume": float(row["volume"] or 0),
  } for row in rows])


def timeframe_status(symbol: str, start_date: str | None, end_date: str | None) -> list[dict[str, Any]]:
  """返回回测周期的缓存状态，不触发上游行情请求。"""
  items: list[dict[str, Any]] = []
  for timeframe in settings.BACKTEST_TIMEFRAMES:
    if not start_date or not end_date:
      items.append({"timeframe": timeframe, "available": False, "reason": "请选择回测日期", "dataStart": None, "dataEnd": None})
      continue
    items.append(_cached_timeframe_status(symbol, timeframe, start_date, end_date))
  return items


def list_dates(symbol: str, timeframe: str) -> list[dict[str, Any]]:
  with state_store._db() as conn:
    with conn.cursor() as cur:
      instrument_id = state_store._instrument_id(cur, symbol)
      cur.execute("""
        SELECT DISTINCT trade_date FROM market_minute_bar
        WHERE instrument_id=%s AND period=%s AND adjustment=%s ORDER BY trade_date DESC
      """, (instrument_id, timeframe, _ADJUSTMENT))
      return [{"date": row["trade_date"].strftime("%Y%m%d"), "hasKline": True, "hasSignal": False} for row in cur.fetchall()]


def status(symbol: str, timeframe: str, preview_limit: int = 20) -> dict[str, Any]:
  """返回数据管理页所需的数据库缓存状态。"""
  if timeframe not in _EXPECTED_BARS:
    raise MarketDataError(f"不支持分钟周期: {timeframe}")
  with state_store._db() as conn:
    with conn.cursor() as cur:
      instrument_id = state_store._instrument_id(cur, symbol)
      cur.execute("""
        SELECT min(trade_date) AS first_date, max(trade_date) AS last_date, count(*) AS bar_count,
               max(cached_at) AS cached_at
        FROM market_minute_bar
        WHERE instrument_id=%s AND period=%s AND adjustment=%s
      """, (instrument_id, timeframe, _ADJUSTMENT))
      summary = cur.fetchone()
      cur.execute("""
        SELECT bar_time, open, high, low, close, volume FROM market_minute_bar
        WHERE instrument_id=%s AND period=%s AND adjustment=%s
        ORDER BY bar_time DESC LIMIT %s
      """, (instrument_id, timeframe, _ADJUSTMENT, preview_limit))
      preview = [{
        "time": row["bar_time"].strftime("%Y%m%d%H%M"), "open": float(row["open"]),
        "high": float(row["high"]), "low": float(row["low"]), "close": float(row["close"]),
        "volume": float(row["volume"] or 0),
      } for row in cur.fetchall()]
      cur.execute("""
        SELECT trade_date, bar_count, message FROM market_minute_coverage
        WHERE instrument_id=%s AND period=%s AND adjustment=%s AND status IN ('incomplete', 'failed')
        ORDER BY trade_date DESC
      """, (instrument_id, timeframe, _ADJUSTMENT))
      gaps = [{"date": row["trade_date"].strftime("%Y%m%d"), "barCount": int(row["bar_count"]), "expected": _EXPECTED_BARS[timeframe], "missing": max(_EXPECTED_BARS[timeframe] - int(row["bar_count"]), 0), "message": row["message"]} for row in cur.fetchall()]
  return {
    "symbol": symbol, "timeframe": timeframe, "path": "postgresql://market_minute_bar",
    "exists": bool(summary["bar_count"]), "firstDate": summary["first_date"].strftime("%Y%m%d") if summary["first_date"] else None,
    "lastDate": summary["last_date"].strftime("%Y%m%d") if summary["last_date"] else None,
    "barCount": int(summary["bar_count"]), "fileSize": 0,
    "mtime": str(summary["cached_at"] or "") or None, "duplicateCount": 0,
    "nonTradingCount": 0, "expectedBarsPerDay": _EXPECTED_BARS[timeframe],
    "gapDates": gaps, "preview": preview,
  }


def snapshot(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  with state_store._db() as conn:
    with conn.cursor() as cur:
      instrument_id = state_store._instrument_id(cur, symbol)
      cur.execute("""
        SELECT count(*) AS count, min(bar_time) AS first_time, max(bar_time) AS last_time,
               max(cached_at) AS cached_at
        FROM market_minute_bar
        WHERE instrument_id=%s AND period=%s AND adjustment=%s
          AND trade_date BETWEEN %s AND %s
      """, (instrument_id, timeframe, _ADJUSTMENT, _date(start_date), _date(end_date)))
      row = cur.fetchone()
  identity = {"symbol": symbol, "timeframe": timeframe, "startDate": start_date, "endDate": end_date, "barCount": int(row["count"]), "lastCachedAt": str(row["cached_at"] or "")}
  identity["hash"] = hashlib.sha256(repr(sorted(identity.items())).encode("utf-8")).hexdigest()
  return identity


def _require_complete(symbol: str, timeframe: str, start_date: str, end_date: str) -> None:
  _validate_request(timeframe, start_date, end_date)
  with state_store._db() as conn:
    with conn.cursor() as cur:
      instrument_id = state_store._instrument_id(cur, symbol)
      dates = _date_range(start_date, end_date)
      statuses = _coverage_status(cur, instrument_id, timeframe, dates)
  invalid = [item.strftime("%Y%m%d") for item in dates if statuses.get(item) in {"incomplete", "failed"}]
  if invalid:
    raise MarketDataError(f"{symbol} {timeframe} 行情不完整: {', '.join(invalid)}")


def _cached_timeframe_status(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  """检查缓存状态，缺失数据保持可运行以便回测时按需拉取。"""
  with state_store._db() as conn:
    with conn.cursor() as cur:
      instrument_id = state_store._instrument_id(cur, symbol)
      dates = _date_range(start_date, end_date)
      statuses = _coverage_status(cur, instrument_id, timeframe, dates)
      cur.execute("""
        SELECT min(trade_date) AS first_date, max(trade_date) AS last_date
        FROM market_minute_bar
        WHERE instrument_id=%s AND period=%s AND adjustment=%s
          AND trade_date BETWEEN %s AND %s
      """, (instrument_id, timeframe, _ADJUSTMENT, dates[0], dates[-1]))
      row = cur.fetchone()
  invalid = [item.strftime("%Y%m%d") for item, status in statuses.items() if status in {"incomplete", "failed"}]
  if invalid:
    return {"timeframe": timeframe, "available": True, "reason": f"将重试补齐: {', '.join(invalid)}", "dataStart": None, "dataEnd": None}
  if len(statuses) == len(dates):
    return {
      "timeframe": timeframe, "available": True, "reason": "数据库缓存可用",
      "dataStart": row["first_date"].strftime("%Y%m%d") if row["first_date"] else None,
      "dataEnd": row["last_date"].strftime("%Y%m%d") if row["last_date"] else None,
    }
  return {"timeframe": timeframe, "available": True, "reason": "首次回测时将自动拉取东财分钟行情", "dataStart": None, "dataEnd": None}


def _coverage_status(cur: Any, instrument_id: int, timeframe: str, dates: list[date]) -> dict[date, str]:
  cur.execute("""
    SELECT trade_date, status FROM market_minute_coverage
    WHERE instrument_id=%s AND period=%s AND adjustment=%s AND source=%s
      AND trade_date BETWEEN %s AND %s
  """, (instrument_id, timeframe, _ADJUSTMENT, _SOURCE, dates[0], dates[-1]))
  return {row["trade_date"]: row["status"] for row in cur.fetchall()}


def _upsert_bars(cur: Any, instrument_id: int, timeframe: str, bars: list[eastmoney_client.MinuteBar]) -> int:
  if not bars:
    return 0
  rows = [(instrument_id, timeframe, _ADJUSTMENT, item.time, item.time.date(), item.open, item.high, item.low, item.close, item.volume, item.amount, _SOURCE) for item in bars]
  cur.executemany("""
    INSERT INTO market_minute_bar (
      instrument_id, period, adjustment, bar_time, trade_date, open, high, low, close, volume, amount, source
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (instrument_id, period, adjustment, bar_time) DO UPDATE SET
      open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
      volume=EXCLUDED.volume, amount=EXCLUDED.amount, source=EXCLUDED.source, cached_at=now()
  """, rows)
  return len(rows)


def _update_coverage(cur: Any, instrument_id: int, timeframe: str, dates: list[date], bars: list[eastmoney_client.MinuteBar]) -> None:
  grouped: dict[date, list[eastmoney_client.MinuteBar]] = defaultdict(list)
  for bar in bars:
    grouped[bar.time.date()].append(bar)
  expected = _EXPECTED_BARS[timeframe]
  rows = []
  for item in dates:
    count = len(grouped[item])
    status = "complete" if count == expected else "empty" if count == 0 else "incomplete"
    rows.append((instrument_id, timeframe, _ADJUSTMENT, item, _SOURCE, status, count, None if status == "complete" else f"预期 {expected} 根，实际 {count} 根"))
  cur.executemany("""
    INSERT INTO market_minute_coverage (
      instrument_id, period, adjustment, trade_date, source, status, bar_count, message
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (instrument_id, period, adjustment, trade_date, source) DO UPDATE SET
      status=EXCLUDED.status, bar_count=EXCLUDED.bar_count, message=EXCLUDED.message,
      last_verified_at=now(), updated_at=now()
  """, rows)


def _mark_failed(cur: Any, instrument_id: int, timeframe: str, dates: list[date], message: str) -> None:
  cur.executemany("""
    INSERT INTO market_minute_coverage (instrument_id, period, adjustment, trade_date, source, status, message)
    VALUES (%s,%s,%s,%s,%s,'failed',%s)
    ON CONFLICT (instrument_id, period, adjustment, trade_date, source) DO UPDATE SET
      status='failed', message=EXCLUDED.message, last_verified_at=now(), updated_at=now()
  """, [(instrument_id, timeframe, _ADJUSTMENT, item, _SOURCE, message) for item in dates])


def _log_fetch(cur: Any, instrument_id: int, timeframe: str, start_date: str, end_date: str, outcome: str, row_count: int, error: str | None) -> None:
  cur.execute("""
    INSERT INTO market_fetch_log (
      instrument_id, requested_by_user_id, period, adjustment, source, requested_from,
      requested_to, outcome, row_count, error_message, finished_at, details
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s)
  """, (instrument_id, settings.DEFAULT_USER_ID, timeframe, _ADJUSTMENT, _SOURCE, _date(start_date), _date(end_date), outcome, row_count, error, Jsonb({"provider": _SOURCE})))


def _validate_request(timeframe: str, start_date: str, end_date: str) -> None:
  if timeframe not in _EXPECTED_BARS:
    raise MarketDataError(f"当前回测仅支持分钟周期: {', '.join(_EXPECTED_BARS)}")
  if not settings.validate_date(start_date) or not settings.validate_date(end_date) or start_date > end_date:
    raise MarketDataError("分钟行情日期范围无效")


def _date(value: str) -> date:
  return datetime.strptime(value, "%Y%m%d").date()


def _date_range(start_date: str, end_date: str) -> list[date]:
  start, end = _date(start_date), _date(end_date)
  return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
