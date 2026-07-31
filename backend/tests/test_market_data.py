# -*- coding: utf-8 -*-
"""QMT ClickHouse 行情读取和派生周期测试。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from backend.services import market_data


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


def test_invalid_symbol_does_not_reach_clickhouse(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(market_data.clickhouse_client, "query_rows", lambda _: pytest.fail("不应查询 ClickHouse"))

  with pytest.raises(market_data.MarketDataError, match="标的格式无效"):
    market_data.list_dates("688981.SH' OR 1=1 --", "1m")
