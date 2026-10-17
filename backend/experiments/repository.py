"""实验预览、创建与确定性生成的 PostgreSQL Adapter。"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from typing import Any, Iterator, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.experiments.canonicalization import digest, with_version
from backend.experiments.compiler import TrialCompiler
from backend.experiments.errors import ExperimentError
from backend.experiments.generators import (
  extend_collection_digest,
  initial_collection_digest,
)
from backend.experiments.resolvers import ResolvedRunTemplate
from backend.experiments.recovery import (
  AggregateFacts,
  allows_automatic_retry,
  derive_aggregate_status,
  normalize_failure,
  should_open_circuit,
  validate_status_transition,
)
from backend.services import state_store
from backend.services.search_query import escape_like_literal
from backend.strategies.runtime_contract import validate_runtime_capability


CREATE_OPERATION = "experiment.create.v1"
CREATE_REVISION_OPERATION = "experiment.revision.create.v1"
CANCEL_OPERATION = "experiment.cancel.v1"
RESUME_OPERATION = "experiment.resume.v1"
RETRY_FAILED_OPERATION = "experiment.retry_failed.v1"
ARCHIVE_OPERATION = "experiment.archive.v1"
RESTORE_ARCHIVE_OPERATION = "experiment.archive.restore.v1"
PURGE_OPERATION = "experiment.purge.create.v1"
_TRANSIENT_BLOCK_CODES = {
  "experiment.capacity_blocked",
  "experiment.manual_queue_reserved",
  "experiment.manual_wait_blocked",
}


def _experiment_search_candidates(
  query: str,
  owner_id: str,
) -> tuple[str, str, list[Any]]:
  normalized = query.strip().lower()
  if not normalized:
    return "", "", []
  pattern = f"%{escape_like_literal(normalized)}%"
  cte = """
    WITH query_candidates AS (
      SELECT candidate.id
      FROM backtest_experiment candidate
      WHERE candidate.owner_user_id=%s
        AND lower(candidate.name) LIKE %s ESCAPE '\\'
      UNION
      SELECT candidate.id
      FROM backtest_experiment candidate
      JOIN backtest_experiment_revision candidate_revision
        ON candidate_revision.id=candidate.current_revision_id
      WHERE candidate.owner_user_id=%s
        AND lower(COALESCE(
          candidate_revision.experiment_spec
            #>> '{runTemplateSnapshot,fixedStructure,strategyName}', ''
        )) LIKE %s ESCAPE '\\'
      UNION
      SELECT candidate.id
      FROM backtest_experiment candidate
      JOIN backtest_experiment_revision candidate_revision
        ON candidate_revision.id=candidate.current_revision_id
      WHERE candidate.owner_user_id=%s
        AND lower(COALESCE(
          candidate_revision.experiment_spec
            #>> '{runTemplateSnapshot,fixedStructure,symbol}', ''
        )) LIKE %s ESCAPE '\\'
    )
  """
  return cte, "JOIN query_candidates matched ON matched.id=e.id", [
    owner_id, pattern, owner_id, pattern, owner_id, pattern,
  ]


@dataclass(frozen=True)
class CommandResult:
  response: dict[str, Any]
  status_code: int


@dataclass(frozen=True)
class MaterializationGate:
  allowed: bool
  code: str | None = None
  message: str | None = None


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
    source: dict[str, str] | None,
    request_id: str | None,
  ) -> CommandResult:
    """原子创建首个实验修订。"""

  def create_revision(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    expected_snapshot_version: int,
    idempotency_key: str,
    request_hash: str,
    preview_token: str,
    expected_bindings: dict[str, str],
    normalized_spec: dict[str, Any],
    request_id: str | None,
  ) -> CommandResult:
    """当前修订终结后原子创建新的不可变修订。"""

  def preview_retry_selection(
    self,
    experiment_id: str,
    expected_revision_id: str,
    selection: dict[str, Any],
  ) -> dict[str, Any]:
    """计算当前失败 Trial 选择集的冻结摘要。"""

  def retry_failed_trials(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    selection: dict[str, Any],
    expected_count: int,
    expected_digest: str,
    reason: str,
    compiler: TrialCompiler,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    """为冻结失败选择集创建新的人工执行。"""

  def materialize_next_trial(self, compiler: TrialCompiler) -> bool:
    """在一个事务中物化一个待执行 Trial。"""

  def reconcile_next_execution(self) -> bool:
    """从普通回测真源同步一个 TrialExecution。"""

  def refresh_next_capacity_block(self) -> bool:
    """重新检查一个可恢复的容量阻塞。"""

  def repair_next_aggregate(self) -> bool:
    """从运行事实修复一个聚合或额度缓存漂移。"""

  def resume_experiment(
    self,
    experiment_id: str,
    expected_revision_id: str,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    """校验后解除当前 Revision 的可恢复执行阻塞。"""

  def archive_experiment(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    expected_snapshot_version: int,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    """归档终态实验但保留完整研究证据。"""

  def restore_archived_experiment(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    expected_snapshot_version: int,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    """在保留期内撤销归档。"""

  def request_purge(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    expected_snapshot_version: int,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    """创建受审计且可恢复的物理清理作业。"""

  def list_experiments(
    self,
    *,
    limit: int,
    cursor: str | None,
    archived: bool,
    query: str,
    status: str | None,
  ) -> dict[str, Any]:
    """游标分页读取当前用户实验。"""

  def list_admin_archived_experiments(
    self,
    *,
    limit: int,
    cursor: str | None,
  ) -> dict[str, Any]:
    """分页读取管理员可执行物理清理的归档实验。"""

  def list_revisions(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
  ) -> dict[str, Any]:
    """游标分页读取一个实验的历史修订。"""

  def list_trials(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
    status: str | None,
    revision_id: str | None,
    query: str = "",
    date_scenario_id: str | None = None,
  ) -> dict[str, Any]:
    """游标分页读取当前实验 Trial。"""

  def list_related_runs(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
    query: str,
  ) -> dict[str, Any]:
    """分页读取整个实验产生的普通回测任务。"""


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
    source: dict[str, str] | None,
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
        source_row = _load_experiment_source(cur, owner_id, source)
        _reserve_plan_for_current_epoch(
          cur, owner_id, int(preview["planned_trial_count"]),
        )
        response = _insert_experiment_graph(
          cur,
          owner_id=owner_id,
          normalized_spec=normalized_spec,
          preview=preview,
          source=source_row,
          request_id=request_id,
        )
        _save_idempotency(
          cur, owner_id, CREATE_OPERATION, idempotency_key, request_hash,
          "experiment", response["experimentId"], 201, response,
        )
        return CommandResult(response, 201)

  def create_revision(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    expected_snapshot_version: int,
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
        _lock_command(cur, owner_id, CREATE_REVISION_OPERATION, idempotency_key)
        replay = _load_idempotency(
          cur, owner_id, CREATE_REVISION_OPERATION, idempotency_key,
        )
        if replay:
          return _replay_result(replay, request_hash)
        preview = _load_preview_for_update(cur, owner_id, preview_token)
        if preview is None:
          raise ExperimentError("experiment.not_found", "预览不存在", 404)
        validate_preview_bindings(preview, expected_bindings, datetime.now(timezone.utc))
        if digest(preview["normalized_spec"]) != digest(normalized_spec):
          raise _preview_stale(["normalizedSpec"])
        graph = _lock_revision_creation_graph(cur, experiment_id, owner_id)
        _validate_revision_creation(
          graph, expected_revision_id, expected_snapshot_version,
        )
        _reserve_plan_for_current_epoch(
          cur, owner_id, int(preview["planned_trial_count"]),
        )
        response = _insert_new_revision(
          cur, graph, normalized_spec, preview, request_id,
        )
        _save_idempotency(
          cur, owner_id, CREATE_REVISION_OPERATION, idempotency_key, request_hash,
          "experiment_revision", response["revisionId"], 201, response,
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
            e.archived_at, e.archive_retention_until, e.purge_job_id,
            e.row_version, e.created_at, e.updated_at,
            related_runs.total_count AS related_run_count,
            related_runs.initial_count AS related_initial_run_count,
            related_runs.retry_count AS related_retry_run_count,
            source_e.id AS source_experiment_id,
            source_e.name AS source_experiment_name,
            source_r.id AS source_revision_id,
            source_r.revision_no AS source_revision_no,
            r.id AS revision_id, r.revision_no, r.experiment_spec,
            r.spec_hash, r.planned_trial_count, r.resource_budget,
            r.failure_policy, r.lifecycle_state, r.lifecycle_version,
            r.terminal_outcome, r.system_block_code,
            r.system_block_message, rollout.contract_epoch,
            storage.decision AS storage_decision,
            s.id AS stage_id, s.stage_type, s.generator_version,
            s.status AS stage_status, s.generated_trial_count,
            s.generation_cursor, s.trial_collection_digest,
            s.in_flight_count, s.terminal_count, s.succeeded_count,
            s.failed_count, s.cancelled_count,
            s.execution_phase,
            trial_counts.ready_count, trial_counts.queued_count,
            s.blocked_reason_code AS stage_reason_code,
            s.blocked_reason_message AS stage_reason_message,
            s.blocked_scope, s.blocked_at, s.consecutive_failure_count
          FROM backtest_experiment e
          JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
          JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
          CROSS JOIN backtest_experiment_rollout_state rollout
          CROSS JOIN backtest_experiment_storage_pressure storage
          LEFT JOIN backtest_experiment_revision source_r
            ON source_r.id=e.source_revision_id
          LEFT JOIN backtest_experiment source_e
            ON source_e.id=source_r.experiment_id
          LEFT JOIN LATERAL (
            SELECT count(*) AS total_count,
              count(*) FILTER (WHERE execution.created_reason='initial') AS initial_count,
              count(*) FILTER (WHERE execution.created_reason<>'initial') AS retry_count
            FROM backtest_experiment_revision related_revision
            JOIN backtest_experiment_stage related_stage
              ON related_stage.revision_id=related_revision.id
            JOIN backtest_experiment_trial related_trial
              ON related_trial.stage_id=related_stage.id
            JOIN backtest_experiment_trial_execution execution
              ON execution.trial_id=related_trial.id
            WHERE related_revision.experiment_id=e.id
          ) related_runs ON true
          LEFT JOIN LATERAL (
            SELECT
              count(*) FILTER (
                WHERE trial.materialization_status='pending'
                  AND trial.summary_status='pending'
              ) AS ready_count,
              count(*) FILTER (WHERE trial.summary_status='queued') AS queued_count
            FROM backtest_experiment_trial trial
            WHERE trial.stage_id=s.id
          ) trial_counts ON true
          WHERE e.id=%s AND e.owner_user_id=%s
        """, (experiment_id, owner_id))
        row = cur.fetchone()
        if row is not None:
          value = dict(row)
          value["error_aggregates"] = _load_error_aggregates(
            cur, str(value["stage_id"]),
          )
          row = value
    if row is None:
      raise ExperimentError("experiment.not_found", "实验不存在", 404)
    return _detail_response(dict(row))

  def list_experiments(
    self,
    *,
    limit: int,
    cursor: str | None,
    archived: bool,
    query: str,
    status: str | None,
  ) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    filters = {"archived": archived, "query": query, "status": status}
    after = (
      _decode_cursor(cursor, "experiment-list", owner_id, filters)
      if cursor else None
    )
    search_cte, search_join, search_params = _experiment_search_candidates(
      query, owner_id,
    )
    params: list[Any] = [*search_params, owner_id]
    filter_sql: list[str] = []
    if status:
      filter_sql.append("AND e.status=%s")
      params.append(status)
    after_sql = ""
    if after:
      after_sql = "AND (e.updated_at, e.id) < (%s::timestamptz, %s::uuid)"
      params.extend([after["updatedAt"], after["experimentId"]])
    params.append(limit + 1)
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        state_store._set_search_statement_timeout(cur)
        cur.execute(f"""
          {search_cte}
          SELECT e.id AS experiment_id, e.name, e.status,
            e.blocked_reason_code, e.blocked_reason_message,
            e.archived_at, e.archive_retention_until, e.purge_job_id,
            e.created_at, e.updated_at,
            source_e.id AS source_experiment_id,
            source_e.name AS source_experiment_name,
            source_r.id AS source_revision_id,
            source_r.revision_no AS source_revision_no,
            r.id AS revision_id, r.revision_no, r.experiment_spec,
            r.planned_trial_count, r.lifecycle_state, r.lifecycle_version,
            r.terminal_outcome, r.system_block_code,
            rollout.contract_epoch, storage.decision AS storage_decision,
            s.id AS stage_id, s.status AS stage_status,
            s.generated_trial_count, s.in_flight_count, s.terminal_count,
            s.succeeded_count, s.failed_count, s.cancelled_count,
            s.execution_phase,
            (SELECT count(*) FROM backtest_experiment_trial trial
             WHERE trial.stage_id=s.id AND trial.summary_status='queued') AS queued_count,
            (SELECT count(*) FROM backtest_experiment_trial trial
             WHERE trial.stage_id=s.id
               AND trial.materialization_status='pending'
               AND trial.summary_status='pending') AS ready_count
          FROM backtest_experiment e
          {search_join}
          JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
          JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
          CROSS JOIN backtest_experiment_rollout_state rollout
          CROSS JOIN backtest_experiment_storage_pressure storage
          LEFT JOIN backtest_experiment_revision source_r
            ON source_r.id=e.source_revision_id
          LEFT JOIN backtest_experiment source_e
            ON source_e.id=source_r.experiment_id
          WHERE e.owner_user_id=%s
            AND e.archived_at IS {'NOT NULL' if archived else 'NULL'}
            {' '.join(filter_sql)}
            {after_sql}
          ORDER BY e.updated_at DESC, e.id DESC
          LIMIT %s
        """, params)
        rows = [dict(row) for row in cur.fetchall()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
      last = page[-1]
      next_cursor = _encode_cursor("experiment-list", owner_id, filters, {
        "updatedAt": last["updated_at"].isoformat(),
        "experimentId": str(last["experiment_id"]),
      })
    return {"items": [_experiment_list_item(row) for row in page], "nextCursor": next_cursor}

  def list_admin_archived_experiments(
    self,
    *,
    limit: int,
    cursor: str | None,
  ) -> dict[str, Any]:
    if not state_store.current_user_is_admin():
      raise ExperimentError("experiment.forbidden", "仅管理员可以查看待清理实验", 403)
    actor_id = state_store.current_user_id()
    bindings = {"scope": "admin-archived"}
    after = (
      _decode_cursor(cursor, "admin-archived-experiment-list", actor_id, bindings)
      if cursor else None
    )
    params: list[Any] = []
    after_sql = ""
    if after:
      after_sql = "AND (e.updated_at, e.id) < (%s::timestamptz, %s::uuid)"
      params.extend([after["updatedAt"], after["experimentId"]])
    params.append(limit + 1)
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute(f"""
          SELECT e.id AS experiment_id, e.name, e.status, e.row_version,
            e.archived_at, e.archive_retention_until, e.purge_job_id,
            e.updated_at,
            COALESCE(e.current_revision_id, purge.revision_id) AS revision_id
          FROM backtest_experiment e
          LEFT JOIN backtest_experiment_purge purge ON purge.id=e.purge_job_id
          WHERE e.archived_at IS NOT NULL {after_sql}
          ORDER BY e.updated_at DESC, e.id DESC
          LIMIT %s
        """, params)
        rows = [dict(row) for row in cur.fetchall()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
      last = page[-1]
      next_cursor = _encode_cursor(
        "admin-archived-experiment-list", actor_id, bindings, {
          "updatedAt": last["updated_at"].isoformat(),
          "experimentId": str(last["experiment_id"]),
        },
      )
    return {
      "items": [_admin_archived_experiment_list_item(row) for row in page],
      "nextCursor": next_cursor,
    }

  def list_revisions(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
  ) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    bindings = {"experimentId": experiment_id}
    after = _decode_cursor(cursor, "revision-list", owner_id, bindings) if cursor else None
    params: list[Any] = [experiment_id, owner_id]
    after_sql = ""
    if after:
      after_sql = "AND r.revision_no < %s"
      params.append(after["revisionNo"])
    params.append(limit + 1)
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _require_owned_experiment(cur, experiment_id, owner_id)
        cur.execute(f"""
          SELECT r.id AS revision_id, r.revision_no, r.status, r.spec_hash,
            r.lifecycle_state, r.lifecycle_version, r.terminal_outcome,
            r.system_block_code,
            r.planned_trial_count, r.terminal_at, r.created_at, r.updated_at,
            s.id AS stage_id, s.status AS stage_status,
            s.generated_trial_count, s.in_flight_count, s.terminal_count,
            s.succeeded_count, s.failed_count, s.cancelled_count,
            s.execution_phase,
            e.current_revision_id
          FROM backtest_experiment_revision r
          JOIN backtest_experiment e ON e.id=r.experiment_id
          JOIN backtest_experiment_stage s
            ON s.revision_id=r.id AND s.stage_index=0
          WHERE r.experiment_id=%s AND r.owner_user_id=%s {after_sql}
          ORDER BY r.revision_no DESC
          LIMIT %s
        """, params)
        rows = [dict(row) for row in cur.fetchall()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
      next_cursor = _encode_cursor("revision-list", owner_id, bindings, {
        "revisionNo": int(page[-1]["revision_no"]),
      })
    return {"items": [_revision_list_item(row) for row in page], "nextCursor": next_cursor}

  def list_trials(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
    status: str | None,
    revision_id: str | None = None,
    query: str = "",
    date_scenario_id: str | None = None,
  ) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    normalized_query = query.strip().removeprefix("#").strip()
    trial_number = int(normalized_query) if normalized_query.isdigit() else None
    valid_trial_number = trial_number is not None and 0 < trial_number <= 2**63
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        selected_revision_id = _resolve_revision_id(
          cur, experiment_id, owner_id, revision_id,
        )
        bindings = {
          "experimentId": experiment_id,
          "revisionId": selected_revision_id,
          "status": status or "",
          "query": normalized_query,
          "dateScenarioId": date_scenario_id or "",
        }
        after = _decode_cursor(cursor, "trial-list", owner_id, bindings) if cursor else None
        params: list[Any] = [experiment_id, owner_id, selected_revision_id]
        filters = ["e.id=%s", "e.owner_user_id=%s", "r.id=%s"]
        if status:
          filters.append("t.summary_status=%s")
          params.append(status)
        if normalized_query:
          filters.append("t.trial_index=%s" if valid_trial_number else "false")
          if valid_trial_number:
            params.append(trial_number - 1)
        if date_scenario_id:
          filters.append(
            "t.dimension_projection #>> '{dateScenario,scenarioId}'=%s",
          )
          params.append(date_scenario_id)
        if after:
          filters.append("t.trial_index > %s")
          params.append(after["trialIndex"])
        params.append(limit + 1)
        cur.execute(f"""
          SELECT t.id AS trial_id, t.trial_index, t.trial_spec,
            t.dimension_projection, t.materialization_status, t.summary_status,
            t.automatic_retry_count, t.retry_not_before,
            s.id AS stage_id,
            active.id AS active_execution_id, active.run_id AS active_run_id,
            selected.id AS selected_execution_id, selected.run_id AS selected_run_id,
            selected.result_hash, selected.result_schema_version,
            selected.actual_data_hash,
            active_run.status AS active_run_status,
            selected_run.status AS selected_run_status,
            latest.id AS latest_execution_id, latest.run_id AS latest_run_id,
            latest_run.status AS latest_run_status,
            COALESCE(latest.error_summary, selected.error_summary, active.error_summary)
              AS error_summary,
            attempts.execution_attempts
          FROM backtest_experiment e
          JOIN backtest_experiment_revision r ON r.experiment_id=e.id
          JOIN backtest_experiment_stage s ON s.revision_id=r.id
          JOIN backtest_experiment_trial t ON t.stage_id=s.id
          LEFT JOIN backtest_experiment_trial_execution active
            ON active.id=t.active_execution_id
          LEFT JOIN backtest_run active_run ON active_run.id=active.run_id
          LEFT JOIN backtest_experiment_trial_execution selected
            ON selected.id=t.selected_execution_id
          LEFT JOIN backtest_run selected_run ON selected_run.id=selected.run_id
          LEFT JOIN LATERAL (
            SELECT execution.*
            FROM backtest_experiment_trial_execution execution
            WHERE execution.trial_id=t.id
            ORDER BY execution.execution_no DESC
            LIMIT 1
          ) latest ON true
          LEFT JOIN backtest_run latest_run ON latest_run.id=latest.run_id
          LEFT JOIN LATERAL (
            SELECT jsonb_agg(jsonb_build_object(
              'executionId', execution.id,
              'executionNo', execution.execution_no,
              'runId', execution.run_id,
              'status', execution_run.status,
              'terminalStatus', execution.terminal_status,
              'createdReason', execution.created_reason,
              'selected', execution.id=t.selected_execution_id,
              'active', execution.id=t.active_execution_id,
              'resultHash', execution.result_hash,
              'errorSummary', execution.error_summary,
              'queuedAt', execution.queued_at,
              'startedAt', execution.started_at,
              'finishedAt', execution.finished_at
            ) ORDER BY execution.execution_no) AS execution_attempts
            FROM backtest_experiment_trial_execution execution
            JOIN backtest_run execution_run ON execution_run.id=execution.run_id
            WHERE execution.trial_id=t.id
          ) attempts ON true
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

  def list_related_runs(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
    query: str,
  ) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    normalized_query = query.strip().removeprefix("#").strip()
    display_no = int(normalized_query) if normalized_query.isdigit() else None
    bindings = {"experimentId": experiment_id, "query": normalized_query}
    after = (
      _decode_cursor(cursor, "experiment-related-run-list", owner_id, bindings)
      if cursor else None
    )
    filters = ["e.id=%s", "e.owner_user_id=%s"]
    params: list[Any] = [experiment_id, owner_id]
    if normalized_query:
      filters.append("run.display_no=%s" if display_no is not None else "false")
      if display_no is not None:
        params.append(display_no)
    if after:
      filters.append("""
        (revision.revision_no < %s OR (
          revision.revision_no=%s AND (
            stage.stage_index > %s OR (
              stage.stage_index=%s AND (
                trial.trial_index > %s OR (
                  trial.trial_index=%s AND execution.execution_no > %s
                )
              )
            )
          )
        ))
      """)
      params.extend([
        after["revisionNo"], after["revisionNo"], after["stageIndex"],
        after["stageIndex"], after["trialIndex"], after["trialIndex"],
        after["executionNo"],
      ])
    params.append(limit + 1)
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _require_owned_experiment(cur, experiment_id, owner_id)
        summary = _load_related_run_summary(cur, experiment_id, owner_id)
        cur.execute(f"""
          SELECT revision.id AS revision_id, revision.revision_no,
            revision.id=e.current_revision_id AS current_revision,
            stage.id AS stage_id, stage.stage_index,
            trial.id AS trial_id, trial.trial_index,
            execution.id AS execution_id, execution.execution_no,
            execution.created_reason,
            execution.id=trial.selected_execution_id AS selected,
            run.id AS run_id, run.display_no, run.status
          FROM backtest_experiment e
          JOIN backtest_experiment_revision revision ON revision.experiment_id=e.id
          JOIN backtest_experiment_stage stage ON stage.revision_id=revision.id
          JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
          JOIN backtest_experiment_trial_execution execution
            ON execution.trial_id=trial.id
          JOIN backtest_run run ON run.id=execution.run_id
          WHERE {' AND '.join(filters)}
          ORDER BY revision.revision_no DESC, stage.stage_index,
            trial.trial_index, execution.execution_no
          LIMIT %s
        """, params)
        rows = [dict(row) for row in cur.fetchall()]
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
      last = page[-1]
      next_cursor = _encode_cursor(
        "experiment-related-run-list", owner_id, bindings, {
          "revisionNo": int(last["revision_no"]),
          "stageIndex": int(last["stage_index"]),
          "trialIndex": int(last["trial_index"]),
          "executionNo": int(last["execution_no"]),
        },
      )
    return {
      **summary,
      "items": [_related_run_list_item(row) for row in page],
      "nextCursor": next_cursor,
    }

  def preview_retry_selection(
    self,
    experiment_id: str,
    expected_revision_id: str,
    selection: dict[str, Any],
  ) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        snapshot = _retry_selection_snapshot(
          cur, experiment_id, owner_id, expected_revision_id, selection,
        )
    return _public_retry_selection(snapshot)

  def retry_failed_trials(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    selection: dict[str, Any],
    expected_count: int,
    expected_digest: str,
    reason: str,
    compiler: TrialCompiler,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, RETRY_FAILED_OPERATION, idempotency_key)
        replay = _load_idempotency(
          cur, owner_id, RETRY_FAILED_OPERATION, idempotency_key,
        )
        if replay:
          return _replay_result(replay, request_hash)
        graph = _lock_retry_graph(
          cur, experiment_id, owner_id, expected_revision_id, selection,
        )
        _validate_retry_selection(graph, expected_count, expected_digest)
        _prepare_retry_revision(cur, graph)
        _validate_retry_capacity(cur, graph, expected_count)
        executions = _create_manual_retry_executions(cur, graph, compiler)
        response = _retry_response(graph, executions, expected_digest)
        _audit_manual_retry(cur, graph, reason, request_id, response)
        _save_idempotency(
          cur, owner_id, RETRY_FAILED_OPERATION, idempotency_key, request_hash,
          "experiment", experiment_id, 202, response,
        )
        return CommandResult(response, 202)

  def materialize_next_trial(self, compiler: TrialCompiler) -> bool:
    from backend.experiments.rollout import ContractEpoch, current_contract_epoch
    from backend.experiments.storage_pressure import StoragePressureGate

    epoch = current_contract_epoch()
    if epoch >= ContractEpoch.LIFECYCLE_V2:
      if not StoragePressureGate().evaluate().allows_new_work:
        return False
    with _coordinator_session() as conn:
      with conn.cursor() as cur:
        candidate = _find_materialization_candidate(cur)
        if candidate is None:
          return False
        locked = _lock_materialization_graph(cur, dict(candidate))
        if locked is None:
          return False
        gate = _materialization_gate(cur, locked)
        if not gate.allowed:
          if epoch < ContractEpoch.ROLLING_WINDOW_V1:
            return _set_materialization_block(cur, locked, gate)
          return False
        if not _can_materialize(locked):
          return False
        template, assignment, dataset, seed = _materialization_inputs(locked)
        payload = compiler.compile_request(template, assignment, dataset, seed)
        _verify_compiled_request(compiler, template, assignment, locked)
        _insert_trial_execution(cur, locked, payload)
        return True

  def refresh_next_capacity_block(self) -> bool:
    from backend.experiments.rollout import ContractEpoch, current_contract_epoch

    with _coordinator_session() as conn:
      with conn.cursor() as cur:
        candidate = _find_capacity_block_candidate(cur)
        if candidate is None:
          return False
        locked = _lock_capacity_block_graph(cur, dict(candidate))
        if locked is None:
          return False
        gate = (
          _materialization_gate(cur, locked)
          if current_contract_epoch() < ContractEpoch.ROLLING_WINDOW_V1
          else MaterializationGate(True)
        )
        return _set_materialization_block(cur, locked, gate, touch=True)

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

  def repair_next_aggregate(self) -> bool:
    with _coordinator_session() as conn:
      with conn.cursor() as cur:
        candidate = _find_aggregate_repair_candidate(cur)
        if candidate is None:
          return False
        cur.execute(
          "SELECT * FROM backtest_experiment_user_quota "
          "WHERE owner_user_id=%s FOR UPDATE",
          (candidate["owner_user_id"],),
        )
        if cur.fetchone() is None:
          return False
        _rebuild_aggregate_status(cur, str(candidate["stage_id"]))
        _repair_user_quota(cur, str(candidate["owner_user_id"]))
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
        graph = _load_current_stage_for_update(cur, experiment_id, owner_id)
        if graph is None:
          raise ExperimentError("experiment.not_found", "实验不存在", 404)
        response = _cancel_stage(cur, graph, request_id)
        _save_idempotency(
          cur, owner_id, CANCEL_OPERATION, idempotency_key, request_hash,
          "experiment", experiment_id, 200, response,
        )
        return CommandResult(response, 200)

  def request_purge(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    expected_snapshot_version: int,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    if not settings.EXPERIMENT_PURGE_ENABLED:
      raise ExperimentError(
        "experiment.purge_disabled", "实验物理清理能力未启用", 503,
      )
    if not state_store.current_user_is_admin():
      raise ExperimentError("experiment.purge_forbidden", "仅管理员可以清理实验", 403)
    actor_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, actor_id, PURGE_OPERATION, idempotency_key)
        replay = _load_idempotency(cur, actor_id, PURGE_OPERATION, idempotency_key)
        if replay:
          return _replay_result(replay, request_hash)
        graph = _lock_admin_purge_experiment(cur, experiment_id)
        _validate_purge_request(
          cur, graph, expected_revision_id, expected_snapshot_version,
        )
        response = _create_purge_job(cur, graph, actor_id, reason, request_id)
        _save_idempotency(
          cur, actor_id, PURGE_OPERATION, idempotency_key, request_hash,
          "experiment_purge", response["purgeId"], 202, response,
        )
        return CommandResult(response, 202)

  def archive_experiment(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    expected_snapshot_version: int,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, ARCHIVE_OPERATION, idempotency_key)
        replay = _load_idempotency(cur, owner_id, ARCHIVE_OPERATION, idempotency_key)
        if replay:
          return _replay_result(replay, request_hash)
        graph = _lock_lifecycle_experiment(cur, experiment_id, owner_id)
        response = _archive_experiment(
          cur, graph, expected_revision_id, expected_snapshot_version,
          reason, request_id,
        )
        _save_idempotency(
          cur, owner_id, ARCHIVE_OPERATION, idempotency_key, request_hash,
          "experiment", experiment_id, 200, response,
        )
        return CommandResult(response, 200)

  def restore_archived_experiment(
    self,
    *,
    experiment_id: str,
    expected_revision_id: str,
    expected_snapshot_version: int,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, RESTORE_ARCHIVE_OPERATION, idempotency_key)
        replay = _load_idempotency(
          cur, owner_id, RESTORE_ARCHIVE_OPERATION, idempotency_key,
        )
        if replay:
          return _replay_result(replay, request_hash)
        graph = _lock_lifecycle_experiment(cur, experiment_id, owner_id)
        response = _restore_archived_experiment(
          cur, graph, expected_revision_id, expected_snapshot_version,
          reason, request_id,
        )
        _save_idempotency(
          cur, owner_id, RESTORE_ARCHIVE_OPERATION, idempotency_key, request_hash,
          "experiment", experiment_id, 200, response,
        )
        return CommandResult(response, 200)

  def resume_experiment(
    self,
    experiment_id: str,
    expected_revision_id: str,
    reason: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _lock_command(cur, owner_id, RESUME_OPERATION, idempotency_key)
        replay = _load_idempotency(cur, owner_id, RESUME_OPERATION, idempotency_key)
        if replay:
          return _replay_result(replay, request_hash)
        graph = _load_current_stage_for_update(cur, experiment_id, owner_id)
        if graph is None:
          raise ExperimentError("experiment.not_found", "实验不存在", 404)
        response = _resume_stage(
          cur, graph, expected_revision_id, reason, request_id,
        )
        _save_idempotency(
          cur, owner_id, RESUME_OPERATION, idempotency_key, request_hash,
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
            AND r.generation_policy_version='legacy'
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
    SELECT q.owner_user_id
    FROM backtest_experiment_user_quota q
    JOIN app_user u ON u.id=q.owner_user_id
    WHERE q.experiment_in_flight_count < %s
      AND u.status='active'
      AND EXISTS (
        SELECT 1
        FROM backtest_experiment_stage s
        JOIN backtest_experiment_revision r ON r.id=s.revision_id
        JOIN backtest_experiment_trial t ON t.stage_id=s.id
        WHERE s.owner_user_id=q.owner_user_id
          AND s.status IN ('queued', 'running')
          AND s.blocked_reason_code IS NULL
          AND s.in_flight_count < %s
          AND r.lifecycle_state='active'
          AND r.cancel_requested_at IS NULL AND r.terminal_at IS NULL
          AND (
            t.materialization_status='pending'
            OR (
              t.materialization_status='materialized'
              AND t.summary_status='pending'
              AND t.retry_not_before <= now()
            )
          )
          AND t.active_execution_id IS NULL
      )
    ORDER BY q.last_materialized_at NULLS FIRST, q.owner_user_id
    LIMIT 1
  """, (
    settings.EXPERIMENT_MAX_IN_FLIGHT_PER_USER,
    settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT,
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
  if quota is None:
    return None
  quota = _sync_manual_reserved_permits(cur, dict(quota))
  cur.execute("""
    SELECT s.id AS stage_id, s.revision_id, r.experiment_id
    FROM backtest_experiment_stage s
    JOIN backtest_experiment_revision r ON r.id=s.revision_id
    WHERE s.owner_user_id=%s
      AND s.status IN ('queued', 'running')
      AND s.blocked_reason_code IS NULL
      AND s.in_flight_count < %s
      AND r.lifecycle_state='active'
      AND r.cancel_requested_at IS NULL AND r.terminal_at IS NULL
      AND EXISTS (
        SELECT 1 FROM backtest_experiment_trial t
        WHERE t.stage_id=s.id AND t.active_execution_id IS NULL
          AND (
            t.materialization_status='pending'
            OR (
              t.materialization_status='materialized'
              AND t.summary_status='pending'
              AND t.retry_not_before <= now()
            )
          )
      )
    ORDER BY s.last_materialized_sequence, s.created_at, s.id
    LIMIT 1
  """, (
    candidate["owner_user_id"], settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT,
  ))
  selected = cur.fetchone()
  if selected is None:
    return None
  cur.execute(
    "SELECT * FROM backtest_experiment_revision WHERE id=%s FOR UPDATE",
    (selected["revision_id"],),
  )
  revision = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment_stage WHERE id=%s FOR UPDATE",
    (selected["stage_id"],),
  )
  stage = cur.fetchone()
  cur.execute("""
    SELECT * FROM backtest_experiment_trial
    WHERE stage_id=%s AND active_execution_id IS NULL
      AND (
        materialization_status='pending'
        OR (
          materialization_status='materialized'
          AND summary_status='pending'
          AND retry_not_before <= now()
        )
      )
    ORDER BY trial_index
    FOR UPDATE SKIP LOCKED
    LIMIT 1
  """, (selected["stage_id"],))
  trial = cur.fetchone()
  if not all((quota, revision, stage, trial)):
    return None
  cur.execute("""
    SELECT count(*) AS value FROM backtest_experiment_asset_hold
    WHERE revision_id=%s AND hold_status <> 'held'
  """, (selected["revision_id"],))
  unavailable_assets = int(cur.fetchone()["value"])
  return {
    "quota": quota,
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
    and revision.get("lifecycle_state", "active") == "active"
    and stage["status"] in {"queued", "running"}
    and stage["blocked_reason_code"] is None
    and int(stage["in_flight_count"])
      < settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT
    and (
      trial["materialization_status"] == "pending"
      or (
        trial["materialization_status"] == "materialized"
        and trial["summary_status"] == "pending"
        and trial.get("retry_not_before") is not None
      )
    )
    and trial["active_execution_id"] is None
    and locked["unavailable_assets"] == 0
  )


def _sync_manual_reserved_permits(cur: Any, quota: dict[str, Any]) -> dict[str, Any]:
  reserved = settings.EXPERIMENT_MANUAL_RESERVED_PERMITS
  if int(quota["manual_reserved_permits"]) == reserved:
    return quota
  cur.execute("""
    UPDATE backtest_experiment_user_quota
    SET manual_reserved_permits=%s, row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
  """, (reserved, quota["owner_user_id"]))
  quota["manual_reserved_permits"] = reserved
  return quota


def _find_capacity_block_candidate(cur: Any) -> dict[str, Any] | None:
  cur.execute("""
    SELECT s.id AS stage_id, s.owner_user_id, s.revision_id
    FROM backtest_experiment_stage s
    JOIN backtest_experiment_revision r ON r.id=s.revision_id
    WHERE s.status IN ('queued', 'running', 'generation_blocked')
      AND s.blocked_reason_code = ANY(%s)
      AND r.cancel_requested_at IS NULL AND r.terminal_at IS NULL
      AND EXISTS (
        SELECT 1 FROM backtest_experiment_trial t
        WHERE t.stage_id=s.id AND t.materialization_status='pending'
          AND t.active_execution_id IS NULL
      )
    ORDER BY s.updated_at, s.id
    LIMIT 1
  """, (list(_TRANSIENT_BLOCK_CODES),))
  row = cur.fetchone()
  return dict(row) if row else None


def _lock_capacity_block_graph(
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
  if quota is None:
    return None
  quota = _sync_manual_reserved_permits(cur, dict(quota))
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
  if not all((revision, stage)):
    return None
  return {"quota": quota, "revision": dict(revision), "stage": dict(stage)}


def _materialization_gate(cur: Any, locked: dict[str, Any]) -> MaterializationGate:
  effective_capacity, global_in_flight = _capacity_snapshot(cur)
  if locked["revision"].get("generation_policy_version") == "legacy":
    budget_gate = _budget_capacity_gate(locked, effective_capacity)
    if not budget_gate.allowed:
      return budget_gate
  manual_gate = _manual_capacity_gate(cur, locked)
  if not manual_gate.allowed:
    return manual_gate
  global_limit = max(
    0, effective_capacity - settings.EXPERIMENT_MANUAL_RESERVED_PERMITS,
  )
  if global_in_flight >= global_limit:
    return MaterializationGate(
      False,
      "experiment.capacity_blocked",
      "当前统一调度容量已用尽，容量恢复后将继续运行",
    )
  return MaterializationGate(True)


def _budget_capacity_gate(
  locked: dict[str, Any],
  effective_capacity: int,
) -> MaterializationGate:
  """rolling_window_v1 激活前保持旧冻结预算语义。"""
  budget = dict(locked["revision"].get("resource_budget") or {})
  estimate = dict(budget.get("estimate") or budget)
  frozen = dict(budget.get("capacityPolicy") or {})
  current = _current_capacity_policy(effective_capacity)
  tightened = [
    key for key in (
      "maxTrials", "maxUserPlannedTrials", "maxEstimatedBars",
      "maxInFlightPerExperiment", "maxInFlightPerUser", "schedulerCapacity",
    )
    if key in frozen and int(current[key]) < int(frozen[key])
  ]
  estimated_bars = int(estimate.get("estimatedBarCount") or 0)
  over_current_budget = (
    int(locked["revision"]["planned_trial_count"]) > settings.EXPERIMENT_MAX_TRIALS
    or estimated_bars > settings.EXPERIMENT_BAR_MAX_ESTIMATED_BARS
    or int(locked["quota"]["reserved_planned_trial_count"])
      > settings.EXPERIMENT_MAX_USER_PLANNED_TRIALS
  )
  if tightened or over_current_budget:
    return MaterializationGate(
      False,
      "experiment.capacity_blocked",
      "系统容量低于该 Revision 创建时预算，恢复后将继续运行",
    )
  return MaterializationGate(True)


def _current_capacity_policy(effective_capacity: int) -> dict[str, int]:
  return {
    "maxTrials": settings.EXPERIMENT_MAX_TRIALS,
    "maxUserPlannedTrials": settings.EXPERIMENT_MAX_USER_PLANNED_TRIALS,
    "maxEstimatedBars": settings.EXPERIMENT_BAR_MAX_ESTIMATED_BARS,
    "maxInFlightPerExperiment": settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT,
    "maxInFlightPerUser": settings.EXPERIMENT_MAX_IN_FLIGHT_PER_USER,
    "schedulerCapacity": effective_capacity,
  }


def _manual_capacity_gate(cur: Any, locked: dict[str, Any]) -> MaterializationGate:
  pending_count, oldest_wait_seconds = _manual_queue_state(
    cur, str(locked["quota"]["owner_user_id"]),
  )
  if pending_count == 0:
    return MaterializationGate(True)
  if oldest_wait_seconds >= settings.EXPERIMENT_MANUAL_MAX_WAIT_SECONDS:
    return MaterializationGate(
      False,
      "experiment.manual_wait_blocked",
      "普通手工回测等待时间已达门禁，领取后实验将继续补充",
    )
  reserved = int(locked["quota"]["manual_reserved_permits"])
  experiment_limit = max(0, settings.EXPERIMENT_MAX_IN_FLIGHT_PER_USER - reserved)
  if int(locked["quota"]["experiment_in_flight_count"]) >= experiment_limit:
    return MaterializationGate(
      False,
      "experiment.manual_queue_reserved",
      "正在为同一用户的普通手工回测保留容量",
    )
  return MaterializationGate(True)


def _manual_queue_state(cur: Any, owner_id: str) -> tuple[int, float]:
  cur.execute("""
    SELECT count(*) AS pending_count,
      COALESCE(EXTRACT(EPOCH FROM now() - min(created_at)), 0) AS oldest_wait_seconds
    FROM (
      SELECT job.created_at
      FROM bar_backtest_job job
      JOIN backtest_run run ON run.id=job.run_id
      WHERE job.owner_user_id=%s AND run.origin_type='manual'
        AND NOT job.cancel_requested
        AND (job.status='queued' OR (job.status='running' AND job.lease_expires_at < now()))
      UNION ALL
      SELECT job.created_at
      FROM l2_backtest_job job
      JOIN backtest_run run ON run.id=job.run_id
      WHERE job.owner_user_id=%s AND run.origin_type='manual'
        AND NOT job.cancel_requested
        AND (job.status='queued' OR (job.status='running' AND job.lease_expires_at < now()))
    ) pending
  """, (owner_id, owner_id))
  row = cur.fetchone() or {}
  return int(row.get("pending_count") or 0), float(row.get("oldest_wait_seconds") or 0)


def _set_materialization_block(
  cur: Any,
  locked: dict[str, Any],
  gate: MaterializationGate,
  *,
  touch: bool = False,
) -> bool:
  stage = locked["stage"]
  current_code = stage.get("blocked_reason_code")
  desired_code = None if gate.allowed else gate.code
  desired_message = None if gate.allowed else gate.message
  if current_code not in _TRANSIENT_BLOCK_CODES and current_code is not None:
    return False
  changed = (
    current_code != desired_code
    or stage.get("blocked_reason_message") != desired_message
  )
  if not changed:
    if touch:
      cur.execute(
        "UPDATE backtest_experiment_stage SET updated_at=now() WHERE id=%s",
        (stage["id"],),
      )
    return False
  _write_materialization_block(cur, locked, desired_code, desired_message)
  _audit_materialization_block(cur, locked, desired_code)
  _rebuild_aggregate_status(cur, str(stage["id"]))
  return True


def _write_materialization_block(
  cur: Any,
  locked: dict[str, Any],
  code: str | None,
  message: str | None,
) -> None:
  stage = locked["stage"]
  revision = locked["revision"]
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET blocked_reason_code=%s, blocked_reason_message=%s,
      row_version=row_version+1, updated_at=now()
    WHERE id=%s
  """, (code, message, stage["id"]))
  cur.execute("""
    UPDATE backtest_experiment_revision
    SET blocked_reason_code=%s, blocked_reason_message=%s, updated_at=now()
    WHERE id=%s
  """, (code, message, revision["id"]))
  cur.execute("""
    UPDATE backtest_experiment
    SET blocked_reason_code=%s, blocked_reason_message=%s,
      row_version=row_version+1, updated_at=now()
    WHERE id=%s
  """, (code, message, revision["experiment_id"]))


def _audit_materialization_block(
  cur: Any,
  locked: dict[str, Any],
  code: str | None,
) -> None:
  stage = locked["stage"]
  revision = locked["revision"]
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id, event_type, detail
    ) VALUES (%s,%s,%s,%s,%s,%s)
  """, (
    stage["owner_user_id"], revision["experiment_id"], revision["id"], stage["id"],
    "stage.materialization_resumed" if code is None else "stage.materialization_blocked",
    Jsonb({"reasonCode": code}),
  ))


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
  created_reason_override: str | None = None,
) -> dict[str, Any]:
  trial = locked["trial"]
  stage = locked["stage"]
  revision = locked["revision"]
  owner_id = str(trial["owner_user_id"])
  execution_id = str(uuid4())
  dispatch_key = f"experiment:{execution_id}"
  is_retry = trial["materialization_status"] == "materialized"
  cur.execute("""
    SELECT COALESCE(max(execution_no),0) + 1 AS execution_no
    FROM backtest_experiment_trial_execution
    WHERE trial_id=%s
  """, (trial["id"],))
  execution_no = int(cur.fetchone()["execution_no"])
  created_reason = created_reason_override or (
    "automatic_retry" if is_retry else "initial"
  )
  run_id = _create_experiment_run(
    cur, payload, owner_id, dispatch_key, execution_id,
    str(revision["experiment_id"]), str(stage["id"]),
  )
  cur.execute("""
    INSERT INTO backtest_experiment_trial_execution (
      id, trial_id, owner_user_id, execution_no, dispatch_key,
      run_id, created_reason
    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
  """, (
    execution_id, trial["id"], owner_id, execution_no,
    dispatch_key, run_id, created_reason,
  ))
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
      summary_status='queued', retry_not_before=NULL,
      automatic_retry_count=automatic_retry_count+%s, updated_at=now()
    WHERE id=%s AND active_execution_id IS NULL
      AND summary_status IN ('pending', 'failed')
  """, (
    execution_id, 1 if created_reason == "automatic_retry" else 0, trial["id"],
  ))
  if cur.rowcount != 1:
    raise ExperimentError("experiment.state_changed", "Trial 状态已经变化", 409)
  cur.execute("""
    UPDATE backtest_experiment_user_quota
    SET experiment_in_flight_count=experiment_in_flight_count+1,
      materialization_sequence=materialization_sequence+1,
      last_materialized_at=now(), row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
    RETURNING materialization_sequence
  """, (owner_id,))
  materialization_sequence = int(cur.fetchone()["materialization_sequence"])
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET in_flight_count=in_flight_count+1,
      last_materialized_sequence=%s, last_materialized_at=now(),
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (materialization_sequence, stage["id"]))
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
    Jsonb({
      "runId": run_id,
      "dispatcherType": "postgres_bar_queue_v1",
      "createdReason": created_reason,
      "executionNo": execution_no,
    }),
  ))
  return {
    "executionId": execution_id,
    "executionNo": execution_no,
    "runId": run_id,
    "dispatchKey": dispatch_key,
    "createdReason": created_reason,
  }


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
      revision.experiment_id, revision.generation_policy_version
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
  if candidate.get("generation_policy_version") == "rolling_window_v1":
    from backend.experiments.rolling_window import _lock_ledgers

    _lock_ledgers(
      cur, str(candidate["owner_user_id"]), str(candidate["stage_id"]),
    )
  cur.execute(
    "SELECT * FROM backtest_experiment_user_quota WHERE owner_user_id=%s FOR UPDATE",
    (candidate["owner_user_id"],),
  )
  quota = cur.fetchone()
  cur.execute(
    "SELECT * FROM backtest_experiment WHERE id=%s FOR UPDATE",
    (candidate["experiment_id"],),
  )
  experiment = cur.fetchone()
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
  if not all((quota, experiment, revision, stage, trial, execution)):
    return None
  cur.execute("SELECT * FROM backtest_run WHERE id=%s FOR UPDATE", (execution["run_id"],))
  run = cur.fetchone()
  if run is None or str(run["owner_user_id"]) != str(execution["owner_user_id"]):
    raise ExperimentError("experiment.execution_identity_conflict", "回测执行归属不一致", 409)
  return {
    "quota": dict(quota),
    "experiment": dict(experiment),
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
  result_hash = _stable_json_hash(result) if result is not None else None
  result_schema = str(result.get("resultSchemaVersion")) if result else None
  data_hash = _actual_data_hash(result, run.get("market_data_snapshot"))
  terminal_sequence = int(locked["stage"]["next_terminal_sequence"])
  failure = _execution_failure(cur, locked) if status == "failed" else None
  retry_scheduled = bool(failure) and _should_retry_execution(cur, locked, failure)
  cur.execute("""
    UPDATE backtest_experiment_trial_execution
    SET result_schema_version=%s, result_hash=%s, actual_data_hash=%s,
      error_summary=%s, terminal_sequence=%s, terminal_status=%s,
      error_category=%s, error_code=%s, error_fingerprint=%s,
      failure_stage=%s, responsibility_component=%s, retryable=%s,
      permit_released_at=now(),
      started_at=COALESCE(started_at,%s), finished_at=COALESCE(%s,now()),
      updated_at=now()
    WHERE id=%s AND permit_released_at IS NULL
  """, (
    result_schema, result_hash, data_hash,
    Jsonb(failure) if failure else None, terminal_sequence, status,
    failure.get("category") if failure else None,
    failure.get("code") if failure else None,
    failure.get("fingerprint") if failure else None,
    failure.get("failureStage") if failure else None,
    failure.get("component") if failure else None,
    bool(failure and failure.get("retryable")),
    run.get("started_at"), run.get("finished_at"), execution["id"],
  ))
  if cur.rowcount != 1:
    return
  _finish_trial_execution(cur, locked, status, retry_scheduled)
  if failure:
    _update_failure_circuit(cur, locked, failure, terminal_sequence)
  elif status == "succeeded":
    _reset_failure_sequence(cur, str(locked["stage"]["id"]))
  aggregate_status = _rebuild_aggregate_status(cur, str(locked["stage"]["id"]))
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id, trial_id,
      execution_id, event_type, detail
    ) VALUES (%s,%s,%s,%s,%s,%s,'trial.reconciled',%s)
  """, (
    execution["owner_user_id"], locked["revision"]["experiment_id"],
    locked["revision"]["id"], locked["stage"]["id"], trial["id"],
    execution["id"], Jsonb({
      "status": status,
      "terminalSequence": terminal_sequence,
      "retryScheduled": retry_scheduled,
      "aggregateStatus": aggregate_status,
      "errorFingerprint": failure.get("fingerprint") if failure else None,
    }),
  ))


def _finish_trial_execution(
  cur: Any,
  locked: dict[str, Any],
  status: str,
  retry_scheduled: bool,
) -> None:
  trial = locked["trial"]
  execution = locked["execution"]
  selected = execution["id"] if status == "succeeded" else trial.get("selected_execution_id")
  summary_status = "pending" if retry_scheduled else status
  retry_at = _automatic_retry_at(locked) if retry_scheduled else None
  rolling_terminal = (
    locked["revision"].get("generation_policy_version") == "rolling_window_v1"
    and not retry_scheduled
  )
  if rolling_terminal:
    cur.execute("""
      UPDATE backtest_experiment_trial
      SET selected_execution_id=%s, updated_at=now()
      WHERE id=%s AND active_execution_id=%s
    """, (selected, trial["id"], execution["id"]))
    from backend.experiments.rolling_window import settle_trial_in_transaction

    settle_trial_in_transaction(
      cur, str(trial["id"]), status,
      f"execution-terminal:{execution['id']}", datetime.now(timezone.utc),
    )
  else:
    cur.execute("""
      UPDATE backtest_experiment_trial
      SET active_execution_id=NULL, selected_execution_id=%s,
        summary_status=%s, retry_not_before=%s, updated_at=now()
      WHERE id=%s AND active_execution_id=%s
    """, (selected, summary_status, retry_at, trial["id"], execution["id"]))
  cur.execute("""
    UPDATE backtest_experiment_user_quota
    SET experiment_in_flight_count=GREATEST(0,experiment_in_flight_count-1),
      row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
  """, (execution["owner_user_id"],))
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET in_flight_count=GREATEST(0,in_flight_count-1),
      next_terminal_sequence=next_terminal_sequence+1,
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (locked["stage"]["id"],))


def _automatic_retry_at(locked: dict[str, Any]) -> datetime:
  policy = dict(locked["revision"].get("failure_policy") or {})
  retry = dict(policy.get("automaticRetry") or {})
  return datetime.now(timezone.utc) + timedelta(
    seconds=int(retry.get("cooldownSeconds") or 0),
  )


def _execution_failure(cur: Any, locked: dict[str, Any]) -> dict[str, Any]:
  cur.execute("""
    SELECT stage, source
    FROM system_error_log
    WHERE run_id=%s
    ORDER BY occurred_at DESC, id DESC
    LIMIT 1
  """, (locked["run"]["id"],))
  diagnostic = cur.fetchone()
  if diagnostic is None:
    cur.execute("""
      SELECT stage, job_kind AS source
      FROM backtest_run_attempt
      WHERE run_id=%s
      ORDER BY attempt_no DESC
      LIMIT 1
    """, (locked["run"]["id"],))
    diagnostic = cur.fetchone()
  spec = dict(locked["revision"]["experiment_spec"])
  template = dict(spec["runTemplateSnapshot"])
  return normalize_failure(
    locked["run"].get("result_summary"),
    stage_type=str(locked["stage"]["stage_type"]),
    template_type=str(template.get("templateType") or "unknown"),
    run_path=str(template.get("runPath") or "unknown"),
    scope_type=str(template.get("scopeType") or "unknown"),
    failure_stage=str(diagnostic["stage"] if diagnostic else "backtest.unknown"),
    component=str(diagnostic["source"] if diagnostic else "backtest_runner"),
  )


def _should_retry_execution(
  cur: Any,
  locked: dict[str, Any],
  failure: dict[str, Any],
) -> bool:
  if locked["revision"].get("cancel_requested_at") is not None:
    return False
  policy = dict(locked["revision"].get("failure_policy") or {})
  retry = dict(policy.get("automaticRetry") or {})
  window_seconds = int(retry.get("observationWindowSeconds") or 0)
  cur.execute("""
    SELECT count(*) AS value
    FROM backtest_experiment_trial_execution
    WHERE trial_id=%s AND created_reason='automatic_retry'
      AND created_at >= now() - (%s * interval '1 second')
  """, (locked["trial"]["id"], window_seconds))
  retries_in_window = int(cur.fetchone()["value"])
  return allows_automatic_retry(
    policy,
    failure,
    automatic_retry_count=int(locked["trial"].get("automatic_retry_count") or 0),
    retry_count_in_window=retries_in_window,
  )


def _update_failure_circuit(
  cur: Any,
  locked: dict[str, Any],
  failure: dict[str, Any],
  terminal_sequence: int,
) -> None:
  policy = dict(locked["revision"].get("failure_policy") or {})
  circuit = dict(policy.get("circuitBreaker") or {})
  window_limit = max(1, int(circuit.get("failureRateWindowSize") or 1))
  cur.execute("""
    SELECT execution.terminal_status, execution.error_fingerprint
    FROM backtest_experiment_trial_execution execution
    JOIN backtest_experiment_trial trial ON trial.id=execution.trial_id
    WHERE trial.stage_id=%s
      AND execution.terminal_sequence > %s
      AND execution.terminal_sequence <= %s
    ORDER BY execution.terminal_sequence DESC
    LIMIT %s
  """, (
    locked["stage"]["id"], locked["stage"].get("circuit_reset_sequence") or 0,
    terminal_sequence, window_limit,
  ))
  rows = [dict(row) for row in cur.fetchall()]
  comparable = [row for row in rows if row["terminal_status"] != "cancelled"]
  fingerprint = str(failure["fingerprint"])
  consecutive = _consecutive_failure_count(comparable, fingerprint)
  same_failures = sum(
    1 for row in comparable
    if row["terminal_status"] == "failed" and row["error_fingerprint"] == fingerprint
  )
  _save_failure_sequence(cur, str(locked["stage"]["id"]), fingerprint, consecutive)
  if should_open_circuit(
    policy,
    consecutive_count=consecutive,
    window_failures=same_failures,
    window_size=len(comparable),
  ):
    _open_execution_circuit(cur, locked, failure, consecutive)


def _consecutive_failure_count(
  rows: list[dict[str, Any]],
  fingerprint: str,
) -> int:
  count = 0
  for row in rows:
    if row["terminal_status"] != "failed" or row["error_fingerprint"] != fingerprint:
      break
    count += 1
  return count


def _save_failure_sequence(
  cur: Any,
  stage_id: str,
  fingerprint: str,
  count: int,
) -> None:
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET consecutive_failure_fingerprint=%s, consecutive_failure_count=%s,
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (fingerprint, count, stage_id))


def _open_execution_circuit(
  cur: Any,
  locked: dict[str, Any],
  failure: dict[str, Any],
  consecutive: int,
) -> None:
  already_open = locked["stage"].get("blocked_reason_code") == "experiment.execution_blocked"
  message = "连续系统性错误已触发实验熔断，请确认运行能力后恢复"
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET blocked_reason_code='experiment.execution_blocked',
      blocked_reason_message=%s, blocked_scope='stage',
      blocked_at=COALESCE(blocked_at,now()), updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (message, locked["stage"]["id"]))
  cur.execute("""
    UPDATE backtest_experiment_revision
    SET system_block_code='execution_circuit',
      system_block_message=%s,
      blocked_reason_code='experiment.execution_blocked',
      blocked_reason_message=%s, updated_at=now()
    WHERE id=%s
  """, (message, message, locked["revision"]["id"]))
  cur.execute("""
    UPDATE backtest_experiment
    SET blocked_reason_code='experiment.execution_blocked',
      blocked_reason_message=%s, updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (message, locked["revision"]["experiment_id"]))
  if not already_open:
    _audit_circuit_open(cur, locked, failure, consecutive)


def _audit_circuit_open(
  cur: Any,
  locked: dict[str, Any],
  failure: dict[str, Any],
  consecutive: int,
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id,
      event_type, detail
    ) VALUES (%s,%s,%s,%s,'experiment.execution_blocked',%s)
  """, (
    locked["stage"]["owner_user_id"], locked["revision"]["experiment_id"],
    locked["revision"]["id"], locked["stage"]["id"],
    Jsonb({
      "errorFingerprint": failure["fingerprint"],
      "errorCode": failure["code"],
      "consecutiveCount": consecutive,
      "blockScope": "stage",
    }),
  ))


def _reset_failure_sequence(cur: Any, stage_id: str) -> None:
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET consecutive_failure_fingerprint=NULL, consecutive_failure_count=0,
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (stage_id,))


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
  _rebuild_aggregate_status(cur, str(locked["stage"]["id"]))


def _rebuild_aggregate_status(cur: Any, stage_id: str) -> str:
  from backend.experiments.lifecycle import project_legacy_status

  graph = _aggregate_graph(cur, stage_id)
  facts = _aggregate_facts(
    cur,
    graph["stage"],
    cancel_requested=graph["revision"].get("cancel_requested_at") is not None,
  )
  derived_status = derive_aggregate_status(facts)
  revision = graph["revision"]
  lifecycle_state = str(revision.get("lifecycle_state") or "active")
  in_flight = facts.queued + facts.running + facts.cancelling
  terminal_outcome = None
  if lifecycle_state == "pausing" and in_flight == 0:
    lifecycle_state = "paused"
  if revision.get("cancel_requested_at") is not None:
    if facts.non_terminal == 0:
      lifecycle_state = "terminal"
      terminal_outcome = derived_status
    else:
      lifecycle_state = "cancelling"
  elif derived_status in {"completed", "partially_completed", "failed", "cancelled"}:
    lifecycle_state = "terminal"
    terminal_outcome = derived_status
  if lifecycle_state == "terminal":
    execution_phase = "settled"
  elif graph["revision"].get("system_block_code") or graph["stage"].get("blocked_reason_code"):
    execution_phase = "blocked"
  elif facts.running + facts.cancelling > 0:
    execution_phase = "running"
  elif facts.pending + facts.queued > 0:
    execution_phase = "queued"
  elif facts.generated < facts.planned:
    execution_phase = "preparing"
  else:
    execution_phase = "queued"
  status = project_legacy_status(
    lifecycle_state, execution_phase,
    graph["revision"].get("system_block_code"), terminal_outcome,
  )
  validate_status_transition(str(graph["stage"]["status"]), status)
  validate_status_transition(str(graph["revision"]["status"]), status)
  validate_status_transition(str(graph["experiment"]["status"]), status)
  _write_aggregate_facts(
    cur, graph, facts, status, lifecycle_state, execution_phase, terminal_outcome,
  )
  return status


def _aggregate_graph(cur: Any, stage_id: str) -> dict[str, dict[str, Any]]:
  cur.execute("""
    SELECT e.id AS experiment_id, e.status AS experiment_status,
      r.id AS revision_id, r.status AS revision_status,
      r.owner_user_id, r.experiment_id AS parent_experiment_id,
      r.planned_trial_count AS revision_planned_trial_count,
      r.cancel_requested_at, r.terminal_at, r.lifecycle_state,
      r.lifecycle_version, r.terminal_outcome,
      r.generation_policy_version, r.plan_quota_released_at,
      r.system_block_code,
      s.*
    FROM backtest_experiment_stage s
    JOIN backtest_experiment_revision r ON r.id=s.revision_id
    JOIN backtest_experiment e ON e.id=r.experiment_id
    WHERE s.id=%s
    FOR UPDATE OF e, r, s
  """, (stage_id,))
  row = dict(cur.fetchone())
  return {
    "experiment": {"id": row["experiment_id"], "status": row["experiment_status"]},
    "revision": {
      "id": row["revision_id"],
      "status": row["revision_status"],
      "owner_user_id": row["owner_user_id"],
      "experiment_id": row["parent_experiment_id"],
      "planned_trial_count": row["revision_planned_trial_count"],
      "cancel_requested_at": row["cancel_requested_at"],
      "terminal_at": row["terminal_at"],
      "lifecycle_state": row["lifecycle_state"],
      "lifecycle_version": row["lifecycle_version"],
      "terminal_outcome": row["terminal_outcome"],
      "generation_policy_version": row["generation_policy_version"],
      "plan_quota_released_at": row["plan_quota_released_at"],
      "system_block_code": row["system_block_code"],
    },
    "stage": row,
  }


def _aggregate_facts(
  cur: Any,
  stage: dict[str, Any],
  *,
  cancel_requested: bool,
) -> AggregateFacts:
  cur.execute("""
    SELECT
      count(*) FILTER (WHERE summary_status='pending') AS pending,
      count(*) FILTER (WHERE summary_status='queued') AS queued,
      count(*) FILTER (WHERE summary_status='running') AS running,
      count(*) FILTER (WHERE summary_status='cancelling') AS cancelling,
      count(*) FILTER (WHERE summary_status='succeeded') AS succeeded,
      count(*) FILTER (WHERE summary_status='failed') AS failed,
      count(*) FILTER (WHERE summary_status='cancelled') AS cancelled
    FROM backtest_experiment_trial
    WHERE stage_id=%s
  """, (stage["id"],))
  counts = cur.fetchone()
  cancelled = int(counts["cancelled"] or 0) + int(stage["cancelled_before_generation"])
  return AggregateFacts(
    planned=int(stage["planned_trial_count"]),
    generated=int(stage["generated_trial_count"]),
    pending=int(counts["pending"] or 0),
    queued=int(counts["queued"] or 0),
    running=int(counts["running"] or 0),
    cancelling=int(counts["cancelling"] or 0),
    succeeded=int(counts["succeeded"] or 0),
    failed=int(counts["failed"] or 0),
    cancelled=cancelled,
    cancel_requested=cancel_requested,
  )


def _write_aggregate_facts(
  cur: Any,
  graph: dict[str, dict[str, Any]],
  facts: AggregateFacts,
  status: str,
  lifecycle_state: str,
  execution_phase: str,
  terminal_outcome: str | None,
) -> None:
  stage = graph["stage"]
  revision = graph["revision"]
  in_flight = facts.queued + facts.running + facts.cancelling
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET status=%s, in_flight_count=%s, terminal_count=%s,
      succeeded_count=%s, failed_count=%s, cancelled_count=%s,
      execution_phase=%s,
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (
    status, in_flight, facts.terminal, facts.succeeded,
    facts.failed, facts.cancelled, execution_phase, stage["id"],
  ))
  terminal = lifecycle_state == "terminal"
  release_plan = (
    terminal
    and revision.get("generation_policy_version") == "legacy"
    and revision.get("plan_quota_released_at") is None
  )
  cur.execute("""
    UPDATE backtest_experiment_revision
    SET status=%s,
      terminal_at=CASE WHEN %s THEN COALESCE(terminal_at,now()) ELSE terminal_at END,
      lifecycle_state=%s,
      lifecycle_version=lifecycle_version + CASE
        WHEN lifecycle_state<>%s THEN 1 ELSE 0 END,
      terminal_outcome=%s,
      plan_quota_released_at=CASE
        WHEN %s THEN COALESCE(plan_quota_released_at,now())
        ELSE plan_quota_released_at
      END,
      updated_at=now()
    WHERE id=%s
  """, (
    status, terminal, lifecycle_state, lifecycle_state,
    terminal_outcome, release_plan, revision["id"],
  ))
  cur.execute("""
    UPDATE backtest_experiment
    SET status=%s, updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (status, revision["experiment_id"]))
  if release_plan:
    _release_plan_quota(cur, revision)


def _release_plan_quota(cur: Any, revision: dict[str, Any]) -> None:
  cur.execute("""
    UPDATE backtest_experiment_user_quota
    SET reserved_planned_trial_count=GREATEST(
      0,reserved_planned_trial_count-%s
    ), row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
  """, (revision["planned_trial_count"], revision["owner_user_id"]))


def _find_aggregate_repair_candidate(cur: Any) -> dict[str, Any] | None:
  cur.execute("""
    WITH trial_counts AS (
      SELECT stage_id,
        count(*) FILTER (WHERE summary_status='pending') AS pending,
        count(*) FILTER (WHERE summary_status='queued') AS queued,
        count(*) FILTER (WHERE summary_status='running') AS running,
        count(*) FILTER (WHERE summary_status='cancelling') AS cancelling,
        count(*) FILTER (WHERE summary_status='succeeded') AS succeeded,
        count(*) FILTER (WHERE summary_status='failed') AS failed,
        count(*) FILTER (WHERE summary_status='cancelled') AS cancelled
      FROM backtest_experiment_trial
      GROUP BY stage_id
    ), facts AS (
      SELECT s.*, r.status AS revision_status, r.cancel_requested_at,
        r.lifecycle_state, r.terminal_outcome, r.system_block_code,
        e.status AS experiment_status, q.experiment_in_flight_count,
        COALESCE(c.pending,0) AS actual_pending,
        COALESCE(c.queued,0) AS actual_queued,
        COALESCE(c.running,0) AS actual_running,
        COALESCE(c.cancelling,0) AS actual_cancelling,
        COALESCE(c.succeeded,0) AS actual_succeeded,
        COALESCE(c.failed,0) AS actual_failed,
        COALESCE(c.cancelled,0) + s.cancelled_before_generation AS actual_cancelled,
        (
          SELECT count(*) FROM backtest_experiment_trial_execution execution
          WHERE execution.owner_user_id=s.owner_user_id
            AND execution.permit_released_at IS NULL
        ) AS actual_user_in_flight
      FROM backtest_experiment_stage s
      JOIN backtest_experiment_revision r ON r.id=s.revision_id
      JOIN backtest_experiment e ON e.id=r.experiment_id
      JOIN backtest_experiment_user_quota q ON q.owner_user_id=s.owner_user_id
      LEFT JOIN trial_counts c ON c.stage_id=s.id
    ), expected AS (
      SELECT facts.*,
        actual_succeeded + actual_failed + actual_cancelled AS actual_terminal,
        actual_queued + actual_running + actual_cancelling AS actual_in_flight,
        CASE
          WHEN lifecycle_state='terminal' AND terminal_outcome IS NOT NULL
            THEN terminal_outcome
          WHEN lifecycle_state='cancelling' THEN 'cancelling'
          WHEN lifecycle_state='paused' THEN 'generation_blocked'
          WHEN lifecycle_state='pausing' AND execution_phase IN ('queued','running')
            THEN execution_phase
          WHEN system_block_code IS NOT NULL
            OR blocked_reason_code IS NOT NULL
            OR execution_phase='blocked'
            THEN 'generation_blocked'
          WHEN cancel_requested_at IS NOT NULL
            AND actual_succeeded + actual_failed + actual_cancelled < planned_trial_count
            THEN 'cancelling'
          WHEN cancel_requested_at IS NULL AND generated_trial_count < planned_trial_count
            THEN 'preparing'
          WHEN cancel_requested_at IS NULL AND actual_running + actual_cancelling > 0
            THEN 'running'
          WHEN cancel_requested_at IS NULL AND actual_pending + actual_queued > 0
            THEN 'queued'
          WHEN actual_succeeded=planned_trial_count THEN 'completed'
          WHEN actual_succeeded > 0 THEN 'partially_completed'
          WHEN cancel_requested_at IS NOT NULL OR actual_cancelled=planned_trial_count
            THEN 'cancelled'
          WHEN actual_failed > 0 THEN 'failed'
          ELSE 'queued'
        END AS expected_status
      FROM facts
    )
    SELECT id AS stage_id, owner_user_id
    FROM expected
    WHERE terminal_count <> actual_terminal
      OR in_flight_count <> actual_in_flight
      OR succeeded_count <> actual_succeeded
      OR failed_count <> actual_failed
      OR cancelled_count <> actual_cancelled
      OR status <> expected_status
      OR revision_status <> expected_status
      OR experiment_status <> expected_status
      OR experiment_in_flight_count <> actual_user_in_flight
    ORDER BY updated_at, id
    LIMIT 1
  """)
  row = cur.fetchone()
  return dict(row) if row else None


def _repair_user_quota(cur: Any, owner_id: str) -> None:
  rolling_active = _generation_policy(cur) == "rolling_window_v1"
  cur.execute("""
    SELECT
      (SELECT count(*) FROM backtest_experiment_trial_execution execution
       WHERE execution.owner_user_id=%s AND execution.permit_released_at IS NULL)
        AS in_flight,
      (SELECT COALESCE(sum(planned_trial_count),0)
       FROM backtest_experiment_revision revision
       WHERE revision.owner_user_id=%s
         AND revision.plan_quota_released_at IS NULL
         AND revision.generation_policy_version='legacy') AS planned
  """, (owner_id, owner_id))
  facts = cur.fetchone()
  cur.execute("""
    UPDATE backtest_experiment_user_quota
    SET experiment_in_flight_count=%s,
      reserved_planned_trial_count=CASE
        WHEN %s THEN reserved_planned_trial_count ELSE %s END,
      row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
  """, (
    int(facts["in_flight"]), rolling_active,
    int(facts["planned"]), owner_id,
  ))


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
    "source": _experiment_source_response(row),
    "subject": {
      **spec["subjectRef"],
      "name": fixed["strategyName"],
    },
    "currentRevisionId": str(row["revision_id"]),
    "revisionNo": int(row["revision_no"]),
    "status": row["status"],
    "lifecycleState": row.get("lifecycle_state", "active"),
    "lifecycleVersion": int(row.get("lifecycle_version") or 1),
    "executionPhase": row.get("execution_phase", "preparing"),
    "systemBlockCode": _project_system_block(row),
    "terminalOutcome": row.get("terminal_outcome"),
    "reason": _row_reason(row),
    "runPath": str(spec["runTemplateSnapshot"].get("runPath") or "unknown"),
    "scopeType": str(spec["runTemplateSnapshot"].get("scopeType") or "unknown"),
    "symbol": fixed["symbol"],
    "timeframe": fixed["timeframe"],
    "fillModel": fixed["fillModel"],
    "plannedTrialCount": int(row["planned_trial_count"]),
    "generatedTrialCount": int(row["generated_trial_count"]),
    "materializedTrialCount": int(row["terminal_count"]) + int(row["in_flight_count"]),
    "readyTrialCount": int(row.get("ready_count") or 0),
    "queuedTrialCount": int(row.get("queued_count") or 0),
    "remainingLogicalTrialCount": max(
      0, int(row["planned_trial_count"]) - int(row["generated_trial_count"]),
    ),
    "runningTrialCount": int(row["in_flight_count"]),
    "succeededTrialCount": int(row["succeeded_count"]),
    "failedTrialCount": int(row["failed_count"]),
    "cancelledTrialCount": int(row["cancelled_count"]),
    "createdAt": row["created_at"].isoformat(),
    "updatedAt": row["updated_at"].isoformat(),
    "archivedAt": row["archived_at"].isoformat() if row["archived_at"] else None,
    "retentionUntil": (
      row["archive_retention_until"].isoformat()
      if row.get("archive_retention_until") else None
    ),
    "purgeJobId": str(row["purge_job_id"]) if row.get("purge_job_id") else None,
  }


def _project_system_block(row: dict[str, Any]) -> str | None:
  epoch = row.get("contract_epoch")
  decision = row.get("storage_decision")
  if epoch in {"lifecycle_v2", "evaluation_v2", "rolling_window_v1"}:
    if not settings.EXPERIMENTS_ENABLED or not settings.EXPERIMENT_BAR_ENABLED:
      return "automation_disabled"
    if decision == "pressured":
      return "storage_pressure"
    if decision == "unknown":
      return "storage_signal_unknown"
  value = row.get("system_block_code")
  return str(value) if value else None


def _admin_archived_experiment_list_item(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "experimentId": str(row["experiment_id"]),
    "name": row["name"],
    "currentRevisionId": str(row["revision_id"]),
    "status": row["status"],
    "snapshotVersion": int(row["row_version"]),
    "archivedAt": row["archived_at"].isoformat(),
    "retentionUntil": (
      row["archive_retention_until"].isoformat()
      if row.get("archive_retention_until") else None
    ),
    "purgeJobId": str(row["purge_job_id"]) if row.get("purge_job_id") else None,
    "updatedAt": row["updated_at"].isoformat(),
  }


def _revision_list_item(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "revisionId": str(row["revision_id"]),
    "revisionNo": int(row["revision_no"]),
    "current": str(row["revision_id"]) == str(row["current_revision_id"]),
    "status": row["status"],
    "lifecycleState": row.get("lifecycle_state", "active"),
    "lifecycleVersion": int(row.get("lifecycle_version") or 1),
    "executionPhase": row.get("execution_phase", "preparing"),
    "systemBlockCode": _project_system_block(row),
    "terminalOutcome": row.get("terminal_outcome"),
    "specHash": row["spec_hash"],
    "stage": {
      "stageId": str(row["stage_id"]),
      "status": row["stage_status"],
      "plannedTrialCount": int(row["planned_trial_count"]),
      "generatedTrialCount": int(row["generated_trial_count"]),
      "inFlightTrialCount": int(row.get("in_flight_count") or 0),
      "terminalTrialCount": int(row.get("terminal_count") or 0),
      "succeededTrialCount": int(row.get("succeeded_count") or 0),
      "failedTrialCount": int(row.get("failed_count") or 0),
      "cancelledTrialCount": int(row.get("cancelled_count") or 0),
    },
    "createdAt": row["created_at"].isoformat(),
    "updatedAt": row["updated_at"].isoformat(),
    "terminalAt": row["terminal_at"].isoformat() if row.get("terminal_at") else None,
  }


def _trial_list_item(row: dict[str, Any]) -> dict[str, Any]:
  trial_spec = dict(row["trial_spec"])
  run_id = row.get("selected_run_id") or row.get("active_run_id") or row.get("latest_run_id")
  execution_id = (
    row.get("selected_execution_id")
    or row.get("active_execution_id")
    or row.get("latest_execution_id")
  )
  return {
    "trialId": str(row["trial_id"]),
    "trialIndex": int(row["trial_index"]),
    "stageId": str(row["stage_id"]),
    "candidateKey": trial_spec.get("candidateKey"),
    "dimensionAssignment": row["dimension_projection"],
    "materializationStatus": row["materialization_status"],
    "summaryStatus": row["summary_status"],
    "automaticRetryCount": int(row.get("automatic_retry_count") or 0),
    "retryNotBefore": (
      row["retry_not_before"].isoformat() if row.get("retry_not_before") else None
    ),
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
    "executionAttempts": row.get("execution_attempts") or [],
    "runId": str(run_id) if run_id else None,
    "runUrl": f"/?workspace=backtest&runId={run_id}" if run_id else None,
    "executionId": str(execution_id) if execution_id else None,
  }


def _load_related_run_summary(
  cur: Any,
  experiment_id: str,
  owner_id: str,
) -> dict[str, int]:
  cur.execute("""
    SELECT count(*) AS total_count,
      count(*) FILTER (WHERE execution.created_reason='initial') AS initial_count,
      count(*) FILTER (WHERE execution.created_reason<>'initial') AS retry_count
    FROM backtest_experiment experiment
    JOIN backtest_experiment_revision revision
      ON revision.experiment_id=experiment.id
    JOIN backtest_experiment_stage stage ON stage.revision_id=revision.id
    JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
    JOIN backtest_experiment_trial_execution execution ON execution.trial_id=trial.id
    WHERE experiment.id=%s AND experiment.owner_user_id=%s
  """, (experiment_id, owner_id))
  row = cur.fetchone() or {}
  return {
    "totalCount": int(row.get("total_count") or 0),
    "initialCount": int(row.get("initial_count") or 0),
    "retryCount": int(row.get("retry_count") or 0),
  }


def _related_run_list_item(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "revisionId": str(row["revision_id"]),
    "revisionNo": int(row["revision_no"]),
    "currentRevision": bool(row["current_revision"]),
    "stageId": str(row["stage_id"]),
    "stageIndex": int(row["stage_index"]),
    "trialId": str(row["trial_id"]),
    "trialIndex": int(row["trial_index"]),
    "executionId": str(row["execution_id"]),
    "executionNo": int(row["execution_no"]),
    "createdReason": str(row["created_reason"]),
    "selected": bool(row["selected"]),
    "runId": str(row["run_id"]),
    "displayNo": int(row["display_no"]),
    "status": str(row["status"]),
  }


def _require_owned_experiment(cur: Any, experiment_id: str, owner_id: str) -> None:
  cur.execute(
    "SELECT 1 FROM backtest_experiment WHERE id=%s AND owner_user_id=%s",
    (experiment_id, owner_id),
  )
  if cur.fetchone() is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)


def _resolve_revision_id(
  cur: Any,
  experiment_id: str,
  owner_id: str,
  revision_id: str | None,
) -> str:
  cur.execute("""
    SELECT revision.id
    FROM backtest_experiment experiment
    JOIN backtest_experiment_revision revision
      ON revision.experiment_id=experiment.id
    WHERE experiment.id=%s AND experiment.owner_user_id=%s
      AND revision.id=COALESCE(%s::uuid,experiment.current_revision_id)
  """, (experiment_id, owner_id, revision_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.not_found", "实验修订不存在", 404)
  return str(row["id"])


def _row_reason(row: dict[str, Any]) -> dict[str, Any] | None:
  code = row.get("blocked_reason_code")
  if not code:
    return None
  return {"code": code, "message": row.get("blocked_reason_message")}


def _load_error_aggregates(cur: Any, stage_id: str) -> list[dict[str, Any]]:
  cur.execute("""
    WITH failures AS (
      SELECT execution.*
      FROM backtest_experiment_trial_execution execution
      JOIN backtest_experiment_trial trial ON trial.id=execution.trial_id
      WHERE trial.stage_id=%s AND execution.error_fingerprint IS NOT NULL
    ), totals AS (
      SELECT error_fingerprint, count(*) AS failure_count,
        max(terminal_sequence) AS last_terminal_sequence
      FROM failures
      GROUP BY error_fingerprint
    ), latest AS (
      SELECT DISTINCT ON (error_fingerprint)
        error_fingerprint, error_category, error_code, error_summary,
        failure_stage, responsibility_component, retryable
      FROM failures
      ORDER BY error_fingerprint, terminal_sequence DESC
    )
    SELECT totals.*, latest.error_category, latest.error_code,
      latest.error_summary, latest.failure_stage,
      latest.responsibility_component, latest.retryable
    FROM totals
    JOIN latest USING (error_fingerprint)
    ORDER BY totals.last_terminal_sequence DESC
    LIMIT 20
  """, (stage_id,))
  return [{
    "fingerprint": str(row["error_fingerprint"]),
    "category": row["error_category"],
    "code": row["error_code"],
    "message": dict(row["error_summary"] or {}).get("message"),
    "failureStage": row["failure_stage"],
    "component": row["responsibility_component"],
    "retryable": bool(row["retryable"]),
    "count": int(row["failure_count"]),
    "lastTerminalSequence": int(row["last_terminal_sequence"]),
  } for row in cur.fetchall()]


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


def _ensure_user_runtime_quota(cur: Any, owner_id: str) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_user_quota (owner_user_id)
    VALUES (%s) ON CONFLICT (owner_user_id) DO NOTHING
  """, (owner_id,))


def _reserve_plan_for_current_epoch(
  cur: Any,
  owner_id: str,
  planned_count: int,
) -> None:
  if _generation_policy(cur) == "rolling_window_v1":
    _ensure_user_runtime_quota(cur, owner_id)
    return
  _reserve_user_plan(cur, owner_id, planned_count)


def _reserve_user_plan(cur: Any, owner_id: str, planned_count: int) -> None:
  _ensure_user_runtime_quota(cur, owner_id)
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


def _retry_selection_snapshot(
  cur: Any,
  experiment_id: str,
  owner_id: str,
  expected_revision_id: str,
  selection: dict[str, Any],
) -> dict[str, Any]:
  context = _load_retry_context(cur, experiment_id, owner_id, lock=False)
  _validate_retry_revision(context, expected_revision_id)
  trials = _load_retry_trials(cur, context["stage"]["id"], selection, lock=False)
  _require_retryable_trials(trials, selection)
  return {
    **context,
    "trials": trials,
    "selection_digest": _retry_selection_digest(expected_revision_id, trials),
  }


def _load_retry_context(
  cur: Any,
  experiment_id: str,
  owner_id: str,
  *,
  lock: bool,
) -> dict[str, Any]:
  lock_sql = "FOR UPDATE OF e, r, s" if lock else ""
  cur.execute(f"""
    SELECT e.id AS experiment_id, e.current_revision_id, e.status AS experiment_status,
      e.row_version, e.archived_at, e.purge_job_id,
      r.*, s.id AS stage_id, s.status AS stage_status,
      s.in_flight_count, s.blocked_reason_code AS stage_reason_code,
      s.blocked_reason_message AS stage_reason_message
    FROM backtest_experiment e
    JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
    JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
    WHERE e.id=%s AND e.owner_user_id=%s
    {lock_sql}
  """, (experiment_id, owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  value = dict(row)
  return {
    "experiment": {
      "id": value["experiment_id"],
      "current_revision_id": value["current_revision_id"],
      "status": value["experiment_status"],
      "row_version": value["row_version"],
      "archived_at": value["archived_at"],
      "purge_job_id": value["purge_job_id"],
    },
    "revision": {**value, "id": value["current_revision_id"]},
    "stage": {
      **value,
      "id": value["stage_id"],
      "status": value["stage_status"],
      "blocked_reason_code": value["stage_reason_code"],
      "blocked_reason_message": value["stage_reason_message"],
    },
  }


def _validate_retry_revision(
  context: dict[str, Any],
  expected_revision_id: str,
) -> None:
  _require_experiment_write_allowed(context["experiment"])
  if str(context["revision"]["id"]) != expected_revision_id:
    raise ExperimentError("experiment.state_changed", "实验修订已经变化", 409)


def _load_retry_trials(
  cur: Any,
  stage_id: Any,
  selection: dict[str, Any],
  *,
  lock: bool,
) -> list[dict[str, Any]]:
  trial_ids = [str(item) for item in selection.get("trialIds") or []]
  lock_sql = "FOR UPDATE OF trial" if lock else ""
  cur.execute(f"""
    SELECT trial.*, latest.id AS latest_execution_id,
      latest.execution_no AS latest_execution_no,
      latest.terminal_status AS latest_terminal_status,
      latest.error_fingerprint AS latest_error_fingerprint,
      latest.error_summary AS latest_error_summary
    FROM backtest_experiment_trial trial
    LEFT JOIN LATERAL (
      SELECT execution.* FROM backtest_experiment_trial_execution execution
      WHERE execution.trial_id=trial.id
      ORDER BY execution.execution_no DESC
      LIMIT 1
    ) latest ON true
    WHERE trial.stage_id=%s AND trial.id=ANY(%s::uuid[])
    ORDER BY trial.trial_index
    {lock_sql}
  """, (stage_id, trial_ids))
  trials = [dict(row) for row in cur.fetchall()]
  if lock and trials:
    execution_ids = [
      str(row["latest_execution_id"])
      for row in trials
      if row.get("latest_execution_id") is not None
    ]
    if execution_ids:
      cur.execute("""
        SELECT id FROM backtest_experiment_trial_execution
        WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE
      """, (execution_ids,))
  return trials


def _require_retryable_trials(
  trials: list[dict[str, Any]],
  selection: dict[str, Any],
) -> None:
  selected_count = len(selection.get("trialIds") or [])
  eligible = all(
    row.get("summary_status") == "failed"
    and row.get("active_execution_id") is None
    and row.get("latest_execution_id") is not None
    and row.get("latest_terminal_status") == "failed"
    for row in trials
  )
  if len(trials) != selected_count or not eligible:
    raise ExperimentError(
      "experiment.selection_changed",
      "失败 Trial 选择集已经变化，请刷新后重新确认",
      409,
    )


def _retry_selection_digest(revision_id: str, trials: list[dict[str, Any]]) -> str:
  return digest(with_version({
    "operationType": "experiment.retry_selection.v1",
    "revisionId": revision_id,
    "trials": [{
      "trialId": str(row["id"]),
      "trialIndex": int(row["trial_index"]),
      "executionId": str(row["latest_execution_id"]),
      "executionNo": int(row["latest_execution_no"]),
      "errorFingerprint": row.get("latest_error_fingerprint"),
    } for row in trials],
  }))


def _public_retry_selection(snapshot: dict[str, Any]) -> dict[str, Any]:
  trials = snapshot["trials"]
  return {
    "experimentId": str(snapshot["experiment"]["id"]),
    "revisionId": str(snapshot["revision"]["id"]),
    "selectedTrialCount": len(trials),
    "selectionDigest": snapshot["selection_digest"],
    "trials": [{
      "trialId": str(row["id"]),
      "trialIndex": int(row["trial_index"]),
      "previousExecutionId": str(row["latest_execution_id"]),
      "previousExecutionNo": int(row["latest_execution_no"]),
      "errorSummary": row.get("latest_error_summary"),
    } for row in trials],
  }


def _lock_retry_graph(
  cur: Any,
  experiment_id: str,
  owner_id: str,
  expected_revision_id: str,
  selection: dict[str, Any],
) -> dict[str, Any]:
  cur.execute("""
    SELECT s.id AS stage_id, r.generation_policy_version
    FROM backtest_experiment e
    JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
    JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
    WHERE e.id=%s AND e.owner_user_id=%s
  """, (experiment_id, owner_id))
  preflight = cur.fetchone()
  if preflight is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  if preflight["generation_policy_version"] == "rolling_window_v1":
    from backend.experiments.rolling_window import _lock_ledgers

    _lock_ledgers(cur, owner_id, str(preflight["stage_id"]))
  cur.execute("""
    SELECT quota.*, app_user.status AS user_status
    FROM backtest_experiment_user_quota quota
    JOIN app_user ON app_user.id=quota.owner_user_id
    WHERE quota.owner_user_id=%s FOR UPDATE OF quota
  """, (owner_id,))
  quota = cur.fetchone()
  if quota is None:
    raise ExperimentError("experiment.not_found", "实验额度记录不存在", 404)
  context = _load_retry_context(cur, experiment_id, owner_id, lock=True)
  _validate_retry_revision(context, expected_revision_id)
  trials = _load_retry_trials(cur, context["stage"]["id"], selection, lock=True)
  _require_retryable_trials(trials, selection)
  cur.execute("""
    SELECT count(*) AS value FROM backtest_experiment_asset_hold
    WHERE revision_id=%s AND hold_status <> 'held'
  """, (context["revision"]["id"],))
  unavailable_assets = int(cur.fetchone()["value"])
  return {
    **context,
    "quota": _sync_manual_reserved_permits(cur, dict(quota)),
    "trials": trials,
    "unavailable_assets": unavailable_assets,
    "selection_digest": _retry_selection_digest(expected_revision_id, trials),
  }


def _validate_retry_selection(
  graph: dict[str, Any],
  expected_count: int,
  expected_digest: str,
) -> None:
  if (
    len(graph["trials"]) != expected_count
    or graph["selection_digest"] != expected_digest
  ):
    raise ExperimentError(
      "experiment.selection_changed",
      "失败 Trial 选择集已经变化，请刷新后重新确认",
      409,
    )


def _prepare_retry_revision(cur: Any, graph: dict[str, Any]) -> None:
  revision = graph["revision"]
  stage = graph["stage"]
  if revision.get("cancel_requested_at") is not None:
    raise ExperimentError("experiment.command_invalid", "已取消修订不能重试", 409)
  if revision.get("system_block_code") or stage.get("blocked_reason_code"):
    raise ExperimentError(
      "experiment.command_invalid", "请先解除当前执行阻塞再重试失败项", 409,
    )
  if graph["quota"].get("user_status") != "active" or graph["unavailable_assets"]:
    raise ExperimentError("experiment.retry_validation_failed", "当前权限或冻结资产不可用", 409)
  if (
    revision.get("generation_policy_version") == "legacy"
    and revision.get("terminal_at") is not None
  ):
    planned = int(revision["planned_trial_count"])
    _reserve_user_plan(cur, str(revision["owner_user_id"]), planned)
    graph["quota"]["reserved_planned_trial_count"] += planned
  _mark_revision_retrying(cur, graph)


def _mark_revision_retrying(cur: Any, graph: dict[str, Any]) -> None:
  revision = graph["revision"]
  stage = graph["stage"]
  cur.execute("""
    UPDATE backtest_experiment_revision
    SET status='running', terminal_at=NULL,
      lifecycle_state='active', terminal_outcome=NULL,
      lifecycle_version=lifecycle_version+1,
      plan_quota_released_at=CASE
        WHEN generation_policy_version='legacy' THEN NULL
        ELSE plan_quota_released_at END,
      updated_at=now()
    WHERE id=%s
  """, (revision["id"],))
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET status='running', updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (stage["id"],))
  cur.execute("""
    UPDATE backtest_experiment
    SET status='running', updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (graph["experiment"]["id"],))
  revision["terminal_at"] = None
  stage["status"] = "running"


def _validate_retry_capacity(
  cur: Any,
  graph: dict[str, Any],
  selected_count: int,
) -> None:
  quota = graph["quota"]
  stage = graph["stage"]
  effective_capacity, global_in_flight = _capacity_snapshot(cur)
  if graph["revision"].get("generation_policy_version") == "legacy":
    budget_gate = _budget_capacity_gate(graph, effective_capacity)
    if not budget_gate.allowed:
      raise ExperimentError(
        budget_gate.code or "experiment.capacity_blocked",
        budget_gate.message or "当前容量不足",
        409,
        retryable=True,
      )
  manual_gate = _manual_capacity_gate(cur, graph)
  if not manual_gate.allowed:
    raise ExperimentError(
      manual_gate.code or "experiment.capacity_blocked",
      manual_gate.message or "当前容量不足",
      409,
      retryable=True,
    )
  global_limit = max(0, effective_capacity - settings.EXPERIMENT_MANUAL_RESERVED_PERMITS)
  remaining = min(
    settings.EXPERIMENT_MAX_IN_FLIGHT_PER_USER - int(quota["experiment_in_flight_count"]),
    settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT - int(stage["in_flight_count"]),
    global_limit - global_in_flight,
  )
  if selected_count > max(0, remaining):
    raise ExperimentError(
      "experiment.capacity_blocked",
      "所选失败项超过当前可用在途容量，请减少选择或稍后重试",
      409,
      retryable=True,
      details={"selectedTrialCount": selected_count, "availableTrialCount": max(0, remaining)},
    )


def _capacity_snapshot(cur: Any) -> tuple[int, int]:
  cur.execute(
    "SELECT * FROM app.experiment_capacity_snapshot(%s)",
    (settings.BACKTEST_BASE_CONCURRENCY,),
  )
  row = cur.fetchone() or {}
  return (
    int(row.get("effective_limit") or 0),
    int(row.get("experiment_in_flight_count") or 0),
  )


def _create_manual_retry_executions(
  cur: Any,
  graph: dict[str, Any],
  compiler: TrialCompiler,
) -> list[dict[str, Any]]:
  executions: list[dict[str, Any]] = []
  for trial in graph["trials"]:
    if graph["revision"].get("generation_policy_version") == "rolling_window_v1":
      from backend.experiments.rolling_window import reopen_trial_in_transaction

      decision = reopen_trial_in_transaction(
        cur, str(trial["id"]),
        f"manual-retry:{graph['selection_digest']}:{trial['id']}",
      )
      if not decision.allowed:
        raise ExperimentError(
          "experiment.capacity_blocked",
          "滚动窗口暂无重试容量，请稍后重试",
          409,
          retryable=True,
        )
    locked = {**graph, "trial": trial}
    template, assignment, dataset, seed = _materialization_inputs(locked)
    payload = compiler.compile_request(template, assignment, dataset, seed)
    _verify_compiled_request(compiler, template, assignment, locked)
    executions.append(_insert_trial_execution(
      cur, locked, payload, created_reason_override="manual_retry",
    ))
  _rebuild_aggregate_status(cur, str(graph["stage"]["id"]))
  return executions


def _retry_response(
  graph: dict[str, Any],
  executions: list[dict[str, Any]],
  selection_digest: str,
) -> dict[str, Any]:
  return {
    "experimentId": str(graph["experiment"]["id"]),
    "revisionId": str(graph["revision"]["id"]),
    "status": "running",
    "operationId": str(uuid4()),
    "selectedTrialCount": len(executions),
    "selectionDigest": selection_digest,
    "executions": executions,
    "replayed": False,
    "acceptedAt": datetime.now(timezone.utc).isoformat(),
  }


def _audit_manual_retry(
  cur: Any,
  graph: dict[str, Any],
  reason: str,
  request_id: str | None,
  response: dict[str, Any],
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id,
      event_type, request_id, detail
    ) VALUES (%s,%s,%s,%s,'experiment.failed_trials_retried',%s,%s)
  """, (
    graph["revision"]["owner_user_id"], graph["experiment"]["id"],
    graph["revision"]["id"], graph["stage"]["id"], request_id,
    Jsonb({
      "reason": reason,
      "selectedTrialCount": response["selectedTrialCount"],
      "selectionDigest": response["selectionDigest"],
      "operationId": response["operationId"],
    }),
  ))


def _lock_revision_creation_graph(
  cur: Any,
  experiment_id: str,
  owner_id: str,
) -> dict[str, Any]:
  cur.execute("""
    SELECT * FROM backtest_experiment
    WHERE id=%s AND owner_user_id=%s
    FOR UPDATE
  """, (experiment_id, owner_id))
  experiment = cur.fetchone()
  if experiment is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  cur.execute(
    "SELECT * FROM backtest_experiment_revision WHERE id=%s FOR UPDATE",
    (experiment["current_revision_id"],),
  )
  revision = cur.fetchone()
  cur.execute("""
    SELECT * FROM backtest_experiment_stage
    WHERE revision_id=%s AND stage_index=0 FOR UPDATE
  """, (experiment["current_revision_id"],))
  stage = cur.fetchone()
  if revision is None or stage is None:
    raise ExperimentError("experiment.state_changed", "实验修订结构不完整", 409)
  return {
    "experiment": dict(experiment),
    "revision": dict(revision),
    "stage": dict(stage),
  }


def _validate_revision_creation(
  graph: dict[str, Any],
  expected_revision_id: str,
  expected_snapshot_version: int,
) -> None:
  experiment = graph["experiment"]
  revision = graph["revision"]
  _require_experiment_write_allowed(experiment)
  if (
    str(revision["id"]) != expected_revision_id
    or int(experiment["row_version"]) != expected_snapshot_version
  ):
    raise ExperimentError("experiment.state_changed", "实验状态已经变化，请刷新后重试", 409)
  if revision.get("terminal_at") is None:
    raise ExperimentError(
      "experiment.active_revision_exists", "当前修订尚未终结，不能创建新修订", 409,
    )


def _insert_new_revision(
  cur: Any,
  graph: dict[str, Any],
  normalized_spec: dict[str, Any],
  preview: dict[str, Any],
  request_id: str | None,
) -> dict[str, Any]:
  experiment = graph["experiment"]
  revision_no = int(graph["revision"]["revision_no"]) + 1
  revision_id = str(uuid4())
  stage_id = str(uuid4())
  stage_spec = dict(normalized_spec["plan"]["stages"][0])
  _insert_revision(
    cur, revision_id, str(experiment["id"]), str(experiment["owner_user_id"]),
    revision_no, normalized_spec, preview,
  )
  _insert_stage(
    cur, stage_id, revision_id, str(experiment["owner_user_id"]),
    stage_spec, preview,
  )
  _insert_asset_holds(
    cur, revision_id, str(experiment["owner_user_id"]), normalized_spec,
  )
  _activate_new_revision(cur, graph, revision_id, normalized_spec)
  _audit_revision_created(cur, graph, revision_id, stage_id, preview, request_id)
  return _revision_created_response(
    cur, str(experiment["id"]), revision_id, stage_id, revision_no, preview,
  )


def _activate_new_revision(
  cur: Any,
  graph: dict[str, Any],
  revision_id: str,
  normalized_spec: dict[str, Any],
) -> None:
  cur.execute("""
    UPDATE backtest_experiment
    SET current_revision_id=%s, name=%s, description=%s, status='preparing',
      blocked_reason_code=NULL, blocked_reason_message=NULL,
      row_version=row_version+1, updated_at=now()
    WHERE id=%s AND current_revision_id=%s
  """, (
    revision_id, normalized_spec["name"], normalized_spec["description"],
    graph["experiment"]["id"], graph["revision"]["id"],
  ))
  if cur.rowcount != 1:
    raise ExperimentError("experiment.state_changed", "实验状态已经变化", 409)


def _audit_revision_created(
  cur: Any,
  graph: dict[str, Any],
  revision_id: str,
  stage_id: str,
  preview: dict[str, Any],
  request_id: str | None,
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id,
      event_type, request_id, detail
    ) VALUES (%s,%s,%s,%s,'experiment.revision_created',%s,%s)
  """, (
    graph["experiment"]["owner_user_id"], graph["experiment"]["id"],
    revision_id, stage_id, request_id,
    Jsonb({"plannedTrialCount": int(preview["planned_trial_count"])}),
  ))


def _revision_created_response(
  cur: Any,
  experiment_id: str,
  revision_id: str,
  stage_id: str,
  revision_no: int,
  preview: dict[str, Any],
) -> dict[str, Any]:
  cur.execute(
    "SELECT created_at FROM backtest_experiment_revision WHERE id=%s",
    (revision_id,),
  )
  created_at = cur.fetchone()["created_at"]
  return {
    "experimentId": experiment_id,
    "revisionId": revision_id,
    "revisionNo": revision_no,
    "stageId": stage_id,
    "status": "preparing",
    "plannedTrialCount": int(preview["planned_trial_count"]),
    "generatedTrialCount": 0,
    "specHash": str(preview["spec_hash"]),
    "replayed": False,
    "createdAt": created_at.isoformat(),
  }


def _load_experiment_source(
  cur: Any,
  owner_id: str,
  source: dict[str, str] | None,
) -> dict[str, Any] | None:
  if source is None:
    return None
  cur.execute("""
    SELECT e.id AS experiment_id, e.name, r.id AS revision_id,
      r.revision_no
    FROM backtest_experiment e
    JOIN backtest_experiment_revision r ON r.experiment_id=e.id
    WHERE e.id=%s AND r.id=%s AND e.owner_user_id=%s
  """, (source["experimentId"], source["revisionId"], owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError(
      "experiment.source_not_found", "来源实验不存在或不可访问", 404,
    )
  return dict(row)


def _insert_experiment_graph(
  cur: Any,
  *,
  owner_id: str,
  normalized_spec: dict[str, Any],
  preview: dict[str, Any],
  source: dict[str, Any] | None,
  request_id: str | None,
) -> dict[str, Any]:
  experiment_id = str(uuid4())
  revision_id = str(uuid4())
  stage_id = str(uuid4())
  stage_spec = dict(normalized_spec["plan"]["stages"][0])
  cur.execute("""
    INSERT INTO backtest_experiment (
      id, owner_user_id, name, description, source_revision_id, status
    ) VALUES (%s,%s,%s,%s,%s,'preparing')
    RETURNING created_at
  """, (
    experiment_id, owner_id, normalized_spec["name"], normalized_spec["description"],
    source["revision_id"] if source else None,
  ))
  created_at = cur.fetchone()["created_at"]
  _insert_revision(
    cur, revision_id, experiment_id, owner_id, 1, normalized_spec, preview,
  )
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
    Jsonb({
      "plannedTrialCount": preview["planned_trial_count"],
      "sourceExperimentId": str(source["experiment_id"]) if source else None,
      "sourceRevisionId": str(source["revision_id"]) if source else None,
    }),
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
  revision_no: int,
  normalized_spec: dict[str, Any],
  preview: dict[str, Any],
) -> None:
  policy = _generation_policy(cur)
  resource_policy = (
    {"admissionPolicy": normalized_spec["admissionPolicy"]}
    if policy == "rolling_window_v1"
    else {"capacityPolicy": normalized_spec["capacityPolicy"]}
  )
  cur.execute("""
    INSERT INTO backtest_experiment_revision (
      id, experiment_id, owner_user_id, revision_no, experiment_spec,
      spec_hash, run_template_digest, dataset_snapshot_digest,
      canonicalization_version, budget_version, resource_budget,
      determinism, planned_trial_count, failure_policy, status,
      generation_policy_version
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'preparing',%s)
  """, (
    revision_id, experiment_id, owner_id, revision_no, Jsonb(normalized_spec),
    preview["spec_hash"], preview["run_template_digest"],
    preview["dataset_snapshot_digest"], normalized_spec["canonicalizationVersion"],
    preview["budget_version"], Jsonb({
      "estimate": normalized_spec["resourceEstimate"],
      **resource_policy,
    }),
    Jsonb(normalized_spec["determinism"]), preview["planned_trial_count"],
    Jsonb(normalized_spec["failurePolicy"]), policy,
  ))


def _generation_policy(cur: Any) -> str:
  cur.execute("""
    SELECT contract_epoch FROM backtest_experiment_rollout_state WHERE singleton
  """)
  row = cur.fetchone()
  if row is None:
    raise state_store.DatabaseError("缺少自动化实验语义纪元单例行")
  return (
    "rolling_window_v1"
    if row["contract_epoch"] == "rolling_window_v1"
    else "legacy"
  )


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
      "scope": row.get("blocked_scope"),
      "blockedAt": row["blocked_at"].isoformat() if row.get("blocked_at") else None,
    }
  spec = dict(row["experiment_spec"])
  return {
    "experimentId": str(row["experiment_id"]),
    "name": row["name"],
    "description": row["description"],
    "source": _experiment_source_response(row),
    "status": row["status"],
    "lifecycleState": row.get("lifecycle_state", "active"),
    "lifecycleVersion": int(row.get("lifecycle_version") or 1),
    "lifecycleCommandsAvailable": row.get("contract_epoch") in {
      "lifecycle_v2", "evaluation_v2", "rolling_window_v1",
    },
    "executionPhase": row.get("execution_phase", "preparing"),
    "systemBlockCode": _project_system_block(row),
    "terminalOutcome": row.get("terminal_outcome"),
    "snapshotVersion": int(row["row_version"]),
    "relatedBacktestRuns": {
      "totalCount": int(row.get("related_run_count") or 0),
      "initialCount": int(row.get("related_initial_run_count") or 0),
      "retryCount": int(row.get("related_retry_run_count") or 0),
    },
    "archivedAt": row["archived_at"].isoformat() if row.get("archived_at") else None,
    "retentionUntil": (
      row["archive_retention_until"].isoformat()
      if row.get("archive_retention_until") else None
    ),
    "purgeJobId": str(row["purge_job_id"]) if row.get("purge_job_id") else None,
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
      "readyTrialCount": int(row.get("ready_count") or 0),
      "queuedTrialCount": int(row.get("queued_count") or 0),
      "remainingLogicalTrialCount": max(
        0, int(row["planned_trial_count"]) - int(row["generated_trial_count"]),
      ),
    },
    "resourceBudget": row["resource_budget"],
    "failurePolicy": row["failure_policy"],
    "errorAggregates": row.get("error_aggregates") or [],
    "consecutiveFailureCount": int(row.get("consecutive_failure_count") or 0),
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


def _experiment_source_response(row: dict[str, Any]) -> dict[str, Any] | None:
  if not row.get("source_revision_id"):
    return None
  return {
    "experimentId": str(row["source_experiment_id"]),
    "experimentName": row["source_experiment_name"],
    "revisionId": str(row["source_revision_id"]),
    "revisionNo": int(row["source_revision_no"]),
  }


def _public_dataset_snapshot(value: Any) -> dict[str, Any]:
  snapshot = dict(value) if isinstance(value, dict) else {}
  research = snapshot.get("researchSignalSnapshot")
  if isinstance(research, dict):
    snapshot["researchSignalSnapshot"] = {
      key: item for key, item in research.items() if key != "artifactFile"
    }
  return snapshot


def _lock_experiment_for_write(
  cur: Any,
  experiment_id: str,
  owner_id: str,
) -> dict[str, Any]:
  cur.execute("""
    SELECT id, archived_at, purge_job_id
    FROM backtest_experiment
    WHERE id=%s AND owner_user_id=%s
    FOR UPDATE
  """, (experiment_id, owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  experiment = dict(row)
  _require_experiment_write_allowed(experiment)
  return experiment


def _require_experiment_write_allowed(experiment: dict[str, Any]) -> None:
  if experiment.get("purge_job_id") is not None:
    raise ExperimentError("experiment.purge_in_progress", "实验已经进入清理流程", 409)
  if experiment.get("archived_at") is not None:
    raise ExperimentError("experiment.archived_read_only", "归档实验只允许读取或恢复", 409)


def _lock_lifecycle_experiment(
  cur: Any,
  experiment_id: str,
  owner_id: str,
) -> dict[str, Any]:
  cur.execute("""
    SELECT e.id, e.owner_user_id, e.status, e.current_revision_id,
      e.archived_at, e.archive_retention_until, e.purge_job_id,
      e.row_version, r.terminal_at
    FROM backtest_experiment e
    JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
    WHERE e.id=%s AND e.owner_user_id=%s
    FOR UPDATE OF e, r
  """, (experiment_id, owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  return dict(row)


def _archive_experiment(
  cur: Any,
  graph: dict[str, Any],
  expected_revision_id: str,
  expected_snapshot_version: int,
  reason: str,
  request_id: str | None,
) -> dict[str, Any]:
  if str(graph["current_revision_id"]) != expected_revision_id:
    raise ExperimentError("experiment.state_changed", "实验当前修订已变化", 409)
  if int(graph["row_version"]) != expected_snapshot_version:
    raise ExperimentError("experiment.state_changed", "实验状态已变化", 409)
  if graph["terminal_at"] is None or graph["status"] not in {
    "completed", "partially_completed", "failed", "cancelled",
  }:
    raise ExperimentError(
      "experiment.archive_requires_terminal", "只有终态实验可以归档", 409,
    )
  if graph["archived_at"] is not None:
    raise ExperimentError("experiment.already_archived", "实验已经归档", 409)
  if graph["purge_job_id"] is not None:
    raise ExperimentError("experiment.purge_in_progress", "实验正在清理", 409)
  cur.execute("""
    UPDATE backtest_experiment
    SET archived_at=now(),
      archive_retention_until=now() + (%s * interval '1 day'),
      row_version=row_version + 1, updated_at=now()
    WHERE id=%s
    RETURNING archived_at, archive_retention_until, row_version
  """, (settings.EXPERIMENT_PURGE_RETENTION_DAYS, graph["id"]))
  updated = cur.fetchone()
  operation_id = str(uuid4())
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, event_type, request_id, detail
    ) VALUES (%s,%s,%s,'experiment.archived',%s,%s)
  """, (
    graph["owner_user_id"], graph["id"], graph["current_revision_id"], request_id,
    Jsonb({
      "reason": reason,
      "operationId": operation_id,
      "retentionUntil": updated["archive_retention_until"].isoformat(),
    }),
  ))
  return {
    "experimentId": str(graph["id"]),
    "revisionId": str(graph["current_revision_id"]),
    "status": graph["status"],
    "snapshotVersion": int(updated["row_version"]),
    "archivedAt": updated["archived_at"].isoformat(),
    "retentionUntil": updated["archive_retention_until"].isoformat(),
    "reason": reason,
    "operationId": operation_id,
    "replayed": False,
    "acceptedAt": updated["archived_at"].isoformat(),
  }


def _restore_archived_experiment(
  cur: Any,
  graph: dict[str, Any],
  expected_revision_id: str,
  expected_snapshot_version: int,
  reason: str,
  request_id: str | None,
) -> dict[str, Any]:
  if str(graph["current_revision_id"]) != expected_revision_id:
    raise ExperimentError("experiment.state_changed", "实验当前修订已变化", 409)
  if int(graph["row_version"]) != expected_snapshot_version:
    raise ExperimentError("experiment.state_changed", "实验状态已变化", 409)
  if graph["archived_at"] is None:
    raise ExperimentError("experiment.not_archived", "实验尚未归档", 409)
  if graph["purge_job_id"] is not None:
    raise ExperimentError("experiment.purge_in_progress", "实验正在清理", 409)
  retention_until = graph["archive_retention_until"]
  if retention_until is None or retention_until <= datetime.now(timezone.utc):
    raise ExperimentError(
      "experiment.archive_retention_expired", "实验归档保留期已结束", 409,
    )
  cur.execute("""
    UPDATE backtest_experiment
    SET archived_at=NULL, archive_retention_until=NULL,
      row_version=row_version + 1, updated_at=now()
    WHERE id=%s
    RETURNING row_version, updated_at AS accepted_at
  """, (graph["id"],))
  updated = cur.fetchone()
  operation_id = str(uuid4())
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, event_type, request_id, detail
    ) VALUES (%s,%s,%s,'experiment.archive_restored',%s,%s)
  """, (
    graph["owner_user_id"], graph["id"], graph["current_revision_id"], request_id,
    Jsonb({"reason": reason, "operationId": operation_id}),
  ))
  return {
    "experimentId": str(graph["id"]),
    "revisionId": str(graph["current_revision_id"]),
    "status": graph["status"],
    "snapshotVersion": int(updated["row_version"]),
    "archivedAt": None,
    "retentionUntil": None,
    "reason": reason,
    "operationId": operation_id,
    "replayed": False,
    "acceptedAt": updated["accepted_at"].isoformat(),
  }


def _lock_admin_purge_experiment(cur: Any, experiment_id: str) -> dict[str, Any]:
  cur.execute("""
    SELECT e.id, e.owner_user_id, e.status, e.current_revision_id,
      e.archived_at, e.archive_retention_until, e.purge_job_id,
      e.row_version, r.terminal_at
    FROM backtest_experiment e
    JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
    WHERE e.id=%s
    FOR UPDATE OF e, r
  """, (experiment_id,))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  return dict(row)


def _validate_purge_request(
  cur: Any,
  graph: dict[str, Any],
  expected_revision_id: str,
  expected_snapshot_version: int,
) -> None:
  if str(graph["current_revision_id"]) != expected_revision_id:
    raise ExperimentError("experiment.state_changed", "实验当前修订已变化", 409)
  if int(graph["row_version"]) != expected_snapshot_version:
    raise ExperimentError("experiment.state_changed", "实验状态已变化", 409)
  if graph["terminal_at"] is None or graph["status"] not in {
    "completed", "partially_completed", "failed", "cancelled",
  }:
    raise ExperimentError("experiment.purge_requires_terminal", "只有终态实验可以清理", 409)
  if graph["archived_at"] is None:
    raise ExperimentError("experiment.purge_requires_archive", "请先归档实验", 409)
  if graph["purge_job_id"] is not None:
    raise ExperimentError("experiment.purge_in_progress", "实验已经进入清理流程", 409)
  retention_until = graph["archive_retention_until"]
  if retention_until is None or retention_until > datetime.now(timezone.utc):
    raise ExperimentError("experiment.purge_retention_active", "实验仍在归档保留期内", 409)
  _ensure_purge_dependencies_terminal(cur, str(graph["id"]))


def _ensure_purge_dependencies_terminal(cur: Any, experiment_id: str) -> None:
  cur.execute("""
    SELECT EXISTS (
      SELECT 1 FROM backtest_experiment_evaluation_run
      WHERE experiment_id=%s AND status IN ('queued','running')
      UNION ALL
      SELECT 1 FROM backtest_experiment_evaluation_export
      WHERE experiment_id=%s AND status IN ('queued','running','cancel_requested')
    ) AS active
  """, (experiment_id, experiment_id))
  if bool(cur.fetchone()["active"]):
    raise ExperimentError(
      "experiment.purge_dependency_active", "实验仍有未终结的评价或导出作业", 409,
    )


def _create_purge_job(
  cur: Any,
  graph: dict[str, Any],
  actor_id: str,
  reason: str,
  request_id: str | None,
) -> dict[str, Any]:
  purge_id = str(uuid4())
  cur.execute("""
    INSERT INTO backtest_experiment_purge (
      id, experiment_id, revision_id, owner_user_id, requested_by_user_id,
      request_id, reason, scope_digest, scope_counts, old_value, new_value
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    RETURNING created_at
  """, (
    purge_id, graph["id"], graph["current_revision_id"], graph["owner_user_id"],
    actor_id, request_id, reason, "0" * 64, Jsonb({}),
    Jsonb(_purge_old_value(graph)),
    Jsonb({"purgeStatus": "queued", "phase": "artifacts"}),
  ))
  accepted_at = cur.fetchone()["created_at"]
  _audit_purge_requested(cur, graph, actor_id, reason, request_id, purge_id)
  scope_counts, scope_digest = _freeze_purge_scope(
    cur, purge_id, str(graph["id"]), str(graph["owner_user_id"]),
  )
  cur.execute("""
    UPDATE backtest_experiment_purge
    SET scope_counts=%s, scope_digest=%s, updated_at=now() WHERE id=%s
  """, (Jsonb(scope_counts), scope_digest, purge_id))
  snapshot_version = _bind_purge_job(cur, purge_id, str(graph["id"]))
  return {
    "purgeId": purge_id, "experimentId": str(graph["id"]),
    "revisionId": str(graph["current_revision_id"]), "status": "queued",
    "phase": "artifacts", "scopeDigest": scope_digest,
    "scopeCounts": scope_counts, "snapshotVersion": snapshot_version,
    "reason": reason, "operationId": purge_id,
    "replayed": False,
    "acceptedAt": accepted_at.isoformat(),
  }


def _purge_old_value(graph: dict[str, Any]) -> dict[str, Any]:
  return {
    "experimentStatus": graph["status"],
    "archivedAt": graph["archived_at"].isoformat(),
    "retentionUntil": graph["archive_retention_until"].isoformat(),
    "snapshotVersion": int(graph["row_version"]),
    "purgeJobId": None,
  }


def _bind_purge_job(cur: Any, purge_id: str, experiment_id: str) -> int:
  cur.execute("""
    UPDATE backtest_experiment
    SET purge_job_id=%s, row_version=row_version + 1, updated_at=now()
    WHERE id=%s RETURNING row_version
  """, (purge_id, experiment_id))
  snapshot_version = int(cur.fetchone()["row_version"])
  cur.execute("""
    UPDATE backtest_experiment_purge
    SET new_value=%s, updated_at=now() WHERE id=%s
  """, (
    Jsonb({
      "purgeStatus": "queued", "phase": "artifacts",
      "snapshotVersion": snapshot_version,
    }),
    purge_id,
  ))
  return snapshot_version


def _audit_purge_requested(
  cur: Any,
  graph: dict[str, Any],
  actor_id: str,
  reason: str,
  request_id: str | None,
  purge_id: str,
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, event_type, request_id, detail
    ) VALUES (%s,%s,%s,'experiment.purge_requested',%s,%s)
  """, (
    graph["owner_user_id"], graph["id"], graph["current_revision_id"], request_id,
    Jsonb({"purgeId": purge_id, "requestedBy": actor_id, "reason": reason}),
  ))


def _freeze_purge_scope(
  cur: Any,
  purge_id: str,
  experiment_id: str,
  owner_id: str,
) -> tuple[dict[str, int], str]:
  _insert_purge_graph_items(cur, purge_id, experiment_id, owner_id)
  cur.execute("""
    SELECT resource_type, resource_id
    FROM backtest_experiment_purge_item
    WHERE purge_id=%s ORDER BY resource_type, resource_id
  """, (purge_id,))
  items = [(str(row["resource_type"]), str(row["resource_id"])) for row in cur]
  counts: dict[str, int] = {}
  for resource_type, _resource_id in items:
    counts[resource_type] = counts.get(resource_type, 0) + 1
  return counts, digest(with_version({"resources": items}))


def _insert_purge_graph_items(
  cur: Any,
  purge_id: str,
  experiment_id: str,
  owner_id: str,
) -> None:
  queries = _purge_scope_queries()
  for resource_type, query in queries:
    _insert_purge_items(
      cur, purge_id, owner_id, resource_type, query, (experiment_id,),
    )
  _insert_purge_items(
    cur, purge_id, owner_id, "idempotency", """
      SELECT id AS resource_id,
        jsonb_build_object('operationType', operation_type) AS resource_snapshot
      FROM backtest_internal_idempotency
      WHERE resource_id IN (
        SELECT resource_id FROM backtest_experiment_purge_item
        WHERE purge_id=%s
      )
    """, (purge_id,),
  )


def _purge_scope_queries() -> tuple[tuple[str, str], ...]:
  revision_scope = """
    SELECT id FROM backtest_experiment_revision WHERE experiment_id=%s
  """
  stage_scope = f"""
    SELECT id FROM backtest_experiment_stage
    WHERE revision_id IN ({revision_scope})
  """
  trial_scope = f"""
    SELECT id FROM backtest_experiment_trial WHERE stage_id IN ({stage_scope})
  """
  execution_scope = f"""
    SELECT id FROM backtest_experiment_trial_execution
    WHERE trial_id IN ({trial_scope})
  """
  return (
    *_purge_evaluation_scope_queries(),
    _purge_audit_scope_query(),
    *_purge_execution_scope_queries(
      revision_scope, stage_scope, trial_scope, execution_scope,
    ),
  )


def _purge_evaluation_scope_queries() -> tuple[tuple[str, str], ...]:
  return (
    ("export_artifact", """
      SELECT id AS resource_id,
        jsonb_build_object('storageKey', storage_key) AS resource_snapshot
      FROM backtest_experiment_evaluation_export
      WHERE experiment_id=%s AND storage_key IS NOT NULL
    """),
    ("export", """
      SELECT id AS resource_id, '{}'::jsonb AS resource_snapshot
      FROM backtest_experiment_evaluation_export WHERE experiment_id=%s
    """),
    ("evaluation_run", """
      SELECT id AS resource_id, '{}'::jsonb AS resource_snapshot
      FROM backtest_experiment_evaluation_run WHERE experiment_id=%s
    """),
    ("evaluation_spec", """
      SELECT id AS resource_id, '{}'::jsonb AS resource_snapshot
      FROM backtest_experiment_evaluation_spec WHERE experiment_id=%s
    """),
  )


def _purge_audit_scope_query() -> tuple[str, str]:
  return ("audit", """
    WITH target AS (SELECT %s::uuid AS experiment_id)
    SELECT audit.id AS resource_id, '{}'::jsonb AS resource_snapshot
    FROM backtest_experiment_audit audit CROSS JOIN target
    WHERE audit.experiment_id=target.experiment_id
      OR audit.revision_id IN (
        SELECT id FROM backtest_experiment_revision
        WHERE experiment_id=target.experiment_id
      )
      OR audit.stage_id IN (
        SELECT stage.id FROM backtest_experiment_stage stage
        JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
        WHERE revision.experiment_id=target.experiment_id
      )
      OR audit.trial_id IN (
        SELECT trial.id FROM backtest_experiment_trial trial
        JOIN backtest_experiment_stage stage ON stage.id=trial.stage_id
        JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
        WHERE revision.experiment_id=target.experiment_id
      )
      OR audit.execution_id IN (
        SELECT execution.id FROM backtest_experiment_trial_execution execution
        JOIN backtest_experiment_trial trial ON trial.id=execution.trial_id
        JOIN backtest_experiment_stage stage ON stage.id=trial.stage_id
        JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
        WHERE revision.experiment_id=target.experiment_id
      )
  """)


def _purge_execution_scope_queries(
  revision_scope: str,
  stage_scope: str,
  trial_scope: str,
  execution_scope: str,
) -> tuple[tuple[str, str], ...]:
  run_scope = f"""
    SELECT run_id FROM backtest_experiment_trial_execution
    WHERE id IN ({execution_scope})
  """
  return (
    ("outbox", f"""
      SELECT id AS resource_id, '{{}}'::jsonb AS resource_snapshot
      FROM backtest_experiment_dispatch_outbox
      WHERE execution_id IN ({execution_scope})
    """),
    ("execution", f"""
      SELECT id AS resource_id, '{{}}'::jsonb AS resource_snapshot
      FROM backtest_experiment_trial_execution WHERE id IN ({execution_scope})
    """),
    ("run", f"""
      SELECT run_id AS resource_id, '{{}}'::jsonb AS resource_snapshot
      FROM backtest_experiment_trial_execution WHERE id IN ({execution_scope})
    """),
    ("run_artifact", f"""
      WITH target_runs AS ({run_scope})
      SELECT DISTINCT artifact.id AS resource_id,
        jsonb_build_object('storageKey', artifact.storage_key) AS resource_snapshot
      FROM artifact_file artifact
      WHERE artifact.run_id IN (SELECT run_id FROM target_runs)
        OR artifact.id IN (
          SELECT snapshot.artifact_file_id
          FROM research_signal_snapshot snapshot
          WHERE snapshot.run_id IN (SELECT run_id FROM target_runs)
        )
    """),
    ("asset_hold", f"""
      SELECT id AS resource_id, '{{}}'::jsonb AS resource_snapshot
      FROM backtest_experiment_asset_hold
      WHERE revision_id IN ({revision_scope})
    """),
    ("trial", f"""
      SELECT id AS resource_id, '{{}}'::jsonb AS resource_snapshot
      FROM backtest_experiment_trial WHERE id IN ({trial_scope})
    """),
    ("stage", f"""
      SELECT id AS resource_id, '{{}}'::jsonb AS resource_snapshot
      FROM backtest_experiment_stage WHERE id IN ({stage_scope})
    """),
    ("revision", """
      SELECT id AS resource_id, '{}'::jsonb AS resource_snapshot
      FROM backtest_experiment_revision WHERE experiment_id=%s
    """),
    ("experiment", """
      SELECT id AS resource_id, '{}'::jsonb AS resource_snapshot
      FROM backtest_experiment WHERE id=%s
    """),
  )


def _insert_purge_items(
  cur: Any,
  purge_id: str,
  owner_id: str,
  resource_type: str,
  query: str,
  params: tuple[Any, ...],
) -> None:
  cur.execute(f"""
    INSERT INTO backtest_experiment_purge_item (
      purge_id, owner_user_id, resource_type, resource_id,
      ordinal, resource_snapshot
    )
    SELECT %s, %s, %s, resource_id,
      row_number() OVER (ORDER BY resource_id) - 1, resource_snapshot
    FROM ({query}) scope
    ON CONFLICT DO NOTHING
  """, (purge_id, owner_id, resource_type, *params))


def _load_current_stage_for_update(
  cur: Any,
  experiment_id: str,
  owner_id: str,
) -> dict[str, Any] | None:
  cur.execute("""
    SELECT q.*, u.status AS user_status
    FROM backtest_experiment_user_quota q
    JOIN app_user u ON u.id=q.owner_user_id
    WHERE q.owner_user_id=%s
    FOR UPDATE OF q
  """, (owner_id,))
  quota = cur.fetchone()
  if quota is None:
    return None
  cur.execute("""
    SELECT e.id AS experiment_id, e.status AS experiment_status,
      e.current_revision_id, e.blocked_reason_code AS experiment_reason_code,
      e.blocked_reason_message AS experiment_reason_message,
      e.archived_at, e.purge_job_id,
      r.*, s.id AS stage_id, s.status AS stage_status,
      s.generated_trial_count, s.planned_trial_count AS stage_planned_trial_count,
      s.cancelled_before_generation, s.in_flight_count,
      s.blocked_reason_code AS stage_reason_code,
      s.blocked_reason_message AS stage_reason_message,
      s.next_terminal_sequence, s.blocked_scope
    FROM backtest_experiment e
    JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
    JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
    WHERE e.id=%s AND e.owner_user_id=%s
    FOR UPDATE OF e, r, s
  """, (experiment_id, owner_id))
  row = cur.fetchone()
  if row is None:
    return None
  value = dict(row)
  _require_experiment_write_allowed(value)
  cur.execute("""
    SELECT count(*) AS value
    FROM backtest_experiment_asset_hold
    WHERE revision_id=%s AND hold_status <> 'held'
  """, (value["current_revision_id"],))
  return {
    "quota": dict(quota),
    "experiment": {
      "id": value["experiment_id"],
      "status": value["experiment_status"],
      "blocked_reason_code": value["experiment_reason_code"],
      "blocked_reason_message": value["experiment_reason_message"],
      "archived_at": value["archived_at"],
      "purge_job_id": value["purge_job_id"],
    },
    "revision": {
      **value,
      "id": value["current_revision_id"],
      "status": value["status"],
    },
    "stage": {
      "id": value["stage_id"],
      "status": value["stage_status"],
      "owner_user_id": owner_id,
      "revision_id": value["current_revision_id"],
      "generated_trial_count": value["generated_trial_count"],
      "planned_trial_count": value["stage_planned_trial_count"],
      "cancelled_before_generation": value["cancelled_before_generation"],
      "in_flight_count": value["in_flight_count"],
      "blocked_reason_code": value["stage_reason_code"],
      "blocked_reason_message": value["stage_reason_message"],
      "next_terminal_sequence": value["next_terminal_sequence"],
      "blocked_scope": value["blocked_scope"],
    },
    "unavailable_assets": int(cur.fetchone()["value"]),
  }


def _cancel_stage(
  cur: Any,
  graph: dict[str, Any],
  request_id: str | None,
) -> dict[str, Any]:
  experiment = graph["experiment"]
  revision = graph["revision"]
  stage = graph["stage"]
  if experiment["status"] in {"completed", "partially_completed", "failed", "cancelled"}:
    return {"experimentId": str(experiment["id"]), "status": experiment["status"]}
  first_request = revision.get("cancel_requested_at") is None
  if first_request:
    _mark_cancel_requested(cur, revision, stage)
    _cancel_waiting_trials(cur, stage)
  _cancel_active_executions(cur, stage, str(stage["owner_user_id"]))
  status = _rebuild_aggregate_status(cur, str(stage["id"]))
  if first_request:
    _audit_cancel_requested(cur, graph, request_id, status)
  return {"experimentId": str(experiment["id"]), "status": status}


def _mark_cancel_requested(
  cur: Any,
  revision: dict[str, Any],
  stage: dict[str, Any],
) -> None:
  remaining = max(
    0,
    int(stage["planned_trial_count"]) - int(stage["generated_trial_count"]),
  )
  cur.execute("""
    UPDATE backtest_experiment_revision
    SET cancel_requested_at=now(), updated_at=now()
    WHERE id=%s AND cancel_requested_at IS NULL
  """, (revision["id"],))
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET cancelled_before_generation=%s,
      generation_lease_owner=NULL, generation_attempt_token=NULL,
      generation_lease_expires_at=NULL, generation_heartbeat_at=NULL,
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (remaining, stage["id"]))


def _cancel_waiting_trials(cur: Any, stage: dict[str, Any]) -> None:
  cur.execute("""
    UPDATE backtest_experiment_trial
    SET materialization_status=CASE
        WHEN materialization_status='pending'
          THEN 'cancelled_before_materialization'
        ELSE materialization_status
      END,
      summary_status='cancelled', retry_not_before=NULL, updated_at=now()
    WHERE stage_id=%s AND active_execution_id IS NULL
      AND summary_status NOT IN ('succeeded', 'failed', 'cancelled')
  """, (stage["id"],))


def _cancel_active_executions(
  cur: Any,
  stage: dict[str, Any],
  owner_id: str,
) -> None:
  cur.execute("""
    SELECT execution.run_id
    FROM backtest_experiment_trial trial
    JOIN backtest_experiment_trial_execution execution
      ON execution.id=trial.active_execution_id
    WHERE trial.stage_id=%s
    ORDER BY trial.trial_index
    FOR UPDATE OF trial, execution
  """, (stage["id"],))
  for row in cur.fetchall():
    state_store.request_backtest_cancel_in_transaction(
      cur, str(row["run_id"]), owner_id,
    )


def _audit_cancel_requested(
  cur: Any,
  graph: dict[str, Any],
  request_id: str | None,
  status: str,
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id,
      event_type, request_id, detail
    ) VALUES (%s,%s,%s,%s,'experiment.cancel_requested',%s,%s)
  """, (
    graph["stage"]["owner_user_id"], graph["experiment"]["id"],
    graph["revision"]["id"], graph["stage"]["id"], request_id,
    Jsonb({"status": status}),
  ))


def _resume_stage(
  cur: Any,
  graph: dict[str, Any],
  expected_revision_id: str,
  reason: str,
  request_id: str | None,
) -> dict[str, Any]:
  revision = graph["revision"]
  stage = graph["stage"]
  if str(revision["id"]) != expected_revision_id:
    raise ExperimentError("experiment.revision_conflict", "实验修订已经变化", 409)
  if revision.get("cancel_requested_at") is not None or revision.get("terminal_at") is not None:
    raise ExperimentError("experiment.command_invalid", "当前实验不能恢复", 409)
  if stage.get("blocked_reason_code") != "experiment.execution_blocked":
    raise ExperimentError("experiment.command_invalid", "当前实验没有可解除的执行熔断", 409)
  _validate_resume_gate(cur, graph)
  cur.execute("""
    UPDATE backtest_experiment_stage
    SET blocked_reason_code=NULL, blocked_reason_message=NULL,
      blocked_scope=NULL, blocked_at=NULL,
      circuit_reset_sequence=next_terminal_sequence-1,
      consecutive_failure_fingerprint=NULL, consecutive_failure_count=0,
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (stage["id"],))
  _clear_parent_block(cur, revision)
  status = _rebuild_aggregate_status(cur, str(stage["id"]))
  _audit_resume(cur, graph, reason, request_id, status)
  return {
    "experimentId": str(graph["experiment"]["id"]),
    "revisionId": str(revision["id"]),
    "status": status,
  }


def _validate_resume_gate(cur: Any, graph: dict[str, Any]) -> None:
  if graph["quota"].get("user_status") != "active" or graph["unavailable_assets"]:
    raise ExperimentError(
      "experiment.resume_validation_failed", "当前权限或冻结资产不可用", 409,
    )
  fixed = graph["revision"]["experiment_spec"]["runTemplateSnapshot"]["fixedStructure"]
  try:
    validate_runtime_capability(
      fixed.get("runtimeVersion"), fixed.get("runtimeCapabilityDigest"),
    )
  except Exception as error:
    raise ExperimentError(
      "experiment.resume_validation_failed", "冻结策略运行能力仍不可用", 409,
    ) from error
  gate = _materialization_gate(cur, graph)
  if not gate.allowed:
    raise ExperimentError(
      "experiment.resume_validation_failed", gate.message or "当前容量不可恢复", 409,
      details={"reasonCode": gate.code},
    )


def _clear_parent_block(cur: Any, revision: dict[str, Any]) -> None:
  cur.execute("""
    UPDATE backtest_experiment_revision
    SET system_block_code=NULL, system_block_message=NULL,
      blocked_reason_code=NULL, blocked_reason_message=NULL, updated_at=now()
    WHERE id=%s
  """, (revision["id"],))
  cur.execute("""
    UPDATE backtest_experiment
    SET blocked_reason_code=NULL, blocked_reason_message=NULL,
      updated_at=now(), row_version=row_version+1
    WHERE id=%s
  """, (revision["experiment_id"],))


def _audit_resume(
  cur: Any,
  graph: dict[str, Any],
  reason: str,
  request_id: str | None,
  status: str,
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id,
      event_type, request_id, detail
    ) VALUES (%s,%s,%s,%s,'experiment.execution_resumed',%s,%s)
  """, (
    graph["stage"]["owner_user_id"], graph["experiment"]["id"],
    graph["revision"]["id"], graph["stage"]["id"], request_id,
    Jsonb({
      "reason": reason,
      "status": status,
      "capabilityDigest": graph["revision"]["experiment_spec"]
        ["runTemplateSnapshot"]["fixedStructure"].get("runtimeCapabilityDigest"),
    }),
  ))


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
