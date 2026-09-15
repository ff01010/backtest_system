from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from backend import app as backend_app
from backend.services import shared_backtest_admin


def publication_row(status: str = "active") -> dict[str, Any]:
  published_at = datetime(2026, 9, 15, tzinfo=timezone.utc)
  return {
    "id": "publication-1",
    "title": "共享策略",
    "description": "策略说明",
    "author_name_snapshot": "测试作者",
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
    "withdrawn_at": None,
    "removed_at": published_at if status == "removed" else None,
    "removed_by_user_id": "admin-1" if status == "removed" else None,
    "removal_reason": "测试下架原因" if status == "removed" else None,
    "updated_at": published_at,
    "source_run_id": "run-1",
  }


class AdminCursor:
  def __init__(self, row: dict[str, Any]) -> None:
    self.row = row
    self.queries: list[tuple[str, Any]] = []
    self.one: dict[str, Any] | None = None

  def __enter__(self) -> "AdminCursor":
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def execute(self, query: str, params: Any = ()) -> None:
    self.queries.append((query, params))
    if "count(*)" in query:
      self.one = {"value": 1}
    elif "app.remove_backtest_publication" in query:
      self.row = {
        **self.row,
        "status": "removed",
        "removed_at": self.row["first_published_at"],
        "removed_by_user_id": params[1],
        "removal_reason": params[2],
      }
      self.one = {"publication": self.row}
    elif "app.restore_backtest_publication" in query:
      self.row = {**self.row, "status": "active"}
      self.one = {"publication": self.row}

  def fetchone(self) -> dict[str, Any]:
    assert self.one is not None
    return self.one

  def fetchall(self) -> list[dict[str, Any]]:
    return [self.row]


class AdminConnection:
  def __init__(self, cursor: AdminCursor) -> None:
    self.cursor_value = cursor

  def cursor(self) -> AdminCursor:
    return self.cursor_value


@contextmanager
def admin_db(connection: AdminConnection) -> Iterator[AdminConnection]:
  yield connection


def use_admin_context(monkeypatch: Any, cursor: AdminCursor) -> None:
  monkeypatch.setattr(
    shared_backtest_admin.state_store,
    "_db",
    lambda: admin_db(AdminConnection(cursor)),
  )
  monkeypatch.setattr(
    shared_backtest_admin.state_store,
    "current_user_is_admin",
    lambda: True,
  )
  monkeypatch.setattr(
    shared_backtest_admin.state_store,
    "current_user_id",
    lambda: "admin-1",
  )


def test_admin_list_uses_management_projection_for_all_owners(monkeypatch: Any) -> None:
  cursor = AdminCursor(publication_row("removed"))
  use_admin_context(monkeypatch, cursor)

  page = shared_backtest_admin.list_shared_backtests_for_admin(status="removed")

  assert page["items"][0]["removalReason"] == "测试下架原因"
  assert page["items"][0]["authorName"] == "测试作者"
  assert "snapshot_json" not in page["items"][0]
  assert all("owner_user_id" not in query for query, _params in cursor.queries)
  assert cursor.queries[0][1] == ["removed"]


def test_admin_removes_active_publication_with_required_reason(monkeypatch: Any) -> None:
  cursor = AdminCursor(publication_row())
  use_admin_context(monkeypatch, cursor)

  item = shared_backtest_admin.remove_publication("publication-1", "  风险说明不完整  ")

  assert item["status"] == "removed"
  assert item["removalReason"] == "风险说明不完整"
  query, params = cursor.queries[0]
  assert "app.remove_backtest_publication" in query
  assert params == ("publication-1", "admin-1", "风险说明不完整")


def test_admin_restores_removed_publication_without_resetting_statistics(monkeypatch: Any) -> None:
  cursor = AdminCursor(publication_row("removed"))
  use_admin_context(monkeypatch, cursor)

  item = shared_backtest_admin.restore_publication("publication-1")

  assert item["status"] == "active"
  assert item["copyCount"] == 7
  assert item["firstPublishedAt"] == "2026-09-15 08:00:00"
  assert "app.restore_backtest_publication" in cursor.queries[0][0]


def test_non_admin_context_cannot_call_admin_service(monkeypatch: Any) -> None:
  monkeypatch.setattr(
    shared_backtest_admin.state_store,
    "current_user_is_admin",
    lambda: False,
  )

  with pytest.raises(PermissionError, match="仅管理员"):
    shared_backtest_admin.remove_publication("publication-1", "原因")


def test_remove_request_rejects_blank_reason() -> None:
  with pytest.raises(ValidationError, match="下架原因不能为空"):
    backend_app.schemas.SharedBacktestRemovalRequest(reason="   ")


def test_admin_endpoint_requires_admin_before_mutation(monkeypatch: Any) -> None:
  monkeypatch.setattr(
    backend_app,
    "_require_admin",
    lambda _request: (_ for _ in ()).throw(HTTPException(403, "仅管理员可执行此操作")),
  )
  called = False

  def remove(*_args: object) -> dict[str, Any]:
    nonlocal called
    called = True
    return {}

  monkeypatch.setattr(backend_app.shared_backtest_admin, "remove_publication", remove)
  payload = backend_app.schemas.SharedBacktestRemovalRequest(reason="原因")

  with pytest.raises(HTTPException) as error:
    backend_app.admin_remove_shared_backtest("publication-1", payload, object())

  assert error.value.status_code == 403
  assert called is False


def test_public_detail_uses_uniform_unavailable_response_and_no_store(monkeypatch: Any) -> None:
  monkeypatch.setattr(
    backend_app.state_store,
    "get_shared_backtest",
    lambda _publication_id: (_ for _ in ()).throw(KeyError("hidden")),
  )
  response = Response()

  with pytest.raises(HTTPException) as error:
    backend_app.shared_backtest_detail("unknown-or-inactive", response)

  assert error.value.status_code == 404
  assert error.value.detail == "该共享回测已不可用"
  assert response.headers["Cache-Control"] == "private, no-store"


def test_admin_migration_keeps_write_permission_narrow() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "035_shared_backtest_admin_lifecycle.sql"
  ).read_text(encoding="utf-8")

  assert "app.current_is_admin()" in migration
  assert "p_actor_user_id IS DISTINCT FROM app.current_user_id()" in migration
  assert "WHERE id = p_publication_id AND status = 'active'" in migration
  assert "WHERE id = p_publication_id AND status = 'removed'" in migration
  assert "removed_by_user_id = p_actor_user_id" in migration
  assert "removal_reason = btrim(p_reason)" in migration
  assert "GRANT UPDATE ON backtest_publication" not in migration
  assert "REVOKE ALL ON FUNCTION" in migration
