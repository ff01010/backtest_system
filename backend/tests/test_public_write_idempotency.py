from __future__ import annotations

from typing import Any

import pytest

from backend.services import state_store


class FakeCursor:
  def __init__(self, rows: list[dict[str, Any] | None]) -> None:
    self.rows = rows
    self.statements: list[str] = []

  def __enter__(self) -> "FakeCursor":
    return self

  def __exit__(self, *_: Any) -> None:
    return None

  def execute(self, statement: str, _: Any = None) -> None:
    self.statements.append(" ".join(statement.split()))

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


def test_public_write_records_first_response_and_safe_audit(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  cursor, _ = _install_database(monkeypatch, [{"id": "key-1"}, None])
  calls = 0

  def operation(_: Any) -> tuple[str, int, dict[str, Any]]:
    nonlocal calls
    calls += 1
    return "config-1", 201, {"strategy": {"config_id": "config-1", "revision": 1}}

  body, replayed, status = state_store.execute_public_write(
    "key-1", "idem-12345678", "a" * 64, "strategy.create", "strategy",
    "req-1", operation,
  )

  assert body["strategy"]["config_id"] == "config-1"
  assert replayed is False
  assert status == 201
  assert calls == 1
  sql = "\n".join(cursor.statements)
  assert "UPDATE api_idempotency_record" in sql
  assert "INSERT INTO api_access_audit" in sql
  assert "source_code" not in sql


def test_public_write_replays_saved_response_without_operation(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _install_database(monkeypatch, [
    {"id": "key-1"},
    {
      "request_hash": "a" * 64,
      "operation_type": "strategy.create",
      "response_status": 201,
      "response_body": {"strategy": {"config_id": "config-1", "revision": 1}},
    },
  ])

  body, replayed, status = state_store.execute_public_write(
    "key-1", "idem-12345678", "a" * 64, "strategy.create", "strategy",
    "req-1", lambda _: pytest.fail("重放不应再次执行资源写入"),
  )

  assert body["strategy"]["config_id"] == "config-1"
  assert replayed is True
  assert status == 201


def test_public_write_rejects_key_reuse_for_different_request(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _install_database(monkeypatch, [
    {"id": "key-1"},
    {
      "request_hash": "b" * 64,
      "operation_type": "strategy.create",
      "response_status": 201,
      "response_body": {},
    },
  ])

  with pytest.raises(state_store.IdempotencyConflictError):
    state_store.execute_public_write(
      "key-1", "idem-12345678", "a" * 64, "strategy.create", "strategy",
      "req-1", lambda _: ("config-1", 201, {}),
    )


def test_public_write_exception_leaves_transaction_for_rollback(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  cursor, connection = _install_database(monkeypatch, [{"id": "key-1"}, None])

  def operation(_: Any) -> tuple[str, int, dict[str, Any]]:
    raise RuntimeError("write failed")

  with pytest.raises(RuntimeError, match="write failed"):
    state_store.execute_public_write(
      "key-1", "idem-12345678", "a" * 64, "strategy.create", "strategy",
      "req-1", operation,
    )

  assert connection.exit_error is RuntimeError
  sql = "\n".join(cursor.statements)
  assert "UPDATE api_idempotency_record" not in sql
  assert "INSERT INTO api_access_audit" not in sql


def test_browser_backtest_first_request_freezes_idempotency_metadata(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _install_database(monkeypatch, [None])
  captured: dict[str, Any] = {}

  def create_job(
    _cur: Any,
    payload: dict[str, Any],
    owner_user_id: str,
    queue_table: str,
    fill_model: str,
  ) -> tuple[str, int]:
    captured.update(
      payload=payload,
      owner_user_id=owner_user_id,
      queue_table=queue_table,
      fill_model=fill_model,
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


def test_browser_backtest_replays_existing_task(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _install_database(monkeypatch, [{
    "id": "run-1",
    "display_no": 7,
    "status": "queued",
    "fill_model": "l2_quote",
    "requested_params": {
      "_browserIdempotency": {
        "key": "browser-12345678",
        "requestHash": "a" * 64,
      },
    },
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
    "requested_params": {
      "_browserIdempotency": {
        "key": "browser-12345678",
        "requestHash": "b" * 64,
      },
    },
  }])

  with pytest.raises(state_store.IdempotencyConflictError):
    state_store.create_browser_backtest_job(
      {"fillModel": "bar_next_open"}, "browser-12345678", "a" * 64,
    )
