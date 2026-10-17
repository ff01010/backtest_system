from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from backend import app as backend_app
from backend.services import state_store


class HistoryCursor:
  def __init__(self, rows: list[dict[str, Any]], total: int) -> None:
    self.rows = rows
    self.total = total
    self.last_query = ""
    self.commands: list[tuple[str, tuple[Any, ...]]] = []

  def __enter__(self) -> "HistoryCursor":
    return self

  def __exit__(self, *args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...]) -> None:
    self.last_query = query
    self.commands.append((query, params))

  def fetchone(self) -> dict[str, int]:
    if "count(*) AS total" in self.last_query:
      return {"total": self.total, "selectable_total": self.total}
    return {"value": self.total}

  def fetchall(self) -> list[dict[str, Any]]:
    if "SELECT DISTINCT" in self.last_query:
      return [{"symbol": "000001.SZ"}, {"symbol": "688981.SH"}]
    return self.rows


class HistoryConnection:
  def __init__(self, cursor: HistoryCursor) -> None:
    self.cursor_value = cursor

  def cursor(self) -> HistoryCursor:
    return self.cursor_value


class FavoriteCursor:
  def __init__(self, owned: bool = True) -> None:
    self.owned = owned
    self.commands: list[tuple[str, tuple[Any, ...]]] = []

  def __enter__(self) -> "FavoriteCursor":
    return self

  def __exit__(self, *args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...]) -> None:
    self.commands.append((query, params))

  def fetchone(self) -> dict[str, datetime | None] | None:
    if not self.owned:
      return None
    favorited = bool(self.commands[-1][1][0])
    return {
      "favorited_at": datetime(2026, 9, 16, tzinfo=timezone.utc) if favorited else None,
    }


class FavoriteConnection:
  def __init__(self, cursor: FavoriteCursor) -> None:
    self.cursor_value = cursor

  def cursor(self) -> FavoriteCursor:
    return self.cursor_value


@contextmanager
def history_db(connection: HistoryConnection) -> Iterator[HistoryConnection]:
  yield connection


def make_run_row(display_no: int) -> dict[str, Any]:
  return {
    "id": f"run-{display_no}",
    "display_no": display_no,
    "requested_params": {
      "strategy": "user_script",
      "symbol": "688981.SH",
      "startDate": "2026-01-01",
      "endDate": "2026-01-31",
    },
    "status": "succeeded",
    "symbol": "688981.SH",
    "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
  }


def test_history_page_exposes_records_after_the_first_thirty(monkeypatch: Any) -> None:
  rows = [make_run_row(display_no) for display_no in range(5, 0, -1)]
  cursor = HistoryCursor(rows, total=65)
  connection = HistoryConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: history_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  page = state_store.list_backtest_run_page(limit=20, offset=60)

  assert [run["displayNo"] for run in page["runs"]] == [5, 4, 3, 2, 1]
  assert page["total"] == 65
  assert page["selectableTotal"] == 65
  assert page["limit"] == 20
  assert page["offset"] == 60
  assert page["symbols"] == ["000001.SZ", "688981.SH"]


def test_history_page_filters_the_complete_user_history(monkeypatch: Any) -> None:
  cursor = HistoryCursor([make_run_row(42)], total=1)
  connection = HistoryConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: history_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  page = state_store.list_backtest_run_page(
    limit=10,
    offset=0,
    query="候选",
    symbol="688981.SH",
    config_id="config-1",
  )

  assert [run["displayNo"] for run in page["runs"]] == [42]
  search_commands = [item for item in cursor.commands if not item[0].startswith("SET LOCAL")]
  count_query, count_params = search_commands[0]
  page_query, page_params = search_commands[1]
  assert "r.owner_user_id=%s" in count_query
  assert "r.config_id=%s" in count_query
  assert "r.search_text_v1 ILIKE %s" in count_query
  assert "LEFT JOIN strategy_config" not in count_query
  assert "LEFT JOIN market_instrument" not in count_query
  assert count_params == (
    "user-1", "config-1", "688981.SH", "%候选%",
    ["succeeded", "failed", "cancelled"],
  )
  assert "count(*) FILTER" in count_query
  assert "ORDER BY r.created_at DESC, r.id DESC" in page_query
  assert page_params == ("user-1", "config-1", "688981.SH", "%候选%", 11)


def test_history_page_filters_and_orders_favorites(monkeypatch: Any) -> None:
  cursor = HistoryCursor([make_run_row(42)], total=1)
  connection = HistoryConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: history_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  state_store.list_backtest_run_page(limit=10, offset=0, favorites_only=True)

  search_commands = [item for item in cursor.commands if not item[0].startswith("SET LOCAL")]
  count_query, _ = search_commands[0]
  page_query, _ = search_commands[1]
  assert "r.favorited_at IS NOT NULL" in count_query
  assert "count(*) FILTER" in count_query
  assert "r.favorited_at IS NOT NULL" in page_query
  assert "ORDER BY r.favorited_at DESC, r.id DESC" in page_query


def test_history_endpoint_returns_pagination_metadata(monkeypatch: Any) -> None:
  expected = {
    "runs": [make_run_row(25)],
    "total": 65,
    "selectableTotal": 60,
    "limit": 20,
    "offset": 40,
    "symbols": ["688981.SH"],
  }
  monkeypatch.setattr(
    backend_app.state_store,
    "list_backtest_run_page",
    lambda **_kwargs: expected,
  )

  response = backend_app.backtest_runs(
    limit=20,
    offset=40,
    query="候选",
    symbol="688981.SH",
    config_id=None,
    favorites_only=False,
  )

  assert response == expected


def test_history_endpoint_maps_database_timeout_to_structured_error(
  monkeypatch: Any,
) -> None:
  def time_out(**_kwargs: Any) -> dict[str, Any]:
    raise state_store.SearchTimeoutError("搜索耗时过长，请缩小条件后重试")

  monkeypatch.setattr(backend_app.state_store, "list_backtest_run_page", time_out)

  with pytest.raises(backend_app.HTTPException) as captured:
    backend_app.backtest_runs(
      limit=20,
      offset=0,
      cursor=None,
      query="候选",
      symbol=None,
      config_id=None,
      favorites_only=False,
      include_totals=True,
      include_filter_options=False,
    )

  assert captured.value.status_code == 504
  assert captured.value.detail == {
    "code": "search_timeout",
    "message": "搜索耗时过长，请缩小条件后重试",
  }


def test_lightweight_option_endpoint_forwards_cursor(monkeypatch: Any) -> None:
  expected = {"items": [], "nextCursor": None, "limit": 20}
  calls: list[dict[str, Any]] = []

  def list_options(**kwargs: Any) -> dict[str, Any]:
    calls.append(kwargs)
    return expected

  monkeypatch.setattr(
    backend_app.state_store,
    "list_backtest_run_options",
    list_options,
  )

  response = backend_app.backtest_run_options(
    query="#42",
    limit=20,
    cursor="opaque-cursor",
  )

  assert response == expected
  assert calls == [{"query": "#42", "limit": 20, "cursor": "opaque-cursor"}]


def test_history_search_accepts_the_displayed_number_prefix(monkeypatch: Any) -> None:
  cursor = HistoryCursor([make_run_row(42)], total=1)
  connection = HistoryConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: history_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  state_store.list_backtest_run_page(limit=10, offset=0, query="#42")

  count_query, count_params = next(
    item for item in cursor.commands if "count(*) AS total" in item[0]
  )
  assert "r.display_no=%s" in count_query
  assert "ILIKE" not in count_query
  assert count_params[:2] == ("user-1", 42)


def test_history_search_accepts_a_zero_padded_display_number(monkeypatch: Any) -> None:
  cursor = HistoryCursor([make_run_row(1)], total=1)
  connection = HistoryConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: history_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  state_store.list_backtest_run_page(limit=10, offset=0, query="#01")

  count_query, count_params = next(
    item for item in cursor.commands if "count(*) AS total" in item[0]
  )
  assert "r.display_no=%s" in count_query
  assert "ILIKE" not in count_query
  assert count_params[:2] == ("user-1", 1)


def test_six_digit_search_uses_two_exact_union_branches() -> None:
  spec = state_store.parse_backtest_run_search("688981")

  query, params = state_store.build_backtest_run_candidate_query(
    spec,
    "user-1",
    favorites_only=True,
  )

  assert " UNION " in query
  assert "r.display_no=%s" in query
  assert "upper(COALESCE(r.requested_params->>'symbol', ''))=ANY(%s)" in query
  assert "ILIKE" not in query
  assert params == (
    "user-1", 688981,
    "user-1", ["688981.SH", "688981.SZ", "688981.BJ"],
  )


def test_later_cursor_page_skips_totals_and_filter_options(monkeypatch: Any) -> None:
  cursor = HistoryCursor([make_run_row(42)], total=65)
  connection = HistoryConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: history_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  page = state_store.list_backtest_run_page(
    limit=10,
    offset=0,
    include_totals=False,
    include_filter_options=False,
  )

  search_commands = [item for item in cursor.commands if not item[0].startswith("SET LOCAL")]
  assert len(search_commands) == 1
  assert "count(*)" not in search_commands[0][0]
  assert "SELECT DISTINCT" not in search_commands[0][0]
  assert page["total"] is None
  assert page["selectableTotal"] is None
  assert page["symbols"] == []


@pytest.mark.parametrize(("favorited", "expected"), [(True, True), (False, False)])
def test_set_backtest_run_favorite_is_idempotent_and_owner_scoped(
  monkeypatch: Any,
  favorited: bool,
  expected: bool,
) -> None:
  cursor = FavoriteCursor()
  connection = FavoriteConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: history_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  result = state_store.set_backtest_run_favorite("run-1", favorited)

  query, params = cursor.commands[0]
  assert "COALESCE(favorited_at, now())" in query
  assert "owner_user_id=%s" in query
  assert params == (favorited, "run-1", "user-1")
  assert result["isFavorited"] is expected
  assert (result["favoritedAt"] is not None) is expected


def test_set_backtest_run_favorite_hides_unowned_run(monkeypatch: Any) -> None:
  cursor = FavoriteCursor(owned=False)
  connection = FavoriteConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: history_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(KeyError):
    state_store.set_backtest_run_favorite("run-2", True)
