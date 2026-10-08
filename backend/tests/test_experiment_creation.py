from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Request
import pytest

from backend import app as app_module
from backend import settings
from backend.experiments.application import ExperimentApplication
from backend.experiments.contracts import (
  ExperimentCreateRequest,
  ExperimentRetryFailedRequest,
  ExperimentRetryPreviewRequest,
  ExperimentRevisionCreateRequest,
)
from backend.experiments.coordinator import ExperimentCoordinator
from backend.experiments.errors import ExperimentError
from backend.experiments.repository import (
  CommandResult,
  validate_preview_bindings,
)
from backend.services import state_store
from backend.tests.test_experiment_preview import (
  StaticDatasetResolver,
  StaticResolver,
  _request,
  _template,
)


class MemoryExperimentRepository:
  def __init__(self) -> None:
    self.preview: dict[str, Any] | None = None
    self.created: dict[str, Any] | None = None
    self.created_revision: dict[str, Any] | None = None
    self.retried: dict[str, Any] | None = None

  def save_preview(self, preview: dict[str, Any]) -> None:
    self.preview = preview

  def load_preview(self, _token: str) -> dict[str, Any] | None:
    return self.preview

  def create_experiment(self, **values: Any) -> CommandResult:
    assert self.preview is not None
    row = {
      "expires_at": self.preview["expiresAt"],
      "spec_hash": self.preview["specHash"],
      "run_template_digest": self.preview["runTemplateDigest"],
      "dataset_snapshot_digest": self.preview["datasetSnapshotDigest"],
      "capability_digest": self.preview["capabilityDigest"],
      "budget_digest": self.preview["budgetDigest"],
    }
    validate_preview_bindings(row, values["expected_bindings"], datetime.now(timezone.utc))
    self.created = values
    return CommandResult({
      "experimentId": "experiment-1",
      "revisionId": "revision-1",
      "stageId": "stage-1",
      "status": "preparing",
      "plannedTrialCount": self.preview["plannedTrialCount"],
      "generatedTrialCount": 0,
      "specHash": self.preview["specHash"],
      "replayed": False,
      "createdAt": "2026-09-29T00:00:00+00:00",
    }, 201)

  def find_created_experiment(self, _key: str) -> CommandResult:
    return CommandResult({"experimentId": "experiment-1", "replayed": True}, 200)

  def create_revision(self, **values: Any) -> CommandResult:
    self.created_revision = values
    return CommandResult({
      "experimentId": values["experiment_id"],
      "revisionId": "revision-2",
      "stageId": "stage-2",
      "status": "preparing",
      "plannedTrialCount": self.preview["plannedTrialCount"] if self.preview else 0,
      "generatedTrialCount": 0,
      "specHash": self.preview["specHash"] if self.preview else "",
      "replayed": False,
      "createdAt": "2026-09-30T00:00:00+00:00",
    }, 201)

  def preview_retry_selection(self, *_args: Any) -> dict[str, Any]:
    return {
      "experimentId": "experiment-1",
      "revisionId": "revision-1",
      "selectedTrialCount": 1,
      "selectionDigest": "b" * 64,
      "trials": [],
    }

  def retry_failed_trials(self, **values: Any) -> CommandResult:
    self.retried = values
    return CommandResult({
      "experimentId": values["experiment_id"],
      "revisionId": values["expected_revision_id"],
      "status": "running",
      "selectedTrialCount": values["expected_count"],
      "selectionDigest": values["expected_digest"],
      "executions": [],
      "replayed": False,
    }, 202)

  def get_experiment(self, _experiment_id: str) -> dict[str, Any]:
    return {"experimentId": "experiment-1"}

  def cancel_experiment(self, *_args: Any) -> CommandResult:
    return CommandResult({"experimentId": "experiment-1", "status": "cancelled"}, 200)


class MemoryGenerationRepository:
  def __init__(self, stage: dict[str, Any]) -> None:
    self.stage = stage
    self.trials: list[dict[str, Any]] = []
    self.claim_count = 0
    self.fail_next_commit = False

  def claim_generation_stage(
    self,
    _worker_id: str,
    _lease_seconds: int,
  ) -> dict[str, Any] | None:
    if self.stage["status"] != "preparing":
      return None
    self.claim_count += 1
    claimed = dict(self.stage)
    claimed["generation_attempt_token"] = f"attempt-{self.claim_count}"
    self.stage["generation_attempt_token"] = claimed["generation_attempt_token"]
    return claimed

  def commit_generation_batch(
    self,
    _stage_id: str,
    attempt_token: str,
    expected_cursor: int,
    trials: list[dict[str, Any]],
  ) -> bool:
    if self.fail_next_commit:
      self.fail_next_commit = False
      raise state_store.DatabaseError("故障注入")
    if (
      attempt_token != self.stage["generation_attempt_token"]
      or expected_cursor != self.stage["generation_cursor"]
    ):
      return False
    self.trials.extend(trials)
    self.stage["generation_cursor"] += len(trials)
    self.stage["generated_trial_count"] = self.stage["generation_cursor"]
    if self.stage["generation_cursor"] == self.stage["planned_trial_count"]:
      self.stage["status"] = "queued"
    return True

  def block_generation(self, *_args: Any) -> None:
    self.stage["status"] = "generation_blocked"


def _enable_experiments(monkeypatch: pytest.MonkeyPatch, batch_size: int = 3) -> None:
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_VALUES_PER_DIMENSION", 50)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_TRIALS", 1000)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_USER_PLANNED_TRIALS", 10_000)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_MAX_ESTIMATED_BARS", 10_000_000)
  monkeypatch.setattr(settings, "EXPERIMENT_PREVIEW_TTL_SECONDS", 900)
  monkeypatch.setattr(settings, "EXPERIMENT_GENERATION_BATCH_SIZE", batch_size)
  monkeypatch.setattr(settings, "EXPERIMENT_COORDINATOR_LEASE_SECONDS", 30)


def _application(repository: MemoryExperimentRepository) -> ExperimentApplication:
  return ExperimentApplication(
    resolver=StaticResolver(_template()),
    dataset_resolver=StaticDatasetResolver(),
    repository=repository,
  )


def _create_request(preview: dict[str, Any]) -> ExperimentCreateRequest:
  return ExperimentCreateRequest(
    previewToken=preview["previewToken"],
    specHash=preview["specHash"],
    experimentSpec=_request(),
  )


def _generation_stage(preview: dict[str, Any]) -> dict[str, Any]:
  normalized = preview["normalizedSpec"]
  stage = normalized["plan"]["stages"][0]
  return {
    "id": "stage-1",
    "experiment_id": "experiment-1",
    "revision_no": 1,
    "revision_id": "revision-1",
    "owner_user_id": "user-1",
    "experiment_spec": normalized,
    "run_template_digest": preview["runTemplateDigest"],
    "determinism": normalized["determinism"],
    "stage_spec": stage,
    "stage_input_fingerprint": stage["stageInputFingerprint"],
    "planned_trial_count": preview["plannedTrialCount"],
    "generated_trial_count": 0,
    "generation_cursor": 0,
    "status": "preparing",
  }


def test_create_revalidates_preview_and_freezes_only_the_plan(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _enable_experiments(monkeypatch)
  repository = MemoryExperimentRepository()
  application = _application(repository)
  preview = application.preview(_request())

  response, status = application.create(_create_request(preview), "create-key-1", "req-1")

  assert status == 201
  assert response["status"] == "preparing"
  assert repository.created is not None
  assert repository.created["normalized_spec"]["plan"]["stages"][0]["plannedTrialCount"] == 8
  assert "trials" not in repository.created["normalized_spec"]


def test_create_rejects_dataset_drift_against_the_saved_preview(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _enable_experiments(monkeypatch)
  repository = MemoryExperimentRepository()
  datasets = StaticDatasetResolver("v1")
  application = ExperimentApplication(
    resolver=StaticResolver(_template()),
    dataset_resolver=datasets,
    repository=repository,
  )
  preview = application.preview(_request())
  datasets.version = "v2"

  with pytest.raises(ExperimentError) as stale:
    application.create(_create_request(preview), "create-key-1")

  assert stale.value.code == "experiment.preview_stale"
  assert repository.created is None


def test_create_revision_repreviews_and_forwards_optimistic_identity(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _enable_experiments(monkeypatch)
  repository = MemoryExperimentRepository()
  application = _application(repository)
  preview = application.preview(_request())
  payload = ExperimentRevisionCreateRequest(
    **_create_request(preview).model_dump(),
    expectedRevisionId="10000000-0000-4000-8000-000000000010",
    expectedSnapshotVersion=7,
  )

  response, status = application.create_revision(
    "experiment-1", payload, "revision-key-1", "req-revision",
  )

  assert status == 201
  assert response["revisionId"] == "revision-2"
  assert repository.created_revision is not None
  assert repository.created_revision["expected_snapshot_version"] == 7
  assert repository.created_revision["preview_token"] == preview["previewToken"]


def test_retry_application_forwards_frozen_selection_and_idempotency(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _enable_experiments(monkeypatch)
  repository = MemoryExperimentRepository()
  application = _application(repository)
  trial_id = "10000000-0000-4000-8000-000000000020"
  preview_request = ExperimentRetryPreviewRequest(
    expectedRevisionId="10000000-0000-4000-8000-000000000010",
    selection={"mode": "trial_ids", "trialIds": [trial_id]},
  )
  summary = application.preview_retry("experiment-1", preview_request)
  retry_request = ExperimentRetryFailedRequest(
    **preview_request.model_dump(),
    expectedSelectedTrialCount=summary["selectedTrialCount"],
    expectedSelectionDigest=summary["selectionDigest"],
    reason="人工确认",
  )

  response, status = application.retry_failed(
    "experiment-1", retry_request, "retry-key-1", "req-retry",
  )

  assert status == 202
  assert response["selectionDigest"] == "b" * 64
  assert repository.retried is not None
  assert repository.retried["selection"]["trialIds"] == [trial_id]
  assert repository.retried["reason"] == "人工确认"


def test_coordinator_resumes_after_batch_failure_without_reordering(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  _enable_experiments(monkeypatch, batch_size=3)
  preview_repository = MemoryExperimentRepository()
  preview = _application(preview_repository).preview(_request())
  generation_repository = MemoryGenerationRepository(_generation_stage(preview))
  coordinator = ExperimentCoordinator(
    repository=generation_repository,
    worker_id="worker-1",
  )
  generation_repository.fail_next_commit = True

  with pytest.raises(state_store.DatabaseError):
    coordinator.run_once()
  while coordinator.run_once():
    pass

  assert [item["trialIndex"] for item in generation_repository.trials] == list(range(8))
  assert len({item["trialFingerprint"] for item in generation_repository.trials}) == 8
  assert generation_repository.stage["status"] == "queued"


def test_create_route_requires_and_forwards_idempotency_key(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  preview = {
    "previewToken": "10000000-0000-4000-8000-000000000001",
    "specHash": "a" * 64,
  }
  payload = ExperimentCreateRequest(
    **preview,
    experimentSpec=_request(),
  )

  class FakeApplication:
    def create(self, _payload: Any, key: str, request_id: str) -> tuple[dict[str, Any], int]:
      assert key == "create-key-1"
      assert request_id == "req-create"
      return {"experimentId": "experiment-1", "status": "preparing"}, 201

  monkeypatch.setattr(app_module, "_experiment_application", FakeApplication())
  request = Request({
    "type": "http",
    "method": "POST",
    "path": "/api/backtest/experiments",
    "headers": [(b"idempotency-key", b"create-key-1")],
  })
  request.state.request_id = "req-create"

  response = app_module.create_experiment(payload, request)

  assert response.status_code == 201


def test_revision_routes_forward_optimistic_identity_and_history_filter(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  experiment_id = UUID("10000000-0000-4000-8000-000000000001")
  revision_id = UUID("10000000-0000-4000-8000-000000000010")
  payload = ExperimentRevisionCreateRequest(
    previewToken="10000000-0000-4000-8000-000000000011",
    specHash="a" * 64,
    experimentSpec=_request(),
    expectedRevisionId=revision_id,
    expectedSnapshotVersion=7,
  )

  class FakeApplication:
    def create_revision(
      self,
      forwarded_experiment_id: str,
      forwarded_payload: ExperimentRevisionCreateRequest,
      key: str,
      request_id: str,
    ) -> tuple[dict[str, Any], int]:
      assert forwarded_experiment_id == str(experiment_id)
      assert forwarded_payload.expectedRevisionId == revision_id
      assert forwarded_payload.expectedSnapshotVersion == 7
      assert key == "revision-key-1"
      assert request_id == "req-revision"
      return {"experimentId": str(experiment_id), "revisionId": "revision-2"}, 201

    def list_trials(self, forwarded_experiment_id: str, **values: Any) -> dict[str, Any]:
      assert forwarded_experiment_id == str(experiment_id)
      assert values["revision_id"] == str(revision_id)
      return {"items": [], "nextCursor": None}

  monkeypatch.setattr(app_module, "_experiment_application", FakeApplication())
  request = Request({
    "type": "http",
    "method": "POST",
    "path": f"/api/backtest/experiments/{experiment_id}/revisions",
    "headers": [(b"idempotency-key", b"revision-key-1")],
  })
  request.state.request_id = "req-revision"

  response = app_module.create_experiment_revision(experiment_id, payload, request)
  trials = app_module.experiment_trials(
    experiment_id, limit=50, cursor=None, revision_id=revision_id, status=None,
  )

  assert response.status_code == 201
  assert trials == {"items": [], "nextCursor": None}


def test_retry_routes_forward_frozen_selection_and_idempotency(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  experiment_id = UUID("10000000-0000-4000-8000-000000000001")
  revision_id = UUID("10000000-0000-4000-8000-000000000010")
  trial_id = UUID("10000000-0000-4000-8000-000000000020")
  preview_payload = ExperimentRetryPreviewRequest(
    expectedRevisionId=revision_id,
    selection={"mode": "trial_ids", "trialIds": [trial_id]},
  )
  retry_payload = ExperimentRetryFailedRequest(
    **preview_payload.model_dump(),
    expectedSelectedTrialCount=1,
    expectedSelectionDigest="b" * 64,
    reason="人工确认",
  )

  class FakeApplication:
    def preview_retry(
      self,
      forwarded_experiment_id: str,
      forwarded_payload: ExperimentRetryPreviewRequest,
    ) -> dict[str, Any]:
      assert forwarded_experiment_id == str(experiment_id)
      assert forwarded_payload.selection.trialIds == [trial_id]
      return {"selectedTrialCount": 1, "selectionDigest": "b" * 64}

    def retry_failed(
      self,
      forwarded_experiment_id: str,
      forwarded_payload: ExperimentRetryFailedRequest,
      key: str,
      request_id: str,
    ) -> tuple[dict[str, Any], int]:
      assert forwarded_experiment_id == str(experiment_id)
      assert forwarded_payload.expectedSelectedTrialCount == 1
      assert forwarded_payload.expectedSelectionDigest == "b" * 64
      assert key == "retry-key-1"
      assert request_id == "req-retry"
      return {"experimentId": str(experiment_id), "status": "running"}, 202

  monkeypatch.setattr(app_module, "_experiment_application", FakeApplication())
  request = Request({
    "type": "http",
    "method": "POST",
    "path": f"/api/backtest/experiments/{experiment_id}/retry-failed",
    "headers": [(b"idempotency-key", b"retry-key-1")],
  })
  request.state.request_id = "req-retry"

  preview = app_module.preview_experiment_retry_failed(experiment_id, preview_payload)
  response = app_module.retry_experiment_failed(experiment_id, retry_payload, request)

  assert preview["selectionDigest"] == "b" * 64
  assert response.status_code == 202


def test_generation_migration_enforces_identity_and_rls_contracts() -> None:
  sql = Path("db/migrations/045_backtest_experiment_generation.sql").read_text(
    encoding="utf-8",
  )

  assert "UNIQUE (stage_id, trial_index)" in sql
  assert "UNIQUE (stage_id, trial_fingerprint)" in sql
  assert "UNIQUE (owner_user_id, operation_type, idempotency_key)" in sql
  assert sql.count("FORCE ROW LEVEL SECURITY") == 8
  assert "backtest_experiment_current_revision_fk" in sql
  assert "backtest_experiment_trial_stage_owner_fk" in sql
  assert "reserved_planned_trial_count" in sql
