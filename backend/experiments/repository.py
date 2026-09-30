"""实验预览、创建与确定性生成的 PostgreSQL Adapter。"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any, Iterator, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.experiments.canonicalization import digest
from backend.experiments.compiler import TrialCompiler
from backend.experiments.errors import ExperimentError
from backend.experiments.generators import (
  extend_collection_digest,
  initial_collection_digest,
)
from backend.experiments.resolvers import ResolvedRunTemplate
from backend.services import state_store
from backend.strategies.runtime_contract import validate_runtime_capability


CREATE_OPERATION = "experiment.create.v1"
CANCEL_OPERATION = "experiment.cancel.v1"


@dataclass(frozen=True)
class CommandResult:
  response: dict[str, Any]
  status_code: int


class PreviewRepository(Protocol):
  def save_preview(self, preview: dict[str, Any]) -> None:
    """保存带 TTL 的用户预览快照。"""

  def load_preview(self, token: str) -> dict[str, Any] | None:
    """读取当前用户自己的预览快照。"""


class ExperimentRepository(PreviewRepository, Protocol):
  def create_experiment(
    self,
    *,
    idempotency_key: str,
    request_hash: str,
    preview_token: str,
    expected_bindings: dict[str, str],
    normalized_spec: dict[str, Any],
    request_id: str | None,
  ) -> CommandResult:
    """原子创建首个实验修订。"""

  def materialize_next_trial(self, compiler: TrialCompiler) -> bool:
    """在一个事务中物化一个待执行 Trial。"""

  def reconcile_next_execution(self) -> bool:
    """从普通回测真源同步一个 TrialExecution。"""

  def list_experiments(
    self,
    *,
    limit: int,
    cursor: str | None,
  ) -> dict[str, Any]:
    """游标分页读取当前用户实验。"""

  def list_trials(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
    status: str | None,
  ) -> dict[str, Any]:
    """游标分页读取当前实验 Trial。"""


class PostgresExperimentRepository:
  def save_preview(self, preview: dict[str, Any]) -> None:
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "DELETE FROM backtest_experiment_preview "
          "WHERE owner_user_id=%s AND expires_at < now()",
          (state_store.current_user_id(),),
        )
        cur.execute("""
          INSERT INTO backtest_experiment_preview (
            token, owner_user_id, normalized_spec, spec_hash,
            run_template_digest, dataset_snapshot_digest,
            capability_digest, budget_digest, budget_version,
            planned_trial_count, preview_summary, expires_at
          ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
          preview["previewToken"], state_store.current_user_id(),
          Jsonb(preview["normalizedSpec"]), preview["specHash"],
          preview["runTemplateDigest"], preview["datasetSnapshotDigest"],
          preview["capabilityDigest"], preview["budgetDigest"],
          preview["budgetVersion"], preview["plannedTrialCount"],
          Jsonb(preview["previewSummary"]), preview["expiresAt"],
        ))

  def load_preview(self, token: str) -> dict[str, Any] | None:
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT * FROM backtest_experiment_preview
          WHERE token=%s AND owner_user_id=%s
        """, (token, state_store.current_user_id()))
        row = cur.fetchone()
    return dict(row) if row else None

  def create_experiment(
    self,
    *,
    idempotency_key: str,
    request_hash: str,
    preview_token: str,
    expected_bindings: dict[str, str],
    normalized_spec: dict[str, Any],
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, CREATE_OPERATION, idempotency_key)
        replay = _load_idempotency(cur, owner_id, CREATE_OPERATION, idempotency_key)
        if replay:
          return _replay_result(replay, request_hash)
        preview = _load_preview_for_update(cur, owner_id, preview_token)
        if preview is None:
          raise ExperimentError("experiment.not_found", "预览不存在", 404)
        validate_preview_bindings(preview, expected_bindings, datetime.now(timezone.utc))
        if digest(preview["normalized_spec"]) != digest(normalized_spec):
          raise _preview_stale(["normalizedSpec"])
        planned_count = int(preview["planned_trial_count"])
        _reserve_user_plan(cur, owner_id, planned_count)
        response = _insert_experiment_graph(
          cur,
          owner_id=owner_id,
          normalized_spec=normalized_spec,
          preview=preview,
          request_id=request_id,
        )
        _save_idempotency(
          cur, owner_id, CREATE_OPERATION, idempotency_key, request_hash,
          "experiment", response["experimentId"], 201, response,
        )
        return CommandResult(response, 201)

  def find_created_experiment(self, idempotency_key: str) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        row = _load_idempotency(cur, owner_id, CREATE_OPERATION, idempotency_key)
    if row is None:
      raise ExperimentError("experiment.not_found", "创建请求不存在", 404)
    body = dict(row.get("response_body") or {})
    if row.get("status") != "completed" or not body:
      return CommandResult({"status": "processing", "replayed": True}, 202)
    body["replayed"] = True
    return CommandResult(body, 200)

  def get_experiment(self, experiment_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT e.id AS experiment_id, e.name, e.description,
            e.status, e.blocked_reason_code, e.blocked_reason_message,
            e.row_version, e.created_at, e.updated_at,
            r.id AS revision_id, r.revision_no, r.experiment_spec,
            r.spec_hash, r.planned_trial_count, r.resource_budget,
            s.id AS stage_id, s.stage_type, s.generator_version,
            s.status AS stage_status, s.generated_trial_count,
            s.generation_cursor, s.trial_collection_digest,
            s.in_flight_count, s.terminal_count, s.succeeded_count,
            s.failed_count, s.cancelled_count,
            s.blocked_reason_code AS stage_reason_code,
            s.blocked_reason_message AS stage_reason_message
          FROM backtest_experiment e
          JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
          JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
          WHERE e.id=%s AND e.owner_user_id=%s
        """, (experiment_id, owner_id))
        row = cur.fetchone()
    if row is None:
      raise ExperimentError("experiment.not_found", "实验不存在", 404)
    return _detail_response(dict(row))

  def list_experiments(
    self,
    *,
    limit: int,
    cursor: str | None,
  ) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    after = _decode_cursor(cursor, "experiment-list", owner_id, {}) if cursor else None
    params: list[Any] = [owner_id]
    after_sql = ""
    if after:
      after_sql = "AND (e.updated_at, e.id) < (%s::timestamptz, %s::uuid)"
      params.extend([after["updatedAt"], after["experimentId"]])
    params.append(limit + 1)
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute(f"""
          SELECT e.id AS experiment_id, e.name, e.status,
            e.blocked_reason_code, e.blocked_reason_message,
            e.archived_at, e.created_at, e.updated_at,
            r.id AS revision_id, r.revision_no, r.experiment_spec,
            r.planned_trial_count,
            s.id AS stage_id, s.status AS stage_status,
            s.generated_trial_count, s.in_flight_count, s.terminal_count,
            s.succeeded_count, s.failed_count, s.cancelled_count
          FROM backtest_experiment e
          JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
          JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
          WHERE e.owner_user_id=%s AND e.archived_at IS NULL {after_sql}
          ORDER BY e.updated_at DESC, e.id DESC
          LIMIT %s
        """, params)
        rows = [dict(row) for row in cur.fetchall()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
      last = page[-1]
      next_cursor = _encode_cursor("experiment-list", owner_id, {}, {
        "updatedAt": last["updated_at"].isoformat(),
        "experimentId": str(last["experiment_id"]),
      })
    return {"items": [_experiment_list_item(row) for row in page], "nextCursor": next_cursor}

  def list_trials(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
    status: str | None,
  ) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    bindings = {"experimentId": experiment_id, "status": status or ""}
    after = _decode_cursor(cursor, "trial-list", owner_id, bindings) if cursor else None
    params: list[Any] = [experiment_id, owner_id]
    filters = ["e.id=%s", "e.owner_user_id=%s"]
    if status:
      filters.append("t.summary_status=%s")
      params.append(status)
    if after:
      filters.append("t.trial_index > %s")
      params.append(after["trialIndex"])
    params.append(limit + 1)
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute(f"""
          SELECT t.id AS trial_id, t.trial_index, t.trial_spec,
            t.dimension_projection, t.materialization_status, t.summary_status,
            s.id AS stage_id,
            active.id AS active_execution_id, active.run_id AS active_run_id,
            selected.id AS selected_execution_id, selected.run_id AS selected_run_id,
            selected.result_hash, selected.result_schema_version,
            selected.actual_data_hash,
            active_run.status AS active_run_status,
            selected_run.status AS selected_run_status,
            COALESCE(selected.error_summary, active.error_summary) AS error_summary
          FROM backtest_experiment e
          JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
          JOIN backtest_experiment_stage s ON s.revision_id=r.id
          JOIN backtest_experiment_trial t ON t.stage_id=s.id
          LEFT JOIN backtest_experiment_trial_execution active
            ON active.id=t.active_execution_id
          LEFT JOIN backtest_run active_run ON active_run.id=active.run_id
          LEFT JOIN backtest_experiment_trial_execution selected
            ON selected.id=t.selected_execution_id
          LEFT JOIN backtest_run selected_run ON selected_run.id=selected.run_id
          WHERE {' AND '.join(filters)}
          ORDER BY t.trial_index
          LIMIT %s
        """, params)
        rows = [dict(row) for row in cur.fetchall()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
      next_cursor = _encode_cursor("trial-list", owner_id, bindings, {
        "trialIndex": int(page[-1]["trial_index"]),
      })
    return {"items": [_trial_list_item(row) for row in page], "nextCursor": next_cursor}

  def materialize_next_trial(self, compiler: TrialCompiler) -> bool:
    with _coordinator_session() as conn:
      with conn.cursor() as cur:
        candidate = _find_materialization_candidate(cur)
        if candidate is None:
          return False
        locked = _lock_materialization_graph(cur, dict(candidate))
        if locked is None or not _can_materialize(locked):
          return False
        template, assignment, dataset, seed = _materialization_inputs(locked)
        payload = compiler.compile_request(template, assignment, dataset, seed)
        _verify_compiled_request(compiler, template, assignment, locked)
        _insert_trial_execution(cur, locked, payload)
        return True

  def reconcile_next_execution(self) -> bool:
    with _coordinator_session() as conn:
      with conn.cursor() as cur:
        candidate = _find_reconciliation_candidate(cur)
        if candidate is None:
          return False
        locked = _lock_reconciliation_graph(cur, dict(candidate))
        if locked is None:
          return False
        _reconcile_execution(cur, locked)
        return True

  def cancel_experiment(
    self,
    experiment_id: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, CANCEL_OPERATION, idempotency_key)
        replay = _load_idempotency(cur, owner_id, CANCEL_OPERATION, idempotency_key)
        if replay:
          return _replay_result(replay, request_hash)
        row = _load_current_stage_for_update(cur, experiment_id, owner_id)
        if row is None:
          raise ExperimentError("experiment.not_found", "实验不存在", 404)
        response = _cancel_stage(cur, dict(row), request_id)
        _save_idempotency(
          cur, owner_id, CANCEL_OPERATION, idempotency_key, request_hash,
          "experiment", experiment_id, 200, response,
        )
        return CommandResult(response, 200)

  def claim_generation_stage(
    self,
    worker_id: str,
    lease_seconds: int,
  ) -> dict[str, Any] | None:
    attempt_token = str(uuid4())
    with _coordinator_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT s.*, r.experiment_id, r.revision_no, r.experiment_spec,
            r.run_template_digest, r.determinism
          FROM backtest_experiment_stage s
          JOIN backtest_experiment_revision r ON r.id=s.revision_id
          WHERE s.status='preparing'
            AND s.blocked_reason_code IS NULL
            AND r.cancel_requested_at IS NULL
            AND (s.generation_lease_expires_at IS NULL OR s.generation_lease_expires_at < now())
          ORDER BY s.created_at, s.id
          FOR UPDATE OF s SKIP LOCKED
          LIMIT 1
        """)
        row = cur.fetchone()
        if row is None:
          return None
        cur.execute("""
          UPDATE backtest_experiment_stage
          SET generation_lease_owner=%s, generation_attempt_token=%s,
            generation_lease_expires_at=now() + (%s * interval '1 second'),
            generation_heartbeat_at=now(), updated_at=now(), row_version=row_version+1
          WHERE id=%s
        """, (worker_id, attempt_token, lease_seconds, row["id"]))
        claimed = dict(row)
        claimed["generation_attempt_token"] = attempt_token
        return claimed

  def commit_generation_batch(
    self,
    stage_id: str,
    attempt_token: str,
    expected_cursor: int,
    trials: list[dict[str, Any]],
  ) -> bool:
    with _coordinator_session() as conn:
      with conn.cursor() as cur:
        stage = _lock_generation_stage(cur, stage_id)
        if stage is None or stage["status"] != "preparing":
          return False
        if str(stage.get("generation_attempt_token")) != attempt_token:
          return False
        lease_expires_at = stage.get("generation_lease_expires_at")
        if not isinstance(lease_expires_at, datetime) or lease_expires_at <= datetime.now(timezone.utc):
          return False
        if int(stage["generation_cursor"]) != expected_cursor:
          return False
        collection_digest = str(stage["incremental_collection_digest"])
        for trial in trials:
          _insert_or_verify_trial(cur, stage, trial)
          collection_digest = extend_collection_digest(
            collection_digest, trial["trialFingerprint"],
          )
        generated_count = expected_cursor + len(trials)
        completed = generated_count == int(stage["planned_trial_count"])
        if completed:
          _verify_generated_collection(cur, stage_id, generated_count, collection_digest)
        _advance_generation_stage(
          cur, stage, attempt_token, generated_count, collection_digest, completed,
        )
        return True

  def block_generation(
    self,
    stage_id: str,
    attempt_token: str,
    code: str,
    message: str,
  ) -> None:
    with _coordinator_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_stage
          SET status='generation_blocked', blocked_reason_code=%s,
            blocked_reason_message=%s, generation_lease_owner=NULL,
            generation_attempt_token=NULL, generation_lease_expires_at=NULL,
            updated_at=now(), row_version=row_version+1
          WHERE id=%s AND generation_attempt_token=%s AND status='preparing'
          RETURNING revision_id
        """, (code, message[:500], stage_id, attempt_token))
        row = cur.fetchone()
        if row is None:
          return
        cur.execute("""
          UPDATE backtest_experiment_revision
          SET status='generation_blocked', blocked_reason_code=%s,
            blocked_reason_message=%s, updated_at=now()
          WHERE id=%s
          RETURNING experiment_id
        """, (code, message[:500], row["revision_id"]))
        revision = cur.fetchone()
        if revision:
          cur.execute("""
            UPDATE backtest_experiment
            SET status='generation_blocked', blocked_reason_code=%s,
              blocked_reason_message=%s, updated_at=now(), row_version=row_version+1
            WHERE id=%s
          """, (code, message[:500], revision["experiment_id"]))


# 保留原名称，避免前序预览测试和外部导入失效。
PostgresPreviewRepository = PostgresExperimentRepository


def _find_materialization_candidate(cur: Any) -> dict[str, Any] | None:
  cur.execute("""
    SELECT t.id AS trial_id, t.owner_user_id, t.stage_id,
      s.revision_id, r.experiment_id
    FROM backtest_experiment_trial t
    JOIN backtest_experiment_stage s ON s.id=t.stage_id
    JOIN backtest_experiment_revision r ON r.id=s.revision_id
    JOIN backtest_experiment_user_quota q ON q.owner_user_id=t.owner_user_id
    JOIN app_user u ON u.id=t.owner_user_id
    WHERE t.materialization_status='pending'
      AND t.active_execution_id IS NULL
      AND s.status IN ('queued', 'running')
      AND s.generated_trial_count=s.planned_trial_count
      AND s.in_flight_count < %s
      AND r.cancel_requested_at IS NULL AND r.terminal_at IS NULL
      AND q.experiment_in_flight_count < %s
      AND u.status='active'
    ORDER BY s.updated_at, s.id, t.trial_index
    LIMIT 1
  """, (
    settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT,
    settings.EXPERIMENT_MAX_IN_FLIGHT_PER_USER,
  ))
  row = cur.fetchone()
  return dict(row) if row else None


def _lock_materialization_graph(
  cur: Any,
  candidate: dict[str, Any],
) -> dict[str, Any] | None:
  cur.execute("""
    SELECT q.*, u.status AS user_status
    FROM backtest_experiment_user_quota q
    JOIN app_user u ON u.id=q.owner_user_id
    WHERE q.owner_user_id=%s FOR UPDATE OF q
  """, (candidate["owner_user_id"],))
  quota = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment_revision WHERE id=%s FOR UPDATE",
    (candidate["revision_id"],),
  )
  revision = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment_stage WHERE id=%s FOR UPDATE",
    (candidate["stage_id"],),
  )
  stage = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment_trial WHERE id=%s FOR UPDATE",
    (candidate["trial_id"],),
  )
  trial = cur.fetchone()
  if not all((quota, revision, stage, trial)):
    return None
  cur.execute("""
    SELECT count(*) AS value FROM backtest_experiment_asset_hold
    WHERE revision_id=%s AND hold_status <> 'held'
  """, (candidate["revision_id"],))
  unavailable_assets = int(cur.fetchone()["value"])
  return {
    "quota": dict(quota),
    "revision": dict(revision),
    "stage": dict(stage),
    "trial": dict(trial),
    "unavailable_assets": unavailable_assets,
  }


def _can_materialize(locked: dict[str, Any]) -> bool:
  quota = locked["quota"]
  revision = locked["revision"]
  stage = locked["stage"]
  trial = locked["trial"]
  return (
    settings.EXPERIMENTS_ENABLED
    and settings.EXPERIMENT_BAR_ENABLED
    and quota["user_status"] == "active"
    and int(quota["experiment_in_flight_count"])
      < settings.EXPERIMENT_MAX_IN_FLIGHT_PER_USER
    and revision["cancel_requested_at"] is None
    and revision["terminal_at"] is None
    and stage["status"] in {"queued", "running"}
    and int(stage["generated_trial_count"]) == int(stage["planned_trial_count"])
    and int(stage["in_flight_count"])
      < settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT
    and trial["materialization_status"] == "pending"
    and trial["active_execution_id"] is None
    and locked["unavailable_assets"] == 0
  )


def _materialization_inputs(
  locked: dict[str, Any],
) -> tuple[ResolvedRunTemplate, dict[str, Any], dict[str, Any], dict[str, Any]]:
  revision = locked["revision"]
  trial_spec = dict(locked["trial"]["trial_spec"])
  experiment_spec = dict(revision["experiment_spec"])
  snapshot = dict(experiment_spec["runTemplateSnapshot"])
  inputs = tuple(snapshot["tunableInputCatalog"]["inputs"])
  defaults = {
    item["sourceName"]: item["defaultValue"]
    for item in inputs
    if item.get("group") == "strategyParameters"
  }
  template = ResolvedRunTemplate(
    snapshot=snapshot,
    digest=str(revision["run_template_digest"]),
    tunable_inputs=inputs,
    defaults={"strategyParameters": defaults},
  )
  fixed = snapshot["fixedStructure"]
  try:
    validate_runtime_capability(
      fixed.get("runtimeVersion"), fixed.get("runtimeCapabilityDigest"),
    )
  except Exception as error:
    raise ExperimentError(
      "experiment.runtime_unavailable", "冻结策略运行能力当前不可用", 409,
    ) from error
  assignment = dict(trial_spec["dimensionAssignment"])
  scenario_id = assignment["dateScenario"]["scenarioId"]
  dataset = next((
    dict(item) for item in experiment_spec["datasetSnapshots"]
    if item.get("scenario", {}).get("scenarioId") == scenario_id
  ), None)
  if dataset is None:
    raise ExperimentError(
      "experiment.execution_snapshot_invalid", "Trial 缺少冻结行情场景", 409,
    )
  return template, assignment, dataset, dict(trial_spec["seedBundle"])


def _verify_compiled_request(
  compiler: TrialCompiler,
  template: ResolvedRunTemplate,
  assignment: dict[str, Any],
  locked: dict[str, Any],
) -> None:
  expected = dict(locked["trial"]["trial_spec"])["compiledRequest"]
  actual = compiler.compile(template, assignment)
  if actual != expected:
    raise ExperimentError(
      "experiment.generation_identity_conflict",
      "Trial 编译结果与冻结摘要不一致",
      409,
    )


def _insert_trial_execution(
  cur: Any,
  locked: dict[str, Any],
  payload: dict[str, Any],
) -> None:
  trial = locked["trial"]
  stage = locked["stage"]
  revision = locked["revision"]
  owner_id = str(trial["owner_user_id"])
  execution_id = str(uuid4())
  dispatch_key = f"experiment:{execution_id}"
  run_id = _create_experiment_run(
    cur, payload, owner_id, dispatch_key, execution_id,
    str(revision["experiment_id"]), str(stage["id"]),
  )
  cur.execute("""
    INSERT INTO backtest_experiment_trial_execution (
      id, trial_id, owner_user_id, execution_no, dispatch_key,
      run_id, created_reason
    ) VALUES (%s,%s,%s,1,%s,%s,'initial')
  """, (execution_id, trial["id"], owner_id, dispatch_key, run_id))
  cur.execute("""
    INSERT INTO backtest_experiment_dispatch_outbox (
      execution_id, owner_user_id, dispatcher_type, dispatch_key,
      payload_digest, status, dispatched_at
    ) VALUES (%s,%s,'postgres_bar_queue_v1',%s,%s,'succeeded',now())
  """, (
    execution_id, owner_id, dispatch_key, _stable_json_hash(payload),
  ))


  cur.execute("""
    UPDATE backtest_experiment_trial
    SET materialization_status='materialized', active_execution_id=%s,
      summary_status='queued', updated_at=now()
    WHERE id=%s AND materialization_status='pending' AND active_execution_id IS NULL
  """, (execution_id, trial["id"]))
  if cur.rowcount != 1:
    raise ExperimentError("experiment.state_changed", "Trial 状态已经变化", 409)
  cur.execute("""
    UPDATE backtest_experiment_user_quota
    SET experiment_in_flight_count=experiment_in_flight_count+1,
      row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
  """, (owner_id,))
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET in_flight_count=in_flight_count+1, updated_at=now(),
      row_version=row_version+1
    WHERE id=%s
  """, (stage["id"],))
  cur.execute("""
    UPDATE backtest_experiment
    SET updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (revision["experiment_id"],))
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id, trial_id,
      execution_id, event_type, detail
    ) VALUES (%s,%s,%s,%s,%s,%s,'trial.materialized',%s)
  """, (
    owner_id, revision["experiment_id"], revision["id"], stage["id"],
    trial["id"], execution_id,
    Jsonb({"runId": run_id, "dispatcherType": "postgres_bar_queue_v1"}),
  ))


def _create_experiment_run(
  cur: Any,
  payload: dict[str, Any],
  owner_id: str,
  dispatch_key: str,
  execution_id: str,
  experiment_id: str,
  stage_id: str,
) -> str:
  cur.execute(
    "SELECT current_setting('app.current_user_id', true) AS current_user_id",
  )
  previous_user_id = str(cur.fetchone()["current_user_id"] or "")
  cur.execute(
    "SELECT set_config('app.current_user_id', %s, true)", (owner_id,),
  )
  run_id, _display_no = state_store.create_queued_backtest_job_in_transaction(
    cur,
    payload,
    owner_id,
    request_key=dispatch_key,
    origin={
      "type": "experiment",
      "experimentId": experiment_id,
      "stageId": stage_id,
      "trialExecutionId": execution_id,
    },
  )
  cur.execute(
    "SELECT set_config('app.current_user_id', %s, true)",
    (previous_user_id,),
  )
  return run_id


def _find_reconciliation_candidate(cur: Any) -> dict[str, Any] | None:
  cur.execute("""
    SELECT execution.id AS execution_id, execution.owner_user_id,
      execution.trial_id, trial.stage_id, stage.revision_id,
      revision.experiment_id
    FROM backtest_experiment_trial_execution execution
    JOIN backtest_experiment_trial trial ON trial.id=execution.trial_id
    JOIN backtest_experiment_stage stage ON stage.id=trial.stage_id
    JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
    JOIN backtest_run run ON run.id=execution.run_id
    WHERE execution.permit_released_at IS NULL
      AND (
        run.status IN ('succeeded', 'failed', 'cancelled')
        OR trial.summary_status <> run.status
        OR (run.started_at IS NOT NULL AND execution.started_at IS NULL)
      )
    ORDER BY execution.created_at, execution.id
    LIMIT 1
  """)
  row = cur.fetchone()
  return dict(row) if row else None


def _lock_reconciliation_graph(
  cur: Any,
  candidate: dict[str, Any],
) -> dict[str, Any] | None:
  cur.execute(
    "SELECT * FROM backtest_experiment_user_quota WHERE owner_user_id=%s FOR UPDATE",
    (candidate["owner_user_id"],),
  )
  quota = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment_revision WHERE id=%s FOR UPDATE",
    (candidate["revision_id"],),
  )
  revision = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment_stage WHERE id=%s FOR UPDATE",
    (candidate["stage_id"],),
  )
  stage = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment_trial WHERE id=%s FOR UPDATE",
    (candidate["trial_id"],),
  )
  trial = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment_trial_execution WHERE id=%s FOR UPDATE",
    (candidate["execution_id"],),
  )
  execution = cur.fetchone()
  if not all((quota, revision, stage, trial, execution)):
    return None
  cur.execute("SELECT * FROM backtest_run WHERE id=%s FOR UPDATE", (execution["run_id"],))
  run = cur.fetchone()
  if run is None or str(run["owner_user_id"]) != str(execution["owner_user_id"]):
    raise ExperimentError("experiment.execution_identity_conflict", "回测执行归属不一致", 409)
  return {
    "quota": dict(quota),
    "revision": dict(revision),
    "stage": dict(stage),
    "trial": dict(trial),
    "execution": dict(execution),
    "run": dict(run),
  }


def _reconcile_execution(cur: Any, locked: dict[str, Any]) -> None:
  execution = locked["execution"]
  trial = locked["trial"]
  run = locked["run"]
  status = str(run["status"])
  if execution["permit_released_at"] is not None:
    return
  if str(trial.get("active_execution_id")) != str(execution["id"]):
    raise ExperimentError(
      "experiment.execution_identity_conflict", "Trial 活动执行归属不一致", 409,
    )
  if status not in {"succeeded", "failed", "cancelled"}:
    _sync_active_execution(cur, locked, status)
    return
  result = run.get("result_json") if isinstance(run.get("result_json"), dict) else None
  summary = run.get("result_summary") if isinstance(run.get("result_summary"), dict) else {}
  result_hash = _stable_json_hash(result) if result is not None else None
  result_schema = str(result.get("resultSchemaVersion")) if result else None
  data_hash = _actual_data_hash(result, run.get("market_data_snapshot"))
  terminal_sequence = int(locked["stage"]["next_terminal_sequence"])
  cur.execute("""
    UPDATE backtest_experiment_trial_execution
    SET result_schema_version=%s, result_hash=%s, actual_data_hash=%s,
      error_summary=%s, terminal_sequence=%s, permit_released_at=now(),
      started_at=COALESCE(started_at,%s), finished_at=COALESCE(%s,now()),
      updated_at=now()
    WHERE id=%s AND permit_released_at IS NULL
  """, (
    result_schema, result_hash, data_hash,
    Jsonb(summary) if status != "succeeded" else None,
    terminal_sequence, run.get("started_at"), run.get("finished_at"), execution["id"],
  ))
  if cur.rowcount != 1:
    return
  selected = execution["id"] if status == "succeeded" else trial.get("selected_execution_id")
  cur.execute("""
    UPDATE backtest_experiment_trial
    SET active_execution_id=NULL, selected_execution_id=%s,
      summary_status=%s, updated_at=now()
    WHERE id=%s AND active_execution_id=%s
  """, (selected, status, trial["id"], execution["id"]))
  cur.execute("""
    UPDATE backtest_experiment_user_quota
    SET experiment_in_flight_count=GREATEST(0,experiment_in_flight_count-1),
      row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
  """, (execution["owner_user_id"],))
  count_column = {
    "succeeded": "succeeded_count",
    "failed": "failed_count",
    "cancelled": "cancelled_count",
  }[status]
  cur.execute(f"""
    UPDATE backtest_experiment_stage
    SET in_flight_count=GREATEST(0,in_flight_count-1),
      terminal_count=terminal_count+1, {count_column}={count_column}+1,
      next_terminal_sequence=next_terminal_sequence+1,
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (locked["stage"]["id"],))
  _update_aggregate_status(cur, locked)
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id, trial_id,
      execution_id, event_type, detail
    ) VALUES (%s,%s,%s,%s,%s,%s,'trial.reconciled',%s)
  """, (
    execution["owner_user_id"], locked["revision"]["experiment_id"],
    locked["revision"]["id"], locked["stage"]["id"], trial["id"],
    execution["id"], Jsonb({"status": status, "terminalSequence": terminal_sequence}),
  ))


def _sync_active_execution(cur: Any, locked: dict[str, Any], status: str) -> None:
  run = locked["run"]
  execution = locked["execution"]
  cur.execute("""
    UPDATE backtest_experiment_trial_execution
    SET started_at=COALESCE(started_at,%s), updated_at=now()
    WHERE id=%s
  """, (run.get("started_at"), execution["id"]))
  cur.execute("""
    UPDATE backtest_experiment_trial SET summary_status=%s, updated_at=now()
    WHERE id=%s AND active_execution_id=%s
  """, (status, locked["trial"]["id"], execution["id"]))
  aggregate_status = "running" if status in {"running", "cancelling"} else "queued"
  cur.execute(
    "UPDATE backtest_experiment_stage SET status=%s, updated_at=now() WHERE id=%s",
    (aggregate_status, locked["stage"]["id"]),
  )
  cur.execute(
    "UPDATE backtest_experiment_revision SET status=%s, updated_at=now() WHERE id=%s",
    (aggregate_status, locked["revision"]["id"]),
  )
  cur.execute("""
    UPDATE backtest_experiment SET status=%s, updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (aggregate_status, locked["revision"]["experiment_id"]))


def _update_aggregate_status(cur: Any, locked: dict[str, Any]) -> None:
  stage_id = locked["stage"]["id"]
  cur.execute("SELECT * FROM backtest_experiment_stage WHERE id=%s", (stage_id,))
  stage = dict(cur.fetchone())
  terminal = int(stage["terminal_count"])
  planned = int(stage["planned_trial_count"])
  if terminal == planned:
    if int(stage["succeeded_count"]) == planned:
      status = "completed"
    elif int(stage["succeeded_count"]) > 0:
      status = "partially_completed"
    elif int(stage["failed_count"]) > 0:
      status = "failed"
    else:
      status = "cancelled"
  elif int(stage["in_flight_count"]) > 0:
    status = "running"
  else:
    status = "queued"
  terminal_at = "now()" if terminal == planned else "NULL"
  cur.execute(
    "UPDATE backtest_experiment_stage SET status=%s, updated_at=now() WHERE id=%s",
    (status, stage_id),
  )
  cur.execute(f"""
    UPDATE backtest_experiment_revision
    SET status=%s, terminal_at=COALESCE(terminal_at,{terminal_at}), updated_at=now()
    WHERE id=%s
  """, (status, locked["revision"]["id"]))
  cur.execute("""
    UPDATE backtest_experiment
    SET status=%s, updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (status, locked["revision"]["experiment_id"]))
  if terminal == planned and locked["revision"]["terminal_at"] is None:
    cur.execute("""
      UPDATE backtest_experiment_user_quota
      SET reserved_planned_trial_count=GREATEST(
        0,reserved_planned_trial_count-%s
      ), row_version=row_version+1, updated_at=now()
      WHERE owner_user_id=%s
    """, (planned, locked["trial"]["owner_user_id"]))


def _actual_data_hash(result: Any, market_snapshot: Any) -> str | None:
  if isinstance(result, dict):
    manifest = result.get("experimentManifest")
    if isinstance(manifest, dict) and isinstance(manifest.get("dataHash"), str):
      return manifest["dataHash"]
    data = result.get("dataSnapshot")
    if isinstance(data, dict):
      bar = data.get("bar")
      if isinstance(bar, dict) and isinstance(bar.get("hash"), str):
        return bar["hash"]
  if isinstance(market_snapshot, dict):
    bar = market_snapshot.get("barSnapshot")
    if isinstance(bar, dict) and isinstance(bar.get("hash"), str):
      return bar["hash"]
  return None


def _stable_json_hash(value: Any) -> str:
  encoded = json.dumps(
    value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
  )
  return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _encode_cursor(
  kind: str,
  owner_id: str,
  bindings: dict[str, Any],
  position: dict[str, Any],
) -> str:
  payload = json.dumps({
    "kind": kind,
    "ownerId": owner_id,
    "bindings": bindings,
    "position": position,
  }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
  signature = hmac.new(
    settings.EXPERIMENT_CURSOR_SIGNING_SECRET.encode("utf-8"),
    payload,
    hashlib.sha256,
  ).digest()
  return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def _decode_cursor(
  value: str,
  kind: str,
  owner_id: str,
  bindings: dict[str, Any],
) -> dict[str, Any]:
  try:
    padded = value + "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    payload, signature = raw[:-32], raw[-32:]
    expected = hmac.new(
      settings.EXPERIMENT_CURSOR_SIGNING_SECRET.encode("utf-8"),
      payload,
      hashlib.sha256,
    ).digest()
    decoded = json.loads(payload.decode("utf-8"))
  except Exception as error:
    raise ExperimentError("experiment.cursor_invalid", "分页游标无效", 400) from error
  if not isinstance(decoded, dict):
    raise ExperimentError("experiment.cursor_invalid", "分页游标无效", 400)
  if not hmac.compare_digest(signature, expected) or decoded != {
    "kind": kind,
    "ownerId": owner_id,
    "bindings": bindings,
    "position": decoded.get("position"),
  }:
    raise ExperimentError("experiment.cursor_invalid", "分页游标无效", 400)
  position = decoded.get("position")
  if not isinstance(position, dict):
    raise ExperimentError("experiment.cursor_invalid", "分页游标无效", 400)
  return position


def _experiment_list_item(row: dict[str, Any]) -> dict[str, Any]:
  spec = dict(row["experiment_spec"])
  fixed = spec["runTemplateSnapshot"]["fixedStructure"]
  return {
    "experimentId": str(row["experiment_id"]),
    "name": row["name"],
    "subject": {
      **spec["subjectRef"],
      "name": fixed["strategyName"],
    },
    "currentRevisionId": str(row["revision_id"]),
    "revisionNo": int(row["revision_no"]),
    "status": row["status"],
    "reason": _row_reason(row),
    "runPath": spec["runTemplateSnapshot"]["runPath"],
    "scopeType": spec["runTemplateSnapshot"]["scopeType"],
    "symbol": fixed["symbol"],
    "timeframe": fixed["timeframe"],
    "fillModel": fixed["fillModel"],
    "plannedTrialCount": int(row["planned_trial_count"]),
    "generatedTrialCount": int(row["generated_trial_count"]),
    "materializedTrialCount": int(row["terminal_count"]) + int(row["in_flight_count"]),
    "queuedTrialCount": max(
      0,
      int(row["planned_trial_count"])
        - int(row["terminal_count"])
        - int(row["in_flight_count"]),
    ),
    "runningTrialCount": int(row["in_flight_count"]),
    "succeededTrialCount": int(row["succeeded_count"]),
    "failedTrialCount": int(row["failed_count"]),
    "cancelledTrialCount": int(row["cancelled_count"]),
    "createdAt": row["created_at"].isoformat(),
    "updatedAt": row["updated_at"].isoformat(),
    "archivedAt": row["archived_at"].isoformat() if row["archived_at"] else None,
  }


def _trial_list_item(row: dict[str, Any]) -> dict[str, Any]:
  trial_spec = dict(row["trial_spec"])
  run_id = row.get("selected_run_id") or row.get("active_run_id")
  execution_id = row.get("selected_execution_id") or row.get("active_execution_id")
  return {
    "trialId": str(row["trial_id"]),
    "trialIndex": int(row["trial_index"]),
    "stageId": str(row["stage_id"]),
    "candidateKey": trial_spec.get("candidateKey"),
    "dimensionAssignment": row["dimension_projection"],
    "materializationStatus": row["materialization_status"],
    "summaryStatus": row["summary_status"],
    "activeExecution": (
      {"executionId": str(row["active_execution_id"]), "runId": str(row["active_run_id"]),
       "status": row.get("active_run_status")}
      if row.get("active_execution_id") else None
    ),
    "selectedExecution": (
      {"executionId": str(row["selected_execution_id"]), "runId": str(row["selected_run_id"]),
       "status": row.get("selected_run_status"), "resultHash": row.get("result_hash"),
       "resultSchemaVersion": row.get("result_schema_version"),
       "actualDataHash": row.get("actual_data_hash")}
      if row.get("selected_execution_id") else None
    ),
    "errorSummary": row.get("error_summary"),
    "runId": str(run_id) if run_id else None,
    "runUrl": f"/?workspace=backtest&runId={run_id}" if run_id else None,
    "executionId": str(execution_id) if execution_id else None,
  }


def _row_reason(row: dict[str, Any]) -> dict[str, Any] | None:
  code = row.get("blocked_reason_code")
  if not code:
    return None
  return {"code": code, "message": row.get("blocked_reason_message")}


def preview_expired(row: dict[str, Any], now: datetime) -> bool:
  expires_at = row.get("expires_at")
  return not isinstance(expires_at, datetime) or expires_at <= now


def validate_preview_bindings(
  row: dict[str, Any],
  expected: dict[str, str],
  now: datetime,
) -> None:
  """校验 TTL 与全部冻结摘要，避免 Token 换绑。"""
  if preview_expired(row, now):
    raise ExperimentError("experiment.preview_expired", "预览已过期，请重新预览", 410)
  columns = {
    "specHash": "spec_hash",
    "runTemplateDigest": "run_template_digest",
    "datasetSnapshotDigest": "dataset_snapshot_digest",
    "capabilityDigest": "capability_digest",
    "budgetDigest": "budget_digest",
  }
  changed = [name for name, column in columns.items() if row.get(column) != expected.get(name)]
  if changed:
    raise _preview_stale(changed)


def _preview_stale(changed: list[str]) -> ExperimentError:
  return ExperimentError(
    "experiment.preview_stale",
    "预览绑定的输入或运行环境已变化，请重新预览",
    409,
    details={"changedBindings": changed},
  )


def _lock_command(cur: Any, owner_id: str, operation: str, key: str) -> None:
  cur.execute(
    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
    (f"{owner_id}:{operation}:{key}",),
  )


def _load_idempotency(
  cur: Any,
  owner_id: str,
  operation: str,
  key: str,
) -> dict[str, Any] | None:
  cur.execute("""
    SELECT * FROM backtest_internal_idempotency
    WHERE owner_user_id=%s AND operation_type=%s AND idempotency_key=%s
  """, (owner_id, operation, key))
  row = cur.fetchone()
  return dict(row) if row else None


def _replay_result(row: dict[str, Any], request_hash: str) -> CommandResult:
  if row["request_hash"] != request_hash:
    raise ExperimentError("idempotency.conflict", "同一幂等键已用于不同请求", 409)
  body = dict(row.get("response_body") or {})
  if row.get("status") != "completed" or not body:
    return CommandResult({"status": "processing", "replayed": True}, 202)
  body["replayed"] = True
  return CommandResult(body, 200)


def _load_preview_for_update(cur: Any, owner_id: str, token: str) -> dict[str, Any] | None:
  cur.execute("""
    SELECT * FROM backtest_experiment_preview
    WHERE token=%s AND owner_user_id=%s
    FOR UPDATE
  """, (token, owner_id))
  row = cur.fetchone()
  return dict(row) if row else None


def _reserve_user_plan(cur: Any, owner_id: str, planned_count: int) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_user_quota (owner_user_id)
    VALUES (%s) ON CONFLICT (owner_user_id) DO NOTHING
  """, (owner_id,))
  cur.execute("""
    SELECT reserved_planned_trial_count
    FROM backtest_experiment_user_quota
    WHERE owner_user_id=%s FOR UPDATE
  """, (owner_id,))
  quota = cur.fetchone()
  reserved = int(quota["reserved_planned_trial_count"])
  if reserved + planned_count > settings.EXPERIMENT_MAX_USER_PLANNED_TRIALS:
    raise ExperimentError(
      "experiment.plan_limit_exceeded",
      "用户未完成实验的计划 Trial 总量超过上限",
      429,
      details={
        "reserved": reserved,
        "requested": planned_count,
        "limit": settings.EXPERIMENT_MAX_USER_PLANNED_TRIALS,
      },
    )
  cur.execute("""
    UPDATE backtest_experiment_user_quota
    SET reserved_planned_trial_count=reserved_planned_trial_count+%s,
      row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
  """, (planned_count, owner_id))


def _insert_experiment_graph(
  cur: Any,
  *,
  owner_id: str,
  normalized_spec: dict[str, Any],
  preview: dict[str, Any],
  request_id: str | None,
) -> dict[str, Any]:
  experiment_id = str(uuid4())
  revision_id = str(uuid4())
  stage_id = str(uuid4())
  stage_spec = dict(normalized_spec["plan"]["stages"][0])
  cur.execute("""
    INSERT INTO backtest_experiment (
      id, owner_user_id, name, description, status
    ) VALUES (%s,%s,%s,%s,'preparing')
    RETURNING created_at
  """, (
    experiment_id, owner_id, normalized_spec["name"], normalized_spec["description"],
  ))
  created_at = cur.fetchone()["created_at"]
  _insert_revision(cur, revision_id, experiment_id, owner_id, normalized_spec, preview)
  _insert_stage(cur, stage_id, revision_id, owner_id, stage_spec, preview)
  cur.execute(
    "UPDATE backtest_experiment SET current_revision_id=%s WHERE id=%s",
    (revision_id, experiment_id),
  )
  _insert_asset_holds(cur, revision_id, owner_id, normalized_spec)
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id,
      event_type, request_id, detail
    ) VALUES (%s,%s,%s,%s,'experiment.created',%s,%s)
  """, (
    owner_id, experiment_id, revision_id, stage_id, request_id,
    Jsonb({"plannedTrialCount": preview["planned_trial_count"]}),
  ))
  return {
    "experimentId": experiment_id,
    "revisionId": revision_id,
    "stageId": stage_id,
    "status": "preparing",
    "plannedTrialCount": int(preview["planned_trial_count"]),
    "generatedTrialCount": 0,
    "specHash": str(preview["spec_hash"]),
    "replayed": False,
    "createdAt": created_at.isoformat(),
  }


def _insert_revision(
  cur: Any,
  revision_id: str,
  experiment_id: str,
  owner_id: str,
  normalized_spec: dict[str, Any],
  preview: dict[str, Any],
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_revision (
      id, experiment_id, owner_user_id, revision_no, experiment_spec,
      spec_hash, run_template_digest, dataset_snapshot_digest,
      canonicalization_version, budget_version, resource_budget,
      determinism, planned_trial_count, status
    ) VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,'preparing')
  """, (
    revision_id, experiment_id, owner_id, Jsonb(normalized_spec),
    preview["spec_hash"], preview["run_template_digest"],
    preview["dataset_snapshot_digest"], normalized_spec["canonicalizationVersion"],
    preview["budget_version"], Jsonb(normalized_spec["resourceEstimate"]),
    Jsonb(normalized_spec["determinism"]), preview["planned_trial_count"],
  ))


def _insert_stage(
  cur: Any,
  stage_id: str,
  revision_id: str,
  owner_id: str,
  stage_spec: dict[str, Any],
  preview: dict[str, Any],
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_stage (
      id, revision_id, owner_user_id, stage_index, stage_type,
      generator_version, stage_spec, stage_input_fingerprint,
      planned_trial_count, incremental_collection_digest, status
    ) VALUES (%s,%s,%s,0,%s,%s,%s,%s,%s,%s,'preparing')
  """, (
    stage_id, revision_id, owner_id, stage_spec["stageType"],
    stage_spec["generatorVersion"], Jsonb(stage_spec),
    stage_spec["stageInputFingerprint"], preview["planned_trial_count"],
    initial_collection_digest(),
  ))


def _insert_asset_holds(
  cur: Any,
  revision_id: str,
  owner_id: str,
  normalized_spec: dict[str, Any],
) -> None:
  manifest = normalized_spec["runTemplateSnapshot"]["assetManifestSnapshot"]
  for asset in manifest.get("assets", []):
    cur.execute("""
      INSERT INTO backtest_experiment_asset_hold (
        revision_id, owner_user_id, asset_type, asset_id,
        exact_version, content_digest
      ) VALUES (%s,%s,%s,%s,%s,%s)
    """, (
      revision_id, owner_id, asset["assetType"], str(asset["assetId"]),
      str(asset["exactVersion"]), asset["contentDigest"],
    ))


def _save_idempotency(
  cur: Any,
  owner_id: str,
  operation: str,
  key: str,
  request_hash: str,
  resource_type: str,
  resource_id: str,
  response_status: int,
  response: dict[str, Any],
) -> None:
  cur.execute("""
    INSERT INTO backtest_internal_idempotency (
      owner_user_id, operation_type, idempotency_key, request_hash,
      status, resource_type, resource_id, response_status,
      response_body, completed_at
    ) VALUES (%s,%s,%s,%s,'completed',%s,%s,%s,%s,now())
  """, (
    owner_id, operation, key, request_hash, resource_type, resource_id,
    response_status, Jsonb(response),
  ))


def _detail_response(row: dict[str, Any]) -> dict[str, Any]:
  reason = None
  reason_code = row.get("stage_reason_code") or row.get("blocked_reason_code")
  if reason_code:
    reason = {
      "code": reason_code,
      "message": row.get("stage_reason_message") or row.get("blocked_reason_message"),
    }
  spec = dict(row["experiment_spec"])
  return {
    "experimentId": str(row["experiment_id"]),
    "name": row["name"],
    "description": row["description"],
    "status": row["status"],
    "snapshotVersion": int(row["row_version"]),
    "revision": {
      "revisionId": str(row["revision_id"]),
      "revisionNo": int(row["revision_no"]),
      "specHash": row["spec_hash"],
    },
    "stage": {
      "stageId": str(row["stage_id"]),
      "stageType": row["stage_type"],
      "generatorVersion": row["generator_version"],
      "status": row["stage_status"],
      "plannedTrialCount": int(row["planned_trial_count"]),
      "generatedTrialCount": int(row["generated_trial_count"]),
      "generationCursor": int(row["generation_cursor"]),
      "trialCollectionDigest": row.get("trial_collection_digest"),
      "inFlightTrialCount": int(row.get("in_flight_count") or 0),
      "terminalTrialCount": int(row.get("terminal_count") or 0),
      "succeededTrialCount": int(row.get("succeeded_count") or 0),
      "failedTrialCount": int(row.get("failed_count") or 0),
      "cancelledTrialCount": int(row.get("cancelled_count") or 0),
      "queuedTrialCount": max(
        0,
        int(row["planned_trial_count"])
          - int(row.get("terminal_count") or 0)
          - int(row.get("in_flight_count") or 0),
      ),
    },
    "resourceBudget": row["resource_budget"],
    "frozenDefinition": {
      "subjectRef": spec["subjectRef"],
      "fixedStructure": spec["runTemplateSnapshot"]["fixedStructure"],
      "plan": spec["plan"],
      "datasetSnapshots": [
        _public_dataset_snapshot(item) for item in spec["datasetSnapshots"]
      ],
      "determinism": spec["determinism"],
    },
    "blockedReason": reason,
    "createdAt": row["created_at"].isoformat(),
    "updatedAt": row["updated_at"].isoformat(),
  }


def _public_dataset_snapshot(value: Any) -> dict[str, Any]:
  snapshot = dict(value) if isinstance(value, dict) else {}
  research = snapshot.get("researchSignalSnapshot")
  if isinstance(research, dict):
    snapshot["researchSignalSnapshot"] = {
      key: item for key, item in research.items() if key != "artifactFile"
    }
  return snapshot


def _load_current_stage_for_update(
  cur: Any,
  experiment_id: str,
  owner_id: str,
) -> dict[str, Any] | None:
  cur.execute("""
    SELECT e.id AS experiment_id, e.status AS experiment_status,
      e.current_revision_id, r.planned_trial_count, r.status AS revision_status,
      s.id AS stage_id, s.status AS stage_status, s.generated_trial_count,
      s.in_flight_count,
      (SELECT count(*) FROM backtest_experiment_trial trial
       WHERE trial.stage_id=s.id AND trial.materialization_status='materialized')
        AS materialized_count
    FROM backtest_experiment e
    JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
    JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
    WHERE e.id=%s AND e.owner_user_id=%s
    FOR UPDATE OF e, r, s
  """, (experiment_id, owner_id))
  row = cur.fetchone()
  return dict(row) if row else None


def _cancel_stage(cur: Any, row: dict[str, Any], request_id: str | None) -> dict[str, Any]:
  if row["stage_status"] != "cancelled":
    if row["stage_status"] not in {"preparing", "queued", "generation_blocked"}:
      raise ExperimentError("experiment.command_invalid", "当前实验状态不能取消", 400)
    if int(row.get("materialized_count") or 0) > 0:
      raise ExperimentError(
        "experiment.command_invalid",
        "当前实验已经物化普通回测任务，运行中批量取消将在后续阶段开放",
        409,
      )
    remaining = int(row["planned_trial_count"]) - int(row["generated_trial_count"])
    cur.execute("""
      UPDATE backtest_experiment_trial
      SET materialization_status='cancelled_before_materialization',
        summary_status='cancelled', updated_at=now()
      WHERE stage_id=%s AND materialization_status='pending'
    """, (row["stage_id"],))
    cancelled_trials = cur.rowcount
    cur.execute("""
      UPDATE backtest_experiment_stage
      SET status='cancelled', cancelled_before_generation=%s,
        terminal_count=terminal_count+%s,
        cancelled_count=cancelled_count+%s,
        generation_lease_owner=NULL, generation_attempt_token=NULL,
        generation_lease_expires_at=NULL, updated_at=now(), row_version=row_version+1
      WHERE id=%s
    """, (remaining, cancelled_trials, cancelled_trials, row["stage_id"]))
    cur.execute("""
      UPDATE backtest_experiment_revision
      SET status='cancelled', cancel_requested_at=now(), terminal_at=now(), updated_at=now()
      WHERE id=%s
    """, (row["current_revision_id"],))
    cur.execute("""
      UPDATE backtest_experiment
      SET status='cancelled', updated_at=now(), row_version=row_version+1
      WHERE id=%s
    """, (row["experiment_id"],))
    cur.execute("""
      UPDATE backtest_experiment_user_quota
      SET reserved_planned_trial_count=GREATEST(
        0, reserved_planned_trial_count-%s
      ), row_version=row_version+1, updated_at=now()
      WHERE owner_user_id=%s
    """, (row["planned_trial_count"], state_store.current_user_id()))
    cur.execute("""
      INSERT INTO backtest_experiment_audit (
        owner_user_id, experiment_id, revision_id, stage_id,
        event_type, request_id, detail
      ) VALUES (%s,%s,%s,%s,'experiment.cancelled',%s,%s)
    """, (
      state_store.current_user_id(), row["experiment_id"],
      row["current_revision_id"], row["stage_id"], request_id,
      Jsonb({"generatedTrialCount": row["generated_trial_count"]}),
    ))
  return {"experimentId": str(row["experiment_id"]), "status": "cancelled"}


@contextmanager
def _coordinator_session() -> Iterator[psycopg.Connection[Any]]:
  if not settings.DATABASE_URL:
    raise state_store.DatabaseError("未设置 TREND_GRID_DATABASE_URL")
  try:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT set_config('app.current_user_id', %s, true)",
          ("00000000-0000-0000-0000-000000000000",),
        )
        cur.execute("SELECT set_config('app.current_is_admin', 'true', true)")
      yield conn
  except psycopg.Error as error:
    raise state_store.DatabaseError(str(error)) from error


def _lock_generation_stage(cur: Any, stage_id: str) -> dict[str, Any] | None:
  cur.execute("""
    SELECT * FROM backtest_experiment_stage WHERE id=%s FOR UPDATE
  """, (stage_id,))
  row = cur.fetchone()
  return dict(row) if row else None


def _insert_or_verify_trial(cur: Any, stage: dict[str, Any], trial: dict[str, Any]) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_trial (
      stage_id, owner_user_id, trial_index, trial_spec,
      trial_spec_digest, trial_fingerprint, dimension_projection
    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT DO NOTHING
    RETURNING id
  """, (
    stage["id"], stage["owner_user_id"], trial["trialIndex"],
    Jsonb(trial["trialSpec"]), trial["trialSpecDigest"],
    trial["trialFingerprint"], Jsonb(trial["dimensionAssignment"]),
  ))
  if cur.fetchone() is not None:
    return
  cur.execute("""
    SELECT trial_index, trial_spec_digest, trial_fingerprint
    FROM backtest_experiment_trial
    WHERE stage_id=%s AND (trial_index=%s OR trial_fingerprint=%s)
  """, (stage["id"], trial["trialIndex"], trial["trialFingerprint"]))
  conflicts = [dict(row) for row in cur.fetchall()]
  safe = len(conflicts) == 1 and conflicts[0] == {
    "trial_index": trial["trialIndex"],
    "trial_spec_digest": trial["trialSpecDigest"],
    "trial_fingerprint": trial["trialFingerprint"],
  }
  if not safe:
    raise ExperimentError(
      "experiment.generation_identity_conflict",
      "Trial 生成身份与已持久化集合冲突",
      409,
    )


def _verify_generated_collection(
  cur: Any,
  stage_id: str,
  generated_count: int,
  expected_digest: str,
) -> None:
  cur.execute("""
    SELECT trial_index, trial_fingerprint
    FROM backtest_experiment_trial
    WHERE stage_id=%s ORDER BY trial_index
  """, (stage_id,))
  rows = cur.fetchall()
  collection_digest = initial_collection_digest()
  contiguous = len(rows) == generated_count
  for index, row in enumerate(rows):
    contiguous = contiguous and int(row["trial_index"]) == index
    collection_digest = extend_collection_digest(collection_digest, row["trial_fingerprint"])
  if not contiguous or collection_digest != expected_digest:
    raise ExperimentError(
      "experiment.generation_identity_conflict",
      "Trial 集合摘要校验失败",
      409,
    )


def _advance_generation_stage(
  cur: Any,
  stage: dict[str, Any],
  attempt_token: str,
  generated_count: int,
  collection_digest: str,
  completed: bool,
) -> None:
  next_status = "queued" if completed else "preparing"
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET generated_trial_count=%s, generation_cursor=%s,
      incremental_collection_digest=%s, trial_collection_digest=%s,
      status=%s, generation_lease_owner=NULL, generation_attempt_token=NULL,
      generation_lease_expires_at=NULL, generation_heartbeat_at=now(),
      updated_at=now(), row_version=row_version+1
    WHERE id=%s AND generation_attempt_token=%s AND status='preparing'
  """, (
    generated_count, generated_count, collection_digest,
    collection_digest if completed else None, next_status,
    stage["id"], attempt_token,
  ))
  if cur.rowcount != 1:
    raise ExperimentError("experiment.state_changed", "生成租约已失效", 409)
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, revision_id, stage_id, event_type, detail
    ) VALUES (%s,%s,%s,%s,%s)
  """, (
    stage["owner_user_id"], stage["revision_id"], stage["id"],
    "stage.generation_completed" if completed else "stage.generation_batch_committed",
    Jsonb({"generatedTrialCount": generated_count}),
  ))
  if not completed:
    return
  cur.execute("""
    UPDATE backtest_experiment_revision SET status='queued', updated_at=now()
    WHERE id=%s RETURNING experiment_id
  """, (stage["revision_id"],))
  revision = cur.fetchone()
  cur.execute("""
    UPDATE backtest_experiment
    SET status='queued', updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (revision["experiment_id"],))
