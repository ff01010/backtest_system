"""自动化实验语义纪元读取与兼容判断。"""

from __future__ import annotations

from enum import IntEnum
import logging
from typing import Any

import psycopg
from psycopg.rows import dict_row

from backend import settings
from backend.services import state_store


logger = logging.getLogger(__name__)


class ContractEpoch(IntEnum):
  LEGACY = 0
  LIFECYCLE_V2 = 1
  EVALUATION_V2 = 2
  ROLLING_WINDOW_V1 = 3


_EPOCH_NAMES = {
  ContractEpoch.LEGACY: "legacy",
  ContractEpoch.LIFECYCLE_V2: "lifecycle_v2",
  ContractEpoch.EVALUATION_V2: "evaluation_v2",
  ContractEpoch.ROLLING_WINDOW_V1: "rolling_window_v1",
}
_EPOCH_VALUES = {name: epoch for epoch, name in _EPOCH_NAMES.items()}
MIN_SUPPORTED_EPOCH = ContractEpoch.LEGACY
MAX_SUPPORTED_EPOCH = ContractEpoch.ROLLING_WINDOW_V1


def epoch_name(epoch: ContractEpoch) -> str:
  return _EPOCH_NAMES[epoch]


def parse_epoch(value: str) -> ContractEpoch:
  try:
    return _EPOCH_VALUES[value]
  except KeyError as error:
    raise ValueError(f"未知自动化实验语义纪元: {value}") from error


def current_contract_epoch() -> ContractEpoch:
  """读取当前单调语义纪元；迁移缺失时拒绝猜测。"""
  if not settings.DATABASE_URL:
    return ContractEpoch.LEGACY
  try:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT contract_epoch
          FROM backtest_experiment_rollout_state
          WHERE singleton
        """)
        row = cur.fetchone()
  except psycopg.Error as error:
    raise state_store.DatabaseError(str(error)) from error
  if row is None:
    raise state_store.DatabaseError("缺少自动化实验语义纪元单例行")
  return parse_epoch(str(row["contract_epoch"]))


def health_snapshot() -> dict[str, Any]:
  epoch = current_contract_epoch()
  return {
    "contractEpoch": epoch_name(epoch),
    "supportedEpochRange": {
      "minimum": epoch_name(MIN_SUPPORTED_EPOCH),
      "maximum": epoch_name(MAX_SUPPORTED_EPOCH),
    },
    "compatible": MIN_SUPPORTED_EPOCH <= epoch <= MAX_SUPPORTED_EPOCH,
    "rolloutObservation": _operational_snapshot(),
  }


def record_legacy_lifecycle_call(route: str) -> None:
  """兼容调用量只能用于下线观察，采集失败不能改变用户命令语义。"""
  if not settings.DATABASE_URL:
    return
  try:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT set_config('app.current_user_id', %s, true)",
          (state_store.current_user_id(),),
        )
        cur.execute("""
          INSERT INTO backtest_experiment_rollout_metric (
            metric_name, cumulative_value, observation_count,
            first_observed_at, last_observed_at, last_nonzero_at, detail
          ) VALUES (%s,1,1,now(),now(),now(),jsonb_build_object('route',%s::text))
          ON CONFLICT (metric_name) DO UPDATE
          SET cumulative_value=backtest_experiment_rollout_metric.cumulative_value+1,
            observation_count=backtest_experiment_rollout_metric.observation_count+1,
            last_observed_at=now(), last_nonzero_at=now(),
            detail=EXCLUDED.detail
        """, (f"v1_lifecycle_call:{route}", route))
  except (psycopg.Error, state_store.DatabaseError) as error:
    logger.warning("旧生命周期接口调用量记录失败: %s", error)


def _operational_snapshot() -> dict[str, Any]:
  unavailable = {
    "status": "unavailable",
    "projectionMismatchCount": None,
    "ledgerMismatchCount": None,
    "expiredGenerationTokenCount": None,
    "oldQuotaNonzeroUserCount": None,
    "v1LifecycleCallCount": None,
  }
  if not settings.DATABASE_URL:
    return unavailable
  try:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
      with conn.cursor() as cur:
        projection_mismatches = _projection_mismatch_count(cur)
        ledger_mismatches = _ledger_mismatch_count(cur)
        cur.execute("""
          SELECT count(*) AS expired_count,
            min(expires_at) AS oldest_expired_at
          FROM backtest_experiment_generation_token
          WHERE status='reserved' AND expires_at <= now()
        """)
        token_row = cur.fetchone()
        cur.execute("""
          SELECT count(*) AS user_count,
            COALESCE(sum(reserved_planned_trial_count),0) AS reserved_count
          FROM backtest_experiment_user_quota
          WHERE reserved_planned_trial_count<>0
        """)
        quota_row = cur.fetchone()
        cur.execute("""
          SELECT COALESCE(sum(cumulative_value),0) AS call_count,
            max(last_nonzero_at) AS last_call_at
          FROM backtest_experiment_rollout_metric
          WHERE metric_name LIKE 'v1_lifecycle_call:%'
        """)
        v1_row = cur.fetchone()
    return {
      "status": "ok",
      "projectionMismatchCount": projection_mismatches,
      "ledgerMismatchCount": ledger_mismatches,
      "expiredGenerationTokenCount": int(token_row["expired_count"]),
      "oldestExpiredGenerationTokenAt": (
        token_row["oldest_expired_at"].isoformat()
        if token_row["oldest_expired_at"] else None
      ),
      "oldQuotaNonzeroUserCount": int(quota_row["user_count"]),
      "oldQuotaReservedTrialCount": int(quota_row["reserved_count"]),
      "v1LifecycleCallCount": int(v1_row["call_count"]),
      "v1LifecycleLastCallAt": (
        v1_row["last_call_at"].isoformat() if v1_row["last_call_at"] else None
      ),
    }
  except (psycopg.Error, state_store.DatabaseError, ValueError) as error:
    logger.warning("自动化实验发布观测读取失败: %s", error)
    return unavailable


def _projection_mismatch_count(cur: Any) -> int:
  from backend.experiments.lifecycle import project_legacy_status

  cur.execute("""
    SELECT experiment.status AS experiment_status,
      revision.status AS revision_status, stage.status AS stage_status,
      revision.lifecycle_state, stage.execution_phase,
      revision.system_block_code, revision.terminal_outcome
    FROM backtest_experiment experiment
    JOIN backtest_experiment_revision revision
      ON revision.id=experiment.current_revision_id
    JOIN backtest_experiment_stage stage
      ON stage.revision_id=revision.id AND stage.stage_index=0
  """)
  mismatches = 0
  for row in cur.fetchall():
    expected = project_legacy_status(
      str(row["lifecycle_state"]),
      str(row["execution_phase"]),
      str(row["system_block_code"]) if row["system_block_code"] else None,
      str(row["terminal_outcome"]) if row["terminal_outcome"] else None,
    )
    if any(row[field] != expected for field in (
      "experiment_status", "revision_status", "stage_status",
    )):
      mismatches += 1
  return mismatches


def _ledger_mismatch_count(cur: Any) -> int:
  cur.execute("""
    WITH stage_facts AS (
      SELECT stage.id AS stage_id, stage.owner_user_id,
        trial.open_count, token.reserved_count
      FROM backtest_experiment_stage stage
      JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
      CROSS JOIN LATERAL (
        SELECT count(*) AS open_count
        FROM backtest_experiment_trial item
        WHERE item.stage_id=stage.id AND item.summary_status NOT IN (
          'succeeded','failed','cancelled'
        )
      ) trial
      CROSS JOIN LATERAL (
        SELECT COALESCE(sum(reserved_count),0) AS reserved_count
        FROM backtest_experiment_generation_token item
        WHERE item.stage_id=stage.id AND item.status='reserved'
          AND item.expires_at>now()
      ) token
      WHERE revision.generation_policy_version='rolling_window_v1'
    ), stage_diff AS (
      SELECT count(*) AS value
      FROM stage_facts fact
      LEFT JOIN backtest_experiment_window_stage ledger
        ON ledger.stage_id=fact.stage_id
      WHERE ledger.stage_id IS NULL OR ledger.open_count<>fact.open_count
        OR ledger.reserved_count<>fact.reserved_count
    ), user_facts AS (
      SELECT owner_user_id, sum(open_count) AS open_count,
        sum(reserved_count) AS reserved_count
      FROM stage_facts GROUP BY owner_user_id
    ), user_diff AS (
      SELECT count(*) AS value
      FROM user_facts fact
      LEFT JOIN backtest_experiment_window_user ledger
        ON ledger.owner_user_id=fact.owner_user_id
      WHERE ledger.owner_user_id IS NULL OR ledger.open_count<>fact.open_count
        OR ledger.reserved_count<>fact.reserved_count
    ), global_facts AS (
      SELECT COALESCE(sum(open_count),0) AS open_count,
        COALESCE(sum(reserved_count),0) AS reserved_count
      FROM user_facts
    )
    SELECT stage_diff.value+user_diff.value+
      CASE WHEN ledger.open_count<>fact.open_count
        OR ledger.reserved_count<>fact.reserved_count THEN 1 ELSE 0 END AS value
    FROM stage_diff, user_diff, global_facts fact
    CROSS JOIN backtest_experiment_window_global ledger
    WHERE ledger.singleton
  """)
  row = cur.fetchone()
  return int(row["value"]) if row is not None else 0


def require_epoch(required: ContractEpoch) -> ContractEpoch:
  current = current_contract_epoch()
  if current < required:
    from backend.experiments.errors import ExperimentError

    raise ExperimentError(
      "experiment.contract_epoch_inactive",
      "该自动化实验能力尚未激活",
      409,
      details={
        "currentEpoch": epoch_name(current),
        "requiredEpoch": epoch_name(required),
      },
    )
  return current
