from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend.services import state_store


class FakeCursor:
  def __init__(
    self,
    row: dict[str, Any] | None,
    rows: list[dict[str, Any]] | None = None,
    fetch_rows: list[dict[str, Any] | None] | None = None,
  ) -> None:
    self.commands: list[tuple[str, tuple[Any, ...] | None]] = []
    self.row = row
    self.rows = rows if rows is not None else ([row] if row is not None else [])
    self.fetch_rows = list(fetch_rows or [])

  def __enter__(self) -> FakeCursor:
    return self

  def __exit__(self, *args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
    self.commands.append((query, params))

  def fetchone(self) -> dict[str, Any] | None:
    if self.fetch_rows:
      return self.fetch_rows.pop(0)
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


def test_runner_availability_uses_fresh_ready_or_running_heartbeat(monkeypatch: Any) -> None:
  cursor = FakeCursor({"available": False})
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store.settings, "BACKTEST_RUNNER_AVAILABILITY_REQUIRED", True)

  assert state_store.has_healthy_backtest_runner("bar") is False
  query, params = cursor.commands[0]
  assert "status IN ('ready', 'running')" in query
  assert "heartbeat_at >=" in query
  assert params == (
    "bar",
    state_store.SCRIPT_RUNTIME_VERSION,
    state_store.RUNTIME_CAPABILITY_DIGEST,
    state_store.settings.BACKTEST_RUNNER_HEALTH_TTL_SECONDS,
  )


def test_runner_availability_accepts_visible_healthy_heartbeat(monkeypatch: Any) -> None:
  cursor = FakeCursor({"available": True})
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store.settings, "BACKTEST_RUNNER_AVAILABILITY_REQUIRED", True)

  assert state_store.has_healthy_backtest_runner("l2") is True


def test_runner_health_reports_queue_age_for_each_job_kind(monkeypatch: Any) -> None:
  cursor = FakeCursor(None, rows=[
    {
      "job_kind": "bar", "healthy_count": 16, "error_count": 0,
      "queued_count": 3, "oldest_queued_seconds": 83,
    },
    {
      "job_kind": "l2", "healthy_count": 15, "error_count": 1,
      "queued_count": 0, "oldest_queued_seconds": None,
    },
  ])
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))

  result = state_store.get_backtest_runner_health()

  assert result == {
    "status": "ok",
    "runners": {
      "bar": {
        "healthy": 16, "errors": 0, "queued": 3,
        "oldestQueuedSeconds": 83,
      },
      "l2": {
        "healthy": 15, "errors": 1, "queued": 0,
        "oldestQueuedSeconds": None,
      },
    },
  }
  query, params = cursor.commands[0]
  assert "oldest_queued_seconds" in query
  assert "now() - min(created_at)" in query
  assert params is not None
  assert len(params) == 12


def test_runner_health_read_policy_does_not_relax_writes() -> None:
  migration_path = Path(__file__).resolve().parents[2] / "db" / "migrations" / (
    "024_backtest_runner_health_read_policy.sql"
  )
  migration = migration_path.read_text(encoding="utf-8")
  policy = migration.split(
    "CREATE POLICY backtest_runner_health_read", 1,
  )[1].split(";", 1)[0]

  assert "FOR SELECT" in policy
  assert "USING (true)" in policy
  assert "WITH CHECK" not in policy


def test_claim_persists_each_attempt_without_overwriting_expired_attempt() -> None:
  job_row = {
    "run_id": "run-b", "owner_user_id": "user-b", "payload": {},
    "attempt_no": 2, "attempt_token": "attempt-b", "lease_expires_at": "later",
  }
  cursor = FakeCursor(None, fetch_rows=[
    {
      "job_kind": "bar", "run_id": "run-b", "owner_user_id": "user-b",
      "owner_active_count": 0,
    },
    {"run_id": "run-b"},
    {"active_count": 0, "override_limit": None},
    job_row,
  ])

  claimed = state_store._claim_backtest_job(cursor, "bar", "runner-b", 30)

  assert claimed == job_row
  commands = "\n".join(query for query, _ in cursor.commands)
  assert "UPDATE backtest_run_attempt" in commands
  assert "termination_reason='lease_expired'" in commands
  assert "INSERT INTO backtest_run_attempt" in commands


def test_queued_job_freezes_runtime_route_in_columns_and_payload(monkeypatch: Any) -> None:
  cursor = FakeCursor(None)
  monkeypatch.setattr(
    state_store, "_create_backtest_run_row", lambda *_, **__: ("run-42", 42),
  )
  monkeypatch.setattr(state_store, "_save_research_signal_snapshot", lambda *_: None)

  result = state_store._create_queued_backtest_job(
    cursor, {"symbol": "688981.SH"}, "user-1", "bar_backtest_job", "bar_next_open",
  )

  assert result == ("run-42", 42)
  insert_query, insert_params = cursor.commands[1]
  assert "runtime_version, capability_digest" in insert_query
  assert insert_params is not None
  assert insert_params[-2:] == (
    state_store.SCRIPT_RUNTIME_VERSION,
    state_store.RUNTIME_CAPABILITY_DIGEST,
  )
  assert insert_params[2].obj["runtimeVersion"] == state_store.SCRIPT_RUNTIME_VERSION
  assert insert_params[2].obj["capabilityDigest"] == state_store.RUNTIME_CAPABILITY_DIGEST


def test_runtime_routing_migration_is_idempotent_and_indexes_both_job_types() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db" / "migrations" / "026_strategy_runtime_capability_routing.sql"
  ).read_text(encoding="utf-8")

  assert migration.count("ADD COLUMN IF NOT EXISTS runtime_version") == 3
  assert migration.count("ADD COLUMN IF NOT EXISTS capability_digest") == 3
  assert "ix_bar_backtest_job_runtime_claim" in migration
  assert "ix_l2_backtest_job_runtime_claim" in migration
  assert "runtime.dependency_missing" in migration
  assert "runtime.dependency_load_failed" in migration


def test_global_scheduler_uses_fairness_burst_for_user_without_slot(monkeypatch: Any) -> None:
  job_row = {
    "run_id": "run-b", "owner_user_id": "user-b", "payload": {}, "attempt_no": 1,
    "attempt_token": "attempt-b", "lease_expires_at": "later",
  }
  cursor = FakeCursor(None, fetch_rows=[
    {"job_kind": "bar", "run_id": "run-b", "owner_user_id": "user-b", "owner_active_count": 0},
    {"run_id": "run-b"},
    {"active_count": 8, "override_limit": None},
    job_row,
  ])
  monkeypatch.setattr(state_store.settings, "BACKTEST_BASE_CONCURRENCY", 8)
  monkeypatch.setattr(state_store.settings, "BACKTEST_BURST_MAX_CONCURRENCY", 12)
  monkeypatch.setattr(state_store.settings, "BACKTEST_USER_GUARANTEED_CONCURRENCY", 1)
  monkeypatch.setattr(state_store.settings, "BACKTEST_FAIRNESS_BURST_ENABLED", True)

  claimed = state_store._claim_backtest_job(cursor, "bar", "runner-b", 30)

  assert claimed == job_row
  slot_params = next(
    params for query, params in cursor.commands
    if "INSERT INTO backtest_execution_slot" in query
  )
  assert slot_params is not None
  assert slot_params[5] is True


def test_global_scheduler_does_not_burst_for_user_with_active_slot(monkeypatch: Any) -> None:
  cursor = FakeCursor(None, fetch_rows=[
    {"job_kind": "bar", "run_id": "run-a", "owner_user_id": "user-a", "owner_active_count": 1},
    {"run_id": "run-a"},
    {"active_count": 8, "override_limit": None},
  ])
  monkeypatch.setattr(state_store.settings, "BACKTEST_BASE_CONCURRENCY", 8)
  monkeypatch.setattr(state_store.settings, "BACKTEST_BURST_MAX_CONCURRENCY", 12)
  monkeypatch.setattr(state_store.settings, "BACKTEST_USER_GUARANTEED_CONCURRENCY", 1)
  monkeypatch.setattr(state_store.settings, "BACKTEST_FAIRNESS_BURST_ENABLED", True)

  assert state_store._claim_backtest_job(cursor, "bar", "runner-a", 30) is None
  assert not any("INSERT INTO backtest_execution_slot" in query for query, _ in cursor.commands)


def test_delete_backtest_run_permanently_removes_completed_run(monkeypatch: Any) -> None:
  cursor = FakeCursor(None, fetch_rows=[{"status": "succeeded"}, None])
  cursor.rowcount = 1
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  assert state_store.delete_backtest_run("run-1") is True
  assert "FOR UPDATE" in cursor.commands[0][0]
  assert cursor.commands[0][1] == ("run-1", "user-1")
  assert "backtest_experiment_trial_execution" in cursor.commands[1][0]
  assert "DELETE FROM backtest_run" in cursor.commands[2][0]
  assert cursor.commands[2][1] == ("run-1", "user-1")


def test_delete_backtest_run_rejects_active_run(monkeypatch: Any) -> None:
  cursor = FakeCursor({"status": "running"})
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(ValueError, match="不能删除"):
    state_store.delete_backtest_run("run-1")
  assert len(cursor.commands) == 1


def test_delete_backtest_run_rejects_experiment_retained_run(
  monkeypatch: Any,
) -> None:
  cursor = FakeCursor(
    None,
    fetch_rows=[{"status": "succeeded"}, {"retained": True}],
  )
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(state_store.ExperimentRunRetainedError):
    state_store.delete_backtest_run("run-1")
  assert not any("DELETE FROM backtest_run" in query for query, _ in cursor.commands)


def test_delete_backtest_runs_requires_all_selected_runs_to_be_terminal(monkeypatch: Any) -> None:
  cursor = FakeCursor(None, [{"id": "run-1", "status": "succeeded"}, {"id": "run-2", "status": "running"}])
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  with pytest.raises(ValueError, match="包含排队或运行中"):
    state_store.delete_backtest_runs(["run-1", "run-2"])
  assert len(cursor.commands) == 1


def test_delete_backtest_runs_deletes_all_selected_runs_atomically(monkeypatch: Any) -> None:
  cursor = FakeCursor(
    None,
    [{"id": "run-1", "status": "succeeded"}, {"id": "run-2", "status": "failed"}],
    fetch_rows=[None],
  )
  cursor.rowcount = 2
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-1")

  assert state_store.delete_backtest_runs(["run-1", "run-2"]) == 2
  assert "FOR UPDATE" in cursor.commands[0][0]
  assert cursor.commands[0][1] == (["run-1", "run-2"], "user-1")
  assert "backtest_experiment_trial_execution" in cursor.commands[1][0]
  assert "DELETE FROM backtest_run" in cursor.commands[2][0]


@pytest.mark.parametrize("status", ["succeeded", "cancelled"])
def test_finish_bar_backtest_job_preserves_terminal_status_without_research_snapshot(
  monkeypatch: Any,
  status: str,
) -> None:
  cursor = FakeCursor(None, fetch_rows=[{"status": status}])
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store.settings, "RESEARCH_RESULT_REPORT_ENABLED", True)
  monkeypatch.setattr(state_store.settings, "BACKTEST_INSTANCE_ID", "server")

  assert state_store.finish_bar_backtest_job(
    "run-1", "attempt-1", status, {"totalReturn": 0.1}, {"equity": []},
  ) is True

  assert "UPDATE bar_backtest_job" in cursor.commands[0][0]
  assert cursor.commands[0][1] == (status, None, "run-1", "attempt-1")
  assert "UPDATE backtest_run" in cursor.commands[1][0]
  assert "market_data_snapshot=COALESCE" in cursor.commands[1][0]
  assert cursor.commands[1][1][0] == status
  assert "DELETE FROM backtest_execution_slot" in cursor.commands[2][0]
  enqueue_query, enqueue_params = next(
    item for item in cursor.commands if "FROM research_signal_snapshot" in item[0]
  )
  assert "FROM research_signal_snapshot" in enqueue_query
  assert "'backtestInstanceId', %s::text" in enqueue_query
  assert "'status', %s::text" in enqueue_query
  assert enqueue_params[:2] == ("server", status)


def test_finish_bar_backtest_job_skips_result_outbox_when_reporting_disabled(
  monkeypatch: Any,
) -> None:
  cursor = FakeCursor(None, fetch_rows=[{"status": "succeeded"}])
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))
  monkeypatch.setattr(state_store.settings, "RESEARCH_RESULT_REPORT_ENABLED", False)

  assert state_store.finish_bar_backtest_job(
    "run-1", "attempt-1", "succeeded", {"totalReturn": 0.1}, {"equity": []},
  ) is True

  assert len(cursor.commands) == 4
  assert "UPDATE backtest_run_attempt" in cursor.commands[3][0]


def test_finish_job_keeps_database_cancellation_over_late_success() -> None:
  cursor = FakeCursor(None, fetch_rows=[{"status": "cancelled"}])

  status = state_store._finish_backtest_job(
    cursor, "bar_backtest_job", "run-1", "attempt-1", "succeeded",
    {"tradeCount": 3}, {"trades": [1]},
  )

  assert status == "cancelled"
  main_params = cursor.commands[1][1]
  assert main_params is not None
  assert main_params[0] == "cancelled"
  assert main_params[2] is None


def test_actual_worker_data_snapshot_is_extracted_for_run_history() -> None:
  bar_snapshot = {"provider": "qmt_kline", "hash": "bar-hash"}
  bar_result = {
    "experimentManifest": {"databaseSnapshot": bar_snapshot},
  }
  l2_result = {
    "dataSnapshot": {
      "bar": {"provider": "mapped_l2_bar", "hash": "l2-bar-hash"},
      "quote": {"source_name": "quant.tick_quote"},
    },
  }

  assert state_store._market_data_snapshot_from_result(bar_result) == {
    "barSnapshot": bar_snapshot,
  }
  assert state_store._market_data_snapshot_from_result(l2_result) == {
    "barSnapshot": l2_result["dataSnapshot"]["bar"],
    "l2Snapshot": l2_result["dataSnapshot"],
  }

def test_claim_bar_job_finalizes_expired_cancelled_jobs_before_claiming(monkeypatch: Any) -> None:
  cursor = FakeCursor(None)
  connection = FakeConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: fake_db(connection))

  assert state_store.claim_bar_backtest_job("runner-1", 30) is None

  assert cursor.commands[0] == (
    "SELECT set_config('lock_timeout', %s, true)",
    ("2000ms",),
  )
  assert cursor.commands[1] == (
    "SELECT pg_advisory_xact_lock(%s)",
    (state_store._BACKTEST_SCHEDULER_LOCK_KEY,),
  )
  assert cursor.commands[2] == (
    "SELECT set_config('lock_timeout', %s, true)",
    ("250ms",),
  )
  cleanup_query, cleanup_params = cursor.commands[3]
  assert "UPDATE bar_backtest_job" in cleanup_query
  assert "status='cancelling' AND lease_expires_at < now()" in cleanup_query
  assert "UPDATE backtest_run AS run" in cleanup_query
  assert cleanup_params is None
  assert "UPDATE l2_backtest_job" in cursor.commands[4][0]
  assert "DELETE FROM backtest_execution_slot" in cursor.commands[5][0]
  claim_query, claim_params = cursor.commands[6]
  assert "UNION ALL" in claim_query
  assert claim_params == (
    state_store.SCRIPT_RUNTIME_VERSION,
    state_store.RUNTIME_CAPABILITY_DIGEST,
    [],
    state_store.SCRIPT_RUNTIME_VERSION,
    state_store.RUNTIME_CAPABILITY_DIGEST,
    [],
    None,
    None,
  )

def test_save_research_signal_snapshot_reuses_matching_artifact(
  tmp_path: Path,
) -> None:
  artifact_path = tmp_path / "dataset_hash.ndjson"
  artifact_path.write_text('{"action":"long"}\n', encoding="utf-8")
  cursor = FakeCursor({"id": "artifact-1"})
  snapshot = {
    "datasetId": "251294ab-87cd-46ab-9d48-d96987e51b84",
    "snapshotHash": "a" * 64,
    "artifactFile": str(artifact_path),
    "manifest": {"version": 1},
    "sourcePartitions": [],
  }

  state_store._save_research_signal_snapshot(cursor, "run-2", "user-1", snapshot)

  artifact_query, artifact_params = cursor.commands[0]
  assert "ON CONFLICT (storage_key) DO UPDATE" in artifact_query
  assert "artifact_file.content_sha256=EXCLUDED.content_sha256" in artifact_query
  assert artifact_params is not None
  assert artifact_params[1] == "run-2"
  snapshot_query, snapshot_params = cursor.commands[1]
  assert "INSERT INTO research_signal_snapshot" in snapshot_query
  assert snapshot_params is not None
  assert snapshot_params[0] == "run-2"
  assert snapshot_params[4] == "artifact-1"


def test_save_research_signal_snapshot_rejects_storage_key_content_conflict(
  tmp_path: Path,
) -> None:
  artifact_path = tmp_path / "dataset_hash.ndjson"
  artifact_path.write_text('{"action":"long"}\n', encoding="utf-8")
  cursor = FakeCursor(None)
  snapshot = {
    "datasetId": "251294ab-87cd-46ab-9d48-d96987e51b84",
    "snapshotHash": "a" * 64,
    "artifactFile": str(artifact_path),
    "manifest": {"version": 1},
  }

  with pytest.raises(state_store.DatabaseError, match="已有存储记录冲突"):
    state_store._save_research_signal_snapshot(cursor, "run-2", "user-1", snapshot)


def test_normalize_legacy_l2_run_sanitizes_summary_after_result_adapter(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    "backend.l2.result_adapter.normalize_l2_result",
    lambda *_: {
      "summary": {
        "error": "internal L2 source detail",
        "errorDetail": {
          "category": "data",
          "code": "data.l2_quote_invalid",
          "message": "internal L2 source detail",
          "hint": "raw table detail",
        },
      },
    },
  )
  row = {
    "requested_params": {},
    "fill_model": "l2_quote",
    "result_json": {"legacy": True},
    "result_summary": {},
    "id": "run-1",
    "display_no": 1,
    "status": "failed",
    "created_at": None,
  }

  normalized = state_store._normalize_run(row)

  assert normalized["summaryJson"]["error"] == "行情数据异常，请联系管理员"
  assert normalized["summaryJson"]["errorDetail"]["message"] == "行情数据异常，请联系管理员"
  assert normalized["summaryJson"]["errorDetail"]["hint"] is None
