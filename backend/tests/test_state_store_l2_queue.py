from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from backend.services import state_store


class FakeCursor:
  def __init__(self, row: dict[str, Any] | None) -> None:
    self.commands: list[tuple[str, tuple[Any, ...] | None]] = []
    self.row = row

  def __enter__(self) -> FakeCursor:
    return self

  def __exit__(self, *args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
    self.commands.append((query, params))

  def fetchone(self) -> dict[str, Any] | None:
    return self.row


class FakeConnection:
  def __init__(self, cursor: FakeCursor) -> None:
    self.cursor_value = cursor

  def cursor(self) -> FakeCursor:
    return self.cursor_value


@contextmanager
def fake_db(connection: FakeConnection) -> Iterator[FakeConnection]:
  yield connection


def test_claim_l2_job_uses_transaction_scoped_fair_scheduler_lock(monkeypatch: Any) -> None:
  cursor = FakeCursor(None)
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))

  job = state_store.claim_l2_backtest_job("runner-1", 30)

  assert job is None
  assert cursor.commands[0] == (
    "SELECT pg_advisory_xact_lock(%s)",
    (state_store._L2_FAIR_CLAIM_LOCK_KEY,),
  )
  claim_query, claim_params = cursor.commands[1]
  assert "FOR UPDATE OF job SKIP LOCKED" in claim_query
  assert "active.owner_user_id=job.owner_user_id" in claim_query
  assert "active.status='running'" in claim_query
  assert "active.lease_expires_at >= now()" in claim_query
  assert "ORDER BY (" in claim_query
  assert claim_params == (30, "runner-1")


def test_claim_l2_job_preserves_attempt_token_result_contract(monkeypatch: Any) -> None:
  cursor = FakeCursor({
    "run_id": "run-1",
    "owner_user_id": "user-1",
    "payload": {"symbol": "688981.SH"},
    "attempt_no": 2,
    "attempt_token": "attempt-2",
  })
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))

  job = state_store.claim_l2_backtest_job("runner-2", 60)

  assert job == {
    "runId": "run-1",
    "ownerUserId": "user-1",
    "payload": {"symbol": "688981.SH"},
    "attemptNo": 2,
    "attemptToken": "attempt-2",
  }
  assert "UPDATE backtest_run SET status='running'" in cursor.commands[2][0]
  assert cursor.commands[2][1] == ("run-1",)
