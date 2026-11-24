from __future__ import annotations

from typing import Any

import pytest

from backend.services import state_store


class FakeCursor:
  def __init__(self, rows: list[dict[str, Any] | None]) -> None:
    self.rows = rows
    self.statements: list[str] = []
    self.params: list[Any] = []

  def __enter__(self) -> "FakeCursor":
    return self

  def __exit__(self, *_: Any) -> None:
    return None

  def execute(self, statement: str, params: Any = None) -> None:
    self.statements.append(" ".join(statement.split()))
    self.params.append(params)

  def fetchone(self) -> dict[str, Any] | None:
    return self.rows.pop(0)


class FakeConnection:
  def __init__(self, cursor: FakeCursor) -> None:
    self.cursor_instance = cursor
    self.exit_error: type[BaseException] | None = None

  def __enter__(self) -> "FakeConnection":
    return self

  def __exit__(
    self,
    error_type: type[BaseException] | None,
    _error: BaseException | None,
    _traceback: Any,
  ) -> None:
    self.exit_error = error_type

  def cursor(self) -> FakeCursor:
    return self.cursor_instance


def _install_database(
  monkeypatch: pytest.MonkeyPatch,
  rows: list[dict[str, Any] | None],
) -> tuple[FakeCursor, FakeConnection]:
  cursor = FakeCursor(rows)
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: connection)
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")
  return cursor, connection


def test_browser_backtest_first_request_freezes_idempotency_metadata(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  cursor, _connection = _install_database(monkeypatch, [None, None])
  captured: dict[str, Any] = {}

  def create_job(
    _cur: Any,
    payload: dict[str, Any],
    owner_user_id: str,
    queue_table: str,
    fill_model: str,
    *,
    request_key: str | None = None,
  ) -> tuple[str, int]:
    captured.update(
      payload=payload,
      owner_user_id=owner_user_id,
      queue_table=queue_table,
      fill_model=fill_model,
      request_key=request_key,
    )
    return "run-1", 7

  monkeypatch.setattr(state_store, "_create_queued_backtest_job", create_job)

  result = state_store.create_browser_backtest_job(
    {"fillModel": "bar_next_open"}, "browser-12345678", "a" * 64,
  )

  assert result == ("run-1", 7, False, "queued", "bar_next_open")
  assert captured["owner_user_id"] == "user-1"
  assert captured["queue_table"] == "bar_backtest_job"
  assert captured["payload"]["_browserIdempotency"] == {
    "key": "browser-12345678",
    "requestHash": "a" * 64,
  }
  assert captured["request_key"] == state_store._browser_backtest_request_key(
    "browser-12345678",
  )
  assert "request_key=%s" in cursor.statements[1]
  assert "ORDER BY created_at" not in " ".join(cursor.statements)


def test_browser_backtest_replays_existing_task(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _install_database(monkeypatch, [{
    "id": "run-1",
    "display_no": 7,
    "status": "queued",
    "fill_model": "l2_quote",
    "request_hash": "a" * 64,
  }])

  result = state_store.create_browser_backtest_job(
    {"fillModel": "l2_quote"}, "browser-12345678", "a" * 64,
  )

  assert result == ("run-1", 7, True, "queued", "l2_quote")


def test_browser_backtest_rejects_idempotency_key_conflict(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _install_database(monkeypatch, [{
    "id": "run-1",
    "display_no": 7,
    "status": "queued",
    "fill_model": "bar_next_open",
    "request_hash": "b" * 64,
  }])

  with pytest.raises(state_store.IdempotencyConflictError):
    state_store.create_browser_backtest_job(
      {"fillModel": "bar_next_open"}, "browser-12345678", "a" * 64,
    )


def test_browser_backtest_replays_legacy_json_idempotency_key(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  cursor, _connection = _install_database(monkeypatch, [None, {
    "id": "run-legacy",
    "display_no": 8,
    "status": "succeeded",
    "fill_model": "bar_next_open",
    "request_hash": "a" * 64,
  }])

  result = state_store.create_browser_backtest_job(
    {"fillModel": "bar_next_open"}, "browser-12345678", "a" * 64,
  )

  assert result == ("run-legacy", 8, True, "succeeded", "bar_next_open")
  assert "request_key=%s" in cursor.statements[1]
  assert "requested_params->'_browserIdempotency'->>'key'=%s" in cursor.statements[2]
  assert "ORDER BY created_at" not in cursor.statements[2]
