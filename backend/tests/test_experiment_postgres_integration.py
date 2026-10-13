from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable, Iterator

import psycopg
from psycopg.rows import dict_row
import pytest

from backend import settings
from backend.experiments.application import ExperimentApplication
from backend.experiments.compiler import LegacySingleBarTrialCompiler
from backend.experiments.contracts import (
  EvaluationExportCreateRequest,
  EvaluationSpecRequest,
  ExperimentCreateRequest,
  ExperimentLifecycleRequest,
  ExperimentPurgeRequest,
  ExperimentRetryFailedRequest,
  ExperimentRetryPreviewRequest,
  ExperimentRevisionCreateRequest,
)
from backend.experiments.coordinator import ExperimentCoordinator
from backend.experiments.errors import ExperimentError
from backend.experiments.evaluation_application import EvaluationApplication
from backend.experiments.evaluation_worker import EvaluationWorker
from backend.experiments.export_application import EvaluationExportApplication
from backend.experiments.export_repository import PostgresEvaluationExportRepository
from backend.experiments.repository import PostgresExperimentRepository
from backend.experiments.purge_repository import PostgresExperimentPurgeRepository
from backend.experiments.purge_worker import ExperimentPurgeWorker
from backend.experiments.rolling_window import GeneratedBatch, RollingTrialWindow
from backend.experiments.storage_pressure import StorageDecision
from backend.services import auth, state_store
from backend.services.user_context import (
  CurrentUser,
  reset_current_user,
  set_current_user,
)
from backend.tests.test_experiment_preview import (
  StaticDatasetResolver,
  StaticResolver,
  _request,
  _template,
)
from backend.tests.test_experiment_execution import (
  _assignment,
  _dataset,
  _execution_template,
)


pytestmark = pytest.mark.skipif(
  os.getenv("RUN_EXPERIMENT_DB_INTEGRATION") != "1",
  reason="需要已迁移的隔离 PostgreSQL",
)

OWNER = CurrentUser(
  id="10000000-0000-4000-8000-000000000001",
  username="experiment-owner",
  display_name="Experiment Owner",
  role="user",
)
OTHER = CurrentUser(
  id="10000000-0000-4000-8000-000000000002",
  username="experiment-other",
  display_name="Experiment Other",
  role="user",
)
OWNER_ADMIN = CurrentUser(
  id=OWNER.id,
  username=OWNER.username,
  display_name=OWNER.display_name,
  role="admin",
)


def _as_user(user: CurrentUser, action: Callable[[], Any]) -> Any:
  token = set_current_user(user)
  try:
    return action()
  finally:
    reset_current_user(token)


@pytest.fixture(scope="module", autouse=True)
def users() -> None:
  admin_url = os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]
  with psycopg.connect(admin_url) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        DELETE FROM bar_backtest_job job USING backtest_run run
        WHERE job.run_id=run.id AND run.origin_type='manual'
          AND run.owner_user_id IN (%s,%s)
      """, (OWNER.id, OTHER.id))
      cur.execute("""
        DELETE FROM backtest_run
        WHERE origin_type='manual' AND owner_user_id IN (%s,%s)
      """, (OWNER.id, OTHER.id))
      cur.execute("""
        UPDATE backtest_run SET origin_trial_execution_id=NULL
        WHERE origin_type='experiment'
      """)
      cur.execute("UPDATE backtest_experiment SET purge_job_id=NULL")
      cur.execute("DELETE FROM backtest_experiment_purge_item")
      cur.execute("DELETE FROM backtest_experiment_purge")
      cur.execute("DELETE FROM backtest_experiment_audit")
      cur.execute("DELETE FROM backtest_signal_detail_export")
      cur.execute("DELETE FROM backtest_experiment_evaluation_export")
      cur.execute("DELETE FROM backtest_experiment_evaluation_exception")
      cur.execute("DELETE FROM backtest_experiment_evaluation_group_result")
      cur.execute("DELETE FROM backtest_experiment_evaluation_result_row")
      cur.execute("DELETE FROM backtest_experiment_evaluation_execution")
      cur.execute("DELETE FROM backtest_experiment_evaluation_run")
      cur.execute("DELETE FROM backtest_experiment_evaluation_spec")
      cur.execute("DELETE FROM backtest_experiment_dispatch_outbox")
      cur.execute("DELETE FROM backtest_experiment_window_event")
      cur.execute("DELETE FROM backtest_experiment_generation_token")
      cur.execute("DELETE FROM backtest_experiment_window_stage")
      cur.execute("DELETE FROM backtest_experiment_window_user")
      cur.execute("""
        UPDATE backtest_experiment_window_global
        SET open_count=0, reserved_count=0, next_user_sequence=1,
          row_version=row_version+1, updated_at=now()
        WHERE singleton
      """)
      cur.execute("DELETE FROM backtest_experiment_trial_execution")
      cur.execute("DELETE FROM backtest_run WHERE origin_type='experiment'")
      cur.execute("DELETE FROM backtest_experiment_trial")
      cur.execute("DELETE FROM backtest_experiment_stage")
      cur.execute("DELETE FROM backtest_experiment_asset_hold")
      cur.execute("UPDATE backtest_experiment SET current_revision_id=NULL")
      cur.execute("DELETE FROM backtest_experiment_revision")
      cur.execute("DELETE FROM backtest_internal_idempotency")
      cur.execute("DELETE FROM backtest_experiment")
      cur.execute("DELETE FROM backtest_experiment_user_quota")
      cur.execute("DELETE FROM backtest_experiment_preview")
      cur.execute("""
        UPDATE backtest_scheduler_capacity
        SET override_limit=NULL, override_expires_at=NULL, update_reason=''
        WHERE singleton
      """)
      for user in (OWNER, OTHER):
        cur.execute("""
          INSERT INTO app_user (
            id, username, display_name, password_hash, role, status
          ) VALUES (%s,%s,%s,'test-only','user','active')
          ON CONFLICT (id) DO NOTHING
        """, (user.id, user.username, user.display_name))
      cur.execute("""
        INSERT INTO market_instrument (
          symbol, exchange, display_name, instrument_type, board, eastmoney_secid
        ) VALUES ('688981','SH','测试标的','stock','star','1.688981')
        ON CONFLICT (exchange, symbol) DO NOTHING
      """)


@pytest.fixture()
def application(monkeypatch: pytest.MonkeyPatch) -> ExperimentApplication:
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_VALUES_PER_DIMENSION", 50)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_TRIALS", 1000)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_USER_PLANNED_TRIALS", 1000)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_MAX_ESTIMATED_BARS", 10_000_000)
  monkeypatch.setattr(settings, "EXPERIMENT_PREVIEW_TTL_SECONDS", 900)
  monkeypatch.setattr(settings, "EXPERIMENT_GENERATION_BATCH_SIZE", 3)
  monkeypatch.setattr(settings, "EXPERIMENT_COORDINATOR_LEASE_SECONDS", 30)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_USER", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_MANUAL_RESERVED_PERMITS", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_MANUAL_MAX_WAIT_SECONDS", 60)
  monkeypatch.setattr(settings, "EXPERIMENT_CURSOR_SIGNING_SECRET", "s" * 32)
  return ExperimentApplication(
    resolver=StaticResolver(_template()),
    dataset_resolver=StaticDatasetResolver(),
    repository=PostgresExperimentRepository(),
  )


class ExecutionDatasetResolver:
  def resolve(
    self,
    _template: object,
    scenarios: tuple[object, ...],
  ) -> tuple[dict[str, Any], ...]:
    return tuple({
      **_dataset(),
      "formalDateCount": len(_dataset()["formalDates"]),
      "warmupDateCount": len(_dataset()["warmupDates"]),
      "scenario": scenario.model_dump(),  # type: ignore[attr-defined]
    } for scenario in scenarios)


class GenerationOnlyRepository:
  def __init__(self) -> None:
    self._repository = PostgresExperimentRepository()

  def claim_generation_stage(self, *args: Any) -> dict[str, Any] | None:
    return self._repository.claim_generation_stage(*args)

  def commit_generation_batch(self, *args: Any) -> bool:
    return self._repository.commit_generation_batch(*args)

  def block_generation(self, *args: Any) -> None:
    self._repository.block_generation(*args)


class AlwaysOpenStorageGate:
  def evaluate(self, now: datetime | None = None) -> StorageDecision:
    return StorageDecision(
      status="open",
      reasons=(),
      missing_signals=(),
      observed_at=now or datetime.now(timezone.utc),
      signals={},
    )


@pytest.fixture()
def execution_application(monkeypatch: pytest.MonkeyPatch) -> ExperimentApplication:
  _clear_experiment_facts()
  monkeypatch.setattr(settings, "EXPERIMENTS_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_VALUES_PER_DIMENSION", 50)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_TRIALS", 1000)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_USER_PLANNED_TRIALS", 1000)
  monkeypatch.setattr(settings, "EXPERIMENT_BAR_MAX_ESTIMATED_BARS", 10_000_000)
  monkeypatch.setattr(settings, "EXPERIMENT_PREVIEW_TTL_SECONDS", 900)
  monkeypatch.setattr(settings, "EXPERIMENT_GENERATION_BATCH_SIZE", 3)
  monkeypatch.setattr(settings, "EXPERIMENT_COORDINATOR_LEASE_SECONDS", 30)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_USER", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_MANUAL_RESERVED_PERMITS", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_MANUAL_MAX_WAIT_SECONDS", 60)
  monkeypatch.setattr(settings, "EXPERIMENT_CURSOR_SIGNING_SECRET", "s" * 32)
  return ExperimentApplication(
    resolver=StaticResolver(_execution_template()),
    dataset_resolver=ExecutionDatasetResolver(),
    repository=PostgresExperimentRepository(),
  )


def _clear_experiment_facts() -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        DELETE FROM bar_backtest_job job USING backtest_run run
        WHERE job.run_id=run.id AND run.origin_type='manual'
          AND run.owner_user_id IN (%s,%s)
      """, (OWNER.id, OTHER.id))
      cur.execute("""
        DELETE FROM backtest_run
        WHERE origin_type='manual' AND owner_user_id IN (%s,%s)
      """, (OWNER.id, OTHER.id))
      cur.execute("""
        UPDATE backtest_run SET origin_trial_execution_id=NULL
        WHERE origin_type='experiment'
      """)
      cur.execute("UPDATE backtest_experiment SET purge_job_id=NULL")
      cur.execute("DELETE FROM backtest_experiment_purge_item")
      cur.execute("DELETE FROM backtest_experiment_purge")
      cur.execute("DELETE FROM backtest_experiment_audit")
      cur.execute("DELETE FROM backtest_signal_detail_export")
      cur.execute("DELETE FROM backtest_experiment_evaluation_export")
      cur.execute("DELETE FROM backtest_experiment_evaluation_exception")
      cur.execute("DELETE FROM backtest_experiment_evaluation_group_result")
      cur.execute("DELETE FROM backtest_experiment_evaluation_result_row")
      cur.execute("DELETE FROM backtest_experiment_evaluation_execution")
      cur.execute("DELETE FROM backtest_experiment_evaluation_run")
      cur.execute("DELETE FROM backtest_experiment_evaluation_spec")
      cur.execute("DELETE FROM backtest_experiment_dispatch_outbox")
      cur.execute("DELETE FROM backtest_experiment_window_event")
      cur.execute("DELETE FROM backtest_experiment_generation_token")
      cur.execute("DELETE FROM backtest_experiment_window_stage")
      cur.execute("DELETE FROM backtest_experiment_window_user")
      cur.execute("""
        UPDATE backtest_experiment_window_global
        SET open_count=0, reserved_count=0, next_user_sequence=1,
          row_version=row_version+1, updated_at=now()
        WHERE singleton
      """)
      cur.execute("DELETE FROM backtest_experiment_trial_execution")
      cur.execute("DELETE FROM backtest_run WHERE origin_type='experiment'")
      cur.execute("DELETE FROM backtest_experiment_trial")
      cur.execute("DELETE FROM backtest_experiment_stage")
      cur.execute("DELETE FROM backtest_experiment_asset_hold")
      cur.execute("UPDATE backtest_experiment SET current_revision_id=NULL")
      cur.execute("DELETE FROM backtest_experiment_revision")
      cur.execute("DELETE FROM backtest_internal_idempotency")
      cur.execute("DELETE FROM backtest_experiment")
      cur.execute("DELETE FROM backtest_experiment_user_quota")
      cur.execute("DELETE FROM backtest_experiment_preview")
      cur.execute("""
        UPDATE backtest_scheduler_capacity
        SET override_limit=NULL, override_expires_at=NULL, update_reason=''
        WHERE singleton
      """)


def _preview_and_payload(
  application: ExperimentApplication,
) -> tuple[dict[str, Any], ExperimentCreateRequest]:
  preview = _as_user(OWNER, lambda: application.preview(_request()))
  payload = ExperimentCreateRequest(
    previewToken=preview["previewToken"],
    specHash=preview["specHash"],
    experimentSpec=_request(),
  )
  return preview, payload


def test_concurrent_same_key_replays_one_experiment_and_generates_exact_trials(
  application: ExperimentApplication,
) -> None:
  preview, payload = _preview_and_payload(application)

  def create() -> tuple[dict[str, Any], int]:
    return _as_user(
      OWNER,
      lambda: application.create(payload, "postgres-same-key", "req-postgres"),
    )

  with ThreadPoolExecutor(max_workers=2) as executor:
    results = list(executor.map(lambda _index: create(), range(2)))

  experiment_ids = {item[0]["experimentId"] for item in results}
  assert len(experiment_ids) == 1
  assert sorted(item[1] for item in results) == [200, 201]
  with pytest.raises(ExperimentError) as conflict:
    changed = payload.model_copy(deep=True)
    changed.experimentSpec.description = "同键不同请求"
    _as_user(OWNER, lambda: application.create(changed, "postgres-same-key"))
  assert conflict.value.code == "idempotency.conflict"

  coordinator = ExperimentCoordinator(
    repository=GenerationOnlyRepository(),
    worker_id="postgres-test-worker",
  )
  assert coordinator.run_once()
  restarted = ExperimentCoordinator(
    repository=GenerationOnlyRepository(),
    worker_id="postgres-test-worker-restarted",
  )
  while restarted.run_once():
    pass

  experiment_id = next(iter(experiment_ids))
  detail = _as_user(OWNER, lambda: application.detail(experiment_id))
  assert detail["status"] == "queued"
  assert detail["stage"]["generatedTrialCount"] == preview["plannedTrialCount"]
  assert detail["stage"]["trialCollectionDigest"]
  _assert_trial_collection(experiment_id, preview["plannedTrialCount"])


def test_copied_experiment_preserves_owned_source_lineage(
  application: ExperimentApplication,
) -> None:
  _preview, source_payload = _preview_and_payload(application)
  source, _status = _as_user(
    OWNER,
    lambda: application.create(source_payload, "postgres-copy-source"),
  )
  copy_preview, base_copy_payload = _preview_and_payload(application)
  copy_payload = ExperimentCreateRequest(
    **base_copy_payload.model_dump(exclude={"source"}),
    source={
      "experimentId": source["experimentId"],
      "revisionId": source["revisionId"],
    },
  )

  copied, copied_status = _as_user(
    OWNER,
    lambda: application.create(copy_payload, "postgres-copy-derived"),
  )
  detail = _as_user(OWNER, lambda: application.detail(copied["experimentId"]))
  page = _as_user(
    OWNER,
    lambda: application.list_experiments(limit=100, cursor=None),
  )
  listed = next(
    item for item in page["items"] if item["experimentId"] == copied["experimentId"]
  )

  assert copied_status == 201
  assert detail["source"] == {
    "experimentId": source["experimentId"],
    "experimentName": source_payload.experimentSpec.name,
    "revisionId": source["revisionId"],
    "revisionNo": 1,
  }
  assert listed["source"] == detail["source"]
  assert detail["revision"]["revisionNo"] == 1
  assert copy_preview["specHash"] == detail["revision"]["specHash"]

  other_preview = _as_user(OTHER, lambda: application.preview(_request()))
  unauthorized = ExperimentCreateRequest(
    previewToken=other_preview["previewToken"],
    specHash=other_preview["specHash"],
    experimentSpec=_request(),
    source=copy_payload.source,
  )
  with pytest.raises(ExperimentError) as hidden:
    _as_user(
      OTHER,
      lambda: application.create(unauthorized, "postgres-copy-cross-owner"),
    )
  assert hidden.value.code == "experiment.source_not_found"


def test_experiment_list_filters_search_status_and_cursor_scope(
  application: ExperimentApplication,
) -> None:
  _clear_experiment_facts()

  def create_named(name: str, key: str) -> str:
    request = _request().model_copy(update={"name": name})
    preview = _as_user(OWNER, lambda: application.preview(request))
    payload = ExperimentCreateRequest(
      previewToken=preview["previewToken"],
      specHash=preview["specHash"],
      experimentSpec=request,
    )
    created, _status = _as_user(
      OWNER, lambda: application.create(payload, key),
    )
    return created["experimentId"]

  alpha_id = create_named("Alpha 参数研究", "postgres-list-filter-alpha")
  create_named("Beta 参数研究", "postgres-list-filter-beta")

  name_page = _as_user(
    OWNER,
    lambda: application.list_experiments(
      limit=20, cursor=None, query="Alpha", status="preparing",
    ),
  )
  strategy_page = _as_user(
    OWNER,
    lambda: application.list_experiments(
      limit=20, cursor=None, query="均值回归",
    ),
  )
  symbol_page = _as_user(
    OWNER,
    lambda: application.list_experiments(
      limit=20, cursor=None, query="688981.SH",
    ),
  )
  first_page = _as_user(
    OWNER,
    lambda: application.list_experiments(limit=1, cursor=None),
  )

  assert [item["experimentId"] for item in name_page["items"]] == [alpha_id]
  assert len(strategy_page["items"]) == 2
  assert len(symbol_page["items"]) == 2
  assert first_page["nextCursor"] is not None
  with pytest.raises(ExperimentError) as mismatched_cursor:
    _as_user(
      OWNER,
      lambda: application.list_experiments(
        limit=1, cursor=first_page["nextCursor"], query="Alpha",
      ),
    )
  assert mismatched_cursor.value.code == "experiment.cursor_invalid"


def test_cross_user_ids_tokens_and_idempotency_keys_are_not_discoverable(
  application: ExperimentApplication,
) -> None:
  _preview, payload = _preview_and_payload(application)
  response, _status = _as_user(
    OWNER,
    lambda: application.create(payload, "postgres-owner-only"),
  )

  for action in (
    lambda: application.detail(response["experimentId"]),
    lambda: application.find_created("postgres-owner-only"),
    lambda: application.create(payload, "postgres-other-user"),
  ):
    with pytest.raises(ExperimentError) as hidden:
      _as_user(OTHER, action)
    assert hidden.value.code == "experiment.not_found"


def test_concurrent_creates_cannot_exceed_user_plan_quota(
  application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_USER_PLANNED_TRIALS", 8)
  preview = _as_user(OTHER, lambda: application.preview(_request()))
  payload = ExperimentCreateRequest(
    previewToken=preview["previewToken"],
    specHash=preview["specHash"],
    experimentSpec=_request(),
  )

  def create(key: str) -> str:
    try:
      _as_user(OTHER, lambda: application.create(payload, key))
      return "created"
    except ExperimentError as error:
      return error.code

  with ThreadPoolExecutor(max_workers=2) as executor:
    outcomes = list(executor.map(create, ("quota-key-1", "quota-key-2")))

  assert sorted(outcomes) == ["created", "experiment.plan_limit_exceeded"]


def test_cancelled_preparing_stage_stops_further_generation(
  application: ExperimentApplication,
) -> None:
  preview, payload = _preview_and_payload(application)
  response, _status = _as_user(
    OWNER,
    lambda: application.create(payload, "postgres-cancel-generation"),
  )
  coordinator = ExperimentCoordinator(
    repository=GenerationOnlyRepository(),
    worker_id="postgres-cancel-worker",
  )
  generated_before_cancel = 0
  for _index in range(20):
    assert coordinator.run_once()
    current = _as_user(OWNER, lambda: application.detail(response["experimentId"]))
    generated_before_cancel = current["stage"]["generatedTrialCount"]
    if generated_before_cancel:
      break
  assert generated_before_cancel > 0
  _as_user(
    OWNER,
    lambda: application.cancel(response["experimentId"], "postgres-cancel-key"),
  )

  for _index in range(10):
    if not coordinator.run_once():
      break
  detail = _as_user(OWNER, lambda: application.detail(response["experimentId"]))
  assert detail["status"] == "cancelled"
  assert detail["stage"]["generatedTrialCount"] == generated_before_cancel


def _execution_request() -> Any:
  return _request(
    parameterGrid=[{"inputId": "strategyParameters.window", "values": [30]}],
    capitalScenarios=["100000"],
    costScenarios=[{"scenarioId": "cost-1", "slippage": "0.01"}],
  )


def _create_execution_experiment(
  application: ExperimentApplication,
  key: str,
  request: Any | None = None,
  user: CurrentUser = OWNER,
) -> str:
  request = request or _execution_request()
  preview = _as_user(user, lambda: application.preview(request))
  payload = ExperimentCreateRequest(
    previewToken=preview["previewToken"],
    specHash=preview["specHash"],
    experimentSpec=request,
  )
  response, _status = _as_user(user, lambda: application.create(payload, key))
  return str(response["experimentId"])


def _manual_retry_request(
  application: ExperimentApplication,
  user: CurrentUser,
  experiment_id: str,
) -> ExperimentRetryFailedRequest:
  detail = _as_user(user, lambda: application.detail(experiment_id))
  page = _as_user(
    user,
    lambda: application.list_trials(
      experiment_id, limit=10, cursor=None, status=None,
    ),
  )
  preview_request = ExperimentRetryPreviewRequest(
    expectedRevisionId=detail["revision"]["revisionId"],
    selection={"mode": "trial_ids", "trialIds": [page["items"][0]["trialId"]]},
  )
  summary = _as_user(
    user, lambda: application.preview_retry(experiment_id, preview_request),
  )
  return ExperimentRetryFailedRequest(
    **preview_request.model_dump(),
    expectedSelectedTrialCount=summary["selectedTrialCount"],
    expectedSelectionDigest=summary["selectionDigest"],
    reason="全局容量并发测试",
  )


def _generate_all(worker_id: str) -> None:
  coordinator = ExperimentCoordinator(
    repository=GenerationOnlyRepository(), worker_id=worker_id,
  )
  while coordinator.run_once():
    pass


def _complete_execution_experiment(
  application: ExperimentApplication,
  key: str,
) -> tuple[str, str]:
  experiment_id = _create_execution_experiment(application, key)
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id=f"{key}-worker",
  )
  assert coordinator.run_once()
  run_id = _run_id_for_experiment(experiment_id)
  _finish_run_for_reconciliation(run_id)
  assert coordinator.run_once()
  return experiment_id, run_id


def _two_trial_execution_request() -> Any:
  return _request(
    parameterGrid=[{"inputId": "strategyParameters.window", "values": [30, 40]}],
    capitalScenarios=["100000"],
    costScenarios=[{"scenarioId": "cost-1", "slippage": "0.01"}],
  )


def _four_trial_execution_request() -> Any:
  return _request(
    parameterGrid=[{
      "inputId": "strategyParameters.window",
      "values": [10, 20, 30, 40],
    }],
    capitalScenarios=["100000"],
    costScenarios=[{"scenarioId": "cost-1", "slippage": "0.01"}],
  )


def test_same_user_experiments_materialize_in_stable_round_robin(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT", 2)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_USER", 2)
  first = _create_execution_experiment(
    execution_application, "round-robin-first", _two_trial_execution_request(),
  )
  second = _create_execution_experiment(
    execution_application, "round-robin-second", _two_trial_execution_request(),
  )
  _generate_all("round-robin-generator")
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="round-robin-worker",
  )

  assert coordinator.run_once()
  assert coordinator.run_once()

  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT origin_experiment_id
        FROM backtest_run
        WHERE origin_type='experiment'
        ORDER BY created_at, id
      """)
      materialized = [str(row[0]) for row in cur.fetchall()]
  assert materialized == [first, second]


def test_mixed_users_keep_cross_user_fairness_before_manual_priority(
  execution_application: ExperimentApplication,
) -> None:
  owner_experiment = _create_execution_experiment(
    execution_application, "mixed-owner-experiment",
  )
  other_experiment = _create_execution_experiment(
    execution_application, "mixed-other-experiment", user=OTHER,
  )
  _generate_all("mixed-user-generator")
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="mixed-user-worker",
  )

  assert coordinator.run_once()
  assert coordinator.run_once()
  owner_run_id = _run_id_for_experiment(owner_experiment)
  other_run_id = _run_id_for_experiment(other_experiment)
  manual_payload = LegacySingleBarTrialCompiler().compile_request(
    _execution_template(), _assignment(), _dataset(), {"baseSeed": 13},
  )
  manual_run_id, _display_no = _as_user(
    OWNER, lambda: state_store.create_bar_backtest_job(manual_payload),
  )
  _occupy_scheduler_slot(owner_run_id)

  with psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"], row_factory=dict_row,
  ) as conn:
    with conn.cursor() as cur:
      candidate = state_store._next_backtest_candidate(cur, "bar")

  assert candidate is not None
  assert str(candidate["owner_user_id"]) == OTHER.id
  assert str(candidate["run_id"]) == other_run_id
  assert str(candidate["run_id"]) != manual_run_id
  _delete_manual_run(manual_run_id)


def test_concurrent_users_cannot_exceed_global_experiment_capacity(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "BACKTEST_BASE_CONCURRENCY", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_MANUAL_RESERVED_PERMITS", 0)
  owner_experiment = _create_execution_experiment(
    execution_application, "global-capacity-owner",
  )
  other_experiment = _create_execution_experiment(
    execution_application, "global-capacity-other", user=OTHER,
  )
  _generate_all("global-capacity-generator")

  def materialize(worker_id: str) -> bool:
    return ExperimentCoordinator(
      repository=PostgresExperimentRepository(), worker_id=worker_id,
    ).run_once()

  with ThreadPoolExecutor(max_workers=2) as executor:
    list(executor.map(materialize, ("global-capacity-1", "global-capacity-2")))

  facts = _global_execution_facts()
  assert facts == {"runs": 1, "in_flight": 1}
  assert materialize("global-capacity-block-reporter")
  details = [
    _as_user(OWNER, lambda: execution_application.detail(owner_experiment)),
    _as_user(OTHER, lambda: execution_application.detail(other_experiment)),
  ]
  assert sum(
    item["blockedReason"] is not None
    and item["blockedReason"]["code"] == "experiment.capacity_blocked"
    for item in details
  ) == 1


def test_manual_wait_gate_blocks_then_resumes_same_revision(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT", 2)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_USER", 2)
  experiment_id = _create_execution_experiment(
    execution_application, "manual-wait-gate",
  )
  _generate_all("manual-wait-generator")
  manual_payload = LegacySingleBarTrialCompiler().compile_request(
    _execution_template(), _assignment(), _dataset(), {"baseSeed": 7},
  )
  manual_run_id, _display_no = _as_user(
    OWNER, lambda: state_store.create_bar_backtest_job(manual_payload),
  )
  _age_manual_job(manual_run_id)
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="manual-wait-worker",
  )

  assert coordinator.run_once()
  blocked = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert blocked["blockedReason"]["code"] == "experiment.manual_wait_blocked"
  assert _execution_facts(experiment_id)["runs"] == 0

  _delete_manual_run(manual_run_id)
  assert coordinator.run_once()
  resumed = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert resumed["blockedReason"] is None
  assert _execution_facts(experiment_id)["runs"] == 1


def test_capacity_override_blocks_without_losing_grid_and_resumes(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "capacity-block-resume",
  )
  _generate_all("capacity-generator")
  before = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  _set_scheduler_capacity_override(8)
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="capacity-worker",
  )

  assert coordinator.run_once()
  blocked = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert blocked["status"] == "generation_blocked"
  assert blocked["blockedReason"]["code"] == "experiment.capacity_blocked"
  assert blocked["snapshotVersion"] > before["snapshotVersion"]
  assert blocked["stage"]["generatedTrialCount"] == blocked["stage"]["plannedTrialCount"]
  assert _execution_facts(experiment_id)["runs"] == 0

  _set_scheduler_capacity_override(None)
  assert coordinator.run_once()
  resumed = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert resumed["blockedReason"] is None
  assert resumed["snapshotVersion"] > blocked["snapshotVersion"]
  assert resumed["revision"]["revisionId"] == blocked["revision"]["revisionId"]
  assert _execution_facts(experiment_id)["runs"] == 1


def test_runner_prefers_manual_job_within_selected_user(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "runner-manual-priority",
  )
  _generate_all("runner-priority-generator")
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="runner-priority-worker",
  )
  assert coordinator.run_once()
  experiment_run_id = _run_id_for_experiment(experiment_id)
  manual_payload = LegacySingleBarTrialCompiler().compile_request(
    _execution_template(), _assignment(), _dataset(), {"baseSeed": 11},
  )
  manual_run_id, _display_no = _as_user(
    OWNER, lambda: state_store.create_bar_backtest_job(manual_payload),
  )

  with psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"], row_factory=dict_row,
  ) as conn:
    with conn.cursor() as cur:
      candidate = state_store._next_backtest_candidate(cur, "bar")

  assert candidate is not None
  assert str(candidate["run_id"]) == manual_run_id
  assert str(candidate["run_id"]) != experiment_run_id
  _delete_manual_run(manual_run_id)


def test_materialization_failure_rolls_back_run_execution_outbox_and_permit(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-materialization-rollback",
  )
  original = state_store.create_queued_backtest_job_in_transaction

  def fail_after_run(*args: Any, **kwargs: Any) -> tuple[str, int]:
    original(*args, **kwargs)
    raise RuntimeError("materialization fault")

  monkeypatch.setattr(
    state_store, "create_queued_backtest_job_in_transaction", fail_after_run,
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="rollback-worker",
  )

  with pytest.raises(RuntimeError, match="materialization fault"):
    coordinator.run_once()

  monkeypatch.setattr(
    state_store, "create_queued_backtest_job_in_transaction", original,
  )
  facts = _execution_facts(experiment_id)
  assert facts == {
    "runs": 0,
    "executions": 0,
    "outbox": 0,
    "in_flight": 0,
    "materialization_status": "pending",
  }
  _as_user(
    OWNER,
    lambda: execution_application.cancel(
      experiment_id, "postgres-materialization-rollback-cancel",
    ),
  )


def test_outbox_failure_rolls_back_materialized_run_and_permit(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-outbox-rollback",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="outbox-worker",
  )

  with _reject_outbox_inserts():
    with pytest.raises(state_store.DatabaseError, match="outbox fault"):
      coordinator.run_once()

  assert _execution_facts(experiment_id) == {
    "runs": 0,
    "executions": 0,
    "outbox": 0,
    "in_flight": 0,
    "materialization_status": "pending",
  }
  _as_user(
    OWNER,
    lambda: execution_application.cancel(
      experiment_id, "postgres-outbox-rollback-cancel",
    ),
  )


def test_concurrent_coordinators_create_one_active_trial_execution(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-concurrent-materialization",
  )
  generation = ExperimentCoordinator(
    repository=GenerationOnlyRepository(), worker_id="generation-worker",
  )
  while generation.run_once():
    pass

  def materialize(worker_id: str) -> bool:
    return ExperimentCoordinator(
      repository=PostgresExperimentRepository(), worker_id=worker_id,
    ).run_once()

  with ThreadPoolExecutor(max_workers=2) as executor:
    outcomes = list(executor.map(materialize, ("materializer-1", "materializer-2")))

  assert sorted(outcomes) == [False, True]
  facts = _execution_facts(experiment_id)
  assert facts["runs"] == 1
  assert facts["executions"] == 1
  assert facts["outbox"] == 1
  assert facts["in_flight"] == 1


def test_complete_standard_bar_execution_reconciles_once_and_keeps_window_bounded(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-complete-execution",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="execution-worker",
  )

  assert coordinator.run_once()
  facts = _execution_facts(experiment_id)
  assert facts["runs"] == 1
  assert facts["executions"] == 1
  assert facts["outbox"] == 1
  assert facts["in_flight"] == 1
  run_id = _run_id_for_experiment(experiment_id)
  _finish_run_for_reconciliation(run_id)

  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert detail["status"] == "completed"
  assert detail["stage"]["succeededTrialCount"] == 1
  assert detail["stage"]["inFlightTrialCount"] == 0
  assert not PostgresExperimentRepository().reconcile_next_execution()
  facts_after_replay = _execution_facts(experiment_id)
  assert facts_after_replay["runs"] == 1
  assert facts_after_replay["in_flight"] == 0

  page = _as_user(
    OWNER,
    lambda: execution_application.list_trials(
      experiment_id, limit=50, cursor=None, status=None,
    ),
  )
  assert page["items"][0]["runId"] == run_id
  assert page["items"][0]["selectedExecution"]["resultHash"]
  assert page["items"][0]["selectedExecution"]["actualDataHash"] == "d" * 64
  filtered = _as_user(
    OWNER,
    lambda: execution_application.list_trials(
      experiment_id,
      limit=20,
      cursor=None,
      status="succeeded",
      query="1",
      date_scenario_id="development-1",
    ),
  )
  missing = _as_user(
    OWNER,
    lambda: execution_application.list_trials(
      experiment_id, limit=20, cursor=None, status=None, query="2",
    ),
  )
  assert [item["trialIndex"] for item in filtered["items"]] == [0]
  assert missing["items"] == []


def test_archive_hides_and_restore_preserves_the_complete_graph(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  experiment_id, run_id = _complete_execution_experiment(
    execution_application, "postgres-archive-restore",
  )
  before = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  request = ExperimentLifecycleRequest(
    expectedRevisionId=before["revision"]["revisionId"],
    expectedSnapshotVersion=before["snapshotVersion"],
    reason="归档集成测试",
  )

  archived, status = _as_user(
    OWNER,
    lambda: execution_application.archive(
      experiment_id, request, "postgres-archive-key", "archive-request",
    ),
  )
  replayed, replay_status = _as_user(
    OWNER,
    lambda: execution_application.archive(
      experiment_id, request, "postgres-archive-key", "archive-request",
    ),
  )

  assert status == 200
  assert replay_status == 200
  assert replayed["replayed"] is True
  assert archived["reason"] == "归档集成测试"
  assert archived["operationId"]
  assert archived["acceptedAt"] == archived["archivedAt"]
  assert experiment_id not in {
    item["experimentId"] for item in _as_user(
      OWNER,
      lambda: execution_application.list_experiments(
        limit=50, cursor=None, archived=False,
      ),
    )["items"]
  }
  archived_page = _as_user(
    OWNER,
    lambda: execution_application.list_experiments(
      limit=50, cursor=None, archived=True,
    ),
  )
  assert [item["experimentId"] for item in archived_page["items"]] == [experiment_id]
  with pytest.raises(ExperimentError) as ordinary_inventory:
    _as_user(
      OWNER,
      lambda: execution_application.list_admin_archived_experiments(
        limit=50, cursor=None,
      ),
    )
  assert ordinary_inventory.value.code == "experiment.forbidden"
  cross_owner_admin = CurrentUser(
    id=OTHER.id,
    username=OTHER.username,
    display_name=OTHER.display_name,
    role="admin",
  )
  admin_page = _as_user(
    cross_owner_admin,
    lambda: execution_application.list_admin_archived_experiments(
      limit=50, cursor=None,
    ),
  )
  admin_item = next(
    item for item in admin_page["items"] if item["experimentId"] == experiment_id
  )
  assert admin_item["currentRevisionId"] == archived["revisionId"]
  assert admin_item["snapshotVersion"] == archived["snapshotVersion"]

  monkeypatch.setattr(settings, "SIGNAL_EXPORT_RUNNER_AVAILABILITY_REQUIRED", False)
  with pytest.raises(state_store.ExperimentRunRetainedError):
    _as_user(
      OWNER,
      lambda: state_store.create_shared_backtest_publication(
        run_id, "归档后共享", "不应创建", [],
      ),
    )
  with pytest.raises(state_store.ExperimentRunRetainedError):
    _as_user(
      OWNER,
      lambda: state_store.create_signal_detail_export(
        {"mode": "ids", "runIds": [run_id]},
        "10000000-0000-4000-8000-000000000099",
      ),
    )
  with pytest.raises(ExperimentError) as hidden:
    _as_user(OTHER, lambda: execution_application.detail(experiment_id))
  assert hidden.value.code == "experiment.not_found"

  restore_request = ExperimentLifecycleRequest(
    expectedRevisionId=archived["revisionId"],
    expectedSnapshotVersion=archived["snapshotVersion"],
    reason="撤销归档集成测试",
  )
  restored, restore_status = _as_user(
    OWNER,
    lambda: execution_application.restore_archive(
      experiment_id, restore_request, "postgres-restore-key", "restore-request",
    ),
  )
  after = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert restore_status == 200
  assert restored["revisionId"] == before["revision"]["revisionId"]
  assert after["revision"] == before["revision"]
  assert after["stage"] == before["stage"]
  assert _run_id_for_experiment(experiment_id) == run_id
  assert restored["reason"] == "撤销归档集成测试"
  assert restored["operationId"]
  assert restored["acceptedAt"]


def test_all_ordinary_deletion_paths_reject_experiment_retained_runs(
  execution_application: ExperimentApplication,
) -> None:
  _experiment_id, run_id = _complete_execution_experiment(
    execution_application, "postgres-retained-delete",
  )
  selection = {"mode": "ids", "runIds": [run_id]}
  summary = _as_user(
    OWNER, lambda: state_store.summarize_backtest_run_selection(selection),
  )

  with pytest.raises(state_store.ExperimentRunRetainedError):
    _as_user(OWNER, lambda: state_store.delete_backtest_run(run_id))
  with pytest.raises(state_store.ExperimentRunRetainedError):
    _as_user(OWNER, lambda: state_store.delete_backtest_runs([run_id]))
  with pytest.raises(state_store.ExperimentRunRetainedError):
    _as_user(
      OWNER,
      lambda: state_store.delete_backtest_run_selection(
        selection,
        summary["selectedRunCount"],
        summary["selectionDigest"],
      ),
    )
  assert _run_id_for_experiment(_experiment_id) == run_id


def test_purge_preserves_shared_run_and_resumes_after_frozen_scope_failure(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENT_PURGE_ENABLED", True)
  monkeypatch.setattr(settings, "EXPERIMENT_PURGE_BATCH_SIZE", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_PURGE_RETRY_DELAY_SECONDS", 1)
  monkeypatch.setattr(settings, "EXPERIMENT_PURGE_MAX_ATTEMPTS", 5)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT", 2)
  monkeypatch.setattr(settings, "EXPERIMENT_MAX_IN_FLIGHT_PER_USER", 2)
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-purge-graph", _two_trial_execution_request(),
  )
  _generate_all("purge-generation-worker")
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="purge-execution-worker",
  )
  assert coordinator.run_once()
  assert coordinator.run_once()
  run_ids = _run_ids_for_experiment(experiment_id)
  for run_id in run_ids:
    _finish_run_for_reconciliation(run_id)
  assert coordinator.run_once()
  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert detail["status"] == "completed"
  _insert_asset_hold(detail["revision"]["revisionId"])
  _publish_run(run_ids[0])
  shared_artifact = _insert_run_artifact(run_ids[0], tmp_path, "shared-signal.ndjson")
  exclusive_artifact = _insert_run_artifact(
    run_ids[1], tmp_path, "exclusive-signal.ndjson",
  )
  next_preview = _as_user(
    OWNER, lambda: execution_application.preview(_execution_request()),
  )

  archive_request = ExperimentLifecycleRequest(
    expectedRevisionId=detail["revision"]["revisionId"],
    expectedSnapshotVersion=detail["snapshotVersion"],
    reason="物理清理前归档",
  )
  archived, _status = _as_user(
    OWNER,
    lambda: execution_application.archive(
      experiment_id, archive_request, "purge-archive-key", "purge-archive-request",
    ),
  )
  _expire_archive_retention(experiment_id)
  request = ExperimentPurgeRequest(
    expectedRevisionId=archived["revisionId"],
    expectedSnapshotVersion=archived["snapshotVersion"],
    reason="管理员确认测试数据达到保留期限",
  )
  admin = OWNER_ADMIN
  purge, status = _as_user(
    admin,
    lambda: execution_application.purge(
      experiment_id, request, "postgres-purge-key", "purge-request-id",
    ),
  )
  replayed, replay_status = _as_user(
    admin,
    lambda: execution_application.purge(
      experiment_id, request, "postgres-purge-key", "purge-request-id",
    ),
  )
  assert status == 202
  assert replay_status == 200
  assert replayed["purgeId"] == purge["purgeId"]
  assert replayed["replayed"] is True
  assert purge["reason"] == "管理员确认测试数据达到保留期限"
  assert purge["operationId"] == purge["purgeId"]
  assert purge["acceptedAt"]
  assert purge["scopeCounts"]["run_artifact"] == 2
  assert _as_user(OTHER, _purge_rls_counts) == (0, 0)
  with pytest.raises(ExperimentError) as forbidden:
    _as_user(
      OTHER, lambda: PostgresExperimentPurgeRepository().get(purge["purgeId"]),
    )
  assert forbidden.value.code == "experiment.purge_forbidden"

  blocked_revision = ExperimentRevisionCreateRequest(
    previewToken=next_preview["previewToken"],
    specHash=next_preview["specHash"],
    experimentSpec=_execution_request(),
    expectedRevisionId=archived["revisionId"],
    expectedSnapshotVersion=purge["snapshotVersion"],
  )
  with pytest.raises(ExperimentError) as revision_conflict:
    _as_user(
      OWNER,
      lambda: execution_application.create_revision(
        experiment_id, blocked_revision, "purge-blocked-revision",
      ),
    )
  assert revision_conflict.value.code == "experiment.purge_in_progress"
  with pytest.raises(ExperimentError) as evaluation_conflict:
    _as_user(
      OWNER,
      lambda: EvaluationApplication().save_spec(
        experiment_id, _evaluation_request(detail), "purge-blocked-evaluation",
      ),
    )
  assert evaluation_conflict.value.code == "experiment.purge_in_progress"

  injected_audit_id = _insert_unfrozen_purge_dependency(
    experiment_id, archived["revisionId"],
  )
  worker = ExperimentPurgeWorker(
    repository=PostgresExperimentPurgeRepository(),
    artifact_root=tmp_path,
    worker_id="purge-worker-first",
  )
  assert worker.run_once()
  failed = _as_user(
    admin, lambda: PostgresExperimentPurgeRepository().get(purge["purgeId"]),
  )
  assert failed["status"] == "failed"
  assert failed["failurePhase"] == "experiment"
  assert _experiment_asset_hold_count(experiment_id) > 0
  assert shared_artifact.exists()
  assert not exclusive_artifact.exists()

  _remove_unfrozen_purge_dependency(injected_audit_id, purge["purgeId"])
  resumed_worker = ExperimentPurgeWorker(
    repository=PostgresExperimentPurgeRepository(),
    artifact_root=tmp_path,
    worker_id="purge-worker-resumed",
  )
  assert resumed_worker.run_once()
  completed = _as_user(
    admin, lambda: PostgresExperimentPurgeRepository().get(purge["purgeId"]),
  )
  assert completed["status"] == "succeeded"
  assert completed["attemptCount"] == 2
  assert completed["requestId"] == "purge-request-id"
  assert completed["reason"] == "管理员确认测试数据达到保留期限"
  assert completed["oldValue"]["experimentStatus"] == "completed"
  assert completed["newValue"]["purgeStatus"] == "succeeded"
  _assert_purged_graph(experiment_id, run_ids)
  assert _experiment_asset_hold_count(experiment_id) == 0
  assert shared_artifact.exists()
  assert _artifact_exists_for_run(run_ids[0])
  assert not _artifact_exists_for_run(run_ids[1])


def test_purge_preserves_run_referenced_by_signal_detail_export(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  monkeypatch.setattr(settings, "EXPERIMENT_PURGE_ENABLED", True)
  monkeypatch.setattr(settings, "SIGNAL_EXPORT_RUNNER_AVAILABILITY_REQUIRED", False)
  experiment_id, run_id = _complete_execution_experiment(
    execution_application, "postgres-purge-signal-export-reference",
  )
  artifact_path = _insert_run_artifact(
    run_id, tmp_path, "signal-export-retained.ndjson",
  )
  exported = _as_user(
    OWNER,
    lambda: state_store.create_signal_detail_export(
      {"mode": "ids", "runIds": [run_id]},
      "10000000-0000-4000-8000-000000000098",
    ),
  )
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  archived, _status = _as_user(
    OWNER,
    lambda: execution_application.archive(
      experiment_id,
      ExperimentLifecycleRequest(
        expectedRevisionId=detail["revision"]["revisionId"],
        expectedSnapshotVersion=detail["snapshotVersion"],
        reason="验证信号导出引用保留",
      ),
      "purge-signal-reference-archive",
    ),
  )
  _expire_archive_retention(experiment_id)
  purge, _purge_status = _as_user(
    OWNER_ADMIN,
    lambda: execution_application.purge(
      experiment_id,
      ExperimentPurgeRequest(
        expectedRevisionId=archived["revisionId"],
        expectedSnapshotVersion=archived["snapshotVersion"],
        reason="管理员清理信号导出引用实验",
      ),
      "purge-signal-reference-request",
    ),
  )

  worker = ExperimentPurgeWorker(
    repository=PostgresExperimentPurgeRepository(),
    artifact_root=tmp_path,
    worker_id="purge-signal-reference-worker",
  )
  assert worker.run_once()
  completed = _as_user(
    OWNER_ADMIN,
    lambda: PostgresExperimentPurgeRepository().get(purge["purgeId"]),
  )
  assert completed["status"] == "succeeded"
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT origin_experiment_id FROM backtest_run WHERE id=%s",
        (run_id,),
      )
      assert cur.fetchone()[0] is None
      cur.execute("""
        SELECT count(*) FROM backtest_signal_detail_export_item
        WHERE export_id=%s AND source_run_id=%s
      """, (exported["id"], run_id))
      assert int(cur.fetchone()[0]) == 1
  assert artifact_path.exists()
  assert _artifact_exists_for_run(run_id)


def test_account_delete_disables_user_with_pending_experiment_without_run(
  application: ExperimentApplication,
) -> None:
  _clear_experiment_facts()
  pending_user = CurrentUser(
    id="10000000-0000-4000-8000-000000000003",
    username="pending-experiment-user",
    display_name="Pending Experiment User",
    role="user",
  )
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO app_user (
          id, username, display_name, password_hash, role, status
        ) VALUES (%s,%s,%s,'test-only','user','active')
        ON CONFLICT (id) DO UPDATE SET status='active'
      """, (pending_user.id, pending_user.username, pending_user.display_name))
  try:
    experiment_id = _create_execution_experiment(
      application, "pending-account-delete", user=pending_user,
    )
    assert _experiment_run_count(experiment_id) == 0

    result = auth.delete_user(pending_user.id, OWNER.id)

    assert result["action"] == "disabled"
    with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT status FROM app_user WHERE id=%s", (pending_user.id,))
        assert cur.fetchone()[0] == "disabled"
  finally:
    _clear_experiment_facts()
    with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
      with conn.cursor() as cur:
        cur.execute("DELETE FROM app_user WHERE id=%s", (pending_user.id,))


def test_account_delete_preserves_purge_requester_audit() -> None:
  requester = CurrentUser(
    id="10000000-0000-4000-8000-000000000004",
    username="purge-requester-admin",
    display_name="Purge Requester Admin",
    role="admin",
  )
  purge_id = "20000000-0000-4000-8000-000000000004"
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO app_user (
          id, username, display_name, password_hash, role, status
        ) VALUES (%s,%s,%s,'test-only','admin','active')
      """, (requester.id, requester.username, requester.display_name))
      cur.execute("""
        INSERT INTO backtest_experiment_purge (
          id, experiment_id, revision_id, owner_user_id, requested_by_user_id,
          reason, scope_digest, scope_counts
        ) VALUES (%s,gen_random_uuid(),gen_random_uuid(),%s,%s,%s,%s,'{}'::jsonb)
      """, (
        purge_id, OWNER.id, requester.id, "保留清理申请人审计", "a" * 64,
      ))
  try:
    result = auth.delete_user(requester.id, OWNER.id)

    assert result["action"] == "disabled"
    with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT status FROM app_user WHERE id=%s", (requester.id,))
        assert cur.fetchone()[0] == "disabled"
  finally:
    with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
      with conn.cursor() as cur:
        cur.execute("DELETE FROM backtest_experiment_purge WHERE id=%s", (purge_id,))
        cur.execute("DELETE FROM app_user WHERE id=%s", (requester.id,))


def test_running_experiment_cancel_stops_new_runs_and_releases_permit_after_reconcile(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-running-cancel",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="cancel-running-worker",
  )
  assert coordinator.run_once()
  run_id = _run_id_for_experiment(experiment_id)
  _mark_experiment_run_running(run_id)

  response, _status = _as_user(
    OWNER,
    lambda: execution_application.cancel(experiment_id, "cancel-running-key"),
  )

  assert response["status"] == "cancelling"
  assert _experiment_run_count(experiment_id) == 1
  _finish_cancelled_run(run_id)
  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert detail["status"] == "cancelled"
  assert detail["stage"]["inFlightTrialCount"] == 0
  assert _experiment_run_count(experiment_id) == 1


def test_cancel_wins_against_late_success_report(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-cancel-late-success",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="cancel-race-worker",
  )
  assert coordinator.run_once()
  runner_user = CurrentUser(
    id=OWNER.id,
    username=OWNER.username,
    display_name=OWNER.display_name,
    role="admin",
  )
  claimed = _as_user(
    runner_user,
    lambda: state_store.claim_bar_backtest_job("cancel-race-runner", 60),
  )
  assert claimed is not None
  response, _status = _as_user(
    OWNER,
    lambda: execution_application.cancel(experiment_id, "cancel-race-key"),
  )
  assert response["status"] == "cancelling"

  accepted = _as_user(
    runner_user,
    lambda: state_store.finish_bar_backtest_job(
      claimed["runId"],
      claimed["attemptToken"],
      "succeeded",
      {"totalReturn": 0.1},
      {
        "resultSchemaVersion": 3,
        "summary": {"totalReturn": 0.1},
        "experimentManifest": {"dataHash": "d" * 64},
      },
    ),
  )
  assert accepted
  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert detail["status"] == "cancelled"
  assert detail["stage"]["succeededTrialCount"] == 0
  assert detail["stage"]["cancelledTrialCount"] == 1


def test_completed_run_wins_against_late_cancel_request(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-success-before-cancel",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="success-race-worker",
  )
  assert coordinator.run_once()
  run_id = _run_id_for_experiment(experiment_id)
  _finish_run_for_reconciliation(run_id)

  response, _status = _as_user(
    OWNER,
    lambda: execution_application.cancel(experiment_id, "success-race-key"),
  )
  assert response["status"] == "cancelling"
  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert detail["status"] == "completed"
  assert detail["stage"]["succeededTrialCount"] == 1
  assert detail["stage"]["cancelledTrialCount"] == 0


def test_retryable_failure_retries_once_then_becomes_terminal(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-bounded-retry",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="retry-worker",
  )
  assert coordinator.run_once()
  first_run_id = _latest_run_id_for_experiment(experiment_id)
  _fail_run_for_reconciliation(first_run_id, retryable=True)
  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert detail["status"] == "queued"
  assert detail["errorAggregates"][0]["count"] == 1

  _make_retry_ready(experiment_id)
  assert coordinator.run_once()
  second_run_id = _latest_run_id_for_experiment(experiment_id)
  assert second_run_id != first_run_id
  _fail_run_for_reconciliation(second_run_id, retryable=True)
  assert coordinator.run_once()

  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert detail["status"] == "failed"
  assert detail["stage"]["failedTrialCount"] == 1
  assert _experiment_run_count(experiment_id) == 2
  page = _as_user(
    OWNER,
    lambda: execution_application.list_trials(
      experiment_id, limit=10, cursor=None, status=None,
    ),
  )
  assert page["items"][0]["automaticRetryCount"] == 1


def test_manual_retry_freezes_selection_and_competing_requests_create_one_execution(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "BACKTEST_BASE_CONCURRENCY", 8)
  _set_scheduler_capacity_override(8)
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-manual-retry",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="manual-retry-worker",
  )
  assert coordinator.run_once()
  first_run_id = _latest_run_id_for_experiment(experiment_id)
  _fail_run_for_reconciliation(first_run_id, retryable=False)
  assert coordinator.run_once()
  page = _as_user(
    OWNER,
    lambda: execution_application.list_trials(
      experiment_id, limit=10, cursor=None, status=None,
    ),
  )
  trial_id = page["items"][0]["trialId"]
  preview_request = ExperimentRetryPreviewRequest(
    expectedRevisionId=_as_user(
      OWNER, lambda: execution_application.detail(experiment_id),
    )["revision"]["revisionId"],
    selection={"mode": "trial_ids", "trialIds": [trial_id]},
  )
  summary = _as_user(
    OWNER, lambda: execution_application.preview_retry(experiment_id, preview_request),
  )
  retry_request = ExperimentRetryFailedRequest(
    **preview_request.model_dump(),
    expectedSelectedTrialCount=summary["selectedTrialCount"],
    expectedSelectionDigest=summary["selectionDigest"],
    reason="并发人工重试测试",
  )

  def retry(key: str) -> tuple[str, str]:
    try:
      _response, status = _as_user(
        OWNER,
        lambda: execution_application.retry_failed(
          experiment_id, retry_request, key, f"request-{key}",
        ),
      )
      return key, f"created:{status}"
    except ExperimentError as error:
      return key, error.code

  with ThreadPoolExecutor(max_workers=2) as executor:
    outcomes = list(executor.map(retry, ("manual-retry-1", "manual-retry-2")))

  assert sorted(value for _key, value in outcomes) == [
    "created:202", "experiment.selection_changed",
  ]
  success_key = next(key for key, value in outcomes if value == "created:202")
  replay, replay_status = _as_user(
    OWNER,
    lambda: execution_application.retry_failed(
      experiment_id, retry_request, success_key, "request-replay",
    ),
  )
  assert replay_status == 200
  assert replay["replayed"] is True
  assert _experiment_run_count(experiment_id) == 2
  attempts = _as_user(
    OWNER,
    lambda: execution_application.list_trials(
      experiment_id, limit=10, cursor=None, status=None,
    ),
  )["items"][0]["executionAttempts"]
  assert [item["executionNo"] for item in attempts] == [1, 2]
  assert [item["createdReason"] for item in attempts] == ["initial", "manual_retry"]
  assert _experiment_run_seeds(experiment_id)[0] == _experiment_run_seeds(experiment_id)[1]
  related = _as_user(
    OWNER,
    lambda: execution_application.list_related_runs(
      experiment_id, limit=10, cursor=None, query="",
    ),
  )
  assert related["totalCount"] == 2
  assert related["initialCount"] == 1
  assert related["retryCount"] == 1
  assert [item["executionNo"] for item in related["items"]] == [1, 2]
  assert all(item["displayNo"] > 0 for item in related["items"])
  selected_display_no = related["items"][0]["displayNo"]
  filtered = _as_user(
    OWNER,
    lambda: execution_application.list_related_runs(
      experiment_id, limit=10, cursor=None, query=f"#{selected_display_no}",
    ),
  )
  assert [item["displayNo"] for item in filtered["items"]] == [selected_display_no]
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert detail["relatedBacktestRuns"] == {
    "totalCount": 2,
    "initialCount": 1,
    "retryCount": 1,
  }


def test_new_revision_requires_terminal_current_and_preserves_history(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-revision-history",
  )
  active = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  preview = _as_user(OWNER, lambda: execution_application.preview(_execution_request()))
  active_request = ExperimentRevisionCreateRequest(
    previewToken=preview["previewToken"],
    specHash=preview["specHash"],
    experimentSpec=_execution_request(),
    expectedRevisionId=active["revision"]["revisionId"],
    expectedSnapshotVersion=active["snapshotVersion"],
  )
  with pytest.raises(ExperimentError) as conflict:
    _as_user(
      OWNER,
      lambda: execution_application.create_revision(
        experiment_id, active_request, "active-revision-conflict",
      ),
    )
  assert conflict.value.code == "experiment.active_revision_exists"

  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="revision-history-worker",
  )
  assert coordinator.run_once()
  first_run_id = _latest_run_id_for_experiment(experiment_id)
  _finish_run_for_reconciliation(first_run_id)
  assert coordinator.run_once()
  terminal = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  old_revision_id = terminal["revision"]["revisionId"]
  next_preview = _as_user(OWNER, lambda: execution_application.preview(_execution_request()))
  next_request = ExperimentRevisionCreateRequest(
    previewToken=next_preview["previewToken"],
    specHash=next_preview["specHash"],
    experimentSpec=_execution_request(),
    expectedRevisionId=old_revision_id,
    expectedSnapshotVersion=terminal["snapshotVersion"],
  )
  created, status = _as_user(
    OWNER,
    lambda: execution_application.create_revision(
      experiment_id, next_request, "create-revision-2",
    ),
  )

  assert status == 201
  assert created["revisionId"] != old_revision_id
  history = _as_user(
    OWNER,
    lambda: execution_application.list_revisions(
      experiment_id, limit=10, cursor=None,
    ),
  )
  assert [item["revisionNo"] for item in history["items"]] == [2, 1]
  assert history["items"][0]["current"] is True
  assert history["items"][1]["status"] == "completed"
  old_trials = _as_user(
    OWNER,
    lambda: execution_application.list_trials(
      experiment_id,
      limit=10,
      cursor=None,
      status=None,
      revision_id=old_revision_id,
    ),
  )
  assert old_trials["items"][0]["runId"] == first_run_id
  assert old_trials["items"][0]["executionAttempts"][0]["selected"] is True


def test_manual_retries_across_users_respect_global_capacity_atomically(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(settings, "BACKTEST_BASE_CONCURRENCY", 8)
  monkeypatch.setattr(settings, "EXPERIMENT_MANUAL_RESERVED_PERMITS", 7)
  _set_scheduler_capacity_override(8)
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="global-retry-capacity",
  )
  experiments: list[tuple[CurrentUser, str, ExperimentRetryFailedRequest]] = []
  for index, user in enumerate((OWNER, OTHER), start=1):
    experiment_id = _create_execution_experiment(
      execution_application, f"global-retry-{index}", user=user,
    )
    assert coordinator.run_once()
    run_id = _latest_run_id_for_experiment(experiment_id)
    _fail_run_for_reconciliation(run_id, retryable=False)
    assert coordinator.run_once()
    experiments.append((
      user,
      experiment_id,
      _manual_retry_request(execution_application, user, experiment_id),
    ))

  def retry(values: tuple[CurrentUser, str, ExperimentRetryFailedRequest]) -> str:
    user, experiment_id, request = values
    try:
      _response, status = _as_user(
        user,
        lambda: execution_application.retry_failed(
          experiment_id, request, f"global-retry-key-{user.id}",
        ),
      )
      return f"created:{status}"
    except ExperimentError as error:
      return error.code

  with ThreadPoolExecutor(max_workers=2) as executor:
    outcomes = list(executor.map(retry, experiments))

  assert sorted(outcomes) == ["created:202", "experiment.capacity_blocked"]
  assert sum(_experiment_run_count(item[1]) for item in experiments) == 3


def test_capacity_snapshot_restores_regular_user_rls_context() -> None:
  def read_snapshot() -> tuple[bool, int, int, bool]:
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT app.current_is_admin() AS value")
        before = bool(cur.fetchone()["value"])
        cur.execute(
          "SELECT * FROM app.experiment_capacity_snapshot(%s)",
          (settings.BACKTEST_BASE_CONCURRENCY,),
        )
        snapshot = cur.fetchone()
        cur.execute("SELECT app.current_is_admin() AS value")
        after = bool(cur.fetchone()["value"])
    return (
      before,
      int(snapshot["effective_limit"]),
      int(snapshot["experiment_in_flight_count"]),
      after,
    )

  before, effective_limit, in_flight, after = _as_user(OWNER, read_snapshot)

  assert before is False
  assert effective_limit >= 0
  assert in_flight >= 0
  assert after is False


def test_three_terminal_sequence_failures_open_circuit_and_resume_same_revision(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application,
    "postgres-circuit-resume",
    _four_trial_execution_request(),
  )
  _generate_all("circuit-generator")
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="circuit-worker",
  )
  assert coordinator.run_once()
  for _index in range(3):
    run_id = _latest_run_id_for_experiment(experiment_id)
    _fail_run_for_reconciliation(run_id, retryable=False)
    assert coordinator.run_once()

  blocked = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert blocked["status"] == "generation_blocked"
  assert blocked["blockedReason"]["code"] == "experiment.execution_blocked"
  assert blocked["blockedReason"]["scope"] == "stage"
  assert blocked["consecutiveFailureCount"] == 3
  assert _experiment_run_count(experiment_id) == 3
  assert not coordinator.run_once()

  response, _status = _as_user(
    OWNER,
    lambda: execution_application.resume(
      experiment_id,
      blocked["revision"]["revisionId"],
      "已确认临时运行故障恢复",
      "resume-circuit-key",
    ),
  )
  assert response["revisionId"] == blocked["revision"]["revisionId"]
  assert response["status"] == "queued"
  assert coordinator.run_once()
  assert _experiment_run_count(experiment_id) == 4


def test_coordinator_repairs_cached_counts_from_execution_facts(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-aggregate-repair",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="repair-worker",
  )
  assert coordinator.run_once()
  _corrupt_aggregate_cache(experiment_id)

  assert PostgresExperimentRepository().repair_next_aggregate()

  facts = _execution_facts(experiment_id)
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  assert facts["in_flight"] == 1
  assert detail["stage"]["inFlightTrialCount"] == 1


def test_evaluation_freezes_results_is_deterministic_and_detects_hash_drift(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-evaluation",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="evaluation-source-worker",
  )
  assert coordinator.run_once()
  run_id = _run_id_for_experiment(experiment_id)
  _finish_run_for_reconciliation(run_id)
  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  application = EvaluationApplication()
  request = _evaluation_request(detail)

  spec, _ = _as_user(
    OWNER,
    lambda: application.save_spec(experiment_id, request, "save-evaluation-1"),
  )
  duplicate_spec, duplicate_status = _as_user(
    OWNER,
    lambda: application.save_spec(experiment_id, request, "save-evaluation-2"),
  )
  assert duplicate_status == 200
  assert duplicate_spec["evaluationSpecId"] == spec["evaluationSpecId"]
  assert duplicate_spec["replayed"] is True

  _set_experiment_stage_status(experiment_id, "running")
  try:
    with pytest.raises(ExperimentError) as incomplete:
      _as_user(
        OWNER,
        lambda: application.create_run(
          experiment_id, spec["evaluationSpecId"], "run-evaluation-incomplete",
        ),
      )
    assert incomplete.value.code == "experiment.evaluation_result_set_incomplete"
  finally:
    _set_experiment_stage_status(experiment_id, "completed")

  first_run, _ = _as_user(
    OWNER,
    lambda: application.create_run(
      experiment_id, spec["evaluationSpecId"], "run-evaluation-1",
    ),
  )
  worker = EvaluationWorker(worker_id="evaluation-worker-test")
  assert worker.run_once()
  first = _as_user(
    OWNER,
    lambda: application.run_detail(experiment_id, first_run["evaluationRunId"]),
  )
  second_run, second_status = _as_user(
    OWNER,
    lambda: application.create_run(
      experiment_id, spec["evaluationSpecId"], "run-evaluation-2",
    ),
  )
  second = _as_user(
    OWNER,
    lambda: application.run_detail(experiment_id, second_run["evaluationRunId"]),
  )
  assert second_status == 200
  assert second_run["evaluationRunId"] == first_run["evaluationRunId"]
  assert second_run["replayed"] is True
  assert first["status"] == second["status"] == "succeeded"
  assert first["resultSetHash"] == second["resultSetHash"]
  assert first["outputHash"] == second["outputHash"]
  assert first["output"] == second["output"]

  data_drifted, _ = _as_user(
    OWNER,
    lambda: application.create_run(
      experiment_id, spec["evaluationSpecId"], "run-evaluation-data-drift",
      force_recompute=True,
    ),
  )
  _set_execution_actual_data_hash(run_id, "f" * 64)
  assert worker.run_once()
  data_failed = _as_user(
    OWNER,
    lambda: application.run_detail(
      experiment_id, data_drifted["evaluationRunId"],
    ),
  )
  assert data_failed["status"] == "failed"
  assert data_failed["error"]["code"] == "experiment.evaluation_data_hash_mismatch"
  _set_execution_actual_data_hash(run_id, "d" * 64)

  drifted, _ = _as_user(
    OWNER,
    lambda: application.create_run(
      experiment_id, spec["evaluationSpecId"], "run-evaluation-drift",
      force_recompute=True,
    ),
  )
  _mutate_result_after_evaluation_freeze(run_id)
  assert worker.run_once()
  failed = _as_user(
    OWNER,
    lambda: application.run_detail(experiment_id, drifted["evaluationRunId"]),
  )
  assert failed["status"] == "failed"
  assert failed["error"]["code"] == "experiment.evaluation_result_hash_mismatch"
  unchanged = _as_user(
    OWNER,
    lambda: application.run_detail(experiment_id, first_run["evaluationRunId"]),
  )
  assert unchanged["outputHash"] == first["outputHash"]
  assert unchanged["output"] == first["output"]
  assert _experiment_run_count(experiment_id) == 1


def test_provisional_evaluation_freezes_missing_trial_without_fake_result(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-provisional-evaluation",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="provisional-source-worker",
  )
  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  payload = _evaluation_request(detail).model_dump(mode="json")
  payload["name"] = "部分评价"
  payload["completeness"] = {
    "allowPartial": True,
    "minimumCompletionRate": "0.0000",
    "requiredDimensionCoverage": [],
  }
  request = EvaluationSpecRequest.model_validate(payload)
  application = EvaluationApplication()
  spec, _ = _as_user(
    OWNER,
    lambda: application.save_spec(experiment_id, request, "save-provisional"),
  )

  created, _ = _as_user(
    OWNER,
    lambda: application.create_run(
      experiment_id, spec["evaluationSpecId"], "run-provisional",
    ),
  )

  assert created["executionCount"] == 0
  assert created["completeness"]["isPartial"] is True
  assert created["completeness"]["missingCount"] == 1
  assert EvaluationWorker(worker_id="provisional-worker-test").run_once()
  result = _as_user(
    OWNER,
    lambda: application.run_detail(experiment_id, created["evaluationRunId"]),
  )
  assert result["status"] == "succeeded"
  assert result["output"]["isPartial"] is True
  assert result["output"]["candidateSelectionEligible"] is False
  assert result["output"]["rows"] == []
  assert result["output"]["completeness"]["exceptions"][0]["status"] == "missing"
  assert _experiment_run_count(experiment_id) == 1


def test_evaluation_export_idempotency_rls_lease_reclaim_and_failure_recovery(
  execution_application: ExperimentApplication,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  experiment_id = _create_execution_experiment(
    execution_application, "postgres-evaluation-export",
  )
  coordinator = ExperimentCoordinator(
    repository=PostgresExperimentRepository(), worker_id="export-source-worker",
  )
  assert coordinator.run_once()
  run_id = _run_id_for_experiment(experiment_id)
  _finish_run_for_reconciliation(run_id)
  assert coordinator.run_once()
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  evaluation = EvaluationApplication()
  spec, _ = _as_user(
    OWNER,
    lambda: evaluation.save_spec(
      experiment_id, _evaluation_request(detail), "save-export-evaluation",
    ),
  )
  evaluation_run, _ = _as_user(
    OWNER,
    lambda: evaluation.create_run(
      experiment_id, spec["evaluationSpecId"], "run-export-evaluation",
    ),
  )
  assert EvaluationWorker(worker_id="export-evaluation-worker").run_once()
  run_detail = _as_user(
    OWNER,
    lambda: evaluation.run_detail(
      experiment_id, evaluation_run["evaluationRunId"],
    ),
  )
  columns = list(run_detail["output"]["columns"])
  request = EvaluationExportCreateRequest(
    format="csv", columns=columns, locale="zh-CN",
  )
  monkeypatch.setattr(
    settings, "EXPERIMENT_EXPORT_WORKER_AVAILABILITY_REQUIRED", False,
  )
  application = EvaluationExportApplication()

  def create_same_export() -> tuple[str, int]:
    response, status = _as_user(
      OWNER,
      lambda: application.create(
        evaluation_run["evaluationRunId"], request,
        "concurrent-export-key", "request-export",
      ),
    )
    return response["exportId"], status

  with ThreadPoolExecutor(max_workers=2) as executor:
    created = list(executor.map(lambda _index: create_same_export(), range(2)))

  assert len({item[0] for item in created}) == 1
  assert sorted(item[1] for item in created) == [200, 202]
  export_id = created[0][0]

  with pytest.raises(ExperimentError) as conflict:
    _as_user(
      OWNER,
      lambda: application.create(
        evaluation_run["evaluationRunId"],
        EvaluationExportCreateRequest(
          format="xlsx", columns=columns, locale="zh-CN",
        ),
        "concurrent-export-key",
      ),
    )
  assert conflict.value.code == "idempotency.conflict"

  with pytest.raises(ExperimentError) as hidden:
    _as_user(
      OTHER,
      lambda: application.list_exports(evaluation_run["evaluationRunId"]),
    )
  assert hidden.value.code == "experiment.evaluation_run_not_found"

  with pytest.raises(ExperimentError) as hidden_download:
    _as_user(OTHER, lambda: application.download_source(export_id))
  assert hidden_download.value.code == "experiment.export_not_found"

  cancelled, cancel_status = _as_user(
    OWNER,
    lambda: application.create(
      evaluation_run["evaluationRunId"],
      EvaluationExportCreateRequest(
        format="xlsx", columns=columns, locale="zh-CN",
      ),
      "cancelled-export-key",
    ),
  )
  cancelled_id = cancelled["exportId"]
  cancel_response, cancel_response_status = _as_user(
    OWNER,
    lambda: application.cancel(cancelled_id, "cancel-export-key"),
  )
  replayed_cancel, replayed_cancel_status = _as_user(
    OWNER,
    lambda: application.cancel(cancelled_id, "cancel-export-key"),
  )
  assert cancel_status == 202
  assert cancel_response_status == 202
  assert replayed_cancel_status == 200
  assert cancel_response["status"] == "cancelled"
  assert replayed_cancel["replayed"] is True

  repository = PostgresEvaluationExportRepository()
  first = repository.claim_next_export("export-worker-1")
  assert first is not None
  _expire_evaluation_export_lease(export_id)
  second = repository.claim_next_export("export-worker-2")
  assert second is not None
  assert second.export["id"] == export_id
  assert second.attempt_token != first.attempt_token
  assert not repository.heartbeat(export_id, first.attempt_token)
  assert repository.fail_export(
    export_id, second.attempt_token, "storage_unavailable",
    "暂时不可用", retryable=True,
  )
  third = repository.claim_next_export("export-worker-3")
  assert third is not None
  assert repository.fail_export(
    export_id, third.attempt_token, "artifact_failed",
    "生成失败", retryable=False,
  )
  failed = _as_user(OWNER, lambda: application.detail(export_id))
  assert failed["status"] == "failed"
  assert failed["attemptCount"] == 3
  assert failed["error"]["code"] == "artifact_failed"

  retried, retry_status = _as_user(
    OWNER,
    lambda: application.retry(export_id, "retry-export-key"),
  )
  assert retry_status == 202
  assert retried["exportId"] != export_id
  assert retried["retryOfExportId"] == export_id


def test_rolling_window_lifecycle_and_ledgers_are_idempotent(
  execution_application: ExperimentApplication,
) -> None:
  _set_contract_epoch("rolling_window_v1")
  try:
    experiment_id = _create_execution_experiment(
      execution_application,
      "postgres-rolling-window",
      _four_trial_execution_request(),
    )
    rolling = RollingTrialWindow(
      storage_gate=AlwaysOpenStorageGate(),  # type: ignore[arg-type]
    )

    with ThreadPoolExecutor(max_workers=4) as executor:
      results = list(executor.map(lambda _index: rolling.generate_next_batch(3), range(4)))
    while _rolling_facts(experiment_id)["generated_count"] < 4:
      rolling.generate_next_batch(3)

    generated = [item for item in results if isinstance(item, GeneratedBatch)]
    assert sum(item.generated_count for item in generated) <= 4
    facts = _rolling_facts(experiment_id)
    assert facts["generated_count"] == 4
    assert facts["global_open"] == facts["user_open"] == facts["stage_open"] == 4
    assert facts["global_reserved"] == 0
    assert facts["user_reserved"] == 0
    assert facts["stage_reserved"] == 0

    first_trial_id = facts["trial_ids"][0]
    first_settlement = rolling.settle_trial(first_trial_id, "failed", "settle-1")
    replayed_settlement = rolling.settle_trial(first_trial_id, "failed", "settle-1")
    assert first_settlement.settled and not first_settlement.replayed
    assert replayed_settlement.settled and replayed_settlement.replayed
    assert _rolling_facts(experiment_id)["global_open"] == 3
    retry = rolling.request_retry(first_trial_id, "retry-1")
    retry_replay = rolling.request_retry(first_trial_id, "retry-1")
    assert retry.allowed and not retry.replayed
    assert retry_replay.allowed and retry_replay.replayed
    assert _rolling_facts(experiment_id)["global_open"] == 4

    detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
    revision_id = detail["revision"]["revisionId"]
    paused, pause_status = _as_user(
      OWNER,
      lambda: execution_application.pause_lifecycle(
        experiment_id, revision_id, detail["lifecycleVersion"],
        "集成测试暂停", "rolling-pause-1",
      ),
    )
    repeated, repeated_status = _as_user(
      OWNER,
      lambda: execution_application.pause_lifecycle(
        experiment_id, revision_id, paused["lifecycleVersion"],
        "重复暂停不新增状态转换", "rolling-pause-2",
      ),
    )
    assert pause_status == 202
    assert repeated_status == 200
    assert paused["lifecycleState"] == repeated["lifecycleState"] == "paused"
    assert paused["lifecycleVersion"] == repeated["lifecycleVersion"]

    resumed, resume_status = _as_user(
      OWNER,
      lambda: execution_application.resume_lifecycle(
        experiment_id, revision_id, repeated["lifecycleVersion"],
        "user_pause", "集成测试恢复", "rolling-resume-1",
      ),
    )
    assert resume_status == 200
    assert resumed["lifecycleState"] == "active"
    cancelled, cancel_status = _as_user(
      OWNER,
      lambda: execution_application.cancel_lifecycle(
        experiment_id, revision_id, resumed["lifecycleVersion"],
        "集成测试取消", "rolling-cancel-1",
      ),
    )
    assert cancel_status == 200
    assert cancelled["lifecycleState"] == "terminal"
    assert cancelled["terminalOutcome"] == "cancelled"
    cancelled_facts = _rolling_facts(experiment_id)
    assert cancelled_facts["global_open"] == 0
    assert cancelled_facts["terminal_count"] == 4
    assert cancelled_facts["cancelled_count"] == 4
  finally:
    _set_contract_epoch("legacy")


def test_evaluation_v2_persists_pages_and_keeps_v1_readable(
  execution_application: ExperimentApplication,
) -> None:
  experiment_id, _run_id = _complete_execution_experiment(
    execution_application, "postgres-evaluation-v2",
  )
  detail = _as_user(OWNER, lambda: execution_application.detail(experiment_id))
  application = EvaluationApplication()
  spec, _ = _as_user(
    OWNER,
    lambda: application.save_spec(
      experiment_id, _evaluation_request(detail), "save-evaluation-v2",
    ),
  )
  legacy_run, _ = _as_user(
    OWNER,
    lambda: application.create_run(
      experiment_id, spec["evaluationSpecId"], "run-evaluation-v1-before-rollout",
    ),
  )
  worker = EvaluationWorker(worker_id="evaluation-version-worker")
  assert worker.run_once()
  legacy_detail = _as_user(
    OWNER,
    lambda: application.run_detail(experiment_id, legacy_run["evaluationRunId"]),
  )
  assert legacy_detail["outputSchemaVersion"] == "evaluation_output_v1"

  _set_contract_epoch("evaluation_v2")
  try:
    still_readable = _as_user(
      OWNER,
      lambda: application.run_detail(experiment_id, legacy_run["evaluationRunId"]),
    )
    assert still_readable["output"] == legacy_detail["output"]
    created, status = _as_user(
      OWNER,
      lambda: application.create_run(
        experiment_id,
        spec["evaluationSpecId"],
        "run-evaluation-v2-after-rollout",
        force_recompute=True,
      ),
    )
    assert status == 202
    assert created["outputSchemaVersion"] == "evaluation_output_v2"
    assert worker.run_once()
    result = _as_user(
      OWNER,
      lambda: application.run_detail(experiment_id, created["evaluationRunId"]),
    )
    page = _as_user(
      OWNER,
      lambda: application.run_results(
        experiment_id, created["evaluationRunId"], after_ordinal=-1, limit=1,
      ),
    )
    assert result["status"] == "succeeded"
    assert result["outputSchemaVersion"] == "evaluation_output_v2"
    assert result["executionSet"] is None
    assert result["output"]["rows"] == []
    assert len(page["items"]) == 1
    assert page["items"][0]["trialExecutionId"]
  finally:
    _set_contract_epoch("legacy")


def _set_contract_epoch(epoch: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute(
        "UPDATE backtest_experiment_rollout_state SET contract_epoch=%s WHERE singleton",
        (epoch,),
      )


def _rolling_facts(experiment_id: str) -> dict[str, Any]:
  with psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"],
    row_factory=dict_row,
  ) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT stage.generated_trial_count AS generated_count,
          stage.terminal_count, stage.cancelled_count,
          global_window.open_count AS global_open,
          global_window.reserved_count AS global_reserved,
          user_window.open_count AS user_open,
          user_window.reserved_count AS user_reserved,
          stage_window.open_count AS stage_open,
          stage_window.reserved_count AS stage_reserved,
          array_agg(trial.id ORDER BY trial.trial_index) AS trial_ids
        FROM backtest_experiment experiment
        JOIN backtest_experiment_stage stage
          ON stage.revision_id=experiment.current_revision_id
        JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
        JOIN backtest_experiment_window_user user_window
          ON user_window.owner_user_id=stage.owner_user_id
        JOIN backtest_experiment_window_stage stage_window
          ON stage_window.stage_id=stage.id
        CROSS JOIN backtest_experiment_window_global global_window
        WHERE experiment.id=%s
        GROUP BY stage.id, global_window.open_count, global_window.reserved_count,
          user_window.open_count, user_window.reserved_count,
          stage_window.open_count, stage_window.reserved_count
      """, (experiment_id,))
      row = cur.fetchone()
  assert row is not None
  facts = dict(row)
  facts["trial_ids"] = [str(value) for value in facts["trial_ids"]]
  return facts


def _execution_facts(experiment_id: str) -> dict[str, Any]:
  with psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"], row_factory=dict_row,
  ) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT
          count(DISTINCT run.id) AS runs,
          count(DISTINCT execution.id) AS executions,
          count(DISTINCT outbox.id) AS outbox,
          max(quota.experiment_in_flight_count) AS in_flight,
          max(trial.materialization_status) AS materialization_status
        FROM backtest_experiment experiment
        JOIN backtest_experiment_revision revision
          ON revision.id=experiment.current_revision_id
        JOIN backtest_experiment_stage stage ON stage.revision_id=revision.id
        JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
        JOIN backtest_experiment_user_quota quota
          ON quota.owner_user_id=experiment.owner_user_id
        LEFT JOIN backtest_experiment_trial_execution execution
          ON execution.trial_id=trial.id
        LEFT JOIN backtest_experiment_dispatch_outbox outbox
          ON outbox.execution_id=execution.id
        LEFT JOIN backtest_run run ON run.id=execution.run_id
        WHERE experiment.id=%s
      """, (experiment_id,))
      row = cur.fetchone()
  return {
    "runs": int(row["runs"]),
    "executions": int(row["executions"]),
    "outbox": int(row["outbox"]),
    "in_flight": int(row["in_flight"]),
    "materialization_status": str(row["materialization_status"]),
  }


def _global_execution_facts() -> dict[str, int]:
  with psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"], row_factory=dict_row,
  ) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT count(DISTINCT run.id) AS runs,
          count(execution.id) FILTER (
            WHERE execution.permit_released_at IS NULL
          ) AS in_flight
        FROM backtest_run run
        JOIN backtest_experiment_trial_execution execution
          ON execution.run_id=run.id
        WHERE run.origin_type='experiment'
      """)
      row = cur.fetchone()
  return {"runs": int(row["runs"]), "in_flight": int(row["in_flight"])}


def _age_manual_job(run_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET created_at=now() - interval '120 seconds'
        WHERE run_id=%s
      """, (run_id,))


def _occupy_scheduler_slot(run_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET status='running', lease_expires_at=now() + interval '5 minutes'
        WHERE run_id=%s
      """, (run_id,))
      cur.execute("UPDATE backtest_run SET status='running' WHERE id=%s", (run_id,))
      cur.execute("""
        INSERT INTO backtest_execution_slot (
          run_id, owner_user_id, job_kind, attempt_token, worker_id,
          is_burst, lease_expires_at
        )
        SELECT id, owner_user_id, 'bar', gen_random_uuid(), 'fairness-test',
          false, now() + interval '5 minutes'
        FROM backtest_run
        WHERE id=%s
      """, (run_id,))


def _delete_manual_run(run_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM bar_backtest_job WHERE run_id=%s", (run_id,))
      cur.execute("DELETE FROM backtest_run WHERE id=%s", (run_id,))


def _set_scheduler_capacity_override(limit: int | None) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_scheduler_capacity
        SET override_limit=%s,
          override_expires_at=CASE
            WHEN %s::integer IS NULL THEN NULL
            ELSE now() + interval '5 minutes'
          END,
          update_reason='test'
        WHERE singleton
      """, (limit, limit))


def _expire_evaluation_export_lease(export_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_experiment_evaluation_export
        SET lease_expires_at=now() - interval '1 second'
        WHERE id=%s
      """, (export_id,))


def _run_id_for_experiment(experiment_id: str) -> str:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id FROM backtest_run
        WHERE origin_experiment_id=%s
      """, (experiment_id,))
      return str(cur.fetchone()[0])


def _run_ids_for_experiment(experiment_id: str) -> list[str]:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id FROM backtest_run
        WHERE origin_experiment_id=%s ORDER BY created_at, id
      """, (experiment_id,))
      return [str(row[0]) for row in cur.fetchall()]


def _publish_run(run_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT set_config('app.current_user_id', %s, true)", (OWNER.id,))
      cur.execute("SELECT set_config('app.current_is_admin', 'true', true)")
      cur.execute("""
        INSERT INTO backtest_publication (
          owner_user_id, author_name_snapshot, source_run_id, title, description,
          snapshot_schema_version, snapshot_json, snapshot_digest, symbol,
          timeframe, strategy_category, start_date, end_date
        ) VALUES (%s,'实验用户',%s,'共享实验运行','清理测试共享引用',1,%s,%s,
          '688981','5m','grid','2026-07-01','2026-07-02')
      """, (
        OWNER.id, run_id, psycopg.types.json.Jsonb({"runId": run_id}), "a" * 64,
      ))


def _insert_run_artifact(run_id: str, root: Path, filename: str) -> Path:
  storage_key = f"experiment-purge-tests/{run_id}/{filename}"
  artifact_path = root / storage_key
  artifact_path.parent.mkdir(parents=True, exist_ok=True)
  artifact_path.write_text('{"signal":1}\n', encoding="utf-8")
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO artifact_file (
          id, owner_user_id, run_id, artifact_type, storage_key,
          original_filename, content_type, byte_size, content_sha256
        ) VALUES (
          gen_random_uuid(), %s, %s, 'research_signal', %s, %s,
          'application/x-ndjson', %s, %s
        ) RETURNING id
      """, (
        OWNER.id, run_id, storage_key, filename, artifact_path.stat().st_size, "f" * 64,
      ))
      artifact_id = cur.fetchone()[0]
      cur.execute("""
        INSERT INTO research_signal_snapshot (
          run_id, dataset_id, dataset_version, snapshot_hash,
          artifact_file_id, manifest, source_partitions
        ) VALUES (%s,gen_random_uuid(),1,%s,%s,'{}'::jsonb,'[]'::jsonb)
      """, (run_id, "e" * 64, artifact_id))
  return artifact_path


def _artifact_exists_for_run(run_id: str) -> bool:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT EXISTS(SELECT 1 FROM artifact_file WHERE run_id=%s)", (run_id,))
      return bool(cur.fetchone()[0])


def _experiment_asset_hold_count(experiment_id: str) -> int:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT count(*) FROM backtest_experiment_asset_hold hold
        JOIN backtest_experiment_revision revision ON revision.id=hold.revision_id
        WHERE revision.experiment_id=%s
      """, (experiment_id,))
      return int(cur.fetchone()[0])


def _insert_asset_hold(revision_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO backtest_experiment_asset_hold (
          revision_id, owner_user_id, asset_type, asset_id,
          exact_version, content_digest
        ) VALUES (%s,%s,'test_asset','asset-1','version-1',%s)
      """, (revision_id, OWNER.id, "d" * 64))


def _expire_archive_retention(experiment_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_experiment
        SET archive_retention_until=now() - interval '1 second'
        WHERE id=%s
      """, (experiment_id,))


def _insert_unfrozen_purge_dependency(experiment_id: str, revision_id: str) -> str:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO backtest_experiment_audit (
          owner_user_id, experiment_id, revision_id, event_type, detail
        ) VALUES (%s,%s,%s,'test.unfrozen_dependency','{}'::jsonb)
        RETURNING id
      """, (OWNER.id, experiment_id, revision_id))
      return str(cur.fetchone()[0])


def _remove_unfrozen_purge_dependency(audit_id: str, purge_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM backtest_experiment_audit WHERE id=%s", (audit_id,))
      cur.execute("""
        UPDATE backtest_experiment_purge
        SET next_attempt_at=now() - interval '1 second' WHERE id=%s
      """, (purge_id,))


def _assert_purged_graph(experiment_id: str, run_ids: list[str]) -> None:
  with psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"], row_factory=dict_row,
  ) as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT count(*) AS value FROM backtest_experiment WHERE id=%s", (
        experiment_id,
      ))
      assert int(cur.fetchone()["value"]) == 0
      cur.execute("""
        SELECT id, origin_experiment_id, origin_trial_execution_id
        FROM backtest_run WHERE id=ANY(%s) ORDER BY id
      """, (run_ids,))
      remaining = cur.fetchall()
      assert [str(row["id"]) for row in remaining] == [run_ids[0]]
      assert remaining[0]["origin_experiment_id"] is None
      assert remaining[0]["origin_trial_execution_id"] is None
      cur.execute(
        "SELECT count(*) AS value FROM backtest_publication WHERE source_run_id=%s",
        (run_ids[0],),
      )
      assert int(cur.fetchone()["value"]) == 1


def _purge_rls_counts() -> tuple[int, int]:
  with state_store.database_session() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT count(*) AS value FROM backtest_experiment_purge")
      jobs = int(cur.fetchone()["value"])
      cur.execute("SELECT count(*) AS value FROM backtest_experiment_purge_item")
      items = int(cur.fetchone()["value"])
  return jobs, items


def _latest_run_id_for_experiment(experiment_id: str) -> str:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id FROM backtest_run
        WHERE origin_experiment_id=%s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
      """, (experiment_id,))
      return str(cur.fetchone()[0])


def _experiment_run_seeds(experiment_id: str) -> list[dict[str, Any]]:
  with psycopg.connect(
    os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"], row_factory=dict_row,
  ) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT requested_params->'seedBundle' AS seed_bundle
        FROM backtest_run
        WHERE origin_experiment_id=%s
        ORDER BY created_at, id
      """, (experiment_id,))
      return [dict(row["seed_bundle"]) for row in cur.fetchall()]


def _mark_experiment_run_running(run_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET status='running', lease_expires_at=now() + interval '5 minutes',
          updated_at=now()
        WHERE run_id=%s
      """, (run_id,))
      cur.execute("""
        UPDATE backtest_run SET status='running', started_at=now()
        WHERE id=%s
      """, (run_id,))


def _finish_cancelled_run(run_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET status='cancelled', cancel_requested=true,
          finished_at=now(), updated_at=now()
        WHERE run_id=%s
      """, (run_id,))
      cur.execute("""
        UPDATE backtest_run SET status='cancelled', finished_at=now()
        WHERE id=%s AND status='cancelling'
      """, (run_id,))


def _fail_run_for_reconciliation(run_id: str, *, retryable: bool) -> None:
  summary = {
    "error": "平台运行时版本不一致",
    "errorDetail": {
      "category": "system_error",
      "code": "runtime.version_mismatch",
      "message": "平台运行时版本不一致",
      "retryable": retryable,
    },
  }
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET status='failed', finished_at=now(), updated_at=now()
        WHERE run_id=%s
      """, (run_id,))
      cur.execute("""
        UPDATE backtest_run
        SET status='failed', result_summary=%s, error_message=%s, finished_at=now()
        WHERE id=%s
      """, (psycopg.types.json.Jsonb(summary), summary["error"], run_id))


def _make_retry_ready(experiment_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_experiment_trial trial
        SET retry_not_before=now() - interval '1 second'
        FROM backtest_experiment_stage stage
        JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
        WHERE trial.stage_id=stage.id AND revision.experiment_id=%s
      """, (experiment_id,))


def _corrupt_aggregate_cache(experiment_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_experiment_stage stage
        SET in_flight_count=99
        FROM backtest_experiment_revision revision
        WHERE stage.revision_id=revision.id AND revision.experiment_id=%s
      """, (experiment_id,))
      cur.execute("""
        UPDATE backtest_experiment_user_quota quota
        SET experiment_in_flight_count=99
        FROM backtest_experiment experiment
        WHERE quota.owner_user_id=experiment.owner_user_id
          AND experiment.id=%s
      """, (experiment_id,))


def _finish_run_for_reconciliation(run_id: str) -> None:
  result = {
    "resultSchemaVersion": 3,
    "summary": {"totalReturn": 0.1},
    "experimentManifest": {"dataHash": "d" * 64},
  }
  admin_url = os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]
  with psycopg.connect(admin_url) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET status='succeeded', finished_at=now(), updated_at=now()
        WHERE run_id=%s
      """, (run_id,))
      cur.execute("""
        UPDATE backtest_run
        SET status='succeeded', result_summary=%s,
          result_json=%s, finished_at=now()
        WHERE id=%s
      """, (
        psycopg.types.json.Jsonb({"totalReturn": 0.1}),
        psycopg.types.json.Jsonb(result),
        run_id,
      ))


def _evaluation_request(detail: dict[str, Any]) -> EvaluationSpecRequest:
  return EvaluationSpecRequest.model_validate({
    "schemaVersion": "evaluation_spec_v1",
    "name": "基础收益评价",
    "source": {
      "experimentRevisionId": detail["revision"]["revisionId"],
      "stageIds": [detail["stage"]["stageId"]],
      "executionSelection": "selected_execution_at_run_creation",
    },
    "catalogs": {
      "metricCatalogVersion": "metric_catalog_v1",
      "dimensionCatalogVersion": "dimension_catalog_v1",
      "normalizerVersion": None,
    },
    "completeness": {
      "allowPartial": False,
      "minimumCompletionRate": "1.0000",
      "requiredDimensionCoverage": [],
    },
    "columns": ["totalReturn"],
    "derivedMetrics": [],
    "filters": [],
    "groupBy": [],
    "aggregates": [],
    "groupFilters": [],
    "sort": [{
      "type": "metric_sort_v1",
      "metricId": "totalReturn",
      "direction": "desc",
    }],
    "visualizations": [],
    "reportSections": [],
  })


def _mutate_result_after_evaluation_freeze(run_id: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_run
        SET result_json=jsonb_set(result_json, '{summary,totalReturn}', '0.2'::jsonb)
        WHERE id=%s
      """, (run_id,))


def _set_execution_actual_data_hash(run_id: str, data_hash: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_experiment_trial_execution
        SET actual_data_hash=%s
        WHERE run_id=%s
      """, (data_hash, run_id))


def _set_experiment_stage_status(experiment_id: str, status: str) -> None:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_experiment_stage stage
        SET status=%s
        FROM backtest_experiment_revision revision
        WHERE stage.revision_id=revision.id
          AND revision.experiment_id=%s
      """, (status, experiment_id))


def _experiment_run_count(experiment_id: str) -> int:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT count(*) FROM backtest_run WHERE origin_experiment_id=%s",
        (experiment_id,),
      )
      return int(cur.fetchone()[0])


@contextmanager
def _reject_outbox_inserts() -> Iterator[None]:
  admin_url = os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]
  with psycopg.connect(admin_url) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        CREATE OR REPLACE FUNCTION reject_experiment_outbox_test()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'outbox fault';
        END;
        $$
      """)
      cur.execute("""
        CREATE TRIGGER reject_experiment_outbox_test
        BEFORE INSERT ON backtest_experiment_dispatch_outbox
        FOR EACH ROW EXECUTE FUNCTION reject_experiment_outbox_test()
      """)
  try:
    yield
  finally:
    with psycopg.connect(admin_url) as conn:
      with conn.cursor() as cur:
        cur.execute("""
          DROP TRIGGER IF EXISTS reject_experiment_outbox_test
          ON backtest_experiment_dispatch_outbox
        """)
        cur.execute("DROP FUNCTION IF EXISTS reject_experiment_outbox_test()")


def _assert_trial_collection(experiment_id: str, expected_count: int) -> None:
  admin_url = os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]
  with psycopg.connect(admin_url, row_factory=dict_row) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT t.trial_index, t.trial_fingerprint
        FROM backtest_experiment_trial t
        JOIN backtest_experiment_stage s ON s.id=t.stage_id
        JOIN backtest_experiment_revision r ON r.id=s.revision_id
        WHERE r.experiment_id=%s ORDER BY t.trial_index
      """, (experiment_id,))
      rows = cur.fetchall()
  assert [int(row["trial_index"]) for row in rows] == list(range(expected_count))
  assert len({row["trial_fingerprint"] for row in rows}) == expected_count
