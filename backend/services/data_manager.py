# -*- coding: utf-8 -*-
"""行情数据状态扫描和本地 CSV 预览。"""

import os
import time
from typing import Any

import pandas as pd

from backend import settings
from backend.services import market_data

EXPECTED_BARS_PER_DAY = {
  "1m": 240,
  "2m": 120,
  "3m": 80,
  "4m": 60,
  "5m": 48,
  "10m": 24,
  "15m": 16,
  "30m": 8,
  "45m": 4,
  "60m": 4,
  "1d": 1,
}


def get_kline_path(symbol: str, timeframe: str) -> str:
  code_market = symbol.replace(".", "_")
  resolved = settings.resolve_kline_path(symbol, timeframe)
  if resolved:
    return resolved
  return os.path.join(settings.KLINE_ROOT, timeframe, code_market, f"{code_market}_{timeframe}.csv")


def get_status(symbol: str, timeframe: str = "5m", preview_limit: int = 20) -> dict[str, Any]:
  """返回 ClickHouse 中 QMT 历史数据状态。"""
  return market_data.status(symbol, timeframe, preview_limit)


def get_csv_status(symbol: str, timeframe: str = "5m", preview_limit: int = 20) -> dict[str, Any]:
  """保留旧 CSV 状态扫描，供迁移期排查使用。"""
  path = get_kline_path(symbol, timeframe)
  empty = {
    "symbol": symbol,
    "timeframe": timeframe,
    "path": path,
    "exists": False,
    "firstDate": None,
    "lastDate": None,
    "barCount": 0,
    "fileSize": 0,
    "mtime": None,
    "duplicateCount": 0,
    "nonTradingCount": 0,
    "expectedBarsPerDay": EXPECTED_BARS_PER_DAY.get(timeframe),
    "gapDates": [],
    "preview": [],
  }
  if not os.path.exists(path):
    return empty

  stat = os.stat(path)
  try:
    df = pd.read_csv(path)
  except Exception as e:
    return {**empty, "exists": True, "mtime": _format_mtime(stat.st_mtime), "error": str(e)}

  df = _normalize_time_column(df)
  if df.empty:
    return {
      **empty,
      "exists": True,
      "fileSize": stat.st_size,
      "mtime": _format_mtime(stat.st_mtime),
    }

  times = df["time"].astype(str)
  duplicate_count = int(times.duplicated().sum())
  date_counts = times.str[:8].value_counts().to_dict()
  gap_dates = _find_gap_dates(date_counts, timeframe)
  preview_cols = [c for c in ["time", "open", "high", "low", "close", "volume"] if c in df.columns]

  return {
    "symbol": symbol,
    "timeframe": timeframe,
    "path": path,
    "exists": True,
    "firstDate": str(times.min())[:8],
    "lastDate": str(times.max())[:8],
    "barCount": int(len(df)),
    "fileSize": int(stat.st_size),
    "mtime": _format_mtime(stat.st_mtime),
    "duplicateCount": duplicate_count,
    "nonTradingCount": _count_non_trading(times, timeframe),
    "expectedBarsPerDay": EXPECTED_BARS_PER_DAY.get(timeframe),
    "gapDates": gap_dates,
    "preview": df[preview_cols].tail(preview_limit).to_dict(orient="records"),
  }


def _normalize_time_column(df: pd.DataFrame) -> pd.DataFrame:
  if "time" not in df.columns and "datetime" in df.columns:
    df = df.rename(columns={"datetime": "time"})
  if "time" not in df.columns:
    raise KeyError("time")
  df["time"] = df["time"].astype(str)
  return df


def _find_gap_dates(date_counts: dict[str, int], timeframe: str) -> list[dict[str, Any]]:
  expected = EXPECTED_BARS_PER_DAY.get(timeframe)
  if not expected:
    return []
  result = []
  for date, count in sorted(date_counts.items()):
    if count < expected:
      result.append({
        "date": date,
        "barCount": int(count),
        "expected": expected,
        "missing": expected - int(count),
      })
  return result


def _count_non_trading(times: pd.Series, timeframe: str) -> int:
  if timeframe == "1d":
    return 0
  count = 0
  for value in times:
    if len(value) < 12:
      continue
    hhmm = int(value[8:12])
    if timeframe == "1m":
      valid = 930 <= hhmm <= 1130 or 1300 <= hhmm <= 1500
    else:
      valid = 930 < hhmm <= 1130 or 1300 < hhmm <= 1500
    if not valid:
      count += 1
  return count


def _format_mtime(value: float) -> str:
  return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))
