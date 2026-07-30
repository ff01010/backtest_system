# -*- coding: utf-8 -*-
"""东财分钟 K 线 HTTP 适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class EastmoneyError(RuntimeError):
  """东财分钟行情请求或响应无效。"""


@dataclass(frozen=True)
class MinuteBar:
  """规范化后的单根分钟 K 线。"""

  time: datetime
  open: float
  high: float
  low: float
  close: float
  volume: float
  amount: float | None


@dataclass(frozen=True)
class MarketInstrument:
  """东财证券目录中的沪深 A 股元数据。"""

  symbol: str
  exchange: str
  display_name: str
  board: str
  eastmoney_secid: str


def fetch_minute_bars(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  timeout_seconds: int = 15,
) -> list[MinuteBar]:
  """请求东财未复权分钟 K 线并转换为统一结构。"""
  if timeframe not in {"1m", "5m"}:
    raise EastmoneyError(f"东财分钟行情不支持周期: {timeframe}")
  secid = _secid(symbol)
  params = {
    "secid": secid,
    "klt": timeframe.removesuffix("m"),
    "fqt": "0",
    "beg": start_date,
    "end": end_date,
    "fields1": "f1,f2,f3,f4,f5,f6",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
  }
  url = f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urlencode(params)}"
  request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
  try:
    with urlopen(request, timeout=timeout_seconds) as response:
      payload = json.loads(response.read().decode("utf-8"))
  except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
    raise EastmoneyError(f"东财分钟行情请求失败: {error}") from error
  rows = payload.get("data", {}).get("klines")
  if rows is None:
    raise EastmoneyError("东财分钟行情响应缺少 kline 数据")
  return _parse_rows(rows, start_date, end_date)


def fetch_a_share_instruments(timeout_seconds: int = 20) -> list[MarketInstrument]:
  """拉取沪深 A 股证券目录；仅返回元数据，不请求行情。"""
  params = {
    "pn": "1", "pz": "10000", "po": "1", "np": "1", "fltt": "2", "invt": "2",
    "fid": "f3", "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
    "fields": "f12,f13,f14",
  }
  url = f"https://push2.eastmoney.com/api/qt/clist/get?{urlencode(params)}"
  payload = _fetch_json(url, timeout_seconds)
  rows = payload.get("data", {}).get("diff")
  if not isinstance(rows, list):
    raise EastmoneyError("东财证券目录响应缺少 diff 数据")
  items = [_parse_market_instrument(row) for row in rows]
  return sorted((item for item in items if item is not None), key=lambda item: item.symbol)


def _fetch_json(url: str, timeout_seconds: int) -> dict[str, Any]:
  request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
  try:
    with urlopen(request, timeout=timeout_seconds) as response:
      payload = json.loads(response.read().decode("utf-8"))
  except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
    raise EastmoneyError(f"东财请求失败: {error}") from error
  if not isinstance(payload, dict):
    raise EastmoneyError("东财响应格式无效")
  return payload


def _parse_market_instrument(row: object) -> MarketInstrument | None:
  if not isinstance(row, dict):
    return None
  code = str(row.get("f12") or "")
  exchange_code = row.get("f13")
  name = str(row.get("f14") or "").strip()
  exchange = "SH" if exchange_code == 1 else "BJ" if code.startswith(("4", "8")) else "SZ" if exchange_code == 0 else ""
  if not code.isdigit() or len(code) != 6 or not exchange or not name:
    return None
  board = "star" if code.startswith("688") else "chinext" if code.startswith("3") else "main"
  secid_prefix = "1" if exchange == "SH" else "0"
  return MarketInstrument(code, exchange, name, board, f"{secid_prefix}.{code}")


def _secid(symbol: str) -> str:
  code, exchange = symbol.split(".", 1)
  return f"{1 if exchange == 'SH' else 0}.{code}"


def _parse_rows(rows: list[str], start_date: str, end_date: str) -> list[MinuteBar]:
  bars: list[MinuteBar] = []
  seen: set[datetime] = set()
  for row in rows:
    values = row.split(",")
    if len(values) < 7:
      raise EastmoneyError("东财分钟行情存在格式不完整的记录")
    time = datetime.strptime(values[0], "%Y-%m-%d %H:%M")
    date = time.strftime("%Y%m%d")
    if not start_date <= date <= end_date or time in seen:
      continue
    try:
      bar = MinuteBar(
        time=time,
        open=float(values[1]),
        close=float(values[2]),
        high=float(values[3]),
        low=float(values[4]),
        volume=float(values[5]),
        amount=float(values[6]) if values[6] not in {"", "-"} else None,
      )
    except ValueError as error:
      raise EastmoneyError(f"东财分钟行情数值非法: {values[0]}") from error
    if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close):
      raise EastmoneyError(f"东财分钟行情 OHLC 非法: {values[0]}")
    seen.add(time)
    bars.append(bar)
  return sorted(bars, key=lambda item: item.time)
