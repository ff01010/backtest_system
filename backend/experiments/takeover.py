"""活动 Experiment Revision 的可重复接管与守恒报告。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.services import state_store


_TRANSIENT_BLOCK_CODES = (
  "experiment.capacity_blocked",
  "experiment.manual_queue_reserved",
  "experiment.manual_wait_blocked",
)
_EXECUTION_CIRCUIT_CODE = "experiment.execution_blocked"


@dataclass(frozen=True)
class TakeoverResult:
  applied: bool
  report: dict[str, Any]


class ExperimentRevisionTakeover:
  """把旧活动 Revision 接管到滚动窗口，异常时保持数据原样。"""

  def run(
    self,
    *,
    apply: bool,
    build_id: str,
    actor: str,
  ) -> TakeoverResult:
    with _session() as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
          ("experiment-revision-takeover-v1",),
        )
        rows = _load_stage_facts(cur, lock=apply)
        report = _build_report(rows)
        if apply and report["anomalyCount"] == 0:
          _apply_takeover(cur, rows, build_id)
          report = _build_report(_load_stage_facts(cur, lock=False))
          report["appliedStageCount"] = len(rows)
          applied = True
        else:
          report["appliedStageCount"] = 0
          applied = False
        _save_audit(cur, apply, applied, build_id, actor, report)
        return TakeoverResult(applied, report)


def _load_stage_facts(cur: Any, *, lock: bool) -> list[dict[str, Any]]:
  if lock:
    cur.execute("""
      SELECT experiment.id
      FROM backtest_experiment experiment
      JOIN backtest_experiment_revision revision
        ON revision.id=experiment.current_revision_id
      JOIN backtest_experiment_stage stage ON stage.revision_id=revision.id
      WHERE revision.terminal_at IS NULL
      ORDER BY revision.owner_user_id, experiment.id, stage.stage_index, stage.id
      FOR UPDATE OF experiment, revision, stage
    """)
    cur.fetchall()
  cur.execute("""
    SELECT experiment.id AS experiment_id,
      revision.id AS revision_id, revision.owner_user_id,
      revision.lifecycle_state, revision.generation_policy_version,
      revision.system_block_code,
      stage.id AS stage_id, stage.status AS legacy_status,
      stage.blocked_reason_code, stage.planned_trial_count,
      stage.generated_trial_count, stage.generation_cursor,
      stage.cancelled_before_generation,
      count(trial.id) AS trial_count,
      count(trial.id) FILTER (WHERE trial.summary_status NOT IN (
        'succeeded', 'failed', 'cancelled'
      )) AS open_count,
      count(trial.id) FILTER (WHERE trial.summary_status IN (
        'succeeded', 'failed', 'cancelled'
      )) AS terminal_count,
      count(trial.id) FILTER (WHERE trial.summary_status='running') AS running_count,
      count(trial.id) FILTER (WHERE trial.summary_status='cancelling') AS cancelling_count,
      count(trial.id) FILTER (WHERE trial.summary_status IN ('pending','queued'))
        AS waiting_count
    FROM backtest_experiment experiment
    JOIN backtest_experiment_revision revision
      ON revision.id=experiment.current_revision_id
    JOIN backtest_experiment_stage stage ON stage.revision_id=revision.id
    LEFT JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
    WHERE revision.terminal_at IS NULL
    GROUP BY experiment.id, revision.id, stage.id
    ORDER BY revision.owner_user_id, experiment.id, stage.stage_index, stage.id
  """)
  return [dict(row) for row in cur.fetchall()]


def _build_report(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
  anomalies: list[dict[str, Any]] = []
  totals = {"planned": 0, "generated": 0, "open": 0, "terminal": 0}
  for row in rows:
    planned = int(row["planned_trial_count"])
    generated = int(row["generated_trial_count"])
    cursor = int(row["generation_cursor"])
    trial_count = int(row["trial_count"])
    open_count = int(row["open_count"])
    terminal_count = int(row["terminal_count"])
    cancelled_tail = int(row["cancelled_before_generation"])
    reasons: list[str] = []
    if not (0 <= generated <= planned):
      reasons.append("generated_out_of_range")
    if cursor != generated:
      reasons.append("cursor_generated_mismatch")
    if trial_count != generated:
      reasons.append("trial_generated_mismatch")
    if open_count + terminal_count != trial_count:
      reasons.append("trial_status_partition_mismatch")
    if cancelled_tail < 0 or cancelled_tail > planned - min(generated, planned):
      reasons.append("cancelled_tail_out_of_range")
    block_code = row.get("blocked_reason_code")
    if block_code and block_code not in {
      *_TRANSIENT_BLOCK_CODES, _EXECUTION_CIRCUIT_CODE,
    }:
      reasons.append("ambiguous_block_reason")
    totals["planned"] += planned
    totals["generated"] += generated
    totals["open"] += open_count
    totals["terminal"] += terminal_count + cancelled_tail
    if reasons:
      anomalies.append({
        "experimentId": str(row["experiment_id"]),
        "revisionId": str(row["revision_id"]),
        "stageId": str(row["stage_id"]),
        "reasons": reasons,
        "planned": planned,
        "generated": generated,
        "cursor": cursor,
        "trialCount": trial_count,
        "open": open_count,
        "terminal": terminal_count,
        "cancelledBeforeGeneration": cancelled_tail,
        "blockedReasonCode": block_code,
      })
  return {
    "reportVersion": "experiment_takeover_report_v1",
    "activeStageCount": len(rows),
    "anomalyCount": len(anomalies),
    "totals": totals,
    "anomalies": anomalies,
  }


def _apply_takeover(
  cur: Any,
  rows: Sequence[dict[str, Any]],
  build_id: str,
) -> None:
  if not rows:
    return
  cur.execute("LOCK TABLE backtest_experiment_window_global IN ROW EXCLUSIVE MODE")
  cur.execute("LOCK TABLE backtest_experiment_window_user IN ROW EXCLUSIVE MODE")
  cur.execute("LOCK TABLE backtest_experiment_window_stage IN ROW EXCLUSIVE MODE")
  cur.execute("""
    UPDATE backtest_experiment_revision revision
    SET system_block_code=CASE
          WHEN stage.blocked_reason_code=%s THEN 'execution_circuit'
          ELSE revision.system_block_code END,
        system_block_message=CASE
          WHEN stage.blocked_reason_code=%s THEN stage.blocked_reason_message
          ELSE revision.system_block_message END,
        blocked_reason_code=CASE
          WHEN stage.blocked_reason_code=ANY(%s) THEN NULL
          ELSE revision.blocked_reason_code END,
        blocked_reason_message=CASE
          WHEN stage.blocked_reason_code=ANY(%s) THEN NULL
          ELSE revision.blocked_reason_message END,
        updated_at=now()
    FROM backtest_experiment_stage stage
    WHERE stage.revision_id=revision.id AND revision.terminal_at IS NULL
  """, (
    _EXECUTION_CIRCUIT_CODE, _EXECUTION_CIRCUIT_CODE,
    list(_TRANSIENT_BLOCK_CODES), list(_TRANSIENT_BLOCK_CODES),
  ))
  cur.execute("""
    UPDATE backtest_experiment experiment
    SET blocked_reason_code=NULL, blocked_reason_message=NULL,
      updated_at=now(), row_version=row_version+1
    FROM backtest_experiment_revision revision
    WHERE revision.id=experiment.current_revision_id
      AND revision.terminal_at IS NULL
      AND EXISTS (
        SELECT 1 FROM backtest_experiment_stage stage
        WHERE stage.revision_id=revision.id
          AND stage.blocked_reason_code=ANY(%s)
      )
  """, (list(_TRANSIENT_BLOCK_CODES),))
  cur.execute("""
    UPDATE backtest_experiment_stage stage
    SET blocked_reason_code=CASE
          WHEN stage.blocked_reason_code=ANY(%s) THEN NULL
          ELSE stage.blocked_reason_code END,
        blocked_reason_message=CASE
          WHEN stage.blocked_reason_code=ANY(%s) THEN NULL
          ELSE stage.blocked_reason_message END,
        blocked_scope=CASE
          WHEN stage.blocked_reason_code=ANY(%s) THEN NULL ELSE stage.blocked_scope END,
        blocked_at=CASE
          WHEN stage.blocked_reason_code=ANY(%s) THEN NULL ELSE stage.blocked_at END,
        execution_phase=CASE
          WHEN stage.blocked_reason_code=%s THEN 'blocked'
          WHEN facts.running_count + facts.cancelling_count > 0 THEN 'running'
          WHEN facts.waiting_count > 0 THEN 'queued'
          WHEN stage.generated_trial_count < stage.planned_trial_count THEN 'preparing'
          ELSE 'queued' END,
        status=CASE
          WHEN stage.blocked_reason_code=%s THEN 'generation_blocked'
          WHEN stage.blocked_reason_code=ANY(%s)
            AND facts.running_count + facts.cancelling_count > 0 THEN 'running'
          WHEN stage.blocked_reason_code=ANY(%s) AND facts.waiting_count > 0 THEN 'queued'
          WHEN stage.blocked_reason_code=ANY(%s)
            AND stage.generated_trial_count < stage.planned_trial_count THEN 'preparing'
          ELSE stage.status END,
        updated_at=now(), row_version=stage.row_version+1
    FROM (
      SELECT source_stage.id AS stage_id,
        count(*) FILTER (WHERE trial.summary_status='running') AS running_count,
        count(*) FILTER (WHERE trial.summary_status='cancelling') AS cancelling_count,
        count(*) FILTER (WHERE trial.summary_status IN ('pending','queued')) AS waiting_count
      FROM backtest_experiment_stage source_stage
      LEFT JOIN backtest_experiment_trial trial ON trial.stage_id=source_stage.id
      GROUP BY source_stage.id
    ) facts, backtest_experiment_revision revision
    WHERE facts.stage_id=stage.id
      AND revision.id=stage.revision_id
      AND revision.terminal_at IS NULL
  """, (
    list(_TRANSIENT_BLOCK_CODES), list(_TRANSIENT_BLOCK_CODES),
    list(_TRANSIENT_BLOCK_CODES), list(_TRANSIENT_BLOCK_CODES),
    _EXECUTION_CIRCUIT_CODE, _EXECUTION_CIRCUIT_CODE,
    list(_TRANSIENT_BLOCK_CODES), list(_TRANSIENT_BLOCK_CODES),
    list(_TRANSIENT_BLOCK_CODES),
  ))
  cur.execute("""
    INSERT INTO backtest_experiment_window_stage (
      stage_id, owner_user_id, open_count, reserved_count, rotation_sequence
    )
    SELECT stage.id, stage.owner_user_id,
      count(trial.id) FILTER (WHERE trial.summary_status NOT IN (
        'succeeded', 'failed', 'cancelled'
      )), 0,
      row_number() OVER (ORDER BY stage.created_at, stage.id)
    FROM backtest_experiment_stage stage
    JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
    LEFT JOIN backtest_experiment_trial trial ON trial.stage_id=stage.id
    WHERE revision.terminal_at IS NULL
    GROUP BY stage.id
    ON CONFLICT (stage_id) DO UPDATE
    SET owner_user_id=EXCLUDED.owner_user_id,
      open_count=EXCLUDED.open_count,
      reserved_count=0,
      row_version=backtest_experiment_window_stage.row_version+1,
      updated_at=now()
  """)
  cur.execute("""
    INSERT INTO backtest_experiment_window_user (
      owner_user_id, open_count, reserved_count, rotation_sequence
    )
    SELECT owner_user_id, sum(open_count), sum(reserved_count),
      row_number() OVER (ORDER BY owner_user_id)
    FROM backtest_experiment_window_stage GROUP BY owner_user_id
    ON CONFLICT (owner_user_id) DO UPDATE
    SET open_count=EXCLUDED.open_count,
      reserved_count=EXCLUDED.reserved_count,
      row_version=backtest_experiment_window_user.row_version+1,
      updated_at=now()
  """)
  cur.execute("""
    UPDATE backtest_experiment_window_global
    SET open_count=(SELECT COALESCE(sum(open_count),0)
                    FROM backtest_experiment_window_user),
      reserved_count=(SELECT COALESCE(sum(reserved_count),0)
                      FROM backtest_experiment_window_user),
      row_version=row_version+1, updated_at=now()
    WHERE singleton
  """)
  cur.execute("""
    UPDATE backtest_experiment_revision revision
    SET generation_policy_version='rolling_window_v1', updated_at=now()
    FROM backtest_experiment experiment
    WHERE experiment.current_revision_id=revision.id
      AND revision.terminal_at IS NULL
      AND revision.generation_policy_version<>'rolling_window_v1'
  """)
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id, event_type, detail
    )
    SELECT stage.owner_user_id, revision.experiment_id, revision.id, stage.id,
      'rolling_window.takeover', %s
    FROM backtest_experiment_stage stage
    JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
    JOIN backtest_experiment experiment ON experiment.current_revision_id=revision.id
    WHERE revision.terminal_at IS NULL
      AND NOT EXISTS (
        SELECT 1 FROM backtest_experiment_audit audit
        WHERE audit.stage_id=stage.id AND audit.event_type='rolling_window.takeover'
      )
  """, (Jsonb({"buildId": build_id}),))


def _save_audit(
  cur: Any,
  requested_apply: bool,
  applied: bool,
  build_id: str,
  actor: str,
  report: dict[str, Any],
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_takeover_audit (
      requested_mode, applied, build_id, actor, report
    ) VALUES (%s,%s,%s,%s,%s)
  """, (
    "apply" if requested_apply else "preflight",
    applied, build_id, actor, Jsonb(report),
  ))


def _session() -> psycopg.Connection[Any]:
  if not settings.DATABASE_URL:
    raise state_store.DatabaseError("未设置 TREND_GRID_DATABASE_URL")
  try:
    conn = psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)
    with conn.cursor() as cur:
      cur.execute(
        "SELECT set_config('app.current_user_id', %s, true)",
        ("00000000-0000-0000-0000-000000000000",),
      )
      cur.execute("SELECT set_config('app.current_is_admin', 'true', true)")
    return conn
  except psycopg.Error as error:
    raise state_store.DatabaseError(str(error)) from error


def main() -> int:
  parser = argparse.ArgumentParser(description="活动实验 Revision 滚动窗口接管")
  parser.add_argument("--apply", action="store_true")
  parser.add_argument("--build-id", required=True)
  parser.add_argument("--actor", required=True)
  args = parser.parse_args()
  result = ExperimentRevisionTakeover().run(
    apply=args.apply,
    build_id=args.build_id,
    actor=args.actor,
  )
  print(json.dumps({
    "ok": result.report["anomalyCount"] == 0,
    "applied": result.applied,
    "report": result.report,
  }, ensure_ascii=False))
  return 0 if result.report["anomalyCount"] == 0 else 2


if __name__ == "__main__":
  raise SystemExit(main())
