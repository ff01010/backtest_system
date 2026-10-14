"""统一 Bar 数据源：正式 Bar 表、QMT K 线与临时 L2 聚合按固定优先级读取。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from backend import settings
from backend.date_codec import format_api_date
from backend.l2.config import has_profile, load_profile, load_temporary_quote_profile
from backend.l2.models import NormalizedBar
from backend.l2.providers import ClickHouseL2Adapter, L2DataError, RedisJsonCache, TemporaryQuoteBarProvider, VersionedBarCache
from backend.services import market_data
from backend.services.backtest_faults import BacktestFault


def load_bars_with_snapshot(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
  """一次任务只选择一个 Bar 来源，禁止跨来源拼接。"""
  _require_supported_timeframe(timeframe, l2=False)
  mapped = _load_mapped_bars(symbol, timeframe, start_date, end_date)
  if mapped is not None:
    bars, snapshot = mapped
    return _normalized_frame(bars), snapshot
  qmt = _load_qmt_bars(symbol, timeframe, start_date, end_date)
  if qmt is not None:
    return qmt
  bars, snapshot = _load_temporary_bars(symbol, timeframe, start_date, end_date)
  return _normalized_frame(bars), snapshot


def ensure_available(symbol: str, timeframe: str, start_date: str, end_date: str) -> dict[str, Any]:
  """验证所选的单一来源可用，并返回可写入任务的快照。"""
  try:
    _, snapshot = load_bars_with_snapshot(symbol, timeframe, start_date, end_date)
    return snapshot
  except L2DataError as error:
    if error.code != "data.l2_bar_missing":
      raise
    raise ValueError(f"Bar 数据不可用于回测: {error}") from error


def load_normalized_bars_with_snapshot(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> tuple[list[NormalizedBar], dict[str, Any]]:
  """供 L2 回放使用，与标准回测保持完全相同的来源优先级。"""
  _require_supported_timeframe(timeframe, l2=True)
  mapped = _load_mapped_bars(symbol, timeframe, start_date, end_date)
  if mapped is not None:
    return mapped
  qmt = _load_qmt_bars(symbol, timeframe, start_date, end_date)
  if qmt is not None:
    frame, snapshot = qmt
    return _qmt_normalized_bars(symbol, timeframe, frame), snapshot
  return _load_temporary_bars(symbol, timeframe, start_date, end_date)


def _require_supported_timeframe(timeframe: str, l2: bool) -> None:
  valid = (
    settings.validate_l2_backtest_timeframe(timeframe)
    if l2
    else settings.validate_backtest_timeframe(timeframe)
  )
  if not valid:
    raise BacktestFault(
      "backtest.timeframe_unsupported",
      f"当前回测引擎不支持 {timeframe}",
      {"timeframe": timeframe},
    )


def _load_mapped_bars(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> tuple[list[NormalizedBar], dict[str, Any]] | None:
  if not has_profile("bar"):
    return None
  provider = ClickHouseL2Adapter(load_profile("bar"))
  bounds = provider.bar_date_range(symbol, timeframe)
  if not _range_is_covered(start_date, end_date, bounds):
    return None
  try:
    bars = provider.load_bars(symbol, timeframe, start_date, end_date)
  except L2DataError as error:
    if error.code == "data.l2_bar_missing":
      return None
    raise
  snapshot = _snapshot(
    "mapped_l2_bar", symbol, timeframe, start_date, end_date, bars, bounds,
  )
  snapshot.update(provider.coverage(symbol, start_date, end_date).__dict__)
  snapshot.update(provider.bar_source_identity(timeframe))
  snapshot["provider"] = "mapped_l2_bar"
  _refresh_snapshot_hash(snapshot)
  return bars, snapshot


def _load_qmt_bars(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> tuple[pd.DataFrame, dict[str, Any]] | None:
  try:
    resolution = market_data.resolve_backtest_data(
      symbol, timeframe, start_date, end_date,
    )
  except market_data.MarketDataError as error:
    if error.reason in {"range_out_of_bounds", "no_complete_coverage"}:
      return None
    raise
  frame, _ = market_data.load_backtest_bars(symbol, resolution)
  snapshot = market_data.snapshot_for_resolution(symbol, resolution, bar_count=len(frame))
  return frame, {**snapshot, "provider": "qmt_kline"}


def _load_temporary_bars(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
) -> tuple[list[NormalizedBar], dict[str, Any]]:
  provider = _temporary_provider()
  bounds = provider.available_date_range(symbol)
  _require_source_range(symbol, timeframe, start_date, end_date, bounds)
  bars = provider.load_bars(symbol, timeframe, start_date, end_date)
  snapshot = _snapshot(
    "temporary_tick_quote_aggregation", symbol, timeframe, start_date, end_date, bars, bounds,
  )
  snapshot.update(provider.cache_snapshot())
  database, table = str(snapshot["inputTable"]).split(".", 1)
  snapshot.update({
    "sourceDatabase": database,
    "sourceTable": table,
    "sourcePeriod": timeframe,
    "derivation": f"{timeframe}-from-quote-{settings.L2_TEMP_BAR_AGGREGATION_VERSION}",
  })
  _refresh_snapshot_hash(snapshot)
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


def _require_source_range(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  bounds: tuple[str, str] | None,
) -> None:
  if not _range_is_covered(start_date, end_date, bounds):
    raise BacktestFault(
      "data.bar_load_failed",
      "回测日期超出 Bar 来源可用边界",
      {"symbol": symbol, "timeframe": timeframe},
    )


def _range_is_covered(
  start_date: str,
  end_date: str,
  bounds: tuple[str, str] | None,
) -> bool:
  if bounds is None:
    return False
  start = format_api_date(start_date)
  end = format_api_date(end_date)
  return bounds[0] <= start <= end <= bounds[1]


def derived_bar_available_dates(symbol: str, timeframe: str, trade_dates: list[str]) -> list[str]:
  """按日判断 Redis 聚合缓存或原始 L2 快照是否可生成 Bar。"""
  return _temporary_provider().available_bar_dates(symbol, trade_dates, timeframe)


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


def _snapshot(
  provider: str,
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  bars: list[Any],
  bounds: tuple[str, str] | None,
) -> dict[str, Any]:
  formal_dates = sorted({format_api_date(item.trade_date) for item in bars})
  identity = {
    "provider": provider,
    "symbol": symbol,
    "timeframe": timeframe,
    "startDate": start_date,
    "endDate": end_date,
    "formalDates": formal_dates,
    "warmupDates": [],
    "warmupTradingDays": 0,
    "barCount": len(bars),
    "sourceFirstDate": bounds[0] if bounds else None,
    "sourceLastDate": bounds[1] if bounds else None,
  }
  identity["hash"] = hashlib.sha256(repr(sorted(identity.items())).encode("utf-8")).hexdigest()
  return identity


def _refresh_snapshot_hash(snapshot: dict[str, Any]) -> None:
  identity = {key: value for key, value in snapshot.items() if key != "hash"}
  snapshot["hash"] = hashlib.sha256(
    repr(sorted(identity.items())).encode("utf-8"),
  ).hexdigest()


def _qmt_normalized_bars(symbol: str, period: str, frame: pd.DataFrame) -> list[NormalizedBar]:
  bars: list[NormalizedBar] = []
  for row in frame.itertuples(index=False):
    try:
      end_time = _parse_qmt_bar_time(row.time)
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
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
      raise BacktestFault(
        "data.l2_bar_invalid",
        "QMT Bar 行数据格式无效",
        {"symbol": symbol, "timeframe": period},
      ) from error
  return bars


def _parse_qmt_bar_time(value: object) -> datetime:
  """兼容 QMT 分钟线的 12 位时间与带秒的 14 位时间。"""
  text = str(value).strip()
  if len(text) == 12:
    text = f"{text}00"
  if len(text) != 14 or not text.isdigit():
    raise ValueError(f"QMT Bar 时间格式无效: {value}")
  return datetime.strptime(text, "%Y%m%d%H%M%S")


def _period_duration(period: str) -> timedelta:
  if period.endswith("m") and period[:-1].isdigit():
    return timedelta(minutes=int(period[:-1]))
  if period.endswith("d") and period[:-1].isdigit():
    return timedelta(days=int(period[:-1]))
  raise ValueError(f"QMT Bar 周期格式无效: {period}")


def _is_a_share_session(value: datetime) -> bool:
  minute = value.hour * 60 + value.minute
  return 9 * 60 + 30 <= minute <= 11 * 60 + 30 or 13 * 60 <= minute <= 15 * 60
