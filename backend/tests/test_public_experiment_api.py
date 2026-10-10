from __future__ import annotations

import hashlib
from typing import Any

import pytest

from backend import app as api_app
from backend.experiments.errors import ExperimentError
from backend.services import auth
from backend.services.user_context import CurrentUser
from backend.tests.test_experiment_preview import _request as _experiment_request
from backend.tests.test_public_strategy_api import _request


EXPERIMENT_ID = "10000000-0000-4000-8000-000000000001"
REVISION_ID = "10000000-0000-4000-8000-000000000002"
TRIAL_ID = "10000000-0000-4000-8000-000000000003"
ALL_EXPERIMENT_SCOPES = frozenset({
  "experiment:submit",
  "experiment:read",
  "experiment:cancel",
})


def _principal(
  scopes: frozenset[str] = ALL_EXPERIMENT_SCOPES,
  role: str = "user",
  key_id: str = "key-1",
) -> tuple[CurrentUser, auth.ApiKeyPrincipal]:
  return (
    CurrentUser(id="user-1", username="tester", display_name="测试用户", role=role),
    auth.ApiKeyPrincipal(id=key_id, owner_user_id="user-1", scopes=scopes),
  )


def _preview_payload() -> dict[str, Any]:
  return _experiment_request().model_dump(mode="json")


def _create_payload() -> dict[str, Any]:
  return {
    "previewToken": "10000000-0000-4000-8000-000000000010",
    "specHash": "a" * 64,
    "experimentSpec": _preview_payload(),
  }


def _retry_preview_payload() -> dict[str, Any]:
  return {
    "expectedRevisionId": REVISION_ID,
    "selection": {"mode": "trial_ids", "trialIds": [TRIAL_ID]},
  }


class FakeExperimentApplication:
  def __init__(self) -> None:
    self.calls: list[tuple[Any, ...]] = []

  def preview(self, payload: Any) -> dict[str, Any]:
    self.calls.append(("preview", payload.name))
    return {"previewToken": "token-1", "plannedTrialCount": 2}

  def create(
    self,
    _payload: Any,
    idempotency_key: str,
    request_id: str,
  ) -> tuple[dict[str, Any], int]:
    self.calls.append(("create", idempotency_key, request_id))
    return {"experimentId": EXPERIMENT_ID, "status": "preparing", "replayed": False}, 201

  def list_experiments(self, **values: Any) -> dict[str, Any]:
    self.calls.append(("list", values))
    return {"items": [{"experimentId": EXPERIMENT_ID}], "nextCursor": None}

  def detail(self, experiment_id: str) -> dict[str, Any]:
    self.calls.append(("detail", experiment_id))
    return {"experimentId": experiment_id, "snapshotVersion": 3}

  def list_trials(self, experiment_id: str, **values: Any) -> dict[str, Any]:
    self.calls.append(("trials", experiment_id, values))
    return {"items": [{"trialId": TRIAL_ID}], "nextCursor": None}

  def preview_retry(self, experiment_id: str, _payload: Any) -> dict[str, Any]:
    self.calls.append(("retry_preview", experiment_id))
    return {"selectedTrialCount": 1, "selectionDigest": "b" * 64}

  def retry_failed(
    self,
    experiment_id: str,
    _payload: Any,
    idempotency_key: str,
    request_id: str,
  ) -> tuple[dict[str, Any], int]:
    self.calls.append(("retry", experiment_id, idempotency_key, request_id))
    return {"experimentId": experiment_id, "status": "running", "replayed": False}, 202

  def resume(
    self,
    experiment_id: str,
    revision_id: str,
    reason: str,
    idempotency_key: str,
    request_id: str,
  ) -> tuple[dict[str, Any], int]:
    self.calls.append((
      "resume", experiment_id, revision_id, reason, idempotency_key, request_id,
    ))
    return {"experimentId": experiment_id, "status": "running", "replayed": False}, 202

  def cancel(
    self,
    experiment_id: str,
    idempotency_key: str,
    request_id: str,
  ) -> tuple[dict[str, Any], int]:
    self.calls.append(("cancel", experiment_id, idempotency_key, request_id))
    return {"experimentId": experiment_id, "status": "cancelled", "replayed": False}, 200


@pytest.fixture
def public_experiment_api(
  monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeExperimentApplication, list[dict[str, Any]]]:
  application = FakeExperimentApplication()
  audits: list[dict[str, Any]] = []
  monkeypatch.setattr(api_app.settings, "PUBLIC_API_ENABLED", True)
  monkeypatch.setattr(api_app.auth, "get_api_key_principal", lambda _: _principal())
  monkeypatch.setattr(api_app, "_experiment_application", application)
  monkeypatch.setattr(
    api_app.auth,
    "audit_api_key_event",
    lambda key_id, owner_user_id, event_type, detail, request_id: audits.append({
      "keyId": key_id,
      "ownerUserId": owner_user_id,
      "eventType": event_type,
      "detail": detail,
      "requestId": request_id,
    }),
  )
  return application, audits


def test_public_experiment_preview_and_read_routes(
  public_experiment_api: tuple[FakeExperimentApplication, list[dict[str, Any]]],
) -> None:
  application, _audits = public_experiment_api
  headers = {"Authorization": "Bearer test"}

  preview = _request(
    "POST", "/api/v1/experiments/preview", json_body=_preview_payload(), headers=headers,
  )
  listed = _request("GET", "/api/v1/experiments", headers=headers)
  detail = _request("GET", f"/api/v1/experiments/{EXPERIMENT_ID}", headers=headers)
  trials = _request(
    "GET", f"/api/v1/experiments/{EXPERIMENT_ID}/trials", headers=headers,
  )

  assert preview.status_code == 200
  assert preview.json()["plannedTrialCount"] == 2
  assert listed.json()["items"][0]["experimentId"] == EXPERIMENT_ID
  assert detail.json()["snapshotVersion"] == 3
  assert trials.json()["items"][0]["trialId"] == TRIAL_ID
  assert ("list", {
    "limit": 50,
    "cursor": None,
    "archived": False,
    "query": "",
    "status": None,
  }) in application.calls
  assert ("trials", EXPERIMENT_ID, {
    "limit": 50,
    "cursor": None,
    "status": None,
    "revision_id": None,
    "query": "",
    "date_scenario_id": None,
  }) in application.calls


def test_public_experiment_write_routes_namespace_idempotency_and_audit(
  public_experiment_api: tuple[FakeExperimentApplication, list[dict[str, Any]]],
) -> None:
  application, audits = public_experiment_api
  headers = {
    "Authorization": "Bearer test",
    "Idempotency-Key": "agent-run-0001",
  }
  expected_key = "public:" + hashlib.sha256(
    b"key-1:agent-run-0001",
  ).hexdigest()

  created = _request(
    "POST", "/api/v1/experiments", json_body=_create_payload(), headers=headers,
  )
  retry_preview = _request(
    "POST",
    f"/api/v1/experiments/{EXPERIMENT_ID}/retry-failed/preview",
    json_body=_retry_preview_payload(),
    headers={"Authorization": "Bearer test"},
  )
  retried = _request(
    "POST",
    f"/api/v1/experiments/{EXPERIMENT_ID}/retry-failed",
    json_body={
      **_retry_preview_payload(),
      "expectedSelectedTrialCount": 1,
      "expectedSelectionDigest": "b" * 64,
      "reason": "人工重试",
    },
    headers=headers,
  )
  resumed = _request(
    "POST",
    f"/api/v1/experiments/{EXPERIMENT_ID}/resume",
    json_body={"expectedRevisionId": REVISION_ID, "reason": "继续运行"},
    headers=headers,
  )
  cancelled = _request(
    "POST", f"/api/v1/experiments/{EXPERIMENT_ID}/cancel", headers=headers,
  )

  assert created.status_code == 201
  assert retry_preview.status_code == 200
  assert retried.status_code == 202
  assert resumed.status_code == 202
  assert cancelled.status_code == 200
  write_calls = [call for call in application.calls if call[0] in {
    "create", "retry", "resume", "cancel",
  }]
  assert all(expected_key in call for call in write_calls)
  assert [item["eventType"] for item in audits] == [
    "experiment.create",
    "experiment.retry_failed",
    "experiment.resume",
    "experiment.cancel",
  ]
  assert all(item["detail"]["resourceId"] == EXPERIMENT_ID for item in audits)
  assert all(set(item["detail"]) == {"operation", "resourceId", "replayed"} for item in audits)


def test_public_experiment_scope_is_not_inherited_from_admin_role(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(api_app.settings, "PUBLIC_API_ENABLED", True)
  monkeypatch.setattr(
    api_app.auth,
    "get_api_key_principal",
    lambda _: _principal(frozenset({"backtest:read"}), role="admin"),
  )

  response = _request(
    "GET", "/api/v1/experiments", headers={"Authorization": "Bearer test"},
  )

  assert response.status_code == 403
  assert response.json()["error"]["code"] == "api_scope_denied"


def test_public_experiment_write_requires_idempotency_key(
  public_experiment_api: tuple[FakeExperimentApplication, list[dict[str, Any]]],
) -> None:
  response = _request(
    "POST",
    "/api/v1/experiments",
    json_body=_create_payload(),
    headers={"Authorization": "Bearer test"},
  )

  assert response.status_code == 422
  assert response.json()["error"]["code"] == "experiment.validation_failed"


def test_public_experiment_errors_keep_code_retryability_and_details(
  public_experiment_api: tuple[FakeExperimentApplication, list[dict[str, Any]]],
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class FailingApplication(FakeExperimentApplication):
    def preview(self, _payload: Any) -> dict[str, Any]:
      raise ExperimentError(
        "experiment.plan_limit_exceeded",
        "实验计划超过当前硬上限",
        429,
        retryable=False,
        details={"blockingReasons": ["trial_limit"]},
      )

  monkeypatch.setattr(api_app, "_experiment_application", FailingApplication())
  headers = {"Authorization": "Bearer test"}

  failed = _request(
    "POST", "/api/v1/experiments/preview", json_body=_preview_payload(), headers=headers,
  )
  invalid = _request(
    "POST", "/api/v1/experiments/preview", json_body={"name": "缺少字段"}, headers=headers,
  )

  assert failed.status_code == 429
  assert failed.json()["error"]["code"] == "experiment.plan_limit_exceeded"
  assert failed.json()["error"]["retryable"] is False
  assert failed.json()["error"]["details"] == {"blockingReasons": ["trial_limit"]}
  assert failed.json()["error"]["requestId"].startswith("req_")
  assert failed.json()["error"]["request_id"] == failed.json()["error"]["requestId"]
  assert invalid.status_code == 422
  assert invalid.json()["error"]["code"] == "experiment.validation_failed"
  assert "schemaVersion" in invalid.json()["error"]["details"]["fields"]


def test_public_experiment_idempotency_namespace_changes_with_api_key() -> None:
  request = api_app.Request({
    "type": "http",
    "method": "POST",
    "path": "/api/v1/experiments",
    "headers": [(b"idempotency-key", b"agent-run-0001")],
  })
  first = api_app._public_experiment_idempotency_key(request, _principal(key_id="key-1")[1])
  second = api_app._public_experiment_idempotency_key(request, _principal(key_id="key-2")[1])

  assert first != second
  assert len(first) == len("public:") + 64
  assert len(second) == len("public:") + 64
