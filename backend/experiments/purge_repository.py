"""实验物理清理作业的 PostgreSQL Adapter。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend import settings
from backend.experiments.errors import ExperimentError
from backend.experiments.purge_worker import PurgeProgress
from backend.services import state_store


_NEXT_PHASE = {
  "audit": "exports",
  "exports": "evaluations",
  "evaluations": "outbox",
  "outbox": "pointers",
  "pointers": "executions",
  "executions": "runs",
  "runs": "asset_holds",
  "asset_holds": "trials",
  "trials": "stages",
  "stages": "revisions",
  "revisions": "experiment",
  "experiment": "completed",
}

_PHASE_RESOURCES = {
  "audit": (("audit", "backtest_experiment_audit"),
            ("idempotency", "backtest_internal_idempotency")),
  "evaluations": (("evaluation_run", "backtest_experiment_evaluation_run"),
                  ("evaluation_spec", "backtest_experiment_evaluation_spec")),
  "outbox": (("outbox", "backtest_experiment_dispatch_outbox"),),
  "executions": (("execution", "backtest_experiment_trial_execution"),),
  "asset_holds": (("asset_hold", "backtest_experiment_asset_hold"),),
  "trials": (("trial", "backtest_experiment_trial"),),
  "stages": (("stage", "backtest_experiment_stage"),),
  "revisions": (("revision", "backtest_experiment_revision"),),
}


class PostgresExperimentPurgeRepository:
  def claim_next(self, worker_id: str) -> dict[str, Any] | None:
    attempt_token = str(uuid4())
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT * FROM backtest_experiment_purge
          WHERE status='queued'
            OR (status='running' AND lease_expires_at < now())
            OR (
              status='failed' AND attempt_count < %s
              AND next_attempt_at IS NOT NULL AND next_attempt_at <= now()
            )
          ORDER BY created_at, id
          FOR UPDATE SKIP LOCKED LIMIT 1
        """, (settings.EXPERIMENT_PURGE_MAX_ATTEMPTS,))
        row = cur.fetchone()
        if row is None:
          return None
        cur.execute("""
          UPDATE backtest_experiment_purge
          SET status='running', lease_owner=%s, attempt_token=%s,
            lease_expires_at=now() + (%s * interval '1 second'),
            next_attempt_at=NULL, attempt_count=attempt_count + 1,
            started_at=COALESCE(started_at,now()), updated_at=now()
          WHERE id=%s RETURNING *
        """, (
          worker_id, attempt_token, settings.EXPERIMENT_PURGE_LEASE_SECONDS,
          row["id"],
        ))
        claimed = dict(cur.fetchone())
    return {
      "id": str(claimed["id"]), "attemptToken": attempt_token,
      "phase": claimed["phase"],
    }

  def heartbeat(self, purge_id: str, attempt_token: str) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_purge
          SET lease_expires_at=now() + (%s * interval '1 second'), updated_at=now()
          WHERE id=%s AND attempt_token=%s AND status='running'
        """, (settings.EXPERIMENT_PURGE_LEASE_SECONDS, purge_id, attempt_token))
        return cur.rowcount == 1

  def load_artifact_batch(
    self, purge_id: str, attempt_token: str,
  ) -> list[dict[str, Any]]:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        _require_job(cur, purge_id, attempt_token, "artifacts")
        _prepare_run_artifact_batch(cur, purge_id)
        cur.execute("""
          SELECT id, resource_snapshot->>'storageKey' AS storage_key
          FROM backtest_experiment_purge_item
          WHERE purge_id=%s AND processed_at IS NULL
            AND (
              (resource_type='export_artifact' AND outcome IS NULL)
              OR (resource_type='run_artifact' AND outcome='pending_unlink')
            )
          ORDER BY ordinal LIMIT %s
        """, (purge_id, settings.EXPERIMENT_PURGE_BATCH_SIZE))
        return [{
          "itemId": str(row["id"]), "storageKey": row["storage_key"],
        } for row in cur.fetchall()]

  def mark_artifacts_removed(
    self, purge_id: str, attempt_token: str, item_ids: list[str],
  ) -> None:
    if not item_ids:
      return
    with _worker_session() as conn:
      with conn.cursor() as cur:
        _require_job(cur, purge_id, attempt_token, "artifacts")
        cur.execute("""
          DELETE FROM artifact_file artifact
          USING backtest_experiment_purge_item item
          WHERE item.purge_id=%s AND item.id=ANY(%s)
            AND item.resource_type='run_artifact'
            AND item.resource_id=artifact.id
            AND artifact.artifact_type='purge_pending'
        """, (purge_id, item_ids))
        cur.execute("""
          UPDATE backtest_experiment_purge_item
          SET outcome='deleted', processed_at=now()
          WHERE purge_id=%s AND id=ANY(%s) AND processed_at IS NULL
        """, (purge_id, item_ids))

  def advance_artifact_phase(self, purge_id: str, attempt_token: str) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        _require_job(cur, purge_id, attempt_token, "artifacts")
        if (
          _has_pending_items(cur, purge_id, "export_artifact")
          or _has_pending_items(cur, purge_id, "run_artifact")
        ):
          return False
        _advance_phase(cur, purge_id, attempt_token, "audit")
        return True

  def process_next_batch(
    self, purge_id: str, attempt_token: str,
  ) -> PurgeProgress:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        job = _require_job(cur, purge_id, attempt_token)
        phase = str(job["phase"])
        if phase == "pointers":
          _clear_graph_pointers(cur, purge_id, str(job["experiment_id"]))
          return _advance(cur, purge_id, attempt_token, phase)
        if phase in {"asset_holds", "revisions"}:
          return _advance(cur, purge_id, attempt_token, phase)
        if phase == "runs":
          handled = _process_run_batch(cur, purge_id)
        elif phase == "exports":
          handled = _process_export_batch(cur, purge_id)
        elif phase == "experiment":
          _finalize_experiment_purge(
            cur, purge_id, attempt_token, str(job["experiment_id"]),
          )
          return PurgeProgress(phase="completed", completed=True)
        else:
          handled = _process_resource_batch(cur, purge_id, phase)
        if handled:
          return PurgeProgress(phase=phase)
        return _advance(cur, purge_id, attempt_token, phase)

  def complete(self, purge_id: str, attempt_token: str) -> None:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT status FROM backtest_experiment_purge WHERE id=%s FOR UPDATE",
          (purge_id,),
        )
        row = cur.fetchone()
        if row is not None and row["status"] == "succeeded":
          return
        _require_job(cur, purge_id, attempt_token, "completed")
        cur.execute("""
          UPDATE backtest_experiment_purge
          SET status='succeeded', completed_at=now(),
            lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL,
            next_attempt_at=NULL,
            new_value=jsonb_build_object(
              'purgeStatus','succeeded','phase','completed'
            ), updated_at=now()
          WHERE id=%s AND attempt_token=%s
        """, (purge_id, attempt_token))

  def fail(
    self, purge_id: str, attempt_token: str, code: str, message: str,
  ) -> None:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_purge
          SET status='failed', failed_at=now(), failure_phase=phase,
            error_code=%s, error_message=%s,
            next_attempt_at=now() + (%s * interval '1 second'),
            lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL,
            new_value=jsonb_build_object(
              'purgeStatus','failed','phase',phase,'errorCode',%s::text
            ),
            updated_at=now()
          WHERE id=%s AND attempt_token=%s AND status='running'
        """, (
          code, message[:500], settings.EXPERIMENT_PURGE_RETRY_DELAY_SECONDS,
          code, purge_id, attempt_token,
        ))

  def get(self, purge_id: str) -> dict[str, Any]:
    if not state_store.current_user_is_admin():
      raise ExperimentError("experiment.purge_forbidden", "仅管理员可以查看清理作业", 403)
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT * FROM backtest_experiment_purge WHERE id=%s", (purge_id,))
        row = cur.fetchone()
    if row is None:
      raise ExperimentError("experiment.purge_not_found", "清理作业不存在", 404)
    return _public_purge(dict(row))


def _require_job(
  cur: Any,
  purge_id: str,
  attempt_token: str,
  phase: str | None = None,
) -> dict[str, Any]:
  cur.execute("""
    SELECT * FROM backtest_experiment_purge
    WHERE id=%s AND attempt_token=%s AND status='running'
    FOR UPDATE
  """, (purge_id, attempt_token))
  row = cur.fetchone()
  if row is None or (phase is not None and row["phase"] != phase):
    raise ExperimentError("experiment.purge_lease_lost", "实验清理租约已失效", 409)
  return dict(row)


def _prepare_run_artifact_batch(cur: Any, purge_id: str) -> None:
  cur.execute("""
    SELECT item.id, item.resource_id
    FROM backtest_experiment_purge_item item
    WHERE item.purge_id=%s AND item.resource_type='run_artifact'
      AND item.processed_at IS NULL AND item.outcome IS NULL
    ORDER BY item.ordinal LIMIT %s
    FOR UPDATE OF item
  """, (purge_id, settings.EXPERIMENT_PURGE_BATCH_SIZE))
  items = [dict(row) for row in cur.fetchall()]
  for item in items:
    artifact_id = str(item["resource_id"])
    cur.execute(
      "SELECT id FROM artifact_file WHERE id=%s FOR UPDATE",
      (artifact_id,),
    )
    if cur.fetchone() is None:
      _set_artifact_pending_unlink(cur, str(item["id"]))
      continue
    if _run_artifact_is_shared(cur, purge_id, artifact_id):
      _mark_items(cur, [str(item["id"])], "preserved_shared")
      continue
    cur.execute("""
      DELETE FROM research_signal_snapshot snapshot
      WHERE snapshot.artifact_file_id=%s
        AND snapshot.run_id IN (
          SELECT resource_id FROM backtest_experiment_purge_item
          WHERE purge_id=%s AND resource_type='run'
        )
    """, (artifact_id, purge_id))
    cur.execute("""
      UPDATE artifact_file
      SET run_id=NULL, artifact_type='purge_pending'
      WHERE id=%s
    """, (artifact_id,))
    if cur.rowcount != 1:
      raise ExperimentError(
        "experiment.purge_dependency_changed", "运行制品依赖图在冻结后发生变化", 409,
      )
    _set_artifact_pending_unlink(cur, str(item["id"]))


def _run_artifact_is_shared(cur: Any, purge_id: str, artifact_id: str) -> bool:
  cur.execute("""
    SELECT EXISTS (
      SELECT 1 FROM research_signal_snapshot snapshot
      WHERE snapshot.artifact_file_id=%s
        AND snapshot.run_id NOT IN (
          SELECT resource_id FROM backtest_experiment_purge_item
          WHERE purge_id=%s AND resource_type='run'
        )
      UNION ALL
      SELECT 1 FROM backtest_publication_artifact
      WHERE source_artifact_file_id=%s
      UNION ALL
      SELECT 1 FROM imported_research_signal_dataset WHERE artifact_file_id=%s
      UNION ALL
      SELECT 1 FROM strategy_config_artifact WHERE artifact_file_id=%s
      UNION ALL
      SELECT 1 FROM artifact_file artifact
      JOIN backtest_publication publication ON publication.source_run_id=artifact.run_id
      WHERE artifact.id=%s
      UNION ALL
      SELECT 1 FROM artifact_file artifact
      JOIN backtest_signal_detail_export_item item
        ON item.source_run_id=artifact.run_id
      WHERE artifact.id=%s
      UNION ALL
      SELECT 1 FROM artifact_file artifact
      WHERE artifact.id=%s AND artifact.run_id IS NOT NULL
        AND artifact.run_id NOT IN (
          SELECT resource_id FROM backtest_experiment_purge_item
          WHERE purge_id=%s AND resource_type='run'
        )
    ) AS shared
  """, (
    artifact_id, purge_id, artifact_id, artifact_id,
    artifact_id, artifact_id, artifact_id, artifact_id, purge_id,
  ))
  return bool(cur.fetchone()["shared"])


def _set_artifact_pending_unlink(cur: Any, item_id: str) -> None:
  cur.execute("""
    UPDATE backtest_experiment_purge_item
    SET outcome='pending_unlink'
    WHERE id=%s AND processed_at IS NULL
  """, (item_id,))


def _process_resource_batch(cur: Any, purge_id: str, phase: str) -> bool:
  resources = _PHASE_RESOURCES.get(phase)
  if resources is None:
    raise ExperimentError("experiment.purge_phase_invalid", "实验清理阶段无效", 500)
  for resource_type, table in resources:
    items = _pending_items(cur, purge_id, resource_type)
    if not items:
      continue
    resource_ids = [str(item["resource_id"]) for item in items]
    cur.execute(f"DELETE FROM {table} WHERE id=ANY(%s)", (resource_ids,))
    _mark_items(cur, [str(item["id"]) for item in items], "deleted")
    return True
  return False


def _process_export_batch(cur: Any, purge_id: str) -> bool:
  cur.execute("""
    SELECT item.id, item.resource_id
    FROM backtest_experiment_purge_item item
    WHERE item.purge_id=%s AND item.resource_type='export'
      AND item.processed_at IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM backtest_experiment_evaluation_export child
        WHERE child.retry_of_export_id=item.resource_id
      )
    ORDER BY item.ordinal DESC LIMIT %s
    FOR UPDATE OF item
  """, (purge_id, settings.EXPERIMENT_PURGE_BATCH_SIZE))
  items = [dict(row) for row in cur.fetchall()]
  if not items:
    if _has_pending_items(cur, purge_id, "export"):
      raise ExperimentError(
        "experiment.purge_dependency_changed", "评价导出依赖图在冻结后发生变化", 409,
      )
    return False
  resource_ids = [str(item["resource_id"]) for item in items]
  cur.execute(
    "DELETE FROM backtest_experiment_evaluation_export WHERE id=ANY(%s)",
    (resource_ids,),
  )
  _mark_items(cur, [str(item["id"]) for item in items], "deleted")
  return True


def _process_run_batch(cur: Any, purge_id: str) -> bool:
  items = _pending_items(cur, purge_id, "run")
  for item in items:
    run_id = str(item["resource_id"])
    cur.execute("SELECT id FROM backtest_run WHERE id=%s FOR UPDATE", (run_id,))
    if cur.fetchone() is None:
      _mark_items(cur, [str(item["id"])], "deleted")
      continue
    cur.execute("""
      SELECT EXISTS (
        SELECT 1 FROM backtest_publication WHERE source_run_id=%s
        UNION ALL
        SELECT 1 FROM backtest_signal_detail_export_item WHERE source_run_id=%s
      ) AS shared
    """, (run_id, run_id))
    shared = bool(cur.fetchone()["shared"])
    if shared:
      cur.execute("""
        UPDATE backtest_run SET origin_experiment_id=NULL, origin_stage_id=NULL,
          origin_trial_execution_id=NULL WHERE id=%s
      """, (run_id,))
      outcome = "preserved_shared"
    else:
      cur.execute("DELETE FROM backtest_run WHERE id=%s", (run_id,))
      outcome = "deleted"
    _mark_items(cur, [str(item["id"])], outcome)
  return bool(items)


def _clear_graph_pointers(cur: Any, purge_id: str, experiment_id: str) -> None:
  cur.execute("""
    UPDATE backtest_experiment_trial
    SET active_execution_id=NULL, selected_execution_id=NULL, updated_at=now()
    WHERE id IN (
      SELECT resource_id FROM backtest_experiment_purge_item
      WHERE purge_id=%s AND resource_type='trial'
    )
  """, (purge_id,))
  cur.execute("""
    UPDATE backtest_run
    SET origin_trial_execution_id=NULL
    WHERE id IN (
      SELECT resource_id FROM backtest_experiment_purge_item
      WHERE purge_id=%s AND resource_type='run'
    )
  """, (purge_id,))
  cur.execute(
    "UPDATE backtest_experiment SET current_revision_id=NULL WHERE id=%s",
    (experiment_id,),
  )


def _finalize_experiment_purge(
  cur: Any,
  purge_id: str,
  attempt_token: str,
  experiment_id: str,
) -> None:
  _assert_final_scope_unchanged(cur, purge_id, experiment_id)
  cur.execute("""
    DELETE FROM backtest_experiment_asset_hold
    WHERE id IN (
      SELECT resource_id FROM backtest_experiment_purge_item
      WHERE purge_id=%s AND resource_type='asset_hold'
    )
  """, (purge_id,))
  cur.execute("""
    DELETE FROM backtest_experiment_revision
    WHERE id IN (
      SELECT resource_id FROM backtest_experiment_purge_item
      WHERE purge_id=%s AND resource_type='revision'
    )
  """, (purge_id,))
  cur.execute("DELETE FROM backtest_experiment WHERE id=%s", (experiment_id,))
  cur.execute("""
    UPDATE backtest_experiment_purge_item
    SET outcome='deleted', processed_at=now()
    WHERE purge_id=%s AND resource_type IN ('asset_hold','revision','experiment')
      AND processed_at IS NULL
  """, (purge_id,))
  cur.execute("""
    UPDATE backtest_experiment_purge
    SET status='succeeded', phase='completed', completed_at=now(),
      lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL,
      next_attempt_at=NULL,
      new_value=jsonb_build_object(
        'purgeStatus','succeeded','phase','completed'
      ), updated_at=now()
    WHERE id=%s AND attempt_token=%s AND status='running'
  """, (purge_id, attempt_token))
  if cur.rowcount != 1:
    raise ExperimentError("experiment.purge_lease_lost", "实验清理租约已失效", 409)


def _assert_final_scope_unchanged(
  cur: Any,
  purge_id: str,
  experiment_id: str,
) -> None:
  cur.execute("""
    SELECT EXISTS (
      SELECT 1 FROM backtest_experiment_revision revision
      WHERE revision.experiment_id=%s AND revision.id NOT IN (
        SELECT resource_id FROM backtest_experiment_purge_item
        WHERE purge_id=%s AND resource_type='revision'
      )
      UNION ALL SELECT 1 FROM backtest_experiment_asset_hold hold
      JOIN backtest_experiment_revision revision ON revision.id=hold.revision_id
      WHERE revision.experiment_id=%s AND hold.id NOT IN (
        SELECT resource_id FROM backtest_experiment_purge_item
        WHERE purge_id=%s AND resource_type='asset_hold'
      )
      UNION ALL SELECT 1 FROM backtest_experiment_stage stage
      JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
      WHERE revision.experiment_id=%s
      UNION ALL SELECT 1 FROM backtest_experiment_evaluation_spec WHERE experiment_id=%s
      UNION ALL SELECT 1 FROM backtest_experiment_evaluation_run WHERE experiment_id=%s
      UNION ALL SELECT 1 FROM backtest_experiment_evaluation_export WHERE experiment_id=%s
      UNION ALL SELECT 1 FROM backtest_experiment_audit WHERE experiment_id=%s
      UNION ALL SELECT 1 FROM backtest_run WHERE origin_experiment_id=%s
    ) AS remaining
  """, (
    experiment_id, purge_id, experiment_id, purge_id,
    experiment_id, experiment_id, experiment_id, experiment_id,
    experiment_id, experiment_id,
  ))
  if bool(cur.fetchone()["remaining"]):
    raise ExperimentError(
      "experiment.purge_dependency_changed", "实验依赖图在冻结后发生变化", 409,
    )


def _pending_items(cur: Any, purge_id: str, resource_type: str) -> list[dict[str, Any]]:
  cur.execute("""
    SELECT id, resource_id FROM backtest_experiment_purge_item
    WHERE purge_id=%s AND resource_type=%s AND processed_at IS NULL
    ORDER BY ordinal DESC LIMIT %s FOR UPDATE
  """, (purge_id, resource_type, settings.EXPERIMENT_PURGE_BATCH_SIZE))
  return [dict(row) for row in cur.fetchall()]


def _has_pending_items(cur: Any, purge_id: str, resource_type: str) -> bool:
  cur.execute("""
    SELECT EXISTS (
      SELECT 1 FROM backtest_experiment_purge_item
      WHERE purge_id=%s AND resource_type=%s AND processed_at IS NULL
    ) AS pending
  """, (purge_id, resource_type))
  return bool(cur.fetchone()["pending"])


def _mark_items(cur: Any, item_ids: list[str], outcome: str) -> None:
  cur.execute("""
    UPDATE backtest_experiment_purge_item
    SET outcome=%s, processed_at=now()
    WHERE id=ANY(%s) AND processed_at IS NULL
  """, (outcome, item_ids))


def _advance(
  cur: Any, purge_id: str, attempt_token: str, phase: str,
) -> PurgeProgress:
  next_phase = _NEXT_PHASE[phase]
  _advance_phase(cur, purge_id, attempt_token, next_phase)
  return PurgeProgress(phase=next_phase, completed=next_phase == "completed")


def _advance_phase(
  cur: Any, purge_id: str, attempt_token: str, next_phase: str,
) -> None:
  cur.execute("""
    UPDATE backtest_experiment_purge SET phase=%s, updated_at=now()
    WHERE id=%s AND attempt_token=%s AND status='running'
  """, (next_phase, purge_id, attempt_token))
  if cur.rowcount != 1:
    raise ExperimentError("experiment.purge_lease_lost", "实验清理租约已失效", 409)


def _public_purge(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "purgeId": str(row["id"]), "experimentId": str(row["experiment_id"]),
    "revisionId": str(row["revision_id"]), "status": row["status"],
    "phase": row["phase"], "reason": row["reason"],
    "scopeDigest": row["scope_digest"], "scopeCounts": row["scope_counts"],
    "oldValue": row.get("old_value") or {}, "newValue": row.get("new_value") or {},
    "requestId": row.get("request_id"), "attemptCount": int(row["attempt_count"]),
    "startedAt": row["started_at"].isoformat() if row.get("started_at") else None,
    "completedAt": row["completed_at"].isoformat() if row.get("completed_at") else None,
    "failurePhase": row.get("failure_phase"), "errorCode": row.get("error_code"),
    "errorMessage": row.get("error_message"),
    "createdAt": row["created_at"].isoformat(),
  }


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
