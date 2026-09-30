from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from typing import Any, Callable, Iterator

import psycopg
from psycopg.rows import dict_row
import pytest

from backend import settings
from backend.experiments.application import ExperimentApplication
from backend.experiments.compiler import LegacySingleBarTrialCompiler
from backend.experiments.contracts import EvaluationSpecRequest, ExperimentCreateRequest
from backend.experiments.coordinator import ExperimentCoordinator
from backend.experiments.errors import ExperimentError
from backend.experiments.evaluation_application import EvaluationApplication
from backend.experiments.evaluation_worker import EvaluationWorker
from backend.experiments.repository import PostgresExperimentRepository
from backend.services import state_store
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
      cur.execute("DELETE FROM backtest_experiment_audit")
      cur.execute("DELETE FROM backtest_experiment_evaluation_run")
      cur.execute("DELETE FROM backtest_experiment_evaluation_spec")
      cur.execute("DELETE FROM backtest_experiment_dispatch_outbox")
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
      cur.execute("DELETE FROM backtest_experiment_audit")
      cur.execute("DELETE FROM backtest_experiment_evaluation_run")
      cur.execute("DELETE FROM backtest_experiment_evaluation_spec")
      cur.execute("DELETE FROM backtest_experiment_dispatch_outbox")
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


def _generate_all(worker_id: str) -> None:
  coordinator = ExperimentCoordinator(
    repository=GenerationOnlyRepository(), worker_id=worker_id,
  )
  while coordinator.run_once():
    pass


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
  assert blocked["status"] == "queued"
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
  assert blocked["status"] == "queued"
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
  second_run, _ = _as_user(
    OWNER,
    lambda: application.create_run(
      experiment_id, spec["evaluationSpecId"], "run-evaluation-2",
    ),
  )
  worker = EvaluationWorker(worker_id="evaluation-worker-test")
  assert worker.run_once()
  assert worker.run_once()
  first = _as_user(
    OWNER,
    lambda: application.run_detail(experiment_id, first_run["evaluationRunId"]),
  )
  second = _as_user(
    OWNER,
    lambda: application.run_detail(experiment_id, second_run["evaluationRunId"]),
  )
  assert first["status"] == second["status"] == "succeeded"
  assert first["resultSetHash"] == second["resultSetHash"]
  assert first["outputHash"] == second["outputHash"]
  assert first["output"] == second["output"]

  data_drifted, _ = _as_user(
    OWNER,
    lambda: application.create_run(
      experiment_id, spec["evaluationSpecId"], "run-evaluation-data-drift",
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


def _run_id_for_experiment(experiment_id: str) -> str:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id FROM backtest_run
        WHERE origin_experiment_id=%s
      """, (experiment_id,))
      return str(cur.fetchone()[0])


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
