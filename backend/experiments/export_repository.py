"""评价导出作业的 PostgreSQL 持久化边界。"""

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
from backend.experiments.canonicalization import digest
from backend.experiments.errors import ExperimentError
from backend.experiments.repository import (
  CommandResult,
  _load_idempotency,
  _lock_command,
  _lock_experiment_for_write,
  _replay_result,
  _save_idempotency,
)
from backend.experiments.exports import validate_columns
from backend.services import state_store


CREATE_EXPORT_OPERATION = "experiment.evaluation_export.create.v1"
CANCEL_EXPORT_OPERATION = "experiment.evaluation_export.cancel.v1"
RETRY_EXPORT_OPERATION = "experiment.evaluation_export.retry.v1"
EXPORT_RENDERER_VERSION = "evaluation_export_renderer_v1"


@dataclass(frozen=True)
class ClaimedExport:
  export: dict[str, Any]
  attempt_token: str


class PostgresEvaluationExportRepository:
  def create_export(
    self,
    *,
    evaluation_run_id: str,
    export_format: str,
    columns: list[str],
    locale: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        replay = _begin_idempotent(
          cur, owner_id, CREATE_EXPORT_OPERATION, idempotency_key, request_hash,
        )
        if replay:
          return replay
        _assert_worker_available(cur)
        run = _load_succeeded_run(cur, evaluation_run_id, owner_id)
        _lock_experiment_for_write(cur, str(run["experiment_id"]), owner_id)
        validate_columns(dict(run["output_snapshot"]), columns)
        response = _insert_export(
          cur, run, export_format, columns, locale, request_hash, request_id,
        )
        _save_idempotency(
          cur, owner_id, CREATE_EXPORT_OPERATION, idempotency_key, request_hash,
          "evaluation_export", response["exportId"], 202, response,
        )
        return CommandResult(response, 202)

  def list_exports(self, evaluation_run_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        _require_owned_run(cur, evaluation_run_id, owner_id)
        cur.execute("""
          SELECT export.*, spec.name AS evaluation_name
          FROM backtest_experiment_evaluation_export export
          JOIN backtest_experiment_evaluation_spec spec
            ON spec.id=export.evaluation_spec_id
          WHERE export.evaluation_run_id=%s AND export.owner_user_id=%s
          ORDER BY export.created_at DESC, export.id DESC
          LIMIT 100
        """, (evaluation_run_id, owner_id))
        items = [_export_item(dict(row)) for row in cur.fetchall()]
    return {"items": items, "nextCursor": None}

  def get_export(self, export_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        row = _load_export(cur, export_id, owner_id)
    if row is None:
      raise _not_found()
    return _export_item(row)

  def cancel_export(
    self,
    *,
    export_id: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        replay = _begin_idempotent(
          cur, owner_id, CANCEL_EXPORT_OPERATION, idempotency_key, request_hash,
        )
        if replay:
          return replay
        row = _load_export(cur, export_id, owner_id, for_update=True)
        if row is None:
          raise _not_found()
        if row["status"] == "queued":
          cur.execute("""
            UPDATE backtest_experiment_evaluation_export
            SET status='cancelled', cancel_requested_at=now(), finished_at=now()
            WHERE id=%s
          """, (export_id,))
          status = "cancelled"
        elif row["status"] == "running":
          cur.execute("""
            UPDATE backtest_experiment_evaluation_export
            SET status='cancel_requested', cancel_requested_at=now()
            WHERE id=%s
          """, (export_id,))
          status = "cancel_requested"
        elif row["status"] == "cancel_requested":
          status = "cancel_requested"
        elif row["status"] == "cancelled":
          status = "cancelled"
        else:
          raise ExperimentError(
            "experiment.export_state_conflict", "当前导出状态不能取消", 409,
          )
        _insert_audit(cur, row, "evaluation.export_cancel_requested", request_id)
        response = {"exportId": export_id, "status": status, "replayed": False}
        _save_idempotency(
          cur, owner_id, CANCEL_EXPORT_OPERATION, idempotency_key, request_hash,
          "evaluation_export", export_id, 202, response,
        )
    return CommandResult(response, 202)

  def retry_export(
    self,
    *,
    export_id: str,
    idempotency_key: str,
    request_hash: str,
    request_id: str | None,
  ) -> CommandResult:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        replay = _begin_idempotent(
          cur, owner_id, RETRY_EXPORT_OPERATION, idempotency_key, request_hash,
        )
        if replay:
          return replay
        original = _load_export(cur, export_id, owner_id, for_update=True)
        if original is None:
          raise _not_found()
        if original["status"] not in {"failed", "cancelled", "expired"}:
          raise ExperimentError(
            "experiment.export_retry_conflict",
            "只有失败、取消或过期的导出可以重新创建",
            409,
          )
        _assert_worker_available(cur)
        run = _load_succeeded_run(cur, str(original["evaluation_run_id"]), owner_id)
        _lock_experiment_for_write(cur, str(run["experiment_id"]), owner_id)
        if run["output_hash"] != original["output_hash"]:
          raise ExperimentError(
            "experiment.export_source_changed", "评价输出摘要已变化", 409,
          )
        response = _insert_export(
          cur, run, str(original["format"]), list(original["columns"]),
          str(original["locale"]), request_hash, request_id,
          retry_of_export_id=export_id,
        )
        _save_idempotency(
          cur, owner_id, RETRY_EXPORT_OPERATION, idempotency_key, request_hash,
          "evaluation_export", response["exportId"], 202, response,
        )
        return CommandResult(response, 202)

  def claim_next_export(self, worker_id: str) -> ClaimedExport | None:
    attempt_token = str(uuid4())
    with _worker_session() as conn:
      with conn.cursor() as cur:
        _finalize_exhausted_leases(cur)
        row = _claim_candidate(cur)
        if row is None:
          return None
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET status='running', lease_owner=%s, attempt_token=%s,
            lease_expires_at=now() + (%s * interval '1 second'),
            attempt_count=attempt_count+1,
            started_at=COALESCE(started_at,now()), updated_at=now()
          WHERE id=%s
          RETURNING *
        """, (
          worker_id, attempt_token, settings.EXPERIMENT_EXPORT_LEASE_SECONDS,
          row["id"],
        ))
        claimed = dict(cur.fetchone())
    return ClaimedExport(_export_worker_item(claimed), attempt_token)

  def load_source(self, export_id: str, attempt_token: str) -> dict[str, Any]:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT export.*, run.output_snapshot, run.output_hash AS current_output_hash,
            run.result_set_hash AS current_result_set_hash, run.catalog_snapshot,
            run.pipeline_version, run.adapter_type, run.adapter_version,
            spec.name AS evaluation_name, spec.version_no AS evaluation_spec_version,
            spec.evaluation_spec, revision.experiment_spec,
            revision.run_template_digest, revision.dataset_snapshot_digest
          FROM backtest_experiment_evaluation_export export
          JOIN backtest_experiment_evaluation_run run
            ON run.id=export.evaluation_run_id
          JOIN backtest_experiment_evaluation_spec spec
            ON spec.id=export.evaluation_spec_id
          JOIN backtest_experiment_revision revision
            ON revision.id=export.revision_id
          WHERE export.id=%s AND export.attempt_token=%s
            AND export.status IN ('running','cancel_requested')
        """, (export_id, attempt_token))
        row = cur.fetchone()
    if row is None:
      raise ExperimentError(
        "experiment.export_lease_lost", "评价导出租约已失效", 409,
      )
    return _source_snapshot(dict(row))

  def heartbeat(
    self,
    export_id: str,
    attempt_token: str,
    progress_completed: int | None = None,
  ) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET lease_expires_at=now() + (%s * interval '1 second'),
            progress_completed=CASE
              WHEN %s::bigint IS NULL THEN progress_completed
              ELSE GREATEST(
                progress_completed, LEAST(progress_total, %s::bigint)
              )
            END,
            updated_at=now()
          WHERE id=%s AND attempt_token=%s AND status='running'
        """, (
          settings.EXPERIMENT_EXPORT_LEASE_SECONDS,
          progress_completed, progress_completed, export_id, attempt_token,
        ))
        return cur.rowcount == 1

  def should_cancel(self, export_id: str, attempt_token: str) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT status, attempt_token
          FROM backtest_experiment_evaluation_export WHERE id=%s
        """, (export_id,))
        row = cur.fetchone()
    return (
      row is None
      or str(row.get("attempt_token") or "") != attempt_token
      or row["status"] == "cancel_requested"
    )

  def complete_export(
    self,
    export_id: str,
    attempt_token: str,
    *,
    storage_key: str,
    filename: str,
    media_type: str,
    byte_size: int,
    content_sha256: str,
  ) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET status='succeeded', progress_completed=progress_total,
            storage_key=%s, download_filename=%s, media_type=%s,
            byte_size=%s, content_sha256=%s,
            expires_at=now() + (%s * interval '1 day'),
            lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL,
            finished_at=now(), error_code=NULL, error_message=NULL
          WHERE id=%s AND attempt_token=%s AND status='running'
        """, (
          storage_key, filename, media_type, byte_size, content_sha256,
          settings.EXPERIMENT_EXPORT_TTL_DAYS, export_id, attempt_token,
        ))
        return cur.rowcount == 1

  def finish_cancelled(self, export_id: str, attempt_token: str) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET status='cancelled', lease_owner=NULL, attempt_token=NULL,
            lease_expires_at=NULL, finished_at=now()
          WHERE id=%s AND attempt_token=%s AND status='cancel_requested'
        """, (export_id, attempt_token))
        return cur.rowcount == 1

  def fail_export(
    self,
    export_id: str,
    attempt_token: str,
    error_code: str,
    error_message: str,
    *,
    retryable: bool,
  ) -> bool:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT attempt_count FROM backtest_experiment_evaluation_export
          WHERE id=%s AND attempt_token=%s FOR UPDATE
        """, (export_id, attempt_token))
        row = cur.fetchone()
        if row is None:
          return False
        retry = retryable and int(row["attempt_count"]) < settings.EXPERIMENT_EXPORT_MAX_ATTEMPTS
        status = "queued" if retry else "failed"
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET status=%s, queued_at=CASE WHEN %s THEN now() ELSE queued_at END,
            progress_completed=CASE WHEN %s THEN 0 ELSE progress_completed END,
            error_code=%s, error_message=%s,
            lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL,
            finished_at=CASE WHEN %s THEN NULL ELSE now() END
          WHERE id=%s AND attempt_token=%s AND status='running'
        """, (
          status, retry, retry, error_code, error_message, retry,
          export_id, attempt_token,
        ))
        return cur.rowcount == 1

  def get_download(self, export_id: str) -> dict[str, Any]:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        row = _load_export(cur, export_id, owner_id, for_update=True)
        if row is None:
          raise _not_found()
        if row["status"] == "succeeded" and _is_expired(row):
          cur.execute("""
            UPDATE backtest_experiment_evaluation_export
            SET status='expired', updated_at=now() WHERE id=%s
          """, (export_id,))
          row["status"] = "expired"
    return row

  def mark_downloaded(self, export_id: str) -> None:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET last_downloaded_at=now(), updated_at=now()
          WHERE id=%s AND owner_user_id=%s AND status='succeeded'
        """, (export_id, owner_id))

  def expire_artifact(self, export_id: str, error_code: str | None = None) -> None:
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET status='expired', error_code=COALESCE(%s,error_code),
            error_message=CASE WHEN %s IS NULL THEN error_message
              ELSE '评价导出制品不可用' END
          WHERE id=%s AND owner_user_id=%s AND status='succeeded'
        """, (error_code, error_code, export_id, owner_id))

  def worker_available(self) -> bool:
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT EXISTS (
            SELECT 1 FROM backtest_experiment_export_worker_health
            WHERE status IN ('ready','running')
              AND heartbeat_at > now() - (%s * interval '1 second')
          ) AS available
        """, (settings.EXPERIMENT_EXPORT_WORKER_TTL_SECONDS,))
        return bool(cur.fetchone()["available"])

  def record_health(
    self,
    worker_id: str,
    status: str,
    export_id: str | None = None,
    error_code: str | None = None,
  ) -> None:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          INSERT INTO backtest_experiment_export_worker_health (
            worker_id, status, current_export_id, last_error_code
          ) VALUES (%s,%s,%s,%s)
          ON CONFLICT (worker_id) DO UPDATE SET
            status=EXCLUDED.status,
            current_export_id=EXCLUDED.current_export_id,
            last_error_code=EXCLUDED.last_error_code,
            heartbeat_at=now()
        """, (worker_id, status, export_id, error_code))

  def expire_records(self) -> list[dict[str, Any]]:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET status='expired', updated_at=now()
          WHERE status='succeeded' AND expires_at <= now()
        """)
        cur.execute("""
          SELECT id, storage_key
          FROM backtest_experiment_evaluation_export
          WHERE status='expired' AND storage_key IS NOT NULL
          ORDER BY finished_at, id
          LIMIT 1000
        """)
        return [dict(row) for row in cur.fetchall()]

  def clear_expired_storage_key(self, export_id: str, storage_key: str) -> None:
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          UPDATE backtest_experiment_evaluation_export
          SET storage_key=NULL, updated_at=now()
          WHERE id=%s AND status='expired' AND storage_key=%s
        """, (export_id, storage_key))

def _begin_idempotent(
  cur: Any,
  owner_id: str,
  operation: str,
  key: str,
  request_hash: str,
) -> CommandResult | None:
  _lock_command(cur, owner_id, operation, key)
  replay = _load_idempotency(cur, owner_id, operation, key)
  return _replay_result(replay, request_hash) if replay else None


def _assert_worker_available(cur: Any) -> None:
  if not settings.EXPERIMENT_EXPORT_WORKER_AVAILABILITY_REQUIRED:
    return
  cur.execute("""
    SELECT EXISTS (
      SELECT 1 FROM backtest_experiment_export_worker_health
      WHERE status IN ('ready','running')
        AND heartbeat_at > now() - (%s * interval '1 second')
    ) AS available
  """, (settings.EXPERIMENT_EXPORT_WORKER_TTL_SECONDS,))
  if not cur.fetchone()["available"]:
    raise ExperimentError(
      "experiment.export_worker_unavailable",
      "评价导出服务暂不可用",
      503,
      retryable=True,
    )


def _load_succeeded_run(cur: Any, run_id: str, owner_id: str) -> dict[str, Any]:
  cur.execute("""
    SELECT run.*, spec.version_no AS spec_version_no, spec.name AS spec_name
    FROM backtest_experiment_evaluation_run run
    JOIN backtest_experiment_evaluation_spec spec
      ON spec.id=run.evaluation_spec_id
    WHERE run.id=%s AND run.owner_user_id=%s
    FOR SHARE OF run
  """, (run_id, owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError(
      "experiment.evaluation_run_not_found", "评价运行不存在", 404,
    )
  value = dict(row)
  if value["status"] != "succeeded" or not value.get("output_hash"):
    raise ExperimentError(
      "experiment.export_source_not_ready", "只有已完成的评价运行可以导出", 409,
    )
  output = value.get("output_snapshot")
  if not isinstance(output, dict) or digest(output) != value["output_hash"]:
    raise ExperimentError(
      "experiment.export_source_hash_mismatch", "评价输出摘要不一致", 409,
    )
  return value


def _require_owned_run(cur: Any, run_id: str, owner_id: str) -> None:
  cur.execute("""
    SELECT 1 FROM backtest_experiment_evaluation_run
    WHERE id=%s AND owner_user_id=%s
  """, (run_id, owner_id))
  if cur.fetchone() is None:
    raise ExperimentError(
      "experiment.evaluation_run_not_found", "评价运行不存在", 404,
    )


def _insert_export(
  cur: Any,
  run: dict[str, Any],
  export_format: str,
  columns: list[str],
  locale: str,
  request_hash: str,
  request_id: str | None,
  *,
  retry_of_export_id: str | None = None,
) -> dict[str, Any]:
  export_id = str(uuid4())
  output = dict(run["output_snapshot"])
  versions = _version_snapshot(run, output)
  cur.execute("""
    INSERT INTO backtest_experiment_evaluation_export (
      id, evaluation_run_id, evaluation_spec_id, experiment_id, revision_id,
      owner_user_id, retry_of_export_id, format, columns, locale,
      output_hash, result_set_hash, version_snapshot, request_hash, progress_total
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
  """, (
    export_id, run["id"], run["evaluation_spec_id"], run["experiment_id"],
    run["revision_id"], run["owner_user_id"], retry_of_export_id,
    export_format, Jsonb(columns), locale, run["output_hash"],
    run["result_set_hash"], Jsonb(versions), request_hash,
    len(list(output.get("rows") or [])),
  ))
  row = {
    "id": export_id,
    "evaluation_run_id": run["id"],
    "evaluation_spec_id": run["evaluation_spec_id"],
    "experiment_id": run["experiment_id"],
    "retry_of_export_id": retry_of_export_id,
    "format": export_format,
    "columns": columns,
    "locale": locale,
    "output_hash": run["output_hash"],
    "status": "queued",
    "progress_completed": 0,
    "progress_total": len(list(output.get("rows") or [])),
    "attempt_count": 0,
    "created_at": datetime.now(timezone.utc),
  }
  _insert_audit(
    cur,
    {**run, "id": export_id, "evaluation_run_id": run["id"]},
    "evaluation.export_created",
    request_id,
  )
  return _export_item(row, replayed=False)


def _version_snapshot(run: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
  return {
    "exportSchemaVersion": "evaluation_export_v1",
    "rendererVersion": EXPORT_RENDERER_VERSION,
    "outputSchemaVersion": output.get("schemaVersion"),
    "pipelineVersion": run["pipeline_version"],
    "statisticsVersion": output.get("statisticsVersion"),
    "metricCatalogVersion": run["metric_catalog_version"],
    "dimensionCatalogVersion": run["dimension_catalog_version"],
    "normalizerVersion": run.get("normalizer_version"),
    "adapterType": run["adapter_type"],
    "adapterVersion": run["adapter_version"],
  }


def _claim_candidate(cur: Any) -> dict[str, Any] | None:
  cur.execute("""
    SELECT export.*
    FROM backtest_experiment_evaluation_export export
    WHERE (
      export.status='queued'
      OR (export.status='running' AND export.lease_expires_at < now())
    )
      AND export.attempt_count < %s
      AND NOT EXISTS (
        SELECT 1 FROM backtest_experiment_evaluation_export active
        WHERE active.owner_user_id=export.owner_user_id
          AND active.id<>export.id
          AND active.status IN ('running','cancel_requested')
      )
    ORDER BY export.queued_at, export.id
    FOR UPDATE OF export SKIP LOCKED
    LIMIT 1
  """, (settings.EXPERIMENT_EXPORT_MAX_ATTEMPTS,))
  row = cur.fetchone()
  return dict(row) if row else None


def _finalize_exhausted_leases(cur: Any) -> None:
  cur.execute("""
    UPDATE backtest_experiment_evaluation_export
    SET status='failed', error_code='attempts_exhausted',
      error_message='评价导出重试次数已用尽', finished_at=now(),
      lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL
    WHERE status='running' AND lease_expires_at < now() AND attempt_count >= %s
  """, (settings.EXPERIMENT_EXPORT_MAX_ATTEMPTS,))
  cur.execute("""
    UPDATE backtest_experiment_evaluation_export
    SET status='cancelled', finished_at=now(),
      lease_owner=NULL, attempt_token=NULL, lease_expires_at=NULL
    WHERE status='cancel_requested' AND lease_expires_at < now()
  """)


def _load_export(
  cur: Any,
  export_id: str,
  owner_id: str,
  *,
  for_update: bool = False,
) -> dict[str, Any] | None:
  lock = " FOR UPDATE OF export" if for_update else ""
  cur.execute(f"""
    SELECT export.*, spec.name AS evaluation_name
    FROM backtest_experiment_evaluation_export export
    JOIN backtest_experiment_evaluation_spec spec
      ON spec.id=export.evaluation_spec_id
    WHERE export.id=%s AND export.owner_user_id=%s{lock}
  """, (export_id, owner_id))
  row = cur.fetchone()
  return dict(row) if row else None


def _export_item(row: dict[str, Any], *, replayed: bool | None = None) -> dict[str, Any]:
  completed = int(row.get("progress_completed") or 0)
  total = int(row.get("progress_total") or 0)
  item = {
    "exportId": str(row["id"]),
    "evaluationRunId": str(row["evaluation_run_id"]),
    "evaluationSpecId": str(row["evaluation_spec_id"]),
    "experimentId": str(row["experiment_id"]),
    "evaluationName": row.get("evaluation_name"),
    "format": row["format"],
    "columns": list(row["columns"]),
    "locale": row["locale"],
    "outputHash": row["output_hash"],
    "status": row["status"],
    "progress": {"completed": completed, "total": total},
    "attemptCount": int(row.get("attempt_count") or 0),
    "artifact": _artifact_item(row),
    "error": _error_item(row),
    "retryOfExportId": (
      str(row["retry_of_export_id"]) if row.get("retry_of_export_id") else None
    ),
    "createdAt": _time(row.get("created_at")),
    "startedAt": _time(row.get("started_at")),
    "finishedAt": _time(row.get("finished_at")),
    "expiresAt": _time(row.get("expires_at")),
  }
  if replayed is not None:
    item["replayed"] = replayed
  return item


def _artifact_item(row: dict[str, Any]) -> dict[str, Any] | None:
  if not row.get("content_sha256"):
    return None
  return {
    "filename": row.get("download_filename"),
    "mediaType": row.get("media_type"),
    "byteSize": int(row.get("byte_size") or 0),
    "sha256": row["content_sha256"],
  }


def _error_item(row: dict[str, Any]) -> dict[str, str] | None:
  if not row.get("error_code"):
    return None
  return {
    "code": str(row["error_code"]),
    "message": str(row.get("error_message") or "评价导出失败"),
  }


def _export_worker_item(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": str(row["id"]),
    "ownerUserId": str(row["owner_user_id"]),
    "format": row["format"],
    "locale": row["locale"],
    "columns": list(row["columns"]),
    "outputHash": row["output_hash"],
    "createdAt": row["created_at"].isoformat(),
  }


def _source_snapshot(row: dict[str, Any]) -> dict[str, Any]:
  output = dict(row.get("output_snapshot") or {})
  if (
    row["current_output_hash"] != row["output_hash"]
    or row["current_result_set_hash"] != row["result_set_hash"]
    or digest(output) != row["output_hash"]
  ):
    raise ExperimentError(
      "experiment.export_source_hash_mismatch", "评价导出冻结来源摘要不一致", 409,
    )
  experiment_spec = dict(row.get("experiment_spec") or {})
  template = dict(experiment_spec.get("runTemplateSnapshot") or {})
  return {
    "output": output,
    "evaluationSpec": dict(row.get("evaluation_spec") or {}),
    "catalogSnapshot": dict(row.get("catalog_snapshot") or {}),
    "experiment": _safe_experiment_summary(experiment_spec),
    "assetVersions": _safe_assets(template),
    "evidence": _evidence(row, output),
  }


def _safe_experiment_summary(spec: dict[str, Any]) -> dict[str, Any]:
  template = dict(spec.get("runTemplateSnapshot") or {})
  plan = dict(spec.get("plan") or {})
  stages = [item for item in list(plan.get("stages") or []) if isinstance(item, dict)]
  return {
    "subject": _whitelist(
      dict(template.get("subjectRef") or spec.get("subjectRef") or {}),
      {"subjectType", "subjectId", "subjectVersion"},
    ),
    "templateType": template.get("templateType"),
    "runPath": template.get("runPath"),
    "scopeType": template.get("scopeType"),
    "fixedStructure": _whitelist(
      dict(template.get("fixedStructure") or {}),
      {
        "strategyName", "symbol", "benchmarkSymbol", "timeframe", "mode",
        "strategy", "strategyCategory", "holdingPolicy",
        "overlaySettlementPolicy", "fillModel", "runtimeVersion",
      },
    ),
    "searchSpace": [_safe_stage(stage) for stage in stages],
    "datasetSnapshots": [
      _safe_dataset_snapshot(item)
      for item in list(spec.get("datasetSnapshots") or [])
      if isinstance(item, dict)
    ],
  }


def _safe_stage(stage: dict[str, Any]) -> dict[str, Any]:
  return {
    key: _redact_sensitive(stage.get(key)) for key in (
      "stageType", "generatorType", "generatorVersion", "plannedTrialCount",
      "stageInputFingerprint", "parameterGrid", "dateScenarios",
      "capitalScenarios", "costScenarios", "executionScenarios",
    ) if key in stage
  }


def _safe_dataset_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
  return _whitelist(snapshot, {
    "datasetSnapshotSchemaVersion", "resolverType", "scenario", "symbol",
    "timeframe", "adjustmentMode", "formalDatePolicy", "warmupPolicy",
    "provider", "derivationRuleVersion", "formalDateCount",
    "formalDatesDigest", "warmupDateCount", "releaseMetadataDigest",
    "datasetFreezeLevel", "contentDigest",
  })


def _redact_sensitive(value: Any, parent_key: str = "") -> Any:
  sensitive = ("token", "secret", "password", "cookie", "dsn", "path", "directory")
  if any(word in parent_key.lower() for word in sensitive):
    return "[REDACTED]"
  if isinstance(value, dict):
    return {
      str(key): _redact_sensitive(item, str(key))
      for key, item in value.items()
    }
  if isinstance(value, list):
    return [_redact_sensitive(item, parent_key) for item in value]
  return value


def _safe_assets(template: dict[str, Any]) -> list[dict[str, Any]]:
  manifest = dict(template.get("assetManifestSnapshot") or {})
  allowed = {"assetType", "assetId", "exactVersion", "contentDigest", "sourceType"}
  return [
    _whitelist(item, allowed)
    for item in list(manifest.get("assets") or []) if isinstance(item, dict)
  ]


def _evidence(row: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
  versions = dict(row.get("version_snapshot") or {})
  return {
    "evaluationRunId": str(row["evaluation_run_id"]),
    "evaluationSpecId": str(row["evaluation_spec_id"]),
    "evaluationSpecVersion": int(row["evaluation_spec_version"]),
    "evaluationName": row.get("evaluation_name"),
    "outputHash": row["output_hash"],
    "resultSetHash": row["result_set_hash"],
    "runTemplateDigest": row["run_template_digest"],
    "datasetSnapshotDigest": row["dataset_snapshot_digest"],
    **versions,
    "outputSchemaVersion": output.get("schemaVersion"),
  }


def _whitelist(value: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
  return {key: value[key] for key in sorted(value) if key in allowed}


def _insert_audit(
  cur: Any,
  row: dict[str, Any],
  event_type: str,
  request_id: str | None,
) -> None:
  export_id = row["id"]
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, evaluation_spec_id,
      evaluation_run_id, evaluation_export_id, event_type, request_id, detail
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb)
  """, (
    row["owner_user_id"], row["experiment_id"], row["revision_id"],
    row["evaluation_spec_id"],
    row.get("evaluation_run_id") or row.get("id"), export_id,
    event_type, request_id,
  ))


def _is_expired(row: dict[str, Any]) -> bool:
  expires_at = row.get("expires_at")
  return not isinstance(expires_at, datetime) or expires_at <= datetime.now(timezone.utc)


def _not_found() -> ExperimentError:
  return ExperimentError("experiment.export_not_found", "评价导出不存在", 404)


def _time(value: Any) -> str | None:
  return value.isoformat() if isinstance(value, datetime) else None


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
