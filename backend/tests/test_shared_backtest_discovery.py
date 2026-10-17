from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from backend import app as backend_app
from backend.services import shared_backtest_discovery


def publication_row(**overrides: Any) -> dict[str, Any]:
  row = {
    "id": "00000000-0000-0000-0000-000000000101",
    "title": "均线策略",
    "author_name_snapshot": "测试作者",
    "tags": ["均线", "趋势"],
    "symbol": "688981.SH",
    "timeframe": "5m",
    "strategy_category": "directional_holding",
    "start_date": "2026-01-01",
    "end_date": "2026-01-31",
    "total_return": 0.12,
    "max_drawdown_rate": 0.04,
    "annual_sharpe": 1.5,
    "copy_count": 3,
    "first_published_at": datetime(2026, 9, 15, tzinfo=timezone.utc),
    "is_favorited": True,
    "has_copied": True,
  }
  row.update(overrides)
  return row


class ListCursor:
  def __init__(self, rows: list[dict[str, Any]]) -> None:
    self.rows = rows
    self.executions: list[tuple[str, tuple[Any, ...]]] = []

  def __enter__(self) -> "ListCursor":
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
    self.executions.append((query, params))

  def fetchone(self) -> dict[str, Any]:
    return {"value": len(self.rows)}

  def fetchall(self) -> list[dict[str, Any]]:
    return self.rows


class WriteCursor:
  def __init__(self, publication_exists: bool = True, affected: int = 1) -> None:
    self.publication_exists = publication_exists
    self.affected = affected
    self.rowcount = 0
    self.executions: list[tuple[str, tuple[Any, ...]]] = []

  def __enter__(self) -> "WriteCursor":
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
    self.executions.append((query, params))
    if "INSERT INTO" in query or "DELETE FROM" in query:
      self.rowcount = self.affected

  def fetchone(self) -> dict[str, Any] | None:
    return {"id": "publication-1"} if self.publication_exists else None


class Connection:
  def __init__(self, cursor: ListCursor | WriteCursor) -> None:
    self.cursor_value = cursor

  def cursor(self) -> ListCursor | WriteCursor:
    return self.cursor_value


@contextmanager
def fake_db(connection: Connection) -> Iterator[Connection]:
  yield connection


def normalized(query: str) -> str:
  return " ".join(query.split())


def test_detail_uses_existing_copy_relation_for_viewer_state(monkeypatch: Any) -> None:
  row = publication_row(
    description="策略说明",
    snapshot_schema_version=1,
    snapshot_digest="a" * 64,
    snapshot_json={
      "strategy": {}, "configuration": {}, "data": {}, "result": {}, "origin": {},
    },
  )

  class DetailCursor(ListCursor):
    def fetchone(self) -> dict[str, Any]:
      return row

  cursor = DetailCursor([])
  monkeypatch.setattr(shared_backtest_discovery.state_store, "current_user_id", lambda: "viewer-1")
  monkeypatch.setattr(
    shared_backtest_discovery.state_store,
    "_db",
    lambda: fake_db(Connection(cursor)),
  )

  detail = shared_backtest_discovery.state_store.get_shared_backtest("publication-1")

  query, params = cursor.executions[0]
  assert "LEFT JOIN backtest_publication_copy copied" in normalized(query)
  assert params == ("viewer-1", "viewer-1", "publication-1")
  assert detail["isFavorited"] is True
  assert detail["hasCopied"] is True


def test_list_search_filters_favorites_and_uses_stable_copy_order(monkeypatch: Any) -> None:
  cursor = ListCursor([publication_row()])
  monkeypatch.setattr(shared_backtest_discovery.state_store, "current_user_id", lambda: "viewer-1")
  monkeypatch.setattr(
    shared_backtest_discovery.state_store,
    "_db",
    lambda: fake_db(Connection(cursor)),
  )

  page = shared_backtest_discovery.list_shared_backtests(
    query="均线",
    strategy_category="directional_holding",
    timeframe="5m",
    sort="copies",
    favorites_only=True,
    limit=12,
    offset=24,
  )

  search_queries = [
    execution for execution in cursor.executions
    if not execution[0].startswith("SET LOCAL")
  ]
  count_sql, count_params = search_queries[0]
  list_sql, list_params = search_queries[1]
  sql = normalized(list_sql)
  assert "p.status='active'" in sql
  assert "p.search_text_v1 ILIKE %s" in sql
  assert "jsonb_array_elements_text(p.tags)" not in sql
  assert "favorite.user_id IS NOT NULL" in sql
  assert "p.strategy_category=%s" in sql and "p.timeframe=%s" in sql
  assert "ORDER BY p.copy_count DESC, p.first_published_at DESC, p.id DESC" in sql
  assert count_params == (
    "viewer-1", "viewer-1", "%均线%", "directional_holding", "5m",
  )
  assert list_params == (*count_params, 12, 24)
  assert "snapshot_json" not in page["items"][0]
  assert page["items"][0]["isFavorited"] is True
  assert page["items"][0]["hasCopied"] is True
  assert "count(*)" in count_sql


def test_latest_order_has_id_tie_breaker(monkeypatch: Any) -> None:
  cursor = ListCursor([])
  monkeypatch.setattr(shared_backtest_discovery.state_store, "current_user_id", lambda: "viewer-1")
  monkeypatch.setattr(
    shared_backtest_discovery.state_store,
    "_db",
    lambda: fake_db(Connection(cursor)),
  )

  shared_backtest_discovery.list_shared_backtests(sort="latest")

  search_queries = [
    execution for execution in cursor.executions
    if not execution[0].startswith("SET LOCAL")
  ]
  assert "ORDER BY p.first_published_at DESC, p.id DESC" in normalized(
    search_queries[1][0],
  )


@pytest.mark.parametrize("affected, expected", [(1, True), (0, False)])
def test_favorite_is_idempotent(monkeypatch: Any, affected: int, expected: bool) -> None:
  cursor = WriteCursor(affected=affected)
  monkeypatch.setattr(shared_backtest_discovery.state_store, "current_user_id", lambda: "viewer-1")
  monkeypatch.setattr(
    shared_backtest_discovery.state_store,
    "_db",
    lambda: fake_db(Connection(cursor)),
  )

  assert shared_backtest_discovery.favorite_shared_backtest("publication-1") is expected
  assert "ON CONFLICT (publication_id, user_id) DO NOTHING" in cursor.executions[1][0]


def test_inactive_publication_cannot_be_newly_favorited(monkeypatch: Any) -> None:
  cursor = WriteCursor(publication_exists=False)
  monkeypatch.setattr(shared_backtest_discovery.state_store, "current_user_id", lambda: "viewer-1")
  monkeypatch.setattr(
    shared_backtest_discovery.state_store,
    "_db",
    lambda: fake_db(Connection(cursor)),
  )

  with pytest.raises(KeyError):
    shared_backtest_discovery.favorite_shared_backtest("publication-1")

  assert len(cursor.executions) == 1


def test_unfavorite_does_not_require_publication_to_remain_active(monkeypatch: Any) -> None:
  cursor = WriteCursor(publication_exists=False, affected=1)
  monkeypatch.setattr(shared_backtest_discovery.state_store, "current_user_id", lambda: "viewer-1")
  monkeypatch.setattr(
    shared_backtest_discovery.state_store,
    "_db",
    lambda: fake_db(Connection(cursor)),
  )

  assert shared_backtest_discovery.unfavorite_shared_backtest("publication-1") is True
  assert len(cursor.executions) == 1
  assert "DELETE FROM backtest_publication_favorite" in cursor.executions[0][0]


def test_favorite_endpoint_hides_unavailable_reason(monkeypatch: Any) -> None:
  def unavailable(_publication_id: str) -> bool:
    raise KeyError("publication-1")

  monkeypatch.setattr(
    backend_app.shared_backtest_discovery,
    "favorite_shared_backtest",
    unavailable,
  )

  with pytest.raises(HTTPException) as error:
    backend_app.favorite_shared_backtest("publication-1")

  assert error.value.status_code == 404
  assert error.value.detail == "该共享回测已不可用"


def test_favorite_migration_preserves_relationships_across_status_changes() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "031_shared_backtest_favorites.sql"
  ).read_text(encoding="utf-8")

  assert "PRIMARY KEY (publication_id, user_id)" in migration
  assert "ENABLE ROW LEVEL SECURITY" in migration
  assert "FORCE ROW LEVEL SECURITY" in migration
  assert "backtest_publication_favorite_create" in migration
  assert "publication.status='active'" in migration
  assert "ON UPDATE CASCADE" not in migration
  assert "DELETE FROM backtest_publication_favorite" not in migration
