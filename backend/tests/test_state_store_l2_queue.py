from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from backend.services import state_store


class FakeCursor:
  def __init__(self, row: dict[str, Any] | None, rows: list[dict[str, Any]] | None = None) -> None:
    self.commands: list[tuple[str, tuple[Any, ...] | None]] = []
    self.row = row
    self.rows = rows if rows is not None else ([row] if row is not None else [])

  def __enter__(self) -> FakeCursor:
    return self

  def __exit__(self, *args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
    self.commands.append((query, params))

  def fetchone(self) -> dict[str, Any] | None:
    return self.row

  def fetchall(self) -> list[dict[str, Any]]:
    return self.rows


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


def test_delete_backtest_run_permanently_removes_completed_run(monkeypatch: Any) -> None:
  cursor = FakeCursor({"status": "succeeded"})
  cursor.rowcount = 1
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  assert state_store.delete_backtest_run("run-1") is True
  assert "FOR UPDATE" in cursor.commands[0][0]
  assert cursor.commands[0][1] == ("run-1", "user-1")
  assert "DELETE FROM backtest_run" in cursor.commands[1][0]
  assert cursor.commands[1][1] == ("run-1", "user-1")


def test_delete_backtest_run_rejects_active_run(monkeypatch: Any) -> None:
  cursor = FakeCursor({"status": "running"})
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(ValueError, match="不能删除"):
    state_store.delete_backtest_run("run-1")
  assert len(cursor.commands) == 1


def test_delete_backtest_runs_requires_all_selected_runs_to_be_terminal(monkeypatch: Any) -> None:
  cursor = FakeCursor(None, [{"id": "run-1", "status": "succeeded"}, {"id": "run-2", "status": "running"}])
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(ValueError, match="包含排队或运行中"):
    state_store.delete_backtest_runs(["run-1", "run-2"])
  assert len(cursor.commands) == 1


def test_delete_backtest_runs_deletes_all_selected_runs_atomically(monkeypatch: Any) -> None:
  cursor = FakeCursor(None, [{"id": "run-1", "status": "succeeded"}, {"id": "run-2", "status": "failed"}])
  cursor.rowcount = 2
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  assert state_store.delete_backtest_runs(["run-1", "run-2"]) == 2
  assert "FOR UPDATE" in cursor.commands[0][0]
  assert cursor.commands[0][1] == (["run-1", "run-2"], "user-1")
  assert "DELETE FROM backtest_run" in cursor.commands[1][0]


@pytest.mark.parametrize("status", ["succeeded", "cancelled"])
def test_finish_bar_backtest_job_preserves_terminal_status_without_research_snapshot(
  monkeypatch: Any,
  status: str,
) -> None:
  cursor = FakeCursor({"run_id": "run-1"})
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))

  assert state_store.finish_bar_backtest_job(
    "run-1", "attempt-1", status, {"totalReturn": 0.1}, {"equity": []},
  ) is True

  assert "UPDATE bar_backtest_job" in cursor.commands[0][0]
  assert cursor.commands[0][1] == (status, None, "run-1", "attempt-1")
  assert "UPDATE backtest_run" in cursor.commands[1][0]
  assert cursor.commands[1][1][0] == status
  enqueue_query, enqueue_params = cursor.commands[2]
  assert "FROM research_signal_snapshot" in enqueue_query
  assert "'status', %s::text" in enqueue_query
  assert enqueue_params[0] == status

def test_claim_bar_job_finalizes_expired_cancelled_jobs_before_claiming(monkeypatch: Any) -> None:
  cursor = FakeCursor(None)
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))

  assert state_store.claim_bar_backtest_job("runner-1", 30) is None

  cleanup_query, cleanup_params = cursor.commands[0]
  assert "UPDATE bar_backtest_job" in cleanup_query
  assert "status='running' AND cancel_requested AND lease_expires_at < now()" in cleanup_query
  assert "UPDATE backtest_run AS run" in cleanup_query
  assert cleanup_params is None
  claim_query, claim_params = cursor.commands[1]
  assert "FOR UPDATE SKIP LOCKED" in claim_query
  assert claim_params == (30, "runner-1")
