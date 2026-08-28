"""A 股交易日历同步：以东方财富上证指数日线中的实际交易日期为准。"""

from __future__ import annotations

from datetime import date
import json
from typing import Any, Iterator
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.services import state_store


_EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_SOURCE = "eastmoney_sse_index"


class TradeCalendarSyncError(RuntimeError):
  """交易日历来源不可用或返回格式异常。"""


def sync_trade_calendar(start_date: str, end_date: str) -> dict[str, Any]:
  """分年度拉取上证指数实际交易日期，并以自然日覆盖写入数据库。"""
  start = date.fromisoformat(start_date)
  end = date.fromisoformat(end_date)
  if start > end:
    raise ValueError("开始日期不能晚于结束日期")
  open_count = 0
  chunks = 0
  for chunk_start, chunk_end in _year_ranges(start, end):
    open_dates = _fetch_open_dates(chunk_start, chunk_end)
    state_store.replace_trade_calendar("SSE", chunk_start.isoformat(), chunk_end.isoformat(), open_dates, _SOURCE)
    open_count += len(open_dates)
    chunks += 1
  return {
    "exchange": "SSE",
    "startDate": start_date,
    "endDate": end_date,
    "openDateCount": open_count,
    "chunks": chunks,
    "source": _SOURCE,
  }


def _year_ranges(start: date, end: date) -> Iterator[tuple[date, date]]:
  current = start
  while current <= end:
    year_end = date(current.year, 12, 31)
    chunk_end = min(year_end, end)
    yield current, chunk_end
    current = date(current.year + 1, 1, 1)


def _fetch_open_dates(start: date, end: date) -> list[str]:
  query = urlencode({
    "secid": "1.000001", "klt": "101", "fqt": "0", "beg": start.strftime("%Y%m%d"),
    "end": end.strftime("%Y%m%d"), "lmt": "1000", "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
  })
  request = Request(f"{_EASTMONEY_KLINE_URL}?{query}", headers={"User-Agent": "Mozilla/5.0"})
  try:
    with urlopen(request, timeout=15) as response:
      payload = json.loads(response.read().decode("utf-8"))
  except (OSError, TimeoutError, URLError, json.JSONDecodeError) as error:
    raise TradeCalendarSyncError(f"交易日历来源不可用: {error}") from error
  dates = _parse_open_dates(payload)
  if not dates:
    raise TradeCalendarSyncError(f"交易日历来源未返回 {start.isoformat()} 至 {end.isoformat()} 的交易日")
  return dates


def _parse_open_dates(payload: dict[str, Any]) -> list[str]:
  data = payload.get("data")
  if not isinstance(data, dict):
    return []
  rows = data.get("klines", [])
  if not isinstance(rows, list):
    return []
  return sorted({str(row).split(',', 1)[0] for row in rows if str(row)[:10].count('-') == 2})
