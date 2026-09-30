"""EvaluationSpec 与 EvaluationRun 的 PostgreSQL 持久化边界。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.experiments.canonicalization import digest, with_version
from backend.experiments.errors import ExperimentError
from backend.experiments.repository import (
  CommandResult,
  _load_idempotency,
  _lock_command,
  _replay_result,
  _save_idempotency,
)
from backend.services import state_store


SAVE_SPEC_OPERATION = "experiment.evaluation_spec.save.v1"
CREATE_RUN_OPERATION = "experiment.evaluation_run.create.v1"
RETRY_RUN_OPERATION = "experiment.evaluation_run.retry.v1"


@dataclass(frozen=True)
class ClaimedEvaluation:
  run: dict[str, Any]
  attempt_token: str


class PostgresEvaluationRepository:
  def experiment_context(self, experiment_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT e.id AS experiment_id, e.current_revision_id,
            r.experiment_spec, r.status AS revision_status
          FROM backtest_experiment e
          JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
          WHERE e.id=%s AND e.owner_user_id=%s
        """, (experiment_id, owner_id))
        row = cur.fetchone()
        if row is None:
          raise ExperimentError("experiment.not_found", "实验不存在", 404)
        cur.execute("""
          SELECT id, stage_index, stage_type, status, planned_trial_count
          FROM backtest_experiment_stage
          WHERE revision_id=%s AND owner_user_id=%s
          ORDER BY stage_index, id
        """, (row["current_revision_id"], owner_id))
        stages = [dict(item) for item in cur.fetchall()]
    context = dict(row)
    context["stages"] = stages
    return context

  def save_spec(
    self,
    *,
    experiment_id: str,
    evaluation_spec: dict[str, Any],
    spec_hash: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, SAVE_SPEC_OPERATION, idempotency_key)
        replay = _load_idempotency(cur, owner_id, SAVE_SPEC_OPERATION, idempotency_key)
        if replay:
          return _replay_result(replay, request_hash)
        revision = _lock_evaluation_revision(cur, experiment_id, owner_id)
        _validate_spec_scope(cur, revision, evaluation_spec)
        existing = _find_spec_by_hash(cur, revision["id"], spec_hash)
        response, status_code = (
          (_spec_item(existing, replayed=True), 200)
          if existing else _insert_spec(
            cur, revision, evaluation_spec, spec_hash, request_id,
          )
        )
        _save_idempotency(
          cur, owner_id, SAVE_SPEC_OPERATION, idempotency_key, request_hash,
          "evaluation_spec", response["evaluationSpecId"], status_code, response,
        )
        return CommandResult(response, status_code)

  def list_specs(self, experiment_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _require_experiment(cur, experiment_id, owner_id)
        cur.execute("""
          SELECT * FROM backtest_experiment_evaluation_spec
          WHERE experiment_id=%s AND owner_user_id=%s
          ORDER BY version_no DESC, id DESC
          LIMIT 100
        """, (experiment_id, owner_id))
        items = [_spec_item(dict(row)) for row in cur.fetchall()]
    return {"items": items, "nextCursor": None}

  def get_spec(self, experiment_id: str, evaluation_spec_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT spec.*, revision.experiment_spec AS revision_experiment_spec
          FROM backtest_experiment_evaluation_spec spec
          JOIN backtest_experiment_revision revision ON revision.id=spec.revision_id
          WHERE spec.id=%s AND spec.experiment_id=%s AND spec.owner_user_id=%s
        """, (evaluation_spec_id, experiment_id, owner_id))
        row = cur.fetchone()
    if row is None:
      raise ExperimentError("experiment.evaluation_spec_not_found", "评价版本不存在", 404)
    return dict(row)

  def create_run(
    self,
    *,
    experiment_id: str,
    evaluation_spec_id: str,
    catalog_snapshot: dict[str, Any],
    pipeline_version: str,
    adapter_type: str,
    adapter_version: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, CREATE_RUN_OPERATION, idempotency_key)
        replay = _load_idempotency(cur, owner_id, CREATE_RUN_OPERATION, idempotency_key)
        if replay:
          return _replay_result(replay, request_hash)
        spec = _load_spec_for_run(cur, experiment_id, evaluation_spec_id, owner_id)
        execution_set = _freeze_execution_set(cur, spec)
        result_set_hash = digest(with_version({"executionSet": execution_set}))
        response = _insert_evaluation_run(
          cur, spec, execution_set, result_set_hash, catalog_snapshot,
          pipeline_version, adapter_type, adapter_version, request_id,
        )
        _save_idempotency(
          cur, owner_id, CREATE_RUN_OPERATION, idempotency_key, request_hash,
          "evaluation_run", response["evaluationRunId"], 202, response,
        )
        return CommandResult(response, 202)

  def list_runs(self, experiment_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _require_experiment(cur, experiment_id, owner_id)
        cur.execute("""
          SELECT run.*, spec.version_no AS spec_version_no, spec.name AS spec_name
          FROM backtest_experiment_evaluation_run run
          JOIN backtest_experiment_evaluation_spec spec
            ON spec.id=run.evaluation_spec_id
          WHERE run.experiment_id=%s AND run.owner_user_id=%s
          ORDER BY run.created_at DESC, run.id DESC
          LIMIT 100
        """, (experiment_id, owner_id))
        items = [_run_item(dict(row), include_output=False) for row in cur.fetchall()]
    return {"items": items, "nextCursor": None}

  def get_run(self, experiment_id: str, run_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        row = _load_run(cur, experiment_id, run_id, owner_id)
    if row is None:
      raise ExperimentError("experiment.evaluation_run_not_found", "评价运行不存在", 404)
    return _run_item(row, include_output=True)

  def retry_run(
    self,
    *,
    experiment_id: str,
    run_id: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, RETRY_RUN_OPERATION, idempotency_key)
        replay = _load_idempotency(cur, owner_id, RETRY_RUN_OPERATION, idempotency_key)
        if replay:
          return _replay_result(replay, request_hash)
        row = _load_run(cur, experiment_id, run_id, owner_id, for_update=True)
        if row is None:
          raise ExperimentError("experiment.evaluation_run_not_found", "评价运行不存在", 404)
        if row["status"] != "failed":
          raise ExperimentError(
            "experiment.evaluation_retry_conflict", "只有失败的评价运行可以重试", 409,
          )
        cur.execute("""
          UPDATE backtest_experiment_evaluation_run
          SET status='queued', error_code=NULL, error_message=NULL,
            lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL,
            queued_at=now(), started_at=NULL, finished_at=NULL, updated_at=now()
          WHERE id=%s
        """, (run_id,))
        _insert_audit(
          cur, owner_id, experiment_id, row["revision_id"],
          row["evaluation_spec_id"], run_id, "evaluation.run_retried", request_id,
        )
        response = {"evaluationRunId": run_id, "status": "queued", "replayed": False}
        _save_idempotency(
          cur, owner_id, RETRY_RUN_OPERATION, idempotency_key, request_hash,
          "evaluation_run", run_id, 202, response,
        )
        return CommandResult(response, 202)

  def claim_next_run(
    self,
    worker_id: str,
    lease_seconds: int,
  ) -> ClaimedEvaluation | None:
    attempt_token = str(uuid4())
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT run.*, spec.evaluation_spec
          FROM backtest_experiment_evaluation_run run
          JOIN backtest_experiment_evaluation_spec spec
            ON spec.id=run.evaluation_spec_id
          WHERE run.status='queued'
             OR (run.status='running' AND run.lease_expires_at < now())
          ORDER BY run.queued_at, run.id
          FOR UPDATE OF run SKIP LOCKED
          LIMIT 1
        """)
        row = cur.fetchone()
        if row is None:
          return None
        cur.execute("""
          UPDATE backtest_experiment_evaluation_run
          SET status='running', lease_owner=%s, attempt_token=%s,
            lease_expires_at=now() + (%s * interval '1 second'),
            attempt_count=attempt_count+1, started_at=COALESCE(started_at,now()),
            updated_at=now()
          WHERE id=%s
        """, (worker_id, attempt_token, lease_seconds, row["id"]))
        claimed = dict(row)
        claimed["status"] = "running"
        claimed["attempt_count"] = int(row["attempt_count"]) + 1
    return ClaimedEvaluation(claimed, attempt_token)

  def load_run_sources(self, run: dict[str, Any]) -> list[dict[str, Any]]:
    execution_set = list(run["execution_set"])
    frozen_set_hash = digest(with_version({"executionSet": execution_set}))
    if frozen_set_hash != run["result_set_hash"]:
      raise ExperimentError(
        "experiment.evaluation_result_set_hash_mismatch",
        "评价冻结的执行集合摘要不一致",
        409,
      )
    execution_ids = [str(item["trialExecutionId"]) for item in execution_set]
    expected = {str(item["trialExecutionId"]): item for item in execution_set}
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT execution.id AS execution_id, execution.run_id,
            execution.result_schema_version, execution.result_hash,
            execution.actual_data_hash, trial.id AS trial_id, trial.trial_index,
            trial.dimension_projection, stage.id AS stage_id, stage.stage_index,
            stage.stage_type, run.status AS run_status, run.result_json,
            run.requested_params, run.fill_model, run.queued_at,
            run.started_at, run.finished_at, revision.experiment_spec
          FROM backtest_experiment_trial_execution execution
          JOIN backtest_experiment_trial trial ON trial.id=execution.trial_id
          JOIN backtest_experiment_stage stage ON stage.id=trial.stage_id
          JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
          JOIN backtest_run run ON run.id=execution.run_id
          WHERE execution.id=ANY(%s::uuid[])
        """, (execution_ids,))
        rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != len(execution_ids):
      raise ExperimentError(
        "experiment.evaluation_result_missing", "评价冻结的执行集合不完整", 409,
      )
    return [_source_row(row, expected[str(row["execution_id"])]) for row in rows]

  def complete_run(
    self,
    run_id: str,
    attempt_token: str,
    output: dict[str, Any],
    output_digest: str,
  ) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_run
          SET status='succeeded', output_snapshot=%s, output_hash=%s,
            error_code=NULL, error_message=NULL, lease_owner=NULL,
            attempt_token=NULL, lease_expires_at=NULL, finished_at=now(), updated_at=now()
          WHERE id=%s AND status='running' AND attempt_token=%s
          RETURNING owner_user_id, experiment_id, revision_id, evaluation_spec_id
        """, (Jsonb(output), output_digest, run_id, attempt_token))
        row = cur.fetchone()
        if row is None:
          return False
        _insert_audit(
          cur, str(row["owner_user_id"]), str(row["experiment_id"]),
          str(row["revision_id"]), str(row["evaluation_spec_id"]), run_id,
          "evaluation.run_succeeded", None,
        )
        return True

  def fail_run(
    self,
    run_id: str,
    attempt_token: str,
    code: str,
    message: str,
  ) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_run
          SET status='failed', error_code=%s, error_message=%s,
            lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL,
            finished_at=now(), updated_at=now()
          WHERE id=%s AND status='running' AND attempt_token=%s
          RETURNING owner_user_id, experiment_id, revision_id, evaluation_spec_id
        """, (code, message[:500], run_id, attempt_token))
        row = cur.fetchone()
        if row is None:
          return False
        _insert_audit(
          cur, str(row["owner_user_id"]), str(row["experiment_id"]),
          str(row["revision_id"]), str(row["evaluation_spec_id"]), run_id,
          "evaluation.run_failed", None, {"code": code},
        )
        return True


def _lock_evaluation_revision(cur: Any, experiment_id: str, owner_id: str) -> dict[str, Any]:
  cur.execute("""
    SELECT revision.* FROM backtest_experiment experiment
    JOIN backtest_experiment_revision revision
      ON revision.id=experiment.current_revision_id
    WHERE experiment.id=%s AND experiment.owner_user_id=%s
    FOR UPDATE OF revision
  """, (experiment_id, owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  return dict(row)


def _validate_spec_scope(
  cur: Any,
  revision: dict[str, Any],
  evaluation_spec: dict[str, Any],
) -> None:
  source = evaluation_spec["source"]
  if str(source["experimentRevisionId"]) != str(revision["id"]):
    raise ExperimentError(
      "experiment.evaluation_source_invalid", "评价来源不是当前实验修订", 409,
    )
  stage_ids = [str(item) for item in source["stageIds"]]
  cur.execute("""
    SELECT id FROM backtest_experiment_stage
    WHERE revision_id=%s AND id=ANY(%s::uuid[])
  """, (revision["id"], stage_ids))
  if len(cur.fetchall()) != len(stage_ids):
    raise ExperimentError(
      "experiment.evaluation_source_invalid", "评价来源包含未知 Stage", 422,
    )


def _find_spec_by_hash(cur: Any, revision_id: Any, spec_hash: str) -> dict[str, Any] | None:
  cur.execute("""
    SELECT * FROM backtest_experiment_evaluation_spec
    WHERE revision_id=%s AND spec_hash=%s
  """, (revision_id, spec_hash))
  row = cur.fetchone()
  return dict(row) if row else None


def _insert_spec(
  cur: Any,
  revision: dict[str, Any],
  evaluation_spec: dict[str, Any],
  spec_hash: str,
  request_id: str | None,
) -> tuple[dict[str, Any], int]:
  cur.execute("""
    SELECT COALESCE(max(version_no),0)+1 AS version_no
    FROM backtest_experiment_evaluation_spec WHERE revision_id=%s
  """, (revision["id"],))
  version_no = int(cur.fetchone()["version_no"])
  spec_id = str(uuid4())
  catalogs = evaluation_spec["catalogs"]
  cur.execute("""
    INSERT INTO backtest_experiment_evaluation_spec (
      id, experiment_id, revision_id, owner_user_id, version_no,
      schema_version, name, evaluation_spec, spec_hash,
      metric_catalog_version, dimension_catalog_version, normalizer_version
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    RETURNING *
  """, (
    spec_id, revision["experiment_id"], revision["id"], revision["owner_user_id"],
    version_no, evaluation_spec["schemaVersion"], evaluation_spec["name"],
    Jsonb(evaluation_spec), spec_hash, catalogs["metricCatalogVersion"],
    catalogs["dimensionCatalogVersion"], catalogs["normalizerVersion"],
  ))
  row = dict(cur.fetchone())
  _insert_audit(
    cur, str(revision["owner_user_id"]), str(revision["experiment_id"]),
    str(revision["id"]), spec_id, None, "evaluation.spec_saved", request_id,
    {"versionNo": version_no, "specHash": spec_hash},
  )
  return _spec_item(row), 201


def _load_spec_for_run(
  cur: Any,
  experiment_id: str,
  evaluation_spec_id: str,
  owner_id: str,
) -> dict[str, Any]:
  cur.execute("""
    SELECT * FROM backtest_experiment_evaluation_spec
    WHERE id=%s AND experiment_id=%s AND owner_user_id=%s
    FOR SHARE
  """, (evaluation_spec_id, experiment_id, owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.evaluation_spec_not_found", "评价版本不存在", 404)
  return dict(row)


def _freeze_execution_set(cur: Any, spec: dict[str, Any]) -> list[dict[str, Any]]:
  evaluation_spec = dict(spec["evaluation_spec"])
  stage_ids = [str(item) for item in evaluation_spec["source"]["stageIds"]]
  cur.execute("""
    SELECT stage.id AS stage_id, stage.stage_index, stage.stage_type,
      stage.status AS stage_status, stage.planned_trial_count,
      trial.id AS trial_id, trial.trial_index, trial.selected_execution_id,
      execution.run_id, execution.result_hash, execution.result_schema_version,
      execution.actual_data_hash, run.status AS run_status
    FROM backtest_experiment_stage stage
    JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
    LEFT JOIN backtest_experiment_trial_execution execution
      ON execution.id=trial.selected_execution_id
    LEFT JOIN backtest_run run ON run.id=execution.run_id
    WHERE stage.revision_id=%s AND stage.id=ANY(%s::uuid[])
    ORDER BY stage.stage_index, trial.trial_index, trial.id
    FOR SHARE OF stage, trial
  """, (spec["revision_id"], stage_ids))
  rows = [dict(row) for row in cur.fetchall()]
  trial_counts: dict[str, int] = {}
  for row in rows:
    stage_id = str(row["stage_id"])
    trial_counts[stage_id] = trial_counts.get(stage_id, 0) + 1
  if not rows or any(
    row["stage_status"] != "completed"
    or trial_counts[str(row["stage_id"])] != int(row["planned_trial_count"])
    or row["selected_execution_id"] is None
    or row["run_status"] != "succeeded"
    or not row["result_hash"]
    for row in rows
  ):
    raise ExperimentError(
      "experiment.evaluation_result_set_incomplete",
      "基础评价要求所选 Stage 的全部 Trial 都具有成功的选定执行",
      409,
    )
  return [{
    "stageId": str(row["stage_id"]),
    "stageIndex": int(row["stage_index"]),
    "stageType": str(row["stage_type"]),
    "trialId": str(row["trial_id"]),
    "trialIndex": int(row["trial_index"]),
    "trialExecutionId": str(row["selected_execution_id"]),
    "runId": str(row["run_id"]),
    "resultHash": str(row["result_hash"]),
    "resultSchemaVersion": str(row["result_schema_version"]),
    "actualDataHash": row["actual_data_hash"],
  } for row in rows]


def _insert_evaluation_run(
  cur: Any,
  spec: dict[str, Any],
  execution_set: list[dict[str, Any]],
  result_set_hash: str,
  catalogs: dict[str, Any],
  pipeline_version: str,
  adapter_type: str,
  adapter_version: str,
  request_id: str | None,
) -> dict[str, Any]:
  run_id = str(uuid4())
  cur.execute("""
    INSERT INTO backtest_experiment_evaluation_run (
      id, evaluation_spec_id, experiment_id, revision_id, owner_user_id,
      pipeline_version, adapter_type, adapter_version,
      metric_catalog_version, dimension_catalog_version, normalizer_version,
      catalog_snapshot, execution_set, execution_count, result_set_hash
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    RETURNING *
  """, (
    run_id, spec["id"], spec["experiment_id"], spec["revision_id"],
    spec["owner_user_id"], pipeline_version, adapter_type, adapter_version,
    spec["metric_catalog_version"], spec["dimension_catalog_version"],
    spec["normalizer_version"], Jsonb(catalogs), Jsonb(execution_set),
    len(execution_set), result_set_hash,
  ))
  row = dict(cur.fetchone())
  _insert_audit(
    cur, str(spec["owner_user_id"]), str(spec["experiment_id"]),
    str(spec["revision_id"]), str(spec["id"]), run_id,
    "evaluation.run_created", request_id,
    {"resultSetHash": result_set_hash, "executionCount": len(execution_set)},
  )
  return _run_item(row, include_output=False)


def _require_experiment(cur: Any, experiment_id: str, owner_id: str) -> None:
  cur.execute(
    "SELECT 1 FROM backtest_experiment WHERE id=%s AND owner_user_id=%s",
    (experiment_id, owner_id),
  )
  if cur.fetchone() is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)


def _load_run(
  cur: Any,
  experiment_id: str,
  run_id: str,
  owner_id: str,
  *,
  for_update: bool = False,
) -> dict[str, Any] | None:
  lock = " FOR UPDATE OF run" if for_update else ""
  cur.execute(f"""
    SELECT run.*, spec.version_no AS spec_version_no, spec.name AS spec_name,
      spec.evaluation_spec
    FROM backtest_experiment_evaluation_run run
    JOIN backtest_experiment_evaluation_spec spec ON spec.id=run.evaluation_spec_id
    WHERE run.id=%s AND run.experiment_id=%s AND run.owner_user_id=%s{lock}
  """, (run_id, experiment_id, owner_id))
  row = cur.fetchone()
  return dict(row) if row else None


def _spec_item(row: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
  return {
    "evaluationSpecId": str(row["id"]),
    "experimentId": str(row["experiment_id"]),
    "revisionId": str(row["revision_id"]),
    "versionNo": int(row["version_no"]),
    "name": row["name"],
    "specHash": row["spec_hash"],
    "spec": row["evaluation_spec"],
    "metricCatalogVersion": row["metric_catalog_version"],
    "dimensionCatalogVersion": row["dimension_catalog_version"],
    "normalizerVersion": row["normalizer_version"],
    "createdAt": row["created_at"].isoformat(),
    "replayed": replayed,
  }


def _run_item(row: dict[str, Any], *, include_output: bool) -> dict[str, Any]:
  item = {
    "evaluationRunId": str(row["id"]),
    "evaluationSpecId": str(row["evaluation_spec_id"]),
    "experimentId": str(row["experiment_id"]),
    "revisionId": str(row["revision_id"]),
    "specVersionNo": row.get("spec_version_no"),
    "specName": row.get("spec_name"),
    "status": row["status"],
    "pipelineVersion": row["pipeline_version"],
    "adapterType": row["adapter_type"],
    "adapterVersion": row["adapter_version"],
    "metricCatalogVersion": row["metric_catalog_version"],
    "dimensionCatalogVersion": row["dimension_catalog_version"],
    "executionCount": int(row["execution_count"]),
    "resultSetHash": row["result_set_hash"],
    "outputHash": row.get("output_hash"),
    "error": _run_error(row),
    "attemptCount": int(row["attempt_count"]),
    "createdAt": row["created_at"].isoformat(),
    "finishedAt": row["finished_at"].isoformat() if row.get("finished_at") else None,
  }
  if include_output:
    item["executionSet"] = row["execution_set"]
    item["output"] = row.get("output_snapshot")
    item["spec"] = row.get("evaluation_spec")
  return item


def _run_error(row: dict[str, Any]) -> dict[str, str] | None:
  if not row.get("error_code"):
    return None
  return {"code": row["error_code"], "message": row.get("error_message") or "评价失败"}


def _source_row(row: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
  frozen_identity = {
    "trialExecutionId": str(row["execution_id"]),
    "runId": str(row["run_id"]),
    "trialId": str(row["trial_id"]),
    "stageId": str(row["stage_id"]),
  }
  if any(frozen_identity[key] != str(expected[key]) for key in frozen_identity):
    raise ExperimentError(
      "experiment.evaluation_result_source_mismatch",
      "评价冻结的执行映射不一致",
      409,
    )
  actual_data_hash = row.get("actual_data_hash")
  expected_data_hash = expected.get("actualDataHash")
  if (
    (str(actual_data_hash) if actual_data_hash is not None else None)
    != (str(expected_data_hash) if expected_data_hash is not None else None)
  ):
    raise ExperimentError(
      "experiment.evaluation_data_hash_mismatch",
      "评价冻结的实际行情摘要不一致",
      409,
    )
  spec = dict(row.get("experiment_spec") or {})
  template = dict(spec.get("runTemplateSnapshot") or {})
  fixed = dict(template.get("fixedStructure") or {})
  inputs = list(dict(template.get("tunableInputCatalog") or {}).get("inputs") or [])
  parameter_dimensions = {
    str(item["inputId"]): str(item["sourceName"])
    for item in inputs
    if isinstance(item, dict) and item.get("inputId") and item.get("sourceName")
  }
  return {
    **row,
    "execution_id": str(row["execution_id"]),
    "run_id": str(row["run_id"]),
    "trial_id": str(row["trial_id"]),
    "stage_id": str(row["stage_id"]),
    "expected_result_hash": str(expected["resultHash"]),
    "run_path": template.get("runPath"),
    "scope_type": template.get("scopeType"),
    "fill_model": row.get("fill_model") or fixed.get("fillModel"),
    "parameter_dimensions": parameter_dimensions,
  }


def _insert_audit(
  cur: Any,
  owner_id: str,
  experiment_id: str,
  revision_id: str,
  evaluation_spec_id: str | None,
  evaluation_run_id: str | None,
  event_type: str,
  request_id: str | None,
  detail: dict[str, Any] | None = None,
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, evaluation_spec_id,
      evaluation_run_id, event_type, request_id, detail
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
  """, (
    owner_id, experiment_id, revision_id, evaluation_spec_id,
    evaluation_run_id, event_type, request_id, Jsonb(detail or {}),
  ))


@contextmanager
def _worker_session() -> Iterator[psycopg.Connection[Any]]:
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
