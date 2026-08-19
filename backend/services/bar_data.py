"""统一 Bar 数据源：正式 Bar 表、QMT K 线与临时 L2 聚合按固定优先级读取。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from backend import settings
from backend.l2.config import has_profile, load_profile, load_temporary_quote_profile
from backend.l2.models import NormalizedBar
from backend.l2.providers import ClickHouseL2Adapter, L2DataError, RedisJsonCache, TemporaryQuoteBarProvider, VersionedBarCache
from backend.services import market_data


def load_bars_with_snapshot(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
  """一次任务只选择一个 Bar 来源，禁止跨来源拼接。"""
  if has_profile("bar"):
    try:
      bars = ClickHouseL2Adapter(load_profile("bar")).load_bars(symbol, timeframe, start_date, end_date)
      return _normalized_frame(bars), _snapshot("mapped_l2_bar", symbol, timeframe, start_date, end_date, len(bars))
    except L2DataError:
      pass
  try:
    frame = market_data.load_bars(symbol, timeframe, start_date, end_date)
    return frame, {**market_data.snapshot(symbol, timeframe, start_date, end_date), "provider": "qmt_kline"}
  except market_data.MarketDataError:
    provider = _temporary_provider()
    bars = provider.load_bars(symbol, timeframe, start_date, end_date)
    snapshot = _snapshot("temporary_tick_quote_aggregation", symbol, timeframe, start_date, end_date, len(bars))
    snapshot.update(provider.cache_snapshot())
    return _normalized_frame(bars), snapshot


def ensure_available(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  """验证所选的单一来源可用，并返回可写入任务的快照。"""
  try:
    _, snapshot = load_bars_with_snapshot(symbol, timeframe, start_date, end_date)
    return snapshot
  except L2DataError as error:
    raise ValueError(f"Bar 数据不可用于回测: {error}") from error


def load_normalized_bars_with_snapshot(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> tuple[list[NormalizedBar], dict[str, Any]]:
  """供 L2 回放使用，与标准回测保持完全相同的来源优先级。"""
  if has_profile("bar"):
    try:
      provider = ClickHouseL2Adapter(load_profile("bar"))
      bars = provider.load_bars(symbol, timeframe, start_date, end_date)
      return bars, {"provider": "mapped_l2_bar", **provider.coverage(symbol, start_date, end_date).__dict__}
    except L2DataError:
      pass
  try:
    frame = market_data.load_bars(symbol, timeframe, start_date, end_date)
    snapshot = {**market_data.snapshot(symbol, timeframe, start_date, end_date), "provider": "qmt_kline"}
    return _qmt_normalized_bars(symbol, timeframe, frame), snapshot
  except market_data.MarketDataError:
    provider = _temporary_provider()
    bars = provider.load_bars(symbol, timeframe, start_date, end_date)
    snapshot = _snapshot("temporary_tick_quote_aggregation", symbol, timeframe, start_date, end_date, len(bars))
    snapshot.update(provider.cache_snapshot())
    return bars, snapshot


def _temporary_provider() -> TemporaryQuoteBarProvider:
  cache = VersionedBarCache(
    RedisJsonCache(settings.L2_REDIS_HOST, settings.L2_REDIS_PORT),
    settings.L2_BAR_CACHE_TTL_SECONDS,
  )
  return TemporaryQuoteBarProvider(
    load_temporary_quote_profile(),
    cache,
    settings.L2_TEMP_BAR_AGGREGATION_VERSION,
    settings.L2_TEMP_MAPPING_VERSION,
  )


def _normalized_frame(bars: list[Any]) -> pd.DataFrame:
  rows = [{
    "time": _bar_time(item.end_time),
    "open": float(item.open),
    "high": float(item.high),
    "low": float(item.low),
    "close": float(item.close),
    "volume": float(item.volume),
  } for item in bars]
  return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


def _bar_time(value: str) -> str:
  return datetime.fromisoformat(str(value)).strftime("%Y%m%d%H%M%S")


def _snapshot(provider: str, symbol: str, timeframe: str, start_date: str, end_date: str, count: int) -> dict[str, Any]:
  identity = {
    "provider": provider,
    "symbol": symbol,
    "timeframe": timeframe,
    "startDate": start_date,
    "endDate": end_date,
    "barCount": count,
  }
  identity["hash"] = hashlib.sha256(repr(sorted(identity.items())).encode("utf-8")).hexdigest()
  return identity


def _qmt_normalized_bars(symbol: str, period: str, frame: pd.DataFrame) -> list[NormalizedBar]:
  bars: list[NormalizedBar] = []
  for row in frame.itertuples(index=False):
    end_time = datetime.strptime(str(row.time), "%Y%m%d%H%M%S")
    if not _is_a_share_session(end_time):
      continue
    start_time = end_time - _period_duration(period)
    bars.append(NormalizedBar(
      symbol=symbol,
      trade_date=end_time.date().isoformat(),
      period=period,
      start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
      end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
      open=float(row.open),
      high=float(row.high),
      low=float(row.low),
      close=float(row.close),
      volume=float(row.volume),
      amount=0.0,
      quality="complete",
      source_version="qmt_kline",
    ))
  return bars


def _period_duration(period: str) -> timedelta:
  if period.endswith("m") and period[:-1].isdigit():
    return timedelta(minutes=int(period[:-1]))
  if period.endswith("d") and period[:-1].isdigit():
    return timedelta(days=int(period[:-1]))
  raise ValueError(f"QMT Bar 周期格式无效: {period}")


def _is_a_share_session(value: datetime) -> bool:
  minute = value.hour * 60 + value.minute
  return 9 * 60 + 30 <= minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60
