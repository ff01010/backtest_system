from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backend import settings
from backend.experiments.purge_worker import ExperimentPurgeWorker, PurgeProgress


class MemoryPurgeRepository:
  def __init__(self) -> None:
    self.claimed = False
    self.artifacts_returned = False
    self.marked: list[str] = []
    self.advanced = False
    self.completed = False
    self.failed: tuple[str, str] | None = None

  def claim_next(self, _worker_id: str) -> dict[str, Any] | None:
    if self.claimed:
      return None
    self.claimed = True
    return {"id": "purge-1", "attemptToken": "attempt-1", "phase": "artifacts"}

  def heartbeat(self, *_args: str) -> bool:
    return True

  def load_artifact_batch(self, *_args: str) -> list[dict[str, Any]]:
    if self.artifacts_returned:
      return []
    self.artifacts_returned = True
    return [{"itemId": "item-1", "storageKey": "evaluation-exports/result.csv"}]

  def mark_artifacts_removed(self, _purge_id: str, _token: str, item_ids: list[str]) -> None:
    self.marked.extend(item_ids)

  def advance_artifact_phase(self, *_args: str) -> bool:
    self.advanced = True
    return True

  def process_next_batch(self, *_args: str) -> PurgeProgress:
    return PurgeProgress(phase="completed", completed=True)

  def complete(self, *_args: str) -> None:
    self.completed = True

  def fail(self, _purge_id: str, _token: str, code: str, _message: str) -> None:
    self.failed = (code, _message)


def test_purge_worker_removes_frozen_artifacts_before_database_graph(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENT_PURGE_ENABLED", True)
  artifact = tmp_path / "evaluation-exports" / "result.csv"
  artifact.parent.mkdir(parents=True)
  artifact.write_text("result", encoding="utf-8")
  repository = MemoryPurgeRepository()
  worker = ExperimentPurgeWorker(
    repository=repository, artifact_root=tmp_path, worker_id="purge-worker-1",
  )

  assert worker.run_once() is True

  assert not artifact.exists()
  assert repository.marked == ["item-1"]
  assert repository.advanced is True
  assert repository.completed is True
  assert repository.failed is None


def test_purge_migration_forces_rls_and_avoids_graph_cascades() -> None:
  migration = Path("db/migrations/053_experiment_archival_purge.sql").read_text(
    encoding="utf-8",
  )

  assert migration.count("FORCE ROW LEVEL SECURITY") == 2
  assert "backtest_experiment_purge_item" in migration
  assert "old_value jsonb" in migration
  assert "new_value jsonb" in migration
  assert "'run_artifact'" in migration
  assert "'pending_unlink'" in migration
  assert "research_signal_snapshot" in migration
  assert "artifact_file" in migration
  assert "ON DELETE CASCADE" not in migration


def test_purge_keeps_storage_key_reserved_until_unlink_succeeds() -> None:
  source = Path("backend/experiments/purge_repository.py").read_text(encoding="utf-8")

  assert "artifact_type='purge_pending'" in source
  assert "SET run_id=NULL, artifact_type='purge_pending'" in source
  assert "backtest_signal_detail_export_item WHERE source_run_id=%s" in source
