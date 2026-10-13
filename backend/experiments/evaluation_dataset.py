"""评价冻结集、批量输入、规范化结果与稳定分页。"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
from typing import Any, Callable, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.experiments.errors import ExperimentError
from backend.services import state_store


DATASET_DIGEST_VERSION = "evaluation_dataset_sha256_v1"
OUTPUT_SCHEMA_VERSION = "evaluation_output_v2"


@dataclass(frozen=True)
class FrozenDataset:
  execution_count: int
  dataset_digest: str
  completeness: dict[str, Any]


class EvaluationDataset:
  """把评价数据规模约束在数据库批量和 API 分页边界内。"""

  def freeze_execution_set(
    self,
    cur: Any,
    *,
    evaluation_run_id: str,
    experiment_id: str,
    owner_user_id: str,
    revision_id: str,
    stage_ids: Sequence[str],
    required_dimension_coverage: Sequence[dict[str, Any]],
  ) -> FrozenDataset:
    cur.execute("""
      SELECT id FROM backtest_experiment_stage
      WHERE revision_id=%s AND id=ANY(%s::uuid[])
      ORDER BY stage_index, id
      FOR SHARE
    """, (revision_id, list(stage_ids)))
    if len(cur.fetchall()) != len(stage_ids):
      raise ExperimentError(
        "experiment.evaluation_source_invalid",
        "评价来源包含未知 Stage",
        422,
      )
    cur.execute("""
      SELECT id FROM backtest_experiment_trial
      WHERE stage_id=ANY(%s::uuid[])
      ORDER BY stage_id, trial_index, id
      FOR SHARE
    """, (list(stage_ids),))
    cur.fetchall()
    stages = self._stage_counts(cur, revision_id, stage_ids)
    cur.execute("""
      INSERT INTO backtest_experiment_evaluation_execution (
        evaluation_run_id, owner_user_id, ordinal, stage_id, trial_id,
        trial_index, trial_execution_id, run_id, result_hash,
        result_schema_version, actual_data_hash
      )
      SELECT %s, %s,
        row_number() OVER (
          ORDER BY stage.stage_index, trial.trial_index, trial.id
        ) - 1,
        stage.id, trial.id, trial.trial_index, execution.id,
        execution.run_id, execution.result_hash,
        execution.result_schema_version, execution.actual_data_hash
      FROM backtest_experiment_stage stage
      JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
      JOIN backtest_experiment_trial_execution execution
        ON execution.id=trial.selected_execution_id
      JOIN backtest_run run ON run.id=execution.run_id
      WHERE stage.revision_id=%s
        AND stage.id=ANY(%s::uuid[])
        AND trial.summary_status='succeeded'
        AND run.status='succeeded'
        AND execution.result_hash IS NOT NULL
      ORDER BY stage.stage_index, trial.trial_index, trial.id
      ON CONFLICT (evaluation_run_id, trial_execution_id) DO NOTHING
    """, (evaluation_run_id, owner_user_id, revision_id, list(stage_ids)))
    digest_value, execution_count = self._digest_frozen_rows(cur, evaluation_run_id)
    if execution_count <= 0:
      raise ExperimentError(
        "experiment.evaluation_result_set_incomplete",
        "评价来源没有可用的成功结果",
        409,
      )
    cur.execute(
      "DELETE FROM backtest_experiment_evaluation_exception "
      "WHERE evaluation_run_id=%s",
      (evaluation_run_id,),
    )
    cur.execute("""
      INSERT INTO backtest_experiment_evaluation_exception (
        evaluation_run_id, owner_user_id, ordinal, stage_id, stage_index,
        trial_id, trial_index, status, materialization_status, error_summary
      )
      SELECT %s, %s,
        row_number() OVER (
          ORDER BY stage.stage_index, trial.trial_index, trial.id
        ) - 1,
        stage.id, stage.stage_index, trial.id, trial.trial_index,
        CASE
          WHEN trial.summary_status IN ('failed','cancelled')
            THEN trial.summary_status
          ELSE 'missing'
        END,
        trial.materialization_status, latest.error_summary
      FROM backtest_experiment_stage stage
      JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
      LEFT JOIN LATERAL (
        SELECT error_summary
        FROM backtest_experiment_trial_execution execution
        WHERE execution.trial_id=trial.id
        ORDER BY execution.execution_no DESC
        LIMIT 1
      ) latest ON true
      LEFT JOIN backtest_experiment_trial_execution selected
        ON selected.id=trial.selected_execution_id
      LEFT JOIN backtest_run selected_run ON selected_run.id=selected.run_id
      WHERE stage.revision_id=%s AND stage.id=ANY(%s::uuid[])
        AND NOT (
          trial.summary_status='succeeded'
          AND trial.selected_execution_id IS NOT NULL
          AND selected_run.status='succeeded'
          AND selected.result_hash IS NOT NULL
        )
      ORDER BY stage.stage_index, trial.trial_index, trial.id
    """, (
      evaluation_run_id, owner_user_id, revision_id, list(stage_ids),
    ))
    completeness = self._completeness(
      cur,
      stages,
      execution_count,
      evaluation_run_id=evaluation_run_id,
      experiment_id=experiment_id,
      requirements=required_dimension_coverage,
    )
    cur.execute("""
      UPDATE backtest_experiment_evaluation_run
      SET output_schema_version=%s, execution_count=%s,
        planned_count=%s, generated_count=%s, succeeded_count=%s,
        failed_count=%s, cancelled_count=%s, missing_count=%s,
        dataset_digest=%s, dataset_digest_version=%s,
        result_set_hash=%s, completeness_snapshot=%s, updated_at=now()
      WHERE id=%s
    """, (
      OUTPUT_SCHEMA_VERSION, execution_count,
      completeness["plannedTrialCount"], completeness["generatedTrialCount"],
      completeness["succeededCount"], completeness["failedCount"],
      completeness["cancelledCount"], completeness["missingCount"],
      digest_value, DATASET_DIGEST_VERSION, digest_value,
      Jsonb(completeness), evaluation_run_id,
    ))
    return FrozenDataset(execution_count, digest_value, completeness)

  def iter_input_batches(
    self,
    evaluation_run_id: str,
    *,
    batch_size: int = 200,
  ) -> Iterator[list[dict[str, Any]]]:
    if batch_size < 1 or batch_size > 2000:
      raise ValueError("评价批量大小必须在 1 到 2000 之间")
    next_ordinal = 0
    while True:
      with _worker_session() as conn:
        with conn.cursor() as cur:
          cur.execute("""
            SELECT frozen.ordinal, frozen.trial_execution_id,
              frozen.result_hash AS frozen_result_hash,
              frozen.result_schema_version AS frozen_result_schema_version,
              frozen.actual_data_hash AS frozen_actual_data_hash,
              execution.id AS execution_id, execution.run_id,
              execution.result_schema_version, execution.result_hash,
              execution.actual_data_hash, trial.id AS trial_id,
              trial.trial_index, trial.trial_spec, trial.dimension_projection,
              stage.id AS stage_id, stage.stage_index, stage.stage_type,
              run.status AS run_status, run.result_json, run.requested_params,
              run.fill_model, run.queued_at, run.started_at, run.finished_at,
              revision.experiment_spec
            FROM backtest_experiment_evaluation_execution frozen
            JOIN backtest_experiment_trial_execution execution
              ON execution.id=frozen.trial_execution_id
            JOIN backtest_experiment_trial trial ON trial.id=frozen.trial_id
            JOIN backtest_experiment_stage stage ON stage.id=frozen.stage_id
            JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
            JOIN backtest_run run ON run.id=frozen.run_id
            WHERE frozen.evaluation_run_id=%s AND frozen.ordinal >= %s
            ORDER BY frozen.ordinal
            LIMIT %s
          """, (evaluation_run_id, next_ordinal, batch_size))
          rows = [dict(row) for row in cur.fetchall()]
      if not rows:
        return
      self._validate_frozen_rows(rows)
      yield rows
      next_ordinal = int(rows[-1]["ordinal"]) + 1

  def evaluate_streaming(
    self,
    run: dict[str, Any],
    source_adapter: Callable[[dict[str, Any]], dict[str, Any]],
    heartbeat: Callable[[], str],
  ) -> dict[str, Any]:
    """按固定批量计算 v2 结果，并把全局排序状态留在数据库。"""
    from backend.experiments.evaluation_streaming import StreamingEvaluationExecutor

    return StreamingEvaluationExecutor(self).evaluate(run, source_adapter, heartbeat)

  def write_result_batch(
    self,
    evaluation_run_id: str,
    owner_user_id: str,
    rows: Sequence[dict[str, Any]],
  ) -> None:
    if not rows:
      return
    values = [(
      evaluation_run_id,
      owner_user_id,
      int(row["sourceOrdinal"]),
      str(row["trialExecutionId"]),
      Jsonb(row["identity"]),
      Jsonb(row["dimensions"]),
      Jsonb(row["metricValues"]),
      Jsonb(row["metricDomains"]),
      Jsonb(row.get("rankingKey") or []),
      Jsonb(row.get("groupKey") or []),
      Jsonb(row.get("values") or {}),
      Jsonb(row.get("unsupportedReasons") or {}),
    ) for row in rows]
    with _worker_session() as conn:
      with conn.cursor() as cur:
        cur.executemany("""
          INSERT INTO backtest_experiment_evaluation_result_row (
            evaluation_run_id, owner_user_id, source_ordinal, trial_execution_id,
            identity, dimensions, metric_values, metric_domains,
            ranking_key, group_key, values, unsupported_reasons
          ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT (evaluation_run_id, source_ordinal) DO UPDATE
          SET trial_execution_id=EXCLUDED.trial_execution_id,
            identity=EXCLUDED.identity,
            dimensions=EXCLUDED.dimensions,
            metric_values=EXCLUDED.metric_values,
            metric_domains=EXCLUDED.metric_domains,
            ordinal=NULL,
            ranking_key=EXCLUDED.ranking_key,
            group_key=EXCLUDED.group_key,
            values=EXCLUDED.values,
            unsupported_reasons=EXCLUDED.unsupported_reasons
        """, values)

  def page_results(
    self,
    evaluation_run_id: str,
    *,
    after_ordinal: int = -1,
    limit: int = 100,
  ) -> dict[str, Any]:
    if limit < 1 or limit > 500:
      raise ValueError("评价分页大小必须在 1 到 500 之间")
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT result.ordinal, result.trial_execution_id,
            result.ranking_key, result.group_key, result.values,
            result.unsupported_reasons, frozen.run_id, frozen.stage_id,
            frozen.trial_id, frozen.trial_index, stage.stage_index,
            trial.dimension_projection
          FROM backtest_experiment_evaluation_result_row result
          JOIN backtest_experiment_evaluation_execution frozen
            ON frozen.evaluation_run_id=result.evaluation_run_id
           AND frozen.trial_execution_id=result.trial_execution_id
          JOIN backtest_experiment_stage stage ON stage.id=frozen.stage_id
          JOIN backtest_experiment_trial trial ON trial.id=frozen.trial_id
          WHERE result.evaluation_run_id=%s AND result.owner_user_id=%s
            AND result.ordinal IS NOT NULL AND result.ordinal > %s
          ORDER BY result.ordinal LIMIT %s
        """, (evaluation_run_id, owner_id, after_ordinal, limit + 1))
        rows = [dict(row) for row in cur.fetchall()]
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
      "items": [self._result_item(row) for row in page],
      "nextCursor": str(page[-1]["ordinal"]) if has_more and page else None,
    }

  def page_groups(
    self,
    evaluation_run_id: str,
    *,
    after_ordinal: int = -1,
    limit: int = 100,
  ) -> dict[str, Any]:
    if limit < 1 or limit > 500:
      raise ValueError("评价分页大小必须在 1 到 500 之间")
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT group_id, ordinal, group_key, member_count, values,
            unsupported_reasons
          FROM backtest_experiment_evaluation_group_result
          WHERE evaluation_run_id=%s AND owner_user_id=%s
            AND ordinal IS NOT NULL AND ordinal > %s
          ORDER BY ordinal LIMIT %s
        """, (evaluation_run_id, owner_id, after_ordinal, limit + 1))
        rows = [dict(row) for row in cur.fetchall()]
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
      "items": [{
        "ordinal": str(row["ordinal"]),
        "groupId": str(row["group_id"]),
        "groupKey": row["group_key"],
        "memberCount": str(row["member_count"]),
        "values": row["values"],
        "unsupportedReasons": row["unsupported_reasons"],
      } for row in page],
      "nextCursor": str(page[-1]["ordinal"]) if has_more and page else None,
    }

  def page_group_members(
    self,
    evaluation_run_id: str,
    group_id: str,
    *,
    after_ordinal: int = -1,
    limit: int = 100,
  ) -> dict[str, Any]:
    if limit < 1 or limit > 500:
      raise ValueError("评价成员分页大小必须在 1 到 500 之间")
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT group_key
          FROM backtest_experiment_evaluation_group_result
          WHERE evaluation_run_id=%s AND owner_user_id=%s AND group_id=%s
        """, (evaluation_run_id, owner_id, group_id))
        group = cur.fetchone()
        if group is None:
          raise ExperimentError(
            "experiment.evaluation_group_not_found", "评价分组不存在", 404,
          )
        cur.execute("""
          SELECT result.source_ordinal AS ordinal, result.trial_execution_id,
            frozen.run_id, frozen.stage_id, frozen.trial_id,
            frozen.trial_index, stage.stage_index, trial.dimension_projection
          FROM backtest_experiment_evaluation_result_row result
          JOIN backtest_experiment_evaluation_execution frozen
            ON frozen.evaluation_run_id=result.evaluation_run_id
           AND frozen.trial_execution_id=result.trial_execution_id
          JOIN backtest_experiment_stage stage ON stage.id=frozen.stage_id
          JOIN backtest_experiment_trial trial ON trial.id=frozen.trial_id
          WHERE result.evaluation_run_id=%s AND result.owner_user_id=%s
            AND result.group_key=%s AND result.source_ordinal > %s
          ORDER BY result.source_ordinal LIMIT %s
        """, (
          evaluation_run_id, owner_id, Jsonb(group["group_key"]),
          after_ordinal, limit + 1,
        ))
        rows = [dict(row) for row in cur.fetchall()]
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
      "items": [{
        "ordinal": str(row["ordinal"]),
        "trialExecutionId": str(row["trial_execution_id"]),
        "runId": str(row["run_id"]),
        "stageId": str(row["stage_id"]),
        "stageIndex": int(row["stage_index"]),
        "trialId": str(row["trial_id"]),
        "trialIndex": int(row["trial_index"]),
        "startDate": dict(row.get("dimension_projection") or {}).get(
          "dateScenario", {},
        ).get("startDate"),
      } for row in page],
      "nextCursor": str(page[-1]["ordinal"]) if has_more and page else None,
    }

  def page_exceptions(
    self,
    evaluation_run_id: str,
    *,
    after_ordinal: int = -1,
    limit: int = 100,
  ) -> dict[str, Any]:
    if limit < 1 or limit > 500:
      raise ValueError("评价异常分页大小必须在 1 到 500 之间")
    owner_id = state_store.current_user_id()
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT ordinal, stage_id, stage_index, trial_id, trial_index,
            status, materialization_status, error_summary
          FROM backtest_experiment_evaluation_exception
          WHERE evaluation_run_id=%s AND owner_user_id=%s AND ordinal > %s
          ORDER BY ordinal LIMIT %s
        """, (evaluation_run_id, owner_id, after_ordinal, limit + 1))
        rows = [dict(row) for row in cur.fetchall()]
    has_more = len(rows) > limit
    page = rows[:limit]
    return {
      "items": [{
        "ordinal": str(row["ordinal"]),
        "stageId": str(row["stage_id"]),
        "stageIndex": int(row["stage_index"]),
        "trialId": str(row["trial_id"]),
        "trialIndex": int(row["trial_index"]),
        "status": row["status"],
        "materializationStatus": row["materialization_status"],
        "error": row["error_summary"],
      } for row in page],
      "nextCursor": str(page[-1]["ordinal"]) if has_more and page else None,
    }

  def iter_persisted_output_rows(
    self,
    evaluation_run_id: str,
    *,
    batch_size: int = 500,
  ) -> Iterator[dict[str, Any]]:
    next_ordinal = 0
    grouped: bool | None = None
    while True:
      with _worker_session() as conn:
        with conn.cursor() as cur:
          if grouped is None:
            cur.execute("""
              SELECT EXISTS (
                SELECT 1 FROM backtest_experiment_evaluation_group_result
                WHERE evaluation_run_id=%s
              ) AS value
            """, (evaluation_run_id,))
            grouped = bool(cur.fetchone()["value"])
          table = (
            "backtest_experiment_evaluation_group_result"
            if grouped else "backtest_experiment_evaluation_result_row"
          )
          cur.execute(f"""
            SELECT * FROM {table}
            WHERE evaluation_run_id=%s AND ordinal >= %s
            ORDER BY ordinal LIMIT %s
          """, (evaluation_run_id, next_ordinal, batch_size))
          rows = [dict(row) for row in cur.fetchall()]
      if not rows:
        return
      for row in rows:
        yield {
          "trialExecutionId": (
            str(row["trial_execution_id"])
            if row.get("trial_execution_id") else None
          ),
          "groupKey": row.get("group_key"),
          "memberCount": (
            int(row["member_count"]) if row.get("member_count") is not None else None
          ),
          "values": row["values"],
          "unsupportedReasons": row.get("unsupported_reasons") or {},
        }
      next_ordinal = int(rows[-1]["ordinal"]) + 1

  def persist_output(
    self,
    cur: Any,
    *,
    evaluation_run_id: str,
    experiment_id: str,
    owner_user_id: str,
    output: dict[str, Any],
  ) -> dict[str, Any]:
    """确认流式计算已经落库，并生成不内嵌行级结果的摘要。"""
    if output.get("normalizedResultsPersisted") is True:
      return self._normalized_output_summary(
        evaluation_run_id=evaluation_run_id,
        experiment_id=experiment_id,
        output=output,
      )
    raise ExperimentError(
      "experiment.evaluation_output_contract_invalid",
      "评价 v2 结果未通过规范化数据集写入",
      500,
    )

  @staticmethod
  def _normalized_output_summary(
    *,
    evaluation_run_id: str,
    experiment_id: str,
    output: dict[str, Any],
  ) -> dict[str, Any]:
    grouped = bool(output.get("grouped"))
    return {
      key: value for key, value in output.items()
      if key not in {"normalizedResultsPersisted", "rows"}
    } | {
      "rows": [],
      "resultPage": (
        f"/api/backtest/experiments/{experiment_id}/evaluation-runs/"
        f"{evaluation_run_id}/results"
      ),
      "groupResultPage": (
        f"/api/backtest/experiments/{experiment_id}/evaluation-runs/"
        f"{evaluation_run_id}/results"
        if grouped else None
      ),
      "visualizationCount": int(output.get("visualizationCount") or 0),
    }

  @staticmethod
  def _stage_counts(
    cur: Any,
    revision_id: str,
    stage_ids: Sequence[str],
  ) -> list[dict[str, Any]]:
    cur.execute("""
      SELECT stage.id, stage.stage_index, stage.status, stage.stage_spec,
        revision.experiment_spec,
        stage.planned_trial_count, stage.generated_trial_count,
        stage.cancelled_before_generation,
        count(trial.id) FILTER (WHERE trial.summary_status='failed') AS failed_count,
        count(trial.id) FILTER (WHERE trial.summary_status='cancelled') AS cancelled_count
      FROM backtest_experiment_stage stage
      JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
      LEFT JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
      WHERE stage.revision_id=%s AND stage.id=ANY(%s::uuid[])
      GROUP BY stage.id, revision.experiment_spec
      ORDER BY stage.stage_index, stage.id
    """, (revision_id, list(stage_ids)))
    rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != len(stage_ids):
      raise ExperimentError(
        "experiment.evaluation_source_invalid",
        "评价来源包含未知 Stage",
        422,
      )
    return rows

  @staticmethod
  def _digest_frozen_rows(cur: Any, run_id: str) -> tuple[str, int]:
    hasher = hashlib.sha256()
    hasher.update(f"{DATASET_DIGEST_VERSION}\n".encode("utf-8"))
    cur.execute("""
      SELECT ordinal, stage_id, trial_id, trial_index, trial_execution_id,
        run_id, result_hash, result_schema_version, actual_data_hash
      FROM backtest_experiment_evaluation_execution
      WHERE evaluation_run_id=%s ORDER BY ordinal
    """, (run_id,))
    count = 0
    while True:
      batch = cur.fetchmany(500)
      if not batch:
        break
      for source in batch:
        encoded = json.dumps(
          {key: str(value) if value is not None else None for key, value in dict(source).items()},
          ensure_ascii=False,
          sort_keys=True,
          separators=(",", ":"),
        )
        hasher.update(encoded.encode("utf-8"))
        hasher.update(b"\n")
        count += 1
    return hasher.hexdigest(), count

  @staticmethod
  def _completeness(
    cur: Any,
    stages: Sequence[dict[str, Any]],
    succeeded_count: int,
    *,
    evaluation_run_id: str,
    experiment_id: str,
    requirements: Sequence[dict[str, Any]],
  ) -> dict[str, Any]:
    planned = sum(int(row["planned_trial_count"]) for row in stages)
    generated = sum(int(row["generated_trial_count"]) for row in stages)
    failed = sum(int(row["failed_count"]) for row in stages)
    cancelled = sum(
      int(row["cancelled_count"]) + int(row["cancelled_before_generation"])
      for row in stages
    )
    missing = max(0, planned - succeeded_count - failed - cancelled)
    intervals = []
    for row in stages:
      start = int(row["generated_trial_count"])
      end = int(row["planned_trial_count"])
      if end > start:
        intervals.append({
          "stageId": str(row["id"]),
          "stageIndex": int(row["stage_index"]),
          "startTrialIndex": start,
          "endTrialIndexExclusive": end,
          "count": end - start,
          "status": (
            "cancelled"
            if int(row["cancelled_before_generation"]) >= end - start
            else "missing"
          ),
        })
    return {
      "plannedTrialCount": planned,
      "generatedTrialCount": generated,
      "succeededCount": succeeded_count,
      "failedCount": failed,
      "cancelledCount": cancelled,
      "missingCount": missing,
      "completionRate": format(succeeded_count / planned, ".4f") if planned else "0.0000",
      "isPartial": succeeded_count != planned,
      "ungeneratedIntervals": intervals,
      "dimensionCoverage": EvaluationDataset._dimension_coverage(
        cur, evaluation_run_id, stages, requirements,
      ),
      "exceptions": intervals,
      "exceptionPage": (
        f"/api/backtest/experiments/{experiment_id}/evaluation-runs/"
        f"{evaluation_run_id}/exceptions"
      ),
      "stageStatuses": [{
        "stageId": str(row["id"]),
        "stageIndex": int(row["stage_index"]),
        "status": str(row["status"]),
      } for row in stages],
    }

  @staticmethod
  def _dimension_coverage(
    cur: Any,
    evaluation_run_id: str,
    stages: Sequence[dict[str, Any]],
    requirements: Sequence[dict[str, Any]],
  ) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for requirement in requirements:
      dimension_id = str(requirement["dimensionId"])
      expected: dict[str, tuple[Any, int]] = {}
      for stage in stages:
        for value, count in _expected_dimension_counts(stage, dimension_id):
          key = _json_key(value)
          current = expected.get(key, (value, 0))
          expected[key] = (value, current[1] + count)
      path = _dimension_json_path(stages, dimension_id)
      cur.execute("""
        SELECT trial.dimension_projection #> %s::text[] AS value,
          count(*) AS succeeded_count
        FROM backtest_experiment_evaluation_execution frozen
        JOIN backtest_experiment_trial trial ON trial.id=frozen.trial_id
        WHERE frozen.evaluation_run_id=%s
        GROUP BY trial.dimension_projection #> %s::text[]
      """, (path, evaluation_run_id, path))
      succeeded = {
        _json_key(row["value"]): int(row["succeeded_count"])
        for row in cur.fetchall()
      }
      values = [{
        "value": value,
        "expectedCount": count,
        "succeededCount": succeeded.get(key, 0),
        "coverageRate": _rate_text(succeeded.get(key, 0), count),
      } for key, (value, count) in sorted(expected.items())]
      result.append({
        "dimensionId": dimension_id,
        "requiredRate": str(requirement["minimumCoverageRate"]),
        "minimumObservedRate": min(
          (item["coverageRate"] for item in values),
          default="0.0000",
        ),
        "values": values,
      })
    return result

  @staticmethod
  def _validate_frozen_rows(rows: Sequence[dict[str, Any]]) -> None:
    for row in rows:
      if (
        str(row["execution_id"]) != str(row["trial_execution_id"])
        or str(row["result_hash"]) != str(row["frozen_result_hash"])
        or str(row["result_schema_version"])
        != str(row["frozen_result_schema_version"])
        or row["run_status"] != "succeeded"
      ):
        raise ExperimentError(
          "experiment.evaluation_result_source_mismatch",
          "评价冻结的执行来源已经变化",
          409,
        )

  @staticmethod
  def _result_item(row: dict[str, Any]) -> dict[str, Any]:
    date_scenario = dict(row.get("dimension_projection") or {}).get(
      "dateScenario", {},
    )
    return {
      "ordinal": str(row["ordinal"]),
      "trialExecutionId": str(row["trial_execution_id"]),
      "runId": str(row["run_id"]),
      "stageId": str(row["stage_id"]),
      "stageIndex": int(row["stage_index"]),
      "trialId": str(row["trial_id"]),
      "trialIndex": int(row["trial_index"]),
      "startDate": date_scenario.get("startDate"),
      "rankingKey": row["ranking_key"],
      "groupKey": row["group_key"],
      "values": row["values"],
      "unsupportedReasons": row["unsupported_reasons"],
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


def _group_key(row: dict[str, Any], output: dict[str, Any]) -> list[Any]:
  values = dict(row.get("values") or {})
  return [values.get(field) for field in list(output.get("groupBy") or [])]


def _expected_dimension_counts(
  stage: dict[str, Any],
  dimension_id: str,
) -> list[tuple[Any, int]]:
  stage_spec = dict(stage.get("stage_spec") or {})
  planned = int(stage["planned_trial_count"])
  candidates = _dimension_candidates(stage_spec, dimension_id)
  if not candidates:
    return [(None, planned)]
  per_candidate = planned // len(candidates)
  counts: dict[str, tuple[Any, int]] = {}
  for value in candidates:
    key = _json_key(value)
    current = counts.get(key, (value, 0))
    counts[key] = (value, current[1] + per_candidate)
  return list(counts.values())


def _dimension_candidates(
  stage_spec: dict[str, Any],
  dimension_id: str,
) -> list[Any]:
  if dimension_id.startswith("parameter."):
    input_id = dimension_id.removeprefix("parameter.")
    for item in list(stage_spec.get("parameterGrid") or []):
      if isinstance(item, dict) and item.get("inputId") == input_id:
        return list(item.get("values") or [])
  mapping = {
    "dateScenarioId": ("dateScenarios", "scenarioId"),
    "dateRole": ("dateScenarios", "role"),
    "dateStart": ("dateScenarios", "startDate"),
    "dateEnd": ("dateScenarios", "endDate"),
    "initialCapital": ("capitalScenarios", None),
    "costScenarioId": ("costScenarios", "scenarioId"),
    "slippage": ("costScenarios", "slippage"),
    "executionScenarioId": ("executionScenarios", "scenarioId"),
  }
  source = mapping.get(dimension_id)
  if source is None:
    return []
  collection = list(stage_spec.get(source[0]) or [])
  return [item.get(source[1]) for item in collection] if source[1] else collection


def _dimension_json_path(
  stages: Sequence[dict[str, Any]],
  dimension_id: str,
) -> list[str]:
  if dimension_id.startswith("parameter."):
    input_id = dimension_id.removeprefix("parameter.")
    experiment_spec = dict(stages[0].get("experiment_spec") or {}) if stages else {}
    template = dict(experiment_spec.get("runTemplateSnapshot") or {})
    inputs = list(dict(template.get("tunableInputCatalog") or {}).get("inputs") or [])
    source_name = next((
      str(item["sourceName"])
      for item in inputs
      if isinstance(item, dict)
      and str(item.get("inputId") or "") == input_id
      and item.get("sourceName")
    ), input_id)
    return ["strategyParameters", source_name]
  mapping = {
    "dateScenarioId": ["dateScenario", "scenarioId"],
    "dateRole": ["dateScenario", "role"],
    "dateStart": ["dateScenario", "startDate"],
    "dateEnd": ["dateScenario", "endDate"],
    "initialCapital": ["initialCapital"],
    "costScenarioId": ["costScenario", "scenarioId"],
    "slippage": ["costScenario", "slippage"],
    "executionScenarioId": ["executionScenario", "scenarioId"],
  }
  return mapping[dimension_id]


def _rate_text(numerator: int, denominator: int) -> str:
  if denominator <= 0:
    return "0.0000"
  return format(Decimal(numerator) / Decimal(denominator), ".4f")


def _json_key(value: Any) -> str:
  return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
