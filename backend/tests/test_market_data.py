# -*- coding: utf-8 -*-
"""QMT ClickHouse 行情读取和派生周期测试。"""

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backend.services import bar_data, clickhouse_client, market_data
from backend.services.backtest_faults import BacktestFault
from backend.l2.models import NormalizedBar


_TIMEZONE = ZoneInfo("Asia/Shanghai")


def test_load_bars_uses_history_dev_for_sh_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
  epoch = int(datetime(2026, 7, 30, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "min(eligible_runs.target_date)" in query:
      return [{"first_date": "2024-03-07", "last_date": "2026-07-30"}]
    if "WITH latest_runs" in query:
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
  raw_query = next(query for query in queries if "qmt_history_dev.qmt_kline_1m_raw" in query)
  assert "qmt_time_ms AS time" in raw_query
  assert "trade_date IN (toDate('2026-07-30'))" in raw_query
  assert "SETTINGS max_execution_time = 25" in raw_query
  assert all(item not in raw_query for item in (
    "argMax", "GROUP BY", "revision", "ingested_at", "qmt_index",
  ))
  assert all("qmt_history_dev" in query for query in queries)


def test_bj_load_uses_published_history_dev_period_table(monkeypatch: pytest.MonkeyPatch) -> None:
  queries: list[str] = []
  start = datetime(2026, 9, 11, 9, 30, tzinfo=_TIMEZONE)

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "min(eligible_runs.target_date)" in query:
      return [{"first_date": "2024-03-07", "last_date": "2026-09-11"}]
    if "WITH latest_runs" in query:
      return [{"trade_date": "2026-09-11", "status": "complete"}]
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
  coverage_query = next(
    query for query in queries if "eligible_runs.target_date AS trade_date" in query
  )
  raw_query = next(
    query for query in queries if "qmt_history_dev.qmt_kline_5m_raw" in query
  )
  assert "qmt_history_dev.qmt_ingest_runs" in coverage_query
  assert "qmt_history_dev.qmt_universe_snapshot" in coverage_query
  assert "argMax" in coverage_query
  assert "latest_runs.state = 'PUBLISHED'" in coverage_query
  assert "latest_runs.validation_result = 'PASS'" in coverage_query
  assert "has(latest_runs.actual_routes, '5m/none')" in coverage_query
  assert "eligible_runs.target_date AS trade_date" in coverage_query
  assert "snapshots.effective_date AS trade_date" not in coverage_query
  assert "qmt_kline_5m_raw" not in coverage_query
  assert "qmt_history_dev.qmt_kline_5m_raw" in raw_query
  assert "adjustment = 'none'" in raw_query
  assert "quality_status" not in raw_query
  assert "is_final = 1" in raw_query
  assert "argMax" not in raw_query
  assert "GROUP BY" not in raw_query


@pytest.mark.parametrize("symbol", ["688981.SH", "000001.SZ", "920001.BJ"])
def test_all_exchanges_use_only_history_dev(symbol: str, monkeypatch: pytest.MonkeyPatch) -> None:
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"trade_date": "2026-09-11", "status": "complete"}]
    pytest.fail("标准 Bar 只应查询新版发布元数据")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  resolution = market_data.resolve_backtest_data(
    symbol, "1m", "2026-09-11", "2026-09-11", ["2026-09-11"],
  )

  assert resolution.source == "history_dev"
  assert all("qmt_history_dev" in query for query in queries)


@pytest.mark.parametrize(("timeframe", "source_period", "derivation"), [
  ("1m", "1m", None),
  ("3m", "1m", "3m-from-1m-v1"),
  ("5m", "5m", None),
  ("15m", "15m", None),
  ("30m", "30m", None),
  ("45m", "1m", "45m-from-1m-v1"),
  ("60m", "1h", None),
  ("1d", "1d", None),
])
def test_source_candidates_only_include_history_dev(
  timeframe: str,
  source_period: str,
  derivation: str | None,
) -> None:
  assert market_data._source_candidates(timeframe) == (
    market_data._SourceCandidate("history_dev", source_period, derivation),
  )


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
  raw_query = next(
    query for query in queries if f"qmt_history_dev.{table}" in query
  )
  assert "qmt_history_dev.qmt_universe_snapshot" in coverage_query
  assert f"qmt_history_dev.{table}" not in coverage_query
  assert f"has(latest_runs.actual_routes, '{source_period}/none')" in coverage_query
  assert f"qmt_history_dev.{table}" in raw_query
  assert f"period = '{source_period}'" in raw_query
  assert "quality_status" not in raw_query
  assert "argMax" not in raw_query
  assert "GROUP BY" not in raw_query


def test_history_dev_skips_unpublished_exchange_day(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"trade_date": "2026-09-10", "status": "complete"}]
    pytest.fail("标准 Bar 只应使用新版发布日期")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  resolution = market_data.resolve_backtest_data(
    "688981.SH", "1m", "2026-09-10", "2026-09-11",
    ["2026-09-10", "2026-09-11"],
  )

  assert resolution.source == "history_dev"
  assert resolution.formal_dates == ("2026-09-10",)
  assert len(queries) == 1


def test_halted_symbol_replays_published_days_across_calendar_gap(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  first_epoch = int(datetime(2025, 2, 21, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  second_epoch = int(datetime(2025, 3, 10, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    if "WITH latest_runs" in query:
      return [
        {"trade_date": "2025-02-21", "status": "complete"},
        {"trade_date": "2025-03-10", "status": "complete"},
      ]
    if "qmt_history_dev.qmt_kline_1m_raw" in query:
      return [
        {"time": first_epoch, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 100},
        {"time": second_epoch, "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 100},
      ]
    pytest.fail("停牌期间只应读取新版行情")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  resolution = market_data.resolve_backtest_data(
    "688126.SH", "1m", "2025-02-21", "2025-03-10",
    ["2025-02-21", "2025-02-24", "2025-02-25", "2025-03-10"],
  )
  frame, warmup_bar_count = market_data.load_backtest_bars("688126.SH", resolution)

  assert resolution.formal_dates == ("2025-02-21", "2025-03-10")
  assert frame["time"].tolist() == ["202502210930", "202503100930"]
  assert warmup_bar_count == 0


def test_published_history_bar_ignores_source_quality_status(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  epoch = int(datetime(2024, 7, 26, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    if "WITH latest_runs" in query:
      return [{"trade_date": "2024-07-26", "status": "complete"}]
    if "qmt_history_dev.qmt_kline_1m_raw" in query:
      if "quality_status = 'valid'" in query:
        return []
      return [{
        "time": epoch, "open": 10.0, "high": 11.0, "low": 9.0,
        "close": 10.5, "volume": 100,
      }]
    pytest.fail("已发布 Bar 只应从新版周期表读取")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  resolution = market_data.resolve_backtest_data(
    "688126.SH", "1m", "2024-07-26", "2024-07-26", ["2024-07-26"],
  )
  frame, _ = market_data.load_backtest_bars("688126.SH", resolution)

  assert resolution.source == "history_dev"
  assert frame["time"].tolist() == ["202407260930"]


def test_history_dev_missing_warmup_rejects_without_querying_another_source(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "min(eligible_runs.target_date)" in query:
      return [{"first_date": "2026-09-11", "last_date": "2026-09-11"}]
    is_warmup = "2026-09-10" in query and "2026-09-11" not in query
    if "WITH latest_runs" in query:
      return [] if is_warmup else [{"trade_date": "2026-09-11", "status": "complete"}]
    pytest.fail("预热不足时不应读取其他行情来源")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.resolve_backtest_data(
      "688981.SH", "1m", "2026-09-11", "2026-09-11", ["2026-09-11"], 1,
    )

  assert raised.value.reason == "warmup_insufficient"
  assert all("qmt_history_dev" in query for query in queries)


def test_incomplete_history_coverage_rejects_backtest(monkeypatch: pytest.MonkeyPatch) -> None:
  def query_rows(query: str) -> list[dict[str, object]]:
    return [{"trade_date": "2026-09-11", "status": "partial"}]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  with pytest.raises(market_data.MarketDataError, match="区间内无完整 QMT 行情"):
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

  with pytest.raises(market_data.MarketDataError, match="区间内无完整 QMT 行情"):
    market_data.ensure_cached("688981.SH", "1m", "20260730", "20260730")


def test_calendar_lag_does_not_block_latest_complete_bar(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  epochs = [
    int(datetime(2026, 9, day, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
    for day in (23, 24)
  ]
  monkeypatch.setattr(
    market_data,
    "_coverage_range_for_source",
    lambda *_: {"firstDate": "2024-03-07", "lastDate": "2026-09-24"},
  )
  monkeypatch.setattr(
    market_data,
    "_coverage_for_source",
    lambda *_: [
      {"date": "2026-09-23", "status": "complete"},
      {"date": "2026-09-24", "status": "complete"},
    ],
  )
  monkeypatch.setattr(
    market_data,
    "_query_kline_rows",
    lambda *_: [
      {
        "time": epoch, "open": 10.0, "high": 10.5,
        "low": 9.8, "close": 10.2, "volume": 100,
      }
      for epoch in epochs
    ],
  )

  resolution = market_data.resolve_backtest_data(
    "600000.SH", "1m", "2024-03-07", "2026-09-24",
  )
  frame, _ = market_data.load_backtest_bars("600000.SH", resolution)

  assert resolution.formal_dates[-1] == "2026-09-24"
  assert frame["time"].tolist()[-1].startswith("20260924")


def test_request_end_after_source_boundary_is_rejected(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    market_data,
    "_coverage_range_for_source",
    lambda *_: {"firstDate": "2024-03-07", "lastDate": "2026-09-24"},
  )
  monkeypatch.setattr(
    market_data,
    "_coverage_for_source",
    lambda *_: pytest.fail("越界请求不应读取区间覆盖日期"),
  )

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.resolve_backtest_data(
      "600000.SH", "1m", "2024-03-07", "2026-09-25",
    )

  assert raised.value.reason == "range_out_of_bounds"


def test_in_range_without_complete_dates_is_not_reported_as_out_of_range(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    market_data,
    "_coverage_range_for_source",
    lambda *_: {"firstDate": "2024-03-07", "lastDate": "2026-09-24"},
  )
  monkeypatch.setattr(market_data, "_coverage_for_source", lambda *_: [])

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.resolve_backtest_data(
      "600000.SH", "1m", "2026-09-20", "2026-09-21",
    )

  assert raised.value.reason == "no_complete_coverage"


def test_frozen_snapshot_restores_source_without_current_coverage_lookup(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    market_data,
    "_coverage_for_source",
    lambda *_: pytest.fail("历史冻结快照不应查询当前覆盖"),
  )
  snapshot = {
    "provider": "qmt_kline",
    "sourceDatabase": "qmt_history_dev",
    "sourceTable": "qmt_kline_1m_raw",
    "sourcePeriod": "1m",
    "derivation": None,
    "formalDates": ["2026-09-23", "2026-09-24"],
    "warmupDates": ["2026-09-22"],
  }

  resolution = market_data.resolve_backtest_data(
    "600000.SH", "1m", "2026-09-23", "2026-09-24",
    snapshot["formalDates"], 1, snapshot,
  )

  assert resolution.source == "history_dev"
  assert resolution.formal_dates == ("2026-09-23", "2026-09-24")
  assert resolution.warmup_dates == ("2026-09-22",)


def test_unsupported_source_snapshot_is_rejected_without_query(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    market_data.clickhouse_client,
    "query_rows",
    lambda *_: pytest.fail("不支持的历史来源快照不应查询 ClickHouse"),
  )
  snapshot = {
    "provider": "qmt_kline",
    "sourceDatabase": "raw_historical_data",
    "sourceTable": "qmt_kline",
    "sourcePeriod": "1m",
    "formalDates": ["2026-09-23"],
    "warmupDates": [],
  }

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.resolve_backtest_data(
      "600000.SH", "1m", "2026-09-23", "2026-09-23",
      snapshot["formalDates"], 0, snapshot,
    )

  assert raised.value.reason == "snapshot_invalid"
  assert str(raised.value) == "历史 Bar 快照的数据来源已不受支持"


def test_weekend_start_date_uses_available_qmt_bars(monkeypatch: pytest.MonkeyPatch) -> None:
  first_epoch = int(datetime(2025, 1, 6, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  second_epoch = int(datetime(2025, 1, 7, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)

  def query_rows(query: str) -> list[dict[str, object]]:
    if "min(eligible_runs.target_date)" in query:
      return [{"first_date": "2025-01-03", "last_date": "2025-01-07"}]
    if "eligible_runs.target_date AS trade_date" in query:
      return [
        {"trade_date": "2025-01-06", "status": "complete"},
        {"trade_date": "2025-01-07", "status": "complete"},
      ]
    if "qmt_history_dev.qmt_kline_1m_raw" in query:
      return [
        {
          "qmt_index": "20250106093000", "time": first_epoch, "open": 10.0, "high": 10.2,
          "low": 9.9, "close": 10.1, "volume": 100,
        },
        {
          "qmt_index": "20250107093000", "time": second_epoch, "open": 10.1, "high": 10.3,
          "low": 10.0, "close": 10.2, "volume": 110,
        },
      ]
    pytest.fail("周末起始日期只应查询新版发布元数据和周期表")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame = market_data.load_bars("688126.SH", "1m", "20250104", "20250107")

  assert frame["time"].tolist() == ["202501060930", "202501070930"]


def test_load_backtest_bars_keeps_available_rows_without_daily_quality_check(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  epoch = int(datetime(2026, 9, 10, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-09-10", "2026-09-11"),
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: [{
    "qmt_index": "20260910093000", "time": epoch, "open": 10.0, "high": 11.0,
    "low": 9.0, "close": 10.5, "volume": 100,
  }])

  frame, _ = market_data.load_backtest_bars("688981.SH", resolution)

  assert frame["time"].tolist() == ["202609100930"]


def test_load_backtest_bars_batches_minute_source_dates(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  formal_dates = tuple(
    f"2026-01-{day:02d}" for day in range(1, 26)
  )
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=formal_dates,
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    batch_number = len(queries)
    timestamp = datetime(2026, 1, 1 + ((batch_number - 1) * 10), 9, 30, tzinfo=_TIMEZONE)
    return [{
      "time": int(timestamp.timestamp() * 1000),
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": 10.5,
      "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame, _ = market_data.load_backtest_bars("688981.SH", resolution)

  assert frame["time"].tolist() == [
    "202601010930", "202601110930", "202601210930",
  ]
  assert [query.count("toDate(") for query in queries] == [10, 10, 5]
  assert "toDate('2026-01-01')" in queries[0]
  assert "toDate('2026-01-10')" in queries[0]
  assert "toDate('2026-01-11')" in queries[1]
  assert "toDate('2026-01-20')" in queries[1]
  assert "toDate('2026-01-21')" in queries[2]
  assert "toDate('2026-01-25')" in queries[2]


def test_load_backtest_bars_queries_only_non_contiguous_requested_dates(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-01-05", "2026-01-09"),
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    timestamp = datetime(2026, 1, 5, 9, 30, tzinfo=_TIMEZONE)
    return [{
      "time": int(timestamp.timestamp() * 1000),
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": 10.5,
      "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  market_data.load_backtest_bars("688981.SH", resolution)

  assert len(queries) == 1
  assert "toDate('2026-01-05')" in queries[0]
  assert "toDate('2026-01-09')" in queries[0]
  assert "BETWEEN" not in queries[0]
  assert "2026-01-06" not in queries[0]


def test_load_backtest_bars_batches_non_minute_source_by_forty_dates(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  first_date = datetime(2026, 1, 1)
  formal_dates = tuple(
    (first_date + timedelta(days=offset)).date().isoformat()
    for offset in range(41)
  )
  resolution = market_data.BacktestDataResolution(
    timeframe="5m",
    source_period="5m",
    derivation=None,
    formal_dates=formal_dates,
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    day = 1 if len(queries) == 1 else 10
    month = 1 if len(queries) == 1 else 2
    timestamp = datetime(2026, month, day, 9, 30, tzinfo=_TIMEZONE)
    return [{
      "time": int(timestamp.timestamp() * 1000),
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": 10.5,
      "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame, _ = market_data.load_backtest_bars("688981.SH", resolution)

  assert len(frame) == 2
  assert [query.count("toDate(") for query in queries] == [40, 1]


@pytest.mark.parametrize("timeframe", ["3m", "45m"])
def test_derived_periods_use_minute_source_batch_size(
  timeframe: str,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  formal_dates = tuple(
    f"2026-01-{day:02d}" for day in range(1, 12)
  )
  resolution = market_data.BacktestDataResolution(
    timeframe=timeframe,
    source_period="1m",
    derivation=f"{timeframe}-from-1m-v1",
    formal_dates=formal_dates,
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    day = 1 if len(queries) == 1 else 11
    timestamp = datetime(2026, 1, day, 9, 30, tzinfo=_TIMEZONE)
    return [{
      "time": int(timestamp.timestamp() * 1000),
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": 10.5,
      "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame, _ = market_data.load_backtest_bars("688981.SH", resolution)

  assert len(frame) == 2
  assert [query.count("toDate(") for query in queries] == [10, 1]


def test_load_backtest_bars_counts_warmup_across_batches(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  warmup_dates = tuple(
    f"2026-01-{day:02d}" for day in range(1, 12)
  )
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-01-12",),
    warmup_dates=warmup_dates,
    available_warmup_days=len(warmup_dates),
    source="history_dev",
  )
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    rows: list[dict[str, object]] = []
    for value in re.findall(r"toDate\('(\d{4}-\d{2}-\d{2})'\)", query):
      timestamp = datetime.fromisoformat(value).replace(
        hour=9,
        minute=30,
        tzinfo=_TIMEZONE,
      )
      rows.append({
        "time": int(timestamp.timestamp() * 1000),
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "volume": 100,
      })
    return rows

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame, warmup_bar_count = market_data.load_backtest_bars("688981.SH", resolution)

  assert len(frame) == 12
  assert warmup_bar_count == 11
  assert [query.count("toDate(") for query in queries] == [10, 2]


def test_load_backtest_bars_allows_empty_intermediate_batch(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  formal_dates = tuple(
    f"2026-01-{day:02d}" for day in range(1, 12)
  )
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=formal_dates,
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  calls = 0

  def query_rows(_: str) -> list[dict[str, object]]:
    nonlocal calls
    calls += 1
    if calls == 1:
      return []
    timestamp = datetime(2026, 1, 11, 9, 30, tzinfo=_TIMEZONE)
    return [{
      "time": int(timestamp.timestamp() * 1000),
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": 10.5,
      "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  frame, _ = market_data.load_backtest_bars("688981.SH", resolution)

  assert frame["time"].tolist() == ["202601110930"]
  assert calls == 2


def test_load_backtest_bars_retries_only_current_failed_batch(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  formal_dates = tuple(
    f"2026-01-{day:02d}" for day in range(1, 12)
  )
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=formal_dates,
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "toDate('2026-01-01')" in query:
      timestamp = datetime(2026, 1, 1, 9, 30, tzinfo=_TIMEZONE)
    elif sum("toDate('2026-01-11')" in item for item in queries) == 1:
      try:
        raise TimeoutError("temporary timeout")
      except TimeoutError as error:
        raise clickhouse_client.ClickHouseError("ClickHouse 查询超时") from error
    else:
      timestamp = datetime(2026, 1, 11, 9, 30, tzinfo=_TIMEZONE)
    return [{
      "time": int(timestamp.timestamp() * 1000),
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": 10.5,
      "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  monkeypatch.setattr(market_data.clickhouse_client.time, "sleep", lambda _: None)

  frame, _ = market_data.load_backtest_bars("688981.SH", resolution)

  assert frame["time"].tolist() == ["202601010930", "202601110930"]
  assert len(queries) == 3
  assert queries[1] == queries[2]
  assert sum("toDate('2026-01-01')" in query for query in queries) == 1


def test_load_backtest_bars_rejects_empty_result(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  resolution = market_data.BacktestDataResolution(
    timeframe="1m", source_period="1m", derivation=None,
    formal_dates=("2026-09-11",), warmup_dates=(), available_warmup_days=0,
    source="history_dev",
  )
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: [])

  with pytest.raises(market_data.MarketDataError, match="区间内无可用 QMT 行情"):
    market_data.load_backtest_bars("688981.SH", resolution)


def test_load_backtest_bars_rejects_duplicate_source_time(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  first_epoch = int(datetime(2026, 9, 11, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  second_epoch = int(datetime(2026, 9, 11, 9, 30, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-09-11",),
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  duplicate_row = {
    "time": first_epoch,
    "open": 10.0,
    "high": 11.0,
    "low": 9.0,
    "close": 10.5,
    "volume": 100,
  }
  monkeypatch.setattr(
    market_data.clickhouse_client,
    "query_rows",
    lambda _: [duplicate_row, {**duplicate_row, "time": second_epoch}],
  )

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.load_backtest_bars("688981.SH", resolution)

  assert raised.value.reason == "invalid_bar_data"
  assert "重复" in str(raised.value)


def test_load_backtest_bars_rejects_non_finite_ohlcv(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  epoch = int(datetime(2026, 9, 11, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-09-11",),
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  monkeypatch.setattr(
    market_data.clickhouse_client,
    "query_rows",
    lambda _: [{
      "time": epoch,
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": float("nan"),
      "volume": 100,
    }],
  )

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.load_backtest_bars("688981.SH", resolution)

  assert raised.value.reason == "invalid_bar_data"
  assert "非有限数值" in str(raised.value)


def test_load_backtest_bars_rejects_unrequested_returned_date(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  epoch = int(datetime(2026, 9, 12, 9, 30, tzinfo=_TIMEZONE).timestamp() * 1000)
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-09-11",),
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  monkeypatch.setattr(
    market_data.clickhouse_client,
    "query_rows",
    lambda _: [{
      "time": epoch,
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": 10.5,
      "volume": 100,
    }],
  )

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.load_backtest_bars("688981.SH", resolution)

  assert raised.value.reason == "invalid_bar_data"
  assert "请求范围外" in str(raised.value)


def test_load_backtest_bars_preserves_failed_batch_error_after_retries(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-09-11",),
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  calls = 0

  def query_rows(_: str) -> list[dict[str, object]]:
    nonlocal calls
    calls += 1
    try:
      raise TimeoutError("temporary timeout")
    except TimeoutError as error:
      raise clickhouse_client.ClickHouseError("ClickHouse 查询超时") from error

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  monkeypatch.setattr(market_data.clickhouse_client.time, "sleep", lambda _: None)

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.load_backtest_bars("688981.SH", resolution)

  assert calls == 2
  assert raised.value.reason == "source_read_failed"
  assert isinstance(raised.value.__cause__, clickhouse_client.ClickHouseError)


def test_load_backtest_bars_rejects_invalid_client_timeout_before_query(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-09-11",),
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  monkeypatch.setattr(market_data.settings, "CLICKHOUSE_TIMEOUT_SECONDS", 1)
  monkeypatch.setattr(
    market_data.clickhouse_client,
    "query_rows",
    lambda _: pytest.fail("非法超时配置不应发起 ClickHouse 查询"),
  )

  with pytest.raises(market_data.MarketDataError) as raised:
    market_data.load_backtest_bars("688981.SH", resolution)

  assert raised.value.reason == "source_config_invalid"


def test_qmt_normalized_bar_uses_completed_bar_time() -> None:
  frame = pd.DataFrame([
    {"time": "20260730090300", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 100},
    {"time": "20260730093000", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 100},
  ])

  bars = bar_data._qmt_normalized_bars("688981.SH", "1m", frame)

  assert len(bars) == 1
  assert bars[0].start_time == "2026-07-30 09:29:00"
  assert bars[0].end_time == "2026-07-30 09:30:00"


def test_mapped_bar_source_does_not_require_bar_on_every_exchange_day(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  bar = NormalizedBar(
    symbol="688126.SH", trade_date="2025-03-10", period="1m",
    start_time="2025-03-10 09:29:00", end_time="2025-03-10 09:30:00",
    open=10.0, high=10.0, low=10.0, close=10.0, volume=100.0,
    amount=1000.0, quality="complete", source_version="test",
  )

  class FakeAdapter:
    def __init__(self, profile: object) -> None:
      del profile

    def load_bars(self, symbol: str, period: str, start_date: str, end_date: str) -> list[NormalizedBar]:
      del symbol, period, start_date, end_date
      return [bar]

    def bar_date_range(self, symbol: str, period: str) -> tuple[str, str]:
      del symbol, period
      return "2025-01-01", "2025-12-31"

    def coverage(self, symbol: str, start_date: str, end_date: str) -> object:
      del symbol, start_date, end_date
      return type("Coverage", (), {})()

    def bar_source_identity(self, period: str) -> dict[str, object]:
      return {
        "sourceDatabase": "mapped", "sourceTable": "bars",
        "sourcePeriod": period, "derivation": None,
      }

  monkeypatch.setattr(bar_data, "has_profile", lambda *_: True)
  monkeypatch.setattr(bar_data, "load_profile", lambda *_: object())
  monkeypatch.setattr(bar_data, "ClickHouseL2Adapter", FakeAdapter)

  frame, snapshot = bar_data.load_bars_with_snapshot("688126.SH", "1m", "2025-02-24", "2025-03-10")

  assert frame["time"].tolist() == ["20250310093000"]
  assert snapshot["provider"] == "mapped_l2_bar"


def test_mapped_bar_source_outside_boundary_is_not_partially_loaded(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class FakeAdapter:
    def __init__(self, profile: object) -> None:
      del profile

    def bar_date_range(self, symbol: str, period: str) -> tuple[str, str]:
      del symbol, period
      return "2026-07-01", "2026-07-30"

    def load_bars(self, *_: object) -> list[NormalizedBar]:
      pytest.fail("越界请求不应读取并返回部分正式映射 Bar")

  monkeypatch.setattr(bar_data, "has_profile", lambda *_: True)
  monkeypatch.setattr(bar_data, "load_profile", lambda *_: object())
  monkeypatch.setattr(bar_data, "ClickHouseL2Adapter", FakeAdapter)

  assert bar_data._load_mapped_bars(
    "688981.SH", "1m", "2026-07-01", "2026-07-31",
  ) is None


def test_qmt_source_read_failure_does_not_fall_back_to_temporary_quotes(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(bar_data, "has_profile", lambda *_: False)
  monkeypatch.setattr(
    bar_data.market_data,
    "resolve_backtest_data",
    lambda *_: (_ for _ in ()).throw(
      market_data.MarketDataError("ClickHouse 查询超时", "source_read_failed"),
    ),
  )
  monkeypatch.setattr(
    bar_data,
    "_temporary_provider",
    lambda: pytest.fail("来源读取失败不应静默切换到临时 Quote"),
  )

  with pytest.raises(market_data.MarketDataError) as raised:
    bar_data.load_bars_with_snapshot(
      "688981.SH", "1m", "2026-07-01", "2026-07-30",
    )

  assert raised.value.reason == "source_read_failed"


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


def test_qmt_normalized_bar_wraps_invalid_source_row() -> None:
  frame = pd.DataFrame([{
    "time": "invalid", "open": 10.0, "high": 10.2, "low": 9.9,
    "close": 10.1, "volume": 100,
  }])

  with pytest.raises(BacktestFault) as raised:
    bar_data._qmt_normalized_bars("688981.SH", "1m", frame)

  assert raised.value.code == "data.l2_bar_invalid"
  assert raised.value.context == {"symbol": "688981.SH", "timeframe": "1m"}


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


def test_daily_resolution_uses_published_daily_bars_for_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
  def coverage(
    _symbol: str,
    candidate: market_data._SourceCandidate,
    start_date: str,
    end_date: str,
  ) -> list[dict[str, object]]:
    del start_date
    date_value = "2026-07-29" if end_date == "2026-07-29" else "2026-07-30"
    return [{"date": date_value, "status": "complete"}]

  monkeypatch.setattr(market_data, "_coverage_for_source", coverage)
  monkeypatch.setattr(
    market_data,
    "_coverage_range_for_source",
    lambda *_: {"firstDate": "2026-07-29", "lastDate": "2026-07-30"},
  )

  resolution = market_data.resolve_backtest_data(
    "688981.SH", "1d", "2026-07-30", "2026-07-30", ["2026-07-30"], 1,
  )

  assert resolution.source_period == "1d"
  assert resolution.derivation is None
  assert resolution.warmup_dates == ("2026-07-29",)
  assert resolution.source == "history_dev"


def test_warmup_skips_halted_exchange_days(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    market_data,
    "_coverage_range_for_source",
    lambda *_: {"firstDate": "2025-09-01", "lastDate": "2025-09-11"},
  )
  monkeypatch.setattr(
    market_data,
    "_coverage_for_source",
    lambda *_: [
      {"date": "2025-09-01", "status": "complete"},
      {"date": "2025-09-02", "status": "complete"},
      {"date": "2025-09-09", "status": "complete"},
      {"date": "2025-09-10", "status": "complete"},
      {"date": "2025-09-11", "status": "complete"},
    ],
  )

  candidate = market_data._SourceCandidate("history_dev", "1m")
  result = market_data._complete_dates_before("688981.SH", candidate, "2025-09-12", 5)

  assert result == ["2025-09-01", "2025-09-02", "2025-09-09", "2025-09-10", "2025-09-11"]


def test_status_uses_only_coverage_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
  queries: list[str] = []
  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"first_date": "2026-01-01", "last_date": "2026-01-25"}]
    pytest.fail("数据状态只能查询覆盖元数据")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  result = market_data.status("688981.SH", "1m")

  assert result["barCount"] is None
  assert result["preview"] == []
  assert result["firstDate"] == "2026-01-01"
  assert result["lastDate"] == "2026-01-25"
  assert len(queries) == 1
  assert "min(eligible_runs.target_date)" in queries[0]
  assert "max(eligible_runs.target_date)" in queries[0]
  assert "snapshots.effective_date" not in queries[0]
  assert all("qmt_kline_1m_raw" not in query for query in queries)


def test_history_preview_reads_unique_final_rows_without_version_aggregation(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  epoch = int(datetime(2026, 9, 11, 15, 0, tzinfo=_TIMEZONE).timestamp() * 1000)
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    return [{
      "time": epoch,
      "open": 10.0,
      "high": 11.0,
      "low": 9.0,
      "close": 10.5,
      "volume": 100,
    }]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  result = market_data._query_preview(
    "688981.SH",
    market_data._SourceCandidate("history_dev", "1m"),
    "2026-09-01",
    "2026-09-11",
    20,
  )

  assert result == [{
    "time": "202609111500",
    "open": 10.0,
    "high": 11.0,
    "low": 9.0,
    "close": 10.5,
    "volume": 100.0,
  }]
  assert len(queries) == 1
  assert "qmt_time_ms AS time" in queries[0]
  assert "ORDER BY bar_time DESC LIMIT 20" in queries[0]
  assert all(item not in queries[0] for item in (
    "argMax", "GROUP BY", "revision", "ingested_at", "qmt_index",
  ))


def test_status_uses_history_range(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  queries: list[str] = []
  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"first_date": "2024-03-07", "last_date": "2026-09-22"}]
    pytest.fail("范围展示只应查询新版发布元数据")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  result = market_data.status("688981.SH", "5m")

  assert result["firstDate"] == "2024-03-07"
  assert result["lastDate"] == "2026-09-22"
  assert result["barCount"] is None
  assert result["path"].endswith(
    "qmt_history_dev.qmt_ingest_runs+qmt_history_dev.qmt_universe_snapshot"
  )
  assert len(queries) == 1


def test_status_returns_empty_when_history_has_no_symbol(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  queries: list[str] = []
  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if "WITH latest_runs" in query:
      return [{"first_date": None, "last_date": None}]
    pytest.fail("范围展示只应查询新版发布元数据")

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)

  result = market_data.status("688981.SH", "5m")

  assert result["exists"] is False
  assert result["firstDate"] is None
  assert result["lastDate"] is None
  assert result["barCount"] == 0
  assert result["path"] is None
  assert len(queries) == 1


def test_status_retries_history_timeout(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  queries: list[str] = []
  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    if len(queries) == 1:
      try:
        raise TimeoutError("temporary timeout")
      except TimeoutError as error:
        raise clickhouse_client.ClickHouseError("ClickHouse 查询超时") from error
    return [{"first_date": "2024-03-07", "last_date": "2026-09-22"}]

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  monkeypatch.setattr(market_data.clickhouse_client.time, "sleep", lambda _: None)

  result = market_data.status("688981.SH", "5m")

  assert result["firstDate"] == "2024-03-07"
  assert result["lastDate"] == "2026-09-22"
  assert len(queries) == 2
  assert all("WITH latest_runs" in query for query in queries)


def test_status_reports_history_failure_after_retries(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  queries: list[str] = []
  def query_rows(query: str) -> list[dict[str, object]]:
    queries.append(query)
    try:
      raise TimeoutError("temporary timeout")
    except TimeoutError as error:
      raise clickhouse_client.ClickHouseError("ClickHouse 查询超时") from error

  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", query_rows)
  monkeypatch.setattr(market_data.clickhouse_client.time, "sleep", lambda _: None)

  with pytest.raises(market_data.MarketDataError, match="查询超时"):
    market_data.status("688981.SH", "5m")

  assert len(queries) == 2
  assert all("WITH latest_runs" in query for query in queries)


def test_load_backtest_bars_wraps_invalid_qmt_row(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  resolution = market_data.BacktestDataResolution(
    timeframe="1m",
    source_period="1m",
    derivation=None,
    formal_dates=("2026-09-10",),
    warmup_dates=(),
    available_warmup_days=0,
    source="history_dev",
  )
  monkeypatch.setattr(
    market_data.clickhouse_client,
    "query_rows",
    lambda _: [{"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100}],
  )

  with pytest.raises(market_data.MarketDataError, match="QMT Bar 行数据格式无效"):
    market_data.load_backtest_bars("688981.SH", resolution)


def test_timeframe_status_does_not_check_selected_range(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    market_data.clickhouse_client,
    "query_rows",
    lambda _: pytest.fail("周期选项不应触发行情完整性查询"),
  )

  result = market_data.timeframe_status("688981.SH", "2026-07-01", "2026-07-30")

  assert [item["timeframe"] for item in result] == list(market_data.settings.BACKTEST_TIMEFRAMES)
  assert all(item["available"] is True for item in result)


def test_invalid_symbol_does_not_reach_clickhouse(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: pytest.fail("不应查询 ClickHouse"))

  with pytest.raises(market_data.MarketDataError, match="标的格式无效"):
    market_data.list_dates("688981.SH' OR 1=1 --", "1m")
