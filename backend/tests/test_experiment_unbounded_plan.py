from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend import settings
from backend.experiments.evaluation_dataset import EvaluationDataset
from backend.experiments.lifecycle import (
  ExperimentLifecycleCommands,
  LifecycleCommand,
  LifecycleCommandResult,
  project_legacy_status,
)
from backend.experiments.storage_pressure import (
  StorageDecision,
  StorageObservation,
  StoragePressureGate,
)


NOW = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)


class MemoryPressureRepository:
  def __init__(self, logical_size: int = 50) -> None:
    self.logical_size = logical_size
    self.saved: StorageDecision | None = None

  def postgres_logical_size(self, now: datetime) -> StorageObservation:
    return StorageObservation("postgres_logical", self.logical_size, None, now)

  def load(self) -> dict[str, Any]:
    if self.saved is None:
      return {}
    return {
      "decision": self.saved.status,
      "signals": self.saved.signals,
      "reasons": list(self.saved.reasons),
      "observed_at": self.saved.observed_at,
    }

  def save(self, decision: StorageDecision, _threshold_version: str) -> None:
    self.saved = decision


class MemoryCapacityProbe:
  def __init__(self, postgres_free: int, artifact_free: int) -> None:
    self.postgres_free = postgres_free
    self.artifact_free = artifact_free
    self.observed_at = NOW

  def observe(self, _now: datetime) -> tuple[StorageObservation, ...]:
    return (
      StorageObservation(
        "postgres_volume_free", self.postgres_free, 1_000, self.observed_at,
      ),
      StorageObservation(
        "artifact_volume_free", self.artifact_free, 1_000, self.observed_at,
      ),
    )


class RecordingLifecycleStore:
  def __init__(self) -> None:
    self.operations: list[str] = []

  def execute(
    self,
    operation: str,
    command: LifecycleCommand,
  ) -> LifecycleCommandResult:
    self.operations.append(operation)
    return LifecycleCommandResult({"experimentId": command.experiment_id}, 202)


def _configure_storage_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
  values = {
    "EXPERIMENT_POSTGRES_LOGICAL_HIGH_WATER_BYTES": 100,
    "EXPERIMENT_POSTGRES_LOGICAL_LOW_WATER_BYTES": 80,
    "EXPERIMENT_POSTGRES_VOLUME_MIN_FREE_BYTES": 100,
    "EXPERIMENT_POSTGRES_VOLUME_RESUME_FREE_BYTES": 200,
    "EXPERIMENT_ARTIFACT_VOLUME_MIN_FREE_BYTES": 100,
    "EXPERIMENT_ARTIFACT_VOLUME_RESUME_FREE_BYTES": 200,
    "EXPERIMENT_STORAGE_SIGNAL_MAX_AGE_SECONDS": 60,
  }
  for name, value in values.items():
    monkeypatch.setattr(settings, name, value)


def test_legacy_status_projection_never_exposes_new_lifecycle_values() -> None:
  assert project_legacy_status("pausing", "running", None, None) == "running"
  assert project_legacy_status("paused", "queued", None, None) == "generation_blocked"
  assert project_legacy_status("cancelling", "running", None, None) == "cancelling"
  assert project_legacy_status("terminal", "settled", None, "cancelled") == "cancelled"
  assert project_legacy_status(
    "active", "queued", "storage_pressure", None,
  ) == "generation_blocked"


def test_lifecycle_commands_only_delegate_through_the_deep_module() -> None:
  store = RecordingLifecycleStore()
  commands = ExperimentLifecycleCommands(store=store)  # type: ignore[arg-type]
  command = LifecycleCommand("experiment", "revision", 3, "stable-key", "reason")

  assert commands.pause(command).status_code == 202
  assert commands.resume(command).status_code == 202
  assert commands.cancel(command).status_code == 202
  assert store.operations == ["pause", "resume", "cancel"]


def test_storage_pressure_gate_is_open_only_with_all_fresh_signals(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _configure_storage_thresholds(monkeypatch)
  repository = MemoryPressureRepository()
  probe = MemoryCapacityProbe(300, 300)

  decision = StoragePressureGate(repository=repository, probe=probe).evaluate(NOW)

  assert decision.status == "open"
  assert decision.missing_signals == ()
  assert set(decision.signals) == {
    "postgres_logical", "postgres_volume_free", "artifact_volume_free",
  }


def test_storage_pressure_gate_uses_hysteresis_before_resuming(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _configure_storage_thresholds(monkeypatch)
  repository = MemoryPressureRepository(logical_size=100)
  probe = MemoryCapacityProbe(100, 300)
  gate = StoragePressureGate(repository=repository, probe=probe)

  assert gate.evaluate(NOW).status == "pressured"
  repository.logical_size = 90
  probe.postgres_free = 150
  assert gate.evaluate(NOW + timedelta(seconds=1)).status == "pressured"
  repository.logical_size = 80
  probe.postgres_free = 200
  probe.observed_at = NOW + timedelta(seconds=2)
  assert gate.evaluate(NOW + timedelta(seconds=2)).status == "open"


def test_storage_pressure_gate_fails_safe_for_stale_or_missing_configuration(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _configure_storage_thresholds(monkeypatch)
  repository = MemoryPressureRepository()
  probe = MemoryCapacityProbe(300, 300)
  probe.observed_at = NOW - timedelta(seconds=61)

  stale = StoragePressureGate(repository=repository, probe=probe).evaluate(NOW)
  assert stale.status == "unknown"
  assert stale.missing_signals == (
    "artifact_volume_free:stale", "postgres_volume_free:stale",
  )

  monkeypatch.setattr(settings, "EXPERIMENT_ARTIFACT_VOLUME_MIN_FREE_BYTES", None)
  missing = StoragePressureGate(
    repository=MemoryPressureRepository(),
    probe=MemoryCapacityProbe(300, 300),
  ).evaluate(NOW)
  assert missing.status == "unknown"
  assert "artifactMinFree" in missing.missing_signals


def test_evaluation_completeness_compacts_a_huge_ungenerated_tail() -> None:
  stages = [{
    "id": "stage-1",
    "stage_index": 0,
    "status": "running",
    "stage_spec": {},
    "experiment_spec": {},
    "planned_trial_count": 9_007_199_254_740_991,
    "generated_trial_count": 3,
    "failed_count": 0,
    "cancelled_count": 0,
    "cancelled_before_generation": 0,
  }]

  result = EvaluationDataset._completeness(
    object(),
    stages,
    3,
    evaluation_run_id="evaluation-1",
    experiment_id="experiment-1",
    requirements=(),
  )

  assert result["plannedTrialCount"] == 9_007_199_254_740_991
  assert result["missingCount"] == 9_007_199_254_740_988
  assert result["ungeneratedIntervals"] == [{
    "stageId": "stage-1",
    "stageIndex": 0,
    "startTrialIndex": 3,
    "endTrialIndexExclusive": 9_007_199_254_740_991,
    "count": 9_007_199_254_740_988,
    "status": "missing",
  }]
