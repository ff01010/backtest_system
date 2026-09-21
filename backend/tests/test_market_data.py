# -*- coding: utf-8 -*-
"""QMT ClickHouse 行情读取和派生周期测试。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backend.services import bar_data, market_data, state_store


_TIMEZONE = ZoneInfo("Asia/Shanghai")


def test_load_bars_prefers_history_dev_for_sh_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
  epoch = int(datetime(2026, 7, 30, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"trade_date": "2026-07-30", "status": "complete"}]
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
  assert any("qmt_history_dev.qmt_ingest_runs" in query for query in queries)
  assert any("qmt_history_dev.qmt_kline_1m_raw" in query and "GROUP BY bar_time" in query for query in queries)
  assert all("raw_historical_data.qmt_kline FINAL" not in query for query in queries)


def test_bj_load_uses_published_history_dev_period_table(monkeypatch: pytest.MonkeyPatch) -> None:
  queries: list[str] = []
  start = datetime(2026, 9, 11, 9, 30, tzinfo=_TIMEZONE)

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"trade_date": "2026-09-11", "status": "complete"}]
    if "qmt_kline_coverage" in query:
      return []
    return [{
      "qmt_index": start.strftime("%Y%m%d%H%M%S"),
      "time": int(start.timestamp() * 1000),
      "open": 1.0, "high": 6.0, "low": 1.0, "close": 6.0, "volume": 60,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame = market_data.load_bars("920001.BJ", "5m", "20260911", "20260911")

  assert frame.to_dict("records") == [{
    "open": 1.0, "high": 6.0, "low": 1.0, "close": 6.0, "volume": 60,
    "time": "202609110930",
  }]
  coverage_query = next(query for query in queries if "WITH latest_runs" in query)
  raw_query = next(query for query in queries if "GROUP BY bar_time" in query)
  assert "qmt_history_dev.qmt_ingest_runs" in coverage_query
  assert "latest_runs.state = 'PUBLISHED'" in coverage_query
  assert "latest_runs.validation_result = 'PASS'" in coverage_query
  assert "has(latest_runs.actual_routes, '5m/none')" in coverage_query
  assert "qmt_history_dev.qmt_kline_5m_raw" in raw_query
  assert "adjustment = 'none'" in raw_query
  assert "quality_status = 'valid' AND is_final = 1" in raw_query


@pytest.mark.parametrize("symbol", ["688981.SH", "000001.SZ", "920001.BJ"])
def test_all_exchanges_prefer_history_dev(symbol: str, monkeypatch: pytest.MonkeyPatch) -> None:
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"trade_date": "2026-09-11", "status": "complete"}]
    pytest.fail("新库完整时不应查询旧库覆盖")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  resolution = market_data.resolve_backtest_data(
    symbol, "1m", "2026-09-11", "2026-09-11", ["2026-09-11"],
  )

  assert resolution.source == "history_dev"
  assert all("raw_historical_data" not in query for query in queries)


@pytest.mark.parametrize(("timeframe", "source_period", "table"), [
  ("1m", "1m", "qmt_kline_1m_raw"),
  ("5m", "5m", "qmt_kline_5m_raw"),
  ("15m", "15m", "qmt_kline_15m_raw"),
  ("30m", "30m", "qmt_kline_30m_raw"),
  ("60m", "1h", "qmt_kline_1h_raw"),
  ("1d", "1d", "qmt_kline_1d_raw"),
])
def test_history_dev_uses_matching_period_table(
  timeframe: str,
  source_period: str,
  table: str,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  queries: list[str] = []
  epoch = int(datetime(2026, 9, 11, 15, 0, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"trade_date": "2026-09-11", "status": "complete"}]
    return [{
      "qmt_index": "20260911150000", "time": epoch, "open": 10.0, "high": 11.0,
      "low": 9.0, "close": 10.5, "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  resolution = market_data.resolve_backtest_data(
    "688981.SH", timeframe, "2026-09-11", "2026-09-11", ["2026-09-11"],
  )
  frame, _ = market_data.load_backtest_bars("688981.SH", resolution)

  assert resolution.source == "history_dev"
  assert resolution.source_period == source_period
  assert resolution.derivation is None
  assert len(frame) == 1
  coverage_query = next(query for query in queries if "WITH latest_runs" in query)
  raw_query = next(query for query in queries if "GROUP BY bar_time" in query)
  assert f"qmt_history_dev.{table}" in coverage_query
  assert f"has(latest_runs.actual_routes, '{source_period}/none')" in coverage_query
  assert f"qmt_history_dev.{table}" in raw_query
  assert f"period = '{source_period}'" in raw_query


def test_incomplete_history_dev_range_falls_back_to_legacy_for_entire_run(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  queries: list[str] = []
  epoch = int(datetime(2026, 9, 10, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"trade_date": "2026-09-10", "status": "complete"}]
    if "qmt_kline_coverage" in query:
      return [
        {"trade_date": "2026-09-10", "status": "complete"},
        {"trade_date": "2026-09-11", "status": "complete"},
      ]
    return [{
      "qmt_index": "20260910093000", "time": epoch, "open": 10.0, "high": 11.0,
      "low": 9.0, "close": 10.5, "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  resolution = market_data.resolve_backtest_data(
    "688981.SH", "1m", "2026-09-10", "2026-09-11",
    ["2026-09-10", "2026-09-11"],
  )
  market_data.load_backtest_bars("688981.SH", resolution)

  assert resolution.source == "legacy"
  assert any("raw_historical_data.qmt_kline FINAL" in query for query in queries)
  assert all(
    "qmt_history_dev.qmt_kline_1m_raw" not in query or "WITH latest_runs" in query
    for query in queries
  )


def test_history_dev_missing_warmup_falls_back_to_legacy_for_entire_run(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(state_store, "is_trade_calendar_range_covered", lambda *_: True)
  monkeypatch.setattr(state_store, "list_open_trade_dates", lambda *_: ["2026-09-10"])

  def query_rows(query: str) -> list[dict[str, object]]:
    is_warmup = "2026-09-10" in query and "2026-09-11" not in query
    if "WITH latest_runs" in query:
      return [] if is_warmup else [{"trade_date": "2026-09-11", "status": "complete"}]
    if "qmt_kline_coverage" in query:
      date_value = "2026-09-10" if is_warmup else "2026-09-11"
      return [{"trade_date": date_value, "status": "complete"}]
    return []

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  resolution = market_data.resolve_backtest_data(
    "688981.SH", "1m", "2026-09-11", "2026-09-11", ["2026-09-11"], 1,
  )

  assert resolution.source == "legacy"
  assert resolution.formal_dates == ("2026-09-11",)
  assert resolution.warmup_dates == ("2026-09-10",)


def test_both_sources_incomplete_rejects_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
  def query_rows(query: str) -> list[dict[str, object]]:
    return [{"trade_date": "2026-09-11", "status": "partial"}]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  with pytest.raises(market_data.MarketDataError, match="QMT 行情不完整"):
    market_data.ensure_cached("920001.BJ", "1m", "20260911", "20260911")


def test_bj_supports_platform_derived_timeframes(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: [
    {"trade_date": "2026-09-11", "status": "complete"},
  ])

  resolution = market_data.resolve_backtest_data(
    "920001.BJ", "45m", "20260911", "20260911", ["2026-09-11"],
  )

  assert resolution.source_period == "1m"
  assert resolution.derivation == "45m-from-1m-v1"
  assert resolution.source == "history_dev"


def test_incomplete_qmt_coverage_rejects_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: [
    {"trade_date": "2026-07-30", "status": "partial"},
  ])

  with pytest.raises(market_data.MarketDataError, match="QMT 行情不完整"):
    market_data.ensure_cached("688981.SH", "1m", "20260730", "20260730")


def test_weekend_start_date_uses_available_qmt_bars(monkeypatch: pytest.MonkeyPatch) -> None:
  epoch = int(datetime(2025, 1, 6, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    if "WITH latest_runs" in query:
      return []
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
  assert resolution.source == "legacy"


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
    "_coverage_for_source",
    lambda *_: [
      {"date": "2025-09-09", "status": "complete"},
      {"date": "2025-09-10", "status": "complete"},
      {"date": "2025-09-11", "status": "complete"},
    ],
  )

  candidate = market_data._SourceCandidate("legacy", "1m")
  result = market_data._complete_dates_before("688981.SH", candidate, "2025-09-12", 5)

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


def test_status_avoids_full_history_summary_and_limits_preview(monkeypatch: pytest.MonkeyPatch) -> None:
  complete_dates = [
    (datetime(2026, 1, 1) + timedelta(days=offset)).date().isoformat()
    for offset in range(25)
  ]
  queries: list[str] = []
  epoch = int(datetime(2026, 1, 25, 15, 0, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"trade_date": item, "status": "complete"} for item in complete_dates]
    if "uniqExact(bar_time)" in query:
      pytest.fail("数据状态不应扫描全历史原始 K 线统计总行数")
    return [{
      "qmt_index": "20260125150000", "time": epoch, "open": 10.0, "high": 11.0,
      "low": 9.0, "close": 10.5, "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  result = market_data.status("688981.SH", "1m")

  preview_query = next(query for query in queries if "GROUP BY bar_time" in query)
  assert result["barCount"] is None
  assert result["preview"][0]["time"] == "202601251500"
  assert "toDate('2026-01-06')" in preview_query
  assert "toDate('2026-01-25')" in preview_query
  assert "toDate('2000-01-01')" not in preview_query
  assert "toDate('2099-12-31')" not in preview_query


def test_invalid_symbol_does_not_reach_clickhouse(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: pytest.fail("不应查询 ClickHouse"))

  with pytest.raises(market_data.MarketDataError, match="标的格式无效"):
    market_data.list_dates("688981.SH' OR 1=1 --", "1m")
