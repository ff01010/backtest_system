"""A 股交易日历同步：新浪日历为主，东方财富指数日线为备用来源。"""

from __future__ import annotations

from datetime import date, timedelta
import json
import re
import time
from typing import Any, Callable, Iterator
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend.services import state_store


_SINA_CALENDAR_URL = "https://finance.sina.com.cn/realstock/company/klc_td_sh.txt"
_EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_SINA_SOURCE = "sina_sse_calendar"
_EASTMONEY_SOURCE = "eastmoney_sse_index"
_RECENT_BACKFILL_DAYS = 7
_MAX_FETCH_ATTEMPTS = 3
_RETRY_DELAYS_SECONDS = (1, 2)
_SINA_BASE_DATE = date(1990, 12, 19)
_SINA_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"


class TradeCalendarSyncError(RuntimeError):
  """交易日历来源不可用或返回格式异常。"""


class _SinaCalendarBitReader:
  """读取新浪交易日历文件使用的低位优先位流。"""

  def __init__(self, payload: str):
    try:
      self._values = [_SINA_ALPHABET.index(character) for character in payload]
    except ValueError as error:
      raise TradeCalendarSyncError("新浪交易日历包含非法编码字符") from error
    self._position = 0
    self._bit_offset = 0

  def read_bit(self) -> bool:
    if self._position >= len(self._values):
      raise TradeCalendarSyncError("新浪交易日历编码提前结束")
    value = bool(self._values[self._position] & (1 << self._bit_offset))
    self._bit_offset += 1
    if self._bit_offset == 6:
      self._bit_offset = 0
      self._position += 1
    return value

  def read_unsigned(self, width: int) -> int:
    value = 0
    for bit_index in range(width):
      if self.read_bit():
        value |= 1 << bit_index
    return value

  def read_signed_length(self) -> int:
    positive = self.read_bit()
    length = 1
    while self.read_bit():
      length += 1
    return length if positive else -length


def sync_trade_calendar(start_date: str, end_date: str) -> dict[str, Any]:
  """首次全量初始化，后续仅增量同步并回补最近自然日。"""
  requested_start = date.fromisoformat(start_date)
  end = date.fromisoformat(end_date)
  if requested_start > end:
    raise ValueError("开始日期不能晚于结束日期")
  start = _sync_start(requested_start, end)
  open_dates, source = _fetch_open_dates(start, end)
  open_count = 0
  chunks = 0
  for chunk_start, chunk_end in _year_ranges(start, end):
    chunk_dates = [value for value in open_dates if chunk_start.isoformat() <= value <= chunk_end.isoformat()]
    state_store.replace_trade_calendar("SSE", chunk_start.isoformat(), chunk_end.isoformat(), chunk_dates, source)
    open_count += len(chunk_dates)
    chunks += 1
  return {
    "exchange": "SSE",
    "mode": "full" if start == requested_start else "incremental",
    "startDate": start_date,
    "syncedStartDate": start.isoformat(),
    "endDate": end_date,
    "openDateCount": open_count,
    "chunks": chunks,
    "source": source,
  }


def _sync_start(requested_start: date, end: date) -> date:
  latest_text = state_store.latest_trade_calendar_date("SSE")
  if latest_text is None:
    return requested_start
  latest = date.fromisoformat(latest_text)
  recent_start = latest - timedelta(days=_RECENT_BACKFILL_DAYS - 1)
  return max(requested_start, min(recent_start, end))


def _year_ranges(start: date, end: date) -> Iterator[tuple[date, date]]:
  current = start
  while current <= end:
    year_end = date(current.year, 12, 31)
    chunk_end = min(year_end, end)
    yield current, chunk_end
    current = date(current.year + 1, 1, 1)


def _fetch_open_dates(start: date, end: date) -> tuple[list[str], str]:
  try:
    return _fetch_sina_open_dates(start, end), _SINA_SOURCE
  except TradeCalendarSyncError as sina_error:
    try:
      dates = []
      for chunk_start, chunk_end in _year_ranges(start, end):
        dates.extend(_fetch_eastmoney_open_dates(chunk_start, chunk_end))
      return sorted(set(dates)), _EASTMONEY_SOURCE
    except TradeCalendarSyncError as eastmoney_error:
      raise TradeCalendarSyncError(
        f"交易日历同步失败；新浪：{sina_error}；东方财富：{eastmoney_error}"
      ) from eastmoney_error


def _fetch_sina_open_dates(start: date, end: date) -> list[str]:
  return _with_retries("新浪交易日历", lambda: _load_sina_open_dates(start, end))


def _load_sina_open_dates(start: date, end: date) -> list[str]:
  request = Request(
    _SINA_CALENDAR_URL,
    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"},
  )
  try:
    with urlopen(request, timeout=15) as response:
      document = response.read().decode("utf-8")
  except (OSError, TimeoutError, URLError, UnicodeDecodeError) as error:
    raise TradeCalendarSyncError(f"请求失败：{error}") from error
  return _parse_sina_open_dates(document, start, end)


def _parse_sina_open_dates(document: str, start: date, end: date) -> list[str]:
  match = re.search(r'var\s+datelist\s*=\s*"([A-Za-z0-9+/]+)"\s*;', document)
  if match is None:
    raise TradeCalendarSyncError("响应中缺少 datelist 编码字段")
  dates = _decode_sina_trading_dates(match.group(1))
  if not dates:
    raise TradeCalendarSyncError("解码后未获得交易日")
  if max(dates) < end:
    raise TradeCalendarSyncError(f"日历仅覆盖至 {max(dates).isoformat()}，未覆盖 {end.isoformat()}")
  return [value.isoformat() for value in dates if start <= value <= end]


def _decode_sina_trading_dates(payload: str) -> list[date]:
  reader = _SinaCalendarBitReader(payload)
  decoder_type = reader.read_unsigned(12)
  variant = 63 ^ reader.read_unsigned(6)
  if decoder_type != 139 or variant > 1:
    raise TradeCalendarSyncError("新浪交易日历编码版本不受支持")
  offset = reader.read_unsigned(18) - 1
  end_offset = reader.read_unsigned(18)
  run_length = -1
  length_width = 0
  dates: list[date] = []
  while offset < end_offset:
    offset = _next_sina_weekday_offset(offset)
    if run_length <= 0:
      if reader.read_bit():
        length_width += reader.read_signed_length()
      if length_width < 0:
        raise TradeCalendarSyncError("新浪交易日历包含非法连续长度")
      run_length = reader.read_unsigned(3 * length_width) + 1
    dates.append(_SINA_BASE_DATE + timedelta(days=offset))
    run_length -= 1
  return dates


def _next_sina_weekday_offset(offset: int) -> int:
  offset += 1
  weekday_offset = offset % 7
  if weekday_offset in (3, 4):
    offset += 5 - weekday_offset
  return offset


def _fetch_eastmoney_open_dates(start: date, end: date) -> list[str]:
  return _with_retries("东方财富交易日历", lambda: _load_eastmoney_open_dates(start, end))


def _load_eastmoney_open_dates(start: date, end: date) -> list[str]:
  query = urlencode({
    "secid": "1.000001", "klt": "101", "fqt": "0", "beg": start.strftime("%Y%m%d"),
    "end": end.strftime("%Y%m%d"), "lmt": "1000", "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
  })
  request = Request(f"{_EASTMONEY_KLINE_URL}?{query}", headers={"User-Agent": "Mozilla/5.0"})
  try:
    with urlopen(request, timeout=15) as response:
      payload = json.loads(response.read().decode("utf-8"))
  except (OSError, TimeoutError, URLError, UnicodeDecodeError, json.JSONDecodeError) as error:
    raise TradeCalendarSyncError(f"请求失败：{error}") from error
  dates = _parse_eastmoney_open_dates(payload)
  if not dates:
    raise TradeCalendarSyncError(f"未返回 {start.isoformat()} 至 {end.isoformat()} 的交易日")
  return dates


def _with_retries(source: str, loader: Callable[[], list[str]]) -> list[str]:
  last_error: TradeCalendarSyncError | None = None
  for attempt in range(_MAX_FETCH_ATTEMPTS):
    try:
      return loader()
    except TradeCalendarSyncError as error:
      last_error = error
      if attempt < len(_RETRY_DELAYS_SECONDS):
        time.sleep(_RETRY_DELAYS_SECONDS[attempt])
  raise TradeCalendarSyncError(f"{source} 连续 {_MAX_FETCH_ATTEMPTS} 次请求失败：{last_error}")


def _parse_eastmoney_open_dates(payload: dict[str, Any]) -> list[str]:
  data = payload.get("data")
  if not isinstance(data, dict):
    return []
  rows = data.get("klines", [])
  if not isinstance(rows, list):
    return []
  return sorted({str(row).split(",", 1)[0] for row in rows if str(row)[:10].count("-") == 2})
