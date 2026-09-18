from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from backend.services import shared_backtest_detail, state_store


class DetailCursor:
  def __init__(self, row: dict[str, Any] | None) -> None:
    self.row = row
    self.query = ""
    self.params: tuple[Any, ...] = ()

  def __enter__(self) -> "DetailCursor":
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...]) -> None:
    self.query = query
    self.params = params

  def fetchone(self) -> dict[str, Any] | None:
    return self.row


class DetailConnection:
  def __init__(self, cursor: DetailCursor) -> None:
    self.cursor_value = cursor

  def cursor(self) -> DetailCursor:
    return self.cursor_value


@contextmanager
def detail_db(connection: DetailConnection) -> Iterator[DetailConnection]:
  yield connection


def install_snapshot(
  monkeypatch: pytest.MonkeyPatch,
  snapshot: dict[str, Any],
  version: int = 1,
) -> DetailCursor:
  cursor = DetailCursor({
    "snapshot_schema_version": version,
    "snapshot_json": snapshot,
  })
  monkeypatch.setattr(
    state_store,
    "_db",
    lambda: detail_db(DetailConnection(cursor)),
  )
  return cursor


def test_result_page_is_stable_and_uses_explicit_item_whitelist(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  cursor = install_snapshot(monkeypatch, {
    "result": {
      "trades": [
        {"time": "t1", "price": 10, "quantity": 100, "accountId": "secret"},
        {"time": "t2", "price": 11, "quantity": 200, "requestKey": "secret"},
        {"time": "t3", "price": 12, "quantity": 300, "traceback": "secret"},
      ],
    },
  })

  first = shared_backtest_detail.get_shared_backtest_result_page(
    "publication-1", "trades", 0, 2,
  )
  second = shared_backtest_detail.get_shared_backtest_result_page(
    "publication-1", "trades", int(first["nextCursor"]), 2,
  )

  assert first == {
    "items": [
      {"time": "t1", "price": 10, "quantity": 100},
      {"time": "t2", "price": 11, "quantity": 200},
    ],
    "nextCursor": "2",
    "total": 3,
  }
  assert second["items"] == [{"time": "t3", "price": 12, "quantity": 300}]
  assert second["nextCursor"] is None
  assert "status='active'" in cursor.query
  assert cursor.params == ("publication-1",)


def test_chart_and_nested_values_remove_sensitive_fields(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  install_snapshot(monkeypatch, {
    "result": {
      "equity": [{
        "date": "2026-01-02",
        "value": 1_010_000,
        "pnl": 10_000,
        "storageKey": "secret",
      }],
      "benchmarkSymbol": "000300.SH",
      "benchmarkEquity": [{
        "date": "2026-01-02",
        "value": 1.01,
        "returnRate": 0.01,
        "internalPath": "C:/private",
      }],
    },
  })

  chart = shared_backtest_detail.get_shared_backtest_chart("publication-1")

  assert chart == {
    "equity": [{"date": "2026-01-02", "pnl": 10_000, "value": 1_010_000}],
    "benchmarkSymbol": "000300.SH",
    "benchmarkEquity": [{
      "date": "2026-01-02", "value": 1.01, "returnRate": 0.01,
    }],
  }
  serialized = str(chart)
  assert "storageKey" not in serialized
  assert "internalPath" not in serialized


def test_unknown_snapshot_version_fails_without_fallback(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  install_snapshot(monkeypatch, {"result": {"trades": []}}, version=2)

  with pytest.raises(ValueError, match="暂不支持共享快照版本: 2"):
    shared_backtest_detail.get_shared_backtest_chart("publication-1")


def test_lightweight_detail_defensively_filters_legacy_sensitive_fields() -> None:
  snapshot = {
    "strategy": {
      "code": "return []", "workerId": "secret", "unlistedDiagnostic": "secret",
    },
    "configuration": {
      "initialCapital": 100_000,
      "params": {"window": 20, "apiToken": "secret"},
      "accountName": "secret",
    },
    "data": {
      "marketData": {
        "provider": "qmt", "sourcePath": "C:/private",
        "formalDates": ["2026-01-02", "2026-01-05"],
      },
      "dependencies": [{
        "type": "research_signal",
        "summary": {
          "datasetId": "private-id", "rowCount": 12, "snapshotHash": "a" * 64,
          "alignmentPlan": [{"signalId": "private-signal"}],
        },
      }],
    },
    "result": {
      "summary": {"totalReturn": 0.1, "requestId": "secret"},
      "warnings": ["样本较少"],
      "trades": [{"accountId": "secret"}],
    },
    "origin": {"publicationId": "origin-1", "ownerUserId": "secret"},
  }
  row = {
    "id": "publication-1",
    "title": "共享策略",
    "description": "说明",
    "author_name_snapshot": "作者",
    "tags": [],
    "symbol": "688981.SH",
    "timeframe": "5m",
    "strategy_category": "directional_holding",
    "start_date": "2026-01-01",
    "end_date": "2026-01-31",
    "total_return": 0.1,
    "max_drawdown_rate": 0.02,
    "annual_sharpe": 1.2,
    "copy_count": 0,
    "first_published_at": datetime(2026, 9, 15, tzinfo=timezone.utc),
    "snapshot_schema_version": 1,
    "snapshot_digest": state_store._shared_snapshot_digest(snapshot),
    "snapshot_json": snapshot,
  }

  detail = state_store._publication_detail(row)
  serialized = str(detail)

  assert detail["configuration"]["params"] == {"window": 20}
  assert detail["result"] == {
    "summary": {"totalReturn": 0.1},
    "warnings": ["样本较少"],
  }
  assert detail["data"] == {
    "marketData": {
      "provider": "qmt",
      "formalDateRange": {"start": "2026-01-02", "end": "2026-01-05", "count": 2},
    },
    "resultData": {},
    "dependencies": [{
      "type": "research_signal",
      "summary": {"rowCount": 12, "snapshotHash": "a" * 64},
    }],
  }
  for forbidden in (
    "workerId", "unlistedDiagnostic", "apiToken", "accountName", "sourcePath", "requestId",
    "ownerUserId", "trades", "datasetId", "alignmentPlan", "private-signal",
  ):
    assert forbidden not in serialized


@pytest.mark.parametrize("cursor,limit", [(-1, 20), (0, 0), (0, 201)])
def test_result_page_rejects_invalid_pagination(
  monkeypatch: pytest.MonkeyPatch,
  cursor: int,
  limit: int,
) -> None:
  install_snapshot(monkeypatch, {"result": {}})

  with pytest.raises(ValueError, match="分页参数无效"):
    shared_backtest_detail.get_shared_backtest_result_page(
      "publication-1", "holdings", cursor, limit,
    )
