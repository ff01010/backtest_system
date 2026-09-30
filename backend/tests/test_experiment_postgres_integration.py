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
from backend.experiments.contracts import ExperimentCreateRequest
from backend.experiments.coordinator import ExperimentCoordinator
from backend.experiments.errors import ExperimentError
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
from backend.tests.test_experiment_execution import _dataset, _execution_template


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
        UPDATE backtest_run SET origin_trial_execution_id=NULL
        WHERE origin_type='experiment'
      """)
      cur.execute("DELETE FROM backtest_experiment_dispatch_outbox")
      cur.execute("DELETE FROM backtest_experiment_audit")
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
        UPDATE backtest_run SET origin_trial_execution_id=NULL
        WHERE origin_type='experiment'
      """)
      cur.execute("DELETE FROM backtest_experiment_dispatch_outbox")
      cur.execute("DELETE FROM backtest_experiment_audit")
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
) -> str:
  request = _execution_request()
  preview = _as_user(OWNER, lambda: application.preview(request))
  payload = ExperimentCreateRequest(
    previewToken=preview["previewToken"],
    specHash=preview["specHash"],
    experimentSpec=request,
  )
  response, _status = _as_user(OWNER, lambda: application.create(payload, key))
  return str(response["experimentId"])


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


def _run_id_for_experiment(experiment_id: str) -> str:
  with psycopg.connect(os.environ["EXPERIMENT_TEST_ADMIN_DATABASE_URL"]) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id FROM backtest_run
        WHERE origin_experiment_id=%s
      """, (experiment_id,))
      return str(cur.fetchone()[0])


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
