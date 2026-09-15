from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from backend import app as backend_app
from backend.services import shared_backtest_lifecycle


def publication_row(status: str = "active") -> dict[str, Any]:
  published_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
  return {
    "id": "publication-1",
    "title": "原共享标题",
    "description": "原策略说明",
    "author_name_snapshot": "作者快照",
    "tags": ["均线"],
    "status": status,
    "symbol": "688981.SH",
    "timeframe": "5m",
    "strategy_category": "directional_holding",
    "start_date": "2026-01-01",
    "end_date": "2026-01-31",
    "total_return": 0.12,
    "max_drawdown_rate": 0.04,
    "annual_sharpe": 1.5,
    "copy_count": 7,
    "first_published_at": published_at,
    "withdrawn_at": published_at if status == "withdrawn" else None,
    "removed_at": published_at if status == "removed" else None,
    "removal_reason": "测试下架原因" if status == "removed" else None,
    "updated_at": published_at,
    "source_run_id": None,
  }


class LifecycleCursor:
  def __init__(self, rows: list[dict[str, Any]]) -> None:
    self.rows = rows
    self.queries: list[tuple[str, Any]] = []
    self.one: dict[str, Any] | None = None

  def __enter__(self) -> "LifecycleCursor":
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def execute(self, query: str, params: Any = ()) -> None:
    self.queries.append((query, params))
    if "count(*)" in query:
      self.one = {"value": len(self.rows)}
    elif "app.update_backtest_publication_metadata" in query:
      updated = {**self.rows[0], "title": params[2], "description": params[3], "tags": params[4].obj}
      self.one = {"publication": updated}

  def fetchone(self) -> dict[str, Any]:
    assert self.one is not None
    return self.one

  def fetchall(self) -> list[dict[str, Any]]:
    return self.rows


class LifecycleConnection:
  def __init__(self, cursor: LifecycleCursor) -> None:
    self.cursor_value = cursor

  def cursor(self) -> LifecycleCursor:
    return self.cursor_value


@contextmanager
def lifecycle_db(connection: LifecycleConnection) -> Iterator[LifecycleConnection]:
  yield connection


def test_mine_lists_all_statuses_under_current_owner(monkeypatch: Any) -> None:
  rows = [publication_row(status) for status in ("active", "withdrawn", "removed")]
  cursor = LifecycleCursor(rows)
  monkeypatch.setattr(
    shared_backtest_lifecycle.state_store,
    "_db",
    lambda: lifecycle_db(LifecycleConnection(cursor)),
  )
  monkeypatch.setattr(
    shared_backtest_lifecycle.state_store,
    "current_user_id",
    lambda: "owner-1",
  )

  page = shared_backtest_lifecycle.list_my_shared_backtests(limit=20, offset=0)

  assert [item["status"] for item in page["items"]] == ["active", "withdrawn", "removed"]
  assert page["items"][0]["sourceRunAvailable"] is False
  assert page["items"][2]["removalReason"] == "测试下架原因"
  assert all("owner_user_id=%s" in query for query, _params in cursor.queries)
  assert cursor.queries[0][1] == ["owner-1"]


def test_metadata_update_only_passes_editable_fields(monkeypatch: Any) -> None:
  cursor = LifecycleCursor([publication_row()])
  monkeypatch.setattr(
    shared_backtest_lifecycle.state_store,
    "_db",
    lambda: lifecycle_db(LifecycleConnection(cursor)),
  )
  monkeypatch.setattr(
    shared_backtest_lifecycle.state_store,
    "current_user_id",
    lambda: "owner-1",
  )

  updated = shared_backtest_lifecycle.update_publication_metadata(
    "publication-1", "新标题", "新说明", ["新标签"],
  )

  assert updated["title"] == "新标题"
  assert updated["copyCount"] == 7
  query, params = cursor.queries[0]
  assert "app.update_backtest_publication_metadata" in query
  assert params[:4] == ("publication-1", "owner-1", "新标题", "新说明")


@pytest.mark.parametrize(
  ("endpoint", "service_name", "status"),
  [
    (backend_app.withdraw_shared_backtest, "withdraw_publication", "withdrawn"),
    (backend_app.relist_shared_backtest, "relist_publication", "active"),
  ],
)
def test_author_transition_endpoints(
  monkeypatch: Any,
  endpoint: Any,
  service_name: str,
  status: str,
) -> None:
  item = shared_backtest_lifecycle._management_item(publication_row(status))
  monkeypatch.setattr(
    backend_app.shared_backtest_lifecycle,
    service_name,
    lambda _publication_id: item,
  )

  assert endpoint("publication-1") == {"publication": item}


def test_author_transition_endpoint_rejects_invalid_state(monkeypatch: Any) -> None:
  def invalid(_publication_id: str) -> dict[str, Any]:
    raise ValueError("已下架的共享回测不能由作者更改状态")

  monkeypatch.setattr(
    backend_app.shared_backtest_lifecycle,
    "relist_publication",
    invalid,
  )

  with pytest.raises(HTTPException) as error:
    backend_app.relist_shared_backtest("publication-1")

  assert error.value.status_code == 409


def test_lifecycle_migration_keeps_privileged_updates_narrow() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "034_shared_backtest_author_lifecycle.sql"
  ).read_text(encoding="utf-8")

  assert "SECURITY DEFINER" in migration
  assert "owner_user_id = p_actor_user_id" in migration
  assert "AND status = 'active'" in migration
  assert "AND status = 'withdrawn'" in migration
  assert "first_published_at =" not in migration
  assert "snapshot_json =" not in migration
  assert "GRANT UPDATE ON backtest_publication" not in migration
  assert "FROM PUBLIC" in migration


def test_publication_foreign_keys_keep_snapshot_after_source_or_owner_delete() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "030_shared_backtest_publications.sql"
  ).read_text(encoding="utf-8")

  assert "owner_user_id uuid REFERENCES app_user(id) ON DELETE SET NULL" in migration
  assert "source_run_id uuid REFERENCES backtest_run(id) ON DELETE SET NULL" in migration
