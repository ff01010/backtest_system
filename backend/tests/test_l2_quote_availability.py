from __future__ import annotations

import argparse
from typing import Any

import pytest

from backend import l2_quote_coverage_sync
from backend.l2.config import load_temporary_quote_profile
from backend.l2.models import NormalizedBar
from backend.l2.providers import ClickHouseL2Adapter, L2DataCoverageError
from backend.services import backtest_preflight, bar_data, l2_quote_availability, state_store


def test_quote_availability_uses_cached_daily_records(monkeypatch: pytest.MonkeyPatch) -> None:
  cached = {
    "2026-08-26": {"status": "complete", "snapshotCount": 12},
    "2026-08-27": {"status": "missing", "snapshotCount": 0},
  }
  monkeypatch.setattr(state_store, "list_l2_quote_daily_availability", lambda *_: cached)
  monkeypatch.setattr(
    ClickHouseL2Adapter,
    "quote_daily_availability",
    lambda *_: (_ for _ in ()).throw(AssertionError("缓存命中不应读取 ClickHouse")),
  )

  result = l2_quote_availability.available_quote_dates("688981.SH", ["2026-08-26", "2026-08-27"])

  assert result == ["2026-08-26"]


def test_quote_availability_does_not_backfill_uncached_dates(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    state_store,
    "list_l2_quote_daily_availability",
    lambda *_: {"2026-08-26": {"status": "complete", "snapshotCount": 10}},
  )
  monkeypatch.setattr(
    ClickHouseL2Adapter,
    "quote_daily_availability",
    lambda *_: pytest.fail("在线范围查询不应回填原始 L2 行情"),
  )
  monkeypatch.setattr(
    state_store,
    "upsert_l2_quote_daily_availability",
    lambda *_: pytest.fail("在线范围查询不应写入 L2 摘要"),
  )

  result = l2_quote_availability.available_quote_dates("688981.SH", ["2026-08-26", "2026-08-27"])

  assert result == ["2026-08-26"]


def test_quote_daily_query_returns_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
  profile = load_temporary_quote_profile()
  adapter = ClickHouseL2Adapter(profile)
  queries: list[str] = []

  def query_rows(query: str) -> list[dict[str, Any]]:
    queries.append(query)
    if query.startswith("DESCRIBE TABLE"):
      return [{"name": column} for column in profile.columns.values()]
    return [{
      "trade_date": "2026-08-26",
      "snapshot_count": 4800,
      "first_quote_time": "2026-08-26 09:30:00",
      "last_quote_time": "2026-08-26 14:56:57",
    }]

  monkeypatch.setattr("backend.l2.providers.clickhouse_client.query_rows", query_rows)

  result = adapter.quote_daily_availability("688981.SH", ["2026-08-26"])

  assert result["2026-08-26"]["snapshotCount"] == 4800
  assert "count() AS snapshot_count" in queries[-1]
  assert "GROUP BY trade_date" in queries[-1]


def test_l2_status_reads_summary_without_querying_raw_quotes(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(l2_quote_availability, "has_profile", lambda _: False)
  monkeypatch.setattr(l2_quote_availability, "_source_key", lambda _: "source-key")
  monkeypatch.setattr(
    state_store,
    "l2_quote_availability_range",
    lambda source_key, symbol: {
      "firstDate": "2026-07-01",
      "lastDate": "2026-07-30",
      "snapshotCount": 96_000,
      "checkedAt": "2026-09-22 10:00:00",
    } if (source_key, symbol) == ("source-key", "688981.SH") else {},
  )
  monkeypatch.setattr(
    ClickHouseL2Adapter,
    "quote_daily_availability",
    lambda *_: pytest.fail("范围展示不应查询 L2 原始行情"),
  )

  result = l2_quote_availability.status("688981.SH", "5m")

  assert result["exists"] is True
  assert result["firstDate"] == "2026-07-01"
  assert result["lastDate"] == "2026-07-30"
  assert result["barCount"] == 96_000
  assert result["preview"] == []


def test_worker_quote_check_reads_only_existing_daily_summary(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(l2_quote_availability, "has_profile", lambda _: False)
  monkeypatch.setattr(l2_quote_availability, "_source_key", lambda _: "source-key")
  monkeypatch.setattr(
    state_store,
    "list_l2_quote_daily_availability",
    lambda *_: {
      "2026-08-26": {"status": "complete"},
      "2026-08-27": {"status": "complete"},
    },
  )
  monkeypatch.setattr(
    ClickHouseL2Adapter,
    "quote_daily_availability",
    lambda *_: pytest.fail("Worker 摘要校验不应扫描或回填原始 L2 行情"),
  )

  l2_quote_availability.require_summarized_quote_dates(
    "688981.SH", ["2026-08-26", "2026-08-27"],
  )


def test_worker_quote_check_rejects_missing_or_incomplete_summary(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(l2_quote_availability, "has_profile", lambda _: False)
  monkeypatch.setattr(l2_quote_availability, "_source_key", lambda _: "source-key")
  monkeypatch.setattr(
    state_store,
    "list_l2_quote_daily_availability",
    lambda *_: {"2026-08-26": {"status": "missing"}},
  )

  with pytest.raises(L2DataCoverageError) as raised:
    l2_quote_availability.require_summarized_quote_dates(
      "688981.SH", ["2026-08-26", "2026-08-27"],
    )

  assert raised.value.code == "data.l2_quote_missing"
  assert raised.value.missing_dates == ("2026-08-26", "2026-08-27")


def test_l2_preview_does_not_read_availability(monkeypatch: pytest.MonkeyPatch) -> None:
  calls: list[list[str]] = []
  monkeypatch.setattr(
    l2_quote_availability,
    "available_quote_dates",
    lambda _symbol, dates: calls.append(dates) or dates,
  )
  monkeypatch.setattr(
    bar_data,
    "derived_bar_available_dates",
    lambda *_: (_ for _ in ()).throw(AssertionError("L2 预览不应按周期查询派生 Bar")),
  )

  result = backtest_preflight.preview("688981.SH", "2026-08-26", "2026-08-27", "l2_quote", "5m")

  assert calls == []
  assert all(item["available"] for item in result["timeframes"])
  assert result["missingDates"] == []
  assert result["calendarDays"] == []


def test_quote_coverage_sync_uses_actual_bar_dates(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  bars = [
    NormalizedBar(
      symbol="688981.SH", trade_date=trade_date, period="5m",
      start_time=f"{trade_date} 09:30:00", end_time=f"{trade_date} 09:35:00",
      open=10.0, high=10.1, low=9.9, close=10.0, volume=100.0,
      amount=1000.0, quality="complete", source_version="test",
    )
    for trade_date in ("2026-08-26", "2026-08-27")
  ]
  captured_dates: list[str] = []
  monkeypatch.setattr(
    l2_quote_coverage_sync,
    "_arguments",
    lambda: argparse.Namespace(
      symbol="688981.SH", start="2026-08-26", end="2026-08-27", timeframe="5m",
    ),
  )
  monkeypatch.setattr(
    l2_quote_coverage_sync.bar_data,
    "load_normalized_bars_with_snapshot",
    lambda *_: (bars, {"provider": "qmt_kline"}),
  )
  monkeypatch.setattr(
    l2_quote_coverage_sync.l2_quote_availability,
    "rebuild_quote_availability",
    lambda _symbol, dates: captured_dates.extend(dates) or {"checked": 2, "available": 2},
  )

  l2_quote_coverage_sync.main()

  assert captured_dates == ["2026-08-26", "2026-08-27"]
