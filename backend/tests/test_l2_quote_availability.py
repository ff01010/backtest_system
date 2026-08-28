from __future__ import annotations

from typing import Any

import pytest

from backend.l2.config import load_temporary_quote_profile
from backend.l2.providers import ClickHouseL2Adapter
from backend.services import backtest_preflight, l2_quote_availability, state_store


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


def test_quote_availability_backfills_only_uncached_dates(monkeypatch: pytest.MonkeyPatch) -> None:
  writes: list[dict[str, Any]] = []
  monkeypatch.setattr(
    state_store,
    "list_l2_quote_daily_availability",
    lambda *_: {"2026-08-26": {"status": "complete", "snapshotCount": 10}},
  )
  monkeypatch.setattr(
    ClickHouseL2Adapter,
    "quote_daily_availability",
    lambda _, _symbol, dates: {
      item: {"snapshotCount": 20, "firstQuoteTime": "09:30:00", "lastQuoteTime": "14:56:57"}
      for item in dates
    },
  )
  monkeypatch.setattr(
    state_store,
    "upsert_l2_quote_daily_availability",
    lambda _source, _symbol, records: writes.extend(records),
  )

  result = l2_quote_availability.available_quote_dates("688981.SH", ["2026-08-26", "2026-08-27"])

  assert result == ["2026-08-26", "2026-08-27"]
  assert [item["tradeDate"] for item in writes] == ["2026-08-27"]
  assert writes[0]["status"] == "complete"


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


def test_l2_preview_reads_availability_once_for_all_timeframes(monkeypatch: pytest.MonkeyPatch) -> None:
  calls: list[list[str]] = []
  monkeypatch.setattr(state_store, "list_open_trade_dates", lambda *_: ["2026-08-26", "2026-08-27"])
  monkeypatch.setattr(state_store, "is_trade_calendar_range_covered", lambda *_: True)
  monkeypatch.setattr(backtest_preflight, "_shanghai_today", lambda: __import__("datetime").date(2026, 8, 27))
  monkeypatch.setattr(
    l2_quote_availability,
    "available_quote_dates",
    lambda _symbol, dates: calls.append(dates) or dates,
  )
  monkeypatch.setattr(
    backtest_preflight.bar_data,
    "derived_bar_available_dates",
    lambda *_: (_ for _ in ()).throw(AssertionError("L2 预览不应按周期查询派生 Bar")),
  )

  result = backtest_preflight.preview("688981.SH", "2026-08-26", "2026-08-27", "l2_quote", "5m")

  assert calls == [["2026-08-26", "2026-08-27"]]
  assert all(item["available"] for item in result["timeframes"])
