# -*- coding: utf-8 -*-
"""QMT ClickHouse 行情读取和派生周期测试。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backend.services import bar_data, market_data, state_store


_TIMEZONE = ZoneInfo("Asia/Shanghai")


def test_load_bars_reads_qmt_raw_rows(monkeypatch: pytest.MonkeyPatch) -> None:
  epoch = int(datetime(2026, 7, 30, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    if "qmt_kline_coverage" in query:
      return [{"trade_date": "2026-07-30", "status": "complete"}]
    return [{
      "qmt_index": "20260730093000", "time": epoch, "open": 10.0, "high": 11.0,
      "low": 9.0, "close": 10.5, "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame = market_data.load_bars("688981.SH", "1m", "20260730", "20260730")

  assert frame.to_dict("records") == [{
    "time": "202607300930", "open": 10.0, "high": 11.0,
    "low": 9.0, "close": 10.5, "volume": 100,
  }]


def test_incomplete_qmt_coverage_rejects_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: [
    {"trade_date": "2026-07-30", "status": "partial"},
  ])

  with pytest.raises(market_data.MarketDataError, match="QMT 行情不完整"):
    market_data.ensure_cached("688981.SH", "1m", "20260730", "20260730")


def test_weekend_start_date_uses_available_qmt_bars(monkeypatch: pytest.MonkeyPatch) -> None:
  epoch = int(datetime(2025, 1, 6, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    if "qmt_kline_coverage" in query:
      return [
        {"trade_date": "2025-01-06", "status": "complete"},
        {"trade_date": "2025-01-07", "status": "complete"},
      ]
    return [{
      "qmt_index": "20250106093000", "time": epoch, "open": 10.0, "high": 10.2,
      "low": 9.9, "close": 10.1, "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame = market_data.load_bars("688126.SH", "1m", "20250104", "20250107")

  assert frame["time"].tolist() == ["202501060930"]


def test_qmt_normalized_bar_uses_completed_bar_time() -> None:
  frame = pd.DataFrame([
    {"time": "20260730090300", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 100},
    {"time": "20260730093000", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 100},
  ])

  bars = bar_data._qmt_normalized_bars("688981.SH", "1m", frame)

  assert len(bars) == 1
  assert bars[0].start_time == "2026-07-30 09:29:00"
  assert bars[0].end_time == "2026-07-30 09:30:00"


def test_qmt_normalized_bar_accepts_minute_time_without_seconds() -> None:
  frame = pd.DataFrame([
    {"time": "202607300930", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 100},
    {"time": "202607300959", "open": 10.1, "high": 10.3, "low": 10.0, "close": 10.2, "volume": 100},
    {"time": "202607301000", "open": 10.2, "high": 10.4, "low": 10.1, "close": 10.3, "volume": 100},
  ])

  bars = bar_data._qmt_normalized_bars("688981.SH", "1m", frame)

  assert [bar.end_time for bar in bars] == [
    "2026-07-30 09:30:00",
    "2026-07-30 09:59:00",
    "2026-07-30 10:00:00",
  ]


def test_15m_aggregation_uses_qmt_right_closed_opening_window() -> None:
  start = datetime(2026, 7, 30, 9, 30, tzinfo=_TIMEZONE)
  rows = []
  for offset in range(16):
    value = float(offset + 1)
    timestamp = start + timedelta(minutes=offset)
    rows.append({
      "qmt_index": timestamp.strftime("%Y%m%d%H%M00"),
      "time": int(timestamp.timestamp() * 1000), "open": value, "high": value + 0.1,
      "low": value - 0.1, "close": value, "volume": 10,
    })

  frame = market_data._rows_to_frame(rows)
  result = market_data._aggregate_minutes(frame, 15)

  assert result.to_dict("records") == [{
    "_bucket": pd.Timestamp("2026-07-30 09:45:00", tz="Asia/Shanghai"),
    "open": 1.0, "high": 16.1, "low": 0.9, "close": 16.0, "volume": 160,
    "time": "202607300945",
  }]


def test_daily_resolution_falls_back_to_complete_minutes_for_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
  def query_rows(query: str) -> list[dict[str, object]]:
    if "period = '1d'" in query:
      return [{"trade_date": "2026-07-30", "status": "partial"}]
    if "period = '1m'" in query:
      return [
        {"trade_date": "2026-07-29", "status": "complete"},
        {"trade_date": "2026-07-30", "status": "complete"},
      ]
    return []

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  monkeypatch.setattr(state_store, "is_trade_calendar_range_covered", lambda *_: True)
  monkeypatch.setattr(state_store, "list_open_trade_dates", lambda *_: ["2026-07-29"])

  resolution = market_data.resolve_backtest_data(
    "688981.SH", "1d", "2026-07-30", "2026-07-30", ["2026-07-30"], 1,
  )

  assert resolution.source_period == "1m"
  assert resolution.derivation == "daily-from-1m-v1"
  assert resolution.warmup_dates == ("2026-07-29",)


def test_warmup_only_uses_contiguous_complete_trade_dates(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(state_store, "is_trade_calendar_range_covered", lambda *_: True)
  monkeypatch.setattr(
    state_store,
    "list_open_trade_dates",
    lambda *_: ["2025-09-01", "2025-09-02", "2025-09-03", "2025-09-04", "2025-09-05",
                "2025-09-08", "2025-09-09", "2025-09-10", "2025-09-11"],
  )
  monkeypatch.setattr(
    market_data,
    "_coverage_for_period",
    lambda *_: [
      {"date": "2025-09-09", "status": "complete"},
      {"date": "2025-09-10", "status": "complete"},
      {"date": "2025-09-11", "status": "complete"},
    ],
  )

  result = market_data._complete_dates_before("688981.SH", "1m", "2025-09-12", 5)

  assert result == ["2025-09-09", "2025-09-10", "2025-09-11"]


def test_daily_aggregation_uses_last_minute_end_time_without_lunch_split() -> None:
  frame = pd.DataFrame([
    {"time": "202607301130", "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100},
    {"time": "202607301500", "open": 10.6, "high": 12.0, "low": 10.0, "close": 11.5, "volume": 200},
  ])
  frame["_timestamp"] = pd.to_datetime(frame["time"], format="%Y%m%d%H%M").dt.tz_localize(_TIMEZONE)

  result = market_data._aggregate_daily_minutes(frame)

  assert result.to_dict("records") == [{
    "time": "202607301500", "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.5, "volume": 300,
  }]


def test_invalid_symbol_does_not_reach_clickhouse(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: pytest.fail("不应查询 ClickHouse"))

  with pytest.raises(market_data.MarketDataError, match="标的格式无效"):
    market_data.list_dates("688981.SH' OR 1=1 --", "1m")
