"""有界滚动 Trial 生成、三级窗口账本与幂等终结。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.experiments.canonicalization import digest, with_version
from backend.experiments.compiler import LegacySingleBarTrialCompiler, TrialCompiler
from backend.experiments.contracts import FiniteGridStage
from backend.experiments.errors import ExperimentError
from backend.experiments.generators import (
  FiniteGridGenerator,
  GeneratedGrid,
  extend_collection_digest,
  seed_bundle,
)
from backend.experiments.resolvers import ResolvedRunTemplate
from backend.experiments.storage_pressure import StoragePressureGate
from backend.services import state_store


@dataclass(frozen=True)
class GeneratedBatch:
  stage_id: str
  token_id: str
  start_cursor: int
  generated_count: int
  next_cursor: int


@dataclass(frozen=True)
class NoWork:
  reason: str


@dataclass(frozen=True)
class SettlementResult:
  settled: bool
  replayed: bool


@dataclass(frozen=True)
class RetryDecision:
  allowed: bool
  replayed: bool
  reason: str | None = None


class RollingTrialWindow:
  def __init__(
    self,
    *,
    generator: FiniteGridGenerator | None = None,
    compiler: TrialCompiler | None = None,
    storage_gate: StoragePressureGate | None = None,
  ) -> None:
    self._generator = generator or FiniteGridGenerator()
    self._compiler = compiler or LegacySingleBarTrialCompiler()
    self._storage_gate = storage_gate or StoragePressureGate()

  def generate_next_batch(
    self,
    batch_limit: int,
    now: datetime | None = None,
  ) -> GeneratedBatch | NoWork:
    observed_now = now or datetime.now(timezone.utc)
    decision = self._storage_gate.evaluate(observed_now)
    if not decision.allows_new_work:
      return NoWork(f"storage_{decision.status}")
    reservation = self._reserve(batch_limit, observed_now)
    if isinstance(reservation, NoWork):
      return reservation
    token_id = str(reservation["token_id"])
    try:
      trials = self._compile_batch(reservation)
      return self._commit(reservation, trials)
    except Exception:
      self._release_token(token_id, "compile_or_commit_failed")
      raise

  def settle_trial(
    self,
    trial_id: str,
    terminal_transition: Literal["succeeded", "failed", "cancelled"],
    event_id: str,
    now: datetime | None = None,
  ) -> SettlementResult:
    with _session() as conn:
      with conn.cursor() as cur:
        return settle_trial_in_transaction(
          cur, trial_id, terminal_transition, event_id,
          now or datetime.now(timezone.utc),
        )

  def request_retry(
    self,
    trial_id: str,
    event_id: str,
    now: datetime | None = None,
  ) -> RetryDecision:
    with _session() as conn:
      with conn.cursor() as cur:
        decision = reopen_trial_in_transaction(cur, trial_id, event_id)
        if decision.allowed and not decision.replayed:
          cur.execute("""
            UPDATE backtest_experiment_trial
            SET summary_status='pending', retry_not_before=%s, updated_at=now()
            WHERE id=%s
          """, (now or datetime.now(timezone.utc), trial_id))
        return decision

  def reconcile_next(self) -> bool:
    with _session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          WITH stage_facts AS (
            SELECT s.id AS stage_id, s.owner_user_id,
              count(t.id) FILTER (WHERE t.summary_status NOT IN (
                'succeeded', 'failed', 'cancelled'
              )) AS actual_open
            FROM backtest_experiment_stage s
            LEFT JOIN backtest_experiment_trial t ON t.stage_id=s.id
            GROUP BY s.id, s.owner_user_id
          ), token_facts AS (
            SELECT stage_id, COALESCE(sum(reserved_count),0) AS actual_reserved
            FROM backtest_experiment_generation_token
            WHERE status='reserved' AND expires_at > now()
            GROUP BY stage_id
          )
          SELECT facts.stage_id, facts.owner_user_id, facts.actual_open,
            COALESCE(tokens.actual_reserved,0) AS actual_reserved,
            ledger.open_count, ledger.reserved_count
          FROM stage_facts facts
          JOIN backtest_experiment_window_stage ledger
            ON ledger.stage_id=facts.stage_id
          LEFT JOIN token_facts tokens ON tokens.stage_id=facts.stage_id
          WHERE ledger.open_count<>facts.actual_open
             OR ledger.reserved_count<>COALESCE(tokens.actual_reserved,0)
          ORDER BY ledger.updated_at, facts.stage_id
          LIMIT 1
        """)
        candidate = cur.fetchone()
        if candidate is None:
          return False
        stage_id = str(candidate["stage_id"])
        owner_id = str(candidate["owner_user_id"])
        before = {
          "open": int(candidate["open_count"]),
          "reserved": int(candidate["reserved_count"]),
        }
        _lock_ledgers(cur, owner_id, stage_id)
        cur.execute("""
          SELECT count(*) FILTER (WHERE summary_status NOT IN (
            'succeeded', 'failed', 'cancelled'
          )) AS open_count
          FROM backtest_experiment_trial WHERE stage_id=%s
        """, (stage_id,))
        actual_open = int(cur.fetchone()["open_count"])
        cur.execute("""
          SELECT COALESCE(sum(reserved_count),0) AS value
          FROM backtest_experiment_generation_token
          WHERE stage_id=%s AND status='reserved' AND expires_at > now()
        """, (stage_id,))
        actual_reserved = int(cur.fetchone()["value"])
        _replace_stage_counts(cur, owner_id, stage_id, actual_open, actual_reserved)
        cur.execute("""
          INSERT INTO backtest_experiment_audit (
            owner_user_id, revision_id, stage_id, event_type, detail
          )
          SELECT %s, revision_id, id, 'rolling_window.reconciled', %s
          FROM backtest_experiment_stage WHERE id=%s
        """, (
          owner_id,
          Jsonb({
            "before": before,
            "after": {"open": actual_open, "reserved": actual_reserved},
          }),
          stage_id,
        ))
        return True

  def _reserve(
    self,
    batch_limit: int,
    now: datetime,
  ) -> dict[str, Any] | NoWork:
    if batch_limit <= 0:
      raise ValueError("batch_limit 必须大于 0")
    with _session() as conn:
      with conn.cursor() as cur:
        _reclaim_expired_tokens(cur, now)
        limits = _window_limits(cur)
        cur.execute("""
          SELECT s.id AS stage_id, s.owner_user_id, s.generation_cursor,
            s.planned_trial_count, s.stage_input_fingerprint, s.stage_spec,
            r.id AS revision_id, r.experiment_id, r.revision_no,
            r.experiment_spec, r.run_template_digest, r.determinism
          FROM backtest_experiment_stage s
          JOIN backtest_experiment_revision r ON r.id=s.revision_id
          LEFT JOIN backtest_experiment_window_user user_window
            ON user_window.owner_user_id=s.owner_user_id
          LEFT JOIN backtest_experiment_window_stage stage_window
            ON stage_window.stage_id=s.id
          CROSS JOIN backtest_experiment_window_global global_window
          WHERE r.lifecycle_state='active'
            AND r.generation_policy_version='rolling_window_v1'
            AND r.terminal_at IS NULL
            AND r.system_block_code IS NULL
            AND s.generation_cursor < s.planned_trial_count
            AND s.blocked_reason_code IS NULL
            AND COALESCE(stage_window.open_count,0)
              + COALESCE(stage_window.reserved_count,0) < %s
            AND COALESCE(user_window.open_count,0)
              + COALESCE(user_window.reserved_count,0) < %s
            AND global_window.open_count+global_window.reserved_count < %s
            AND NOT EXISTS (
              SELECT 1 FROM backtest_experiment_generation_token token
              WHERE token.stage_id=s.id AND token.status='reserved'
            )
          ORDER BY COALESCE(user_window.rotation_sequence,0),
            COALESCE(stage_window.rotation_sequence,0), s.created_at, s.id
          LIMIT 1
        """, (limits["stage"], limits["user"], limits["global"]))
        selected = cur.fetchone()
        if selected is None:
          return NoWork("no_eligible_stage")
        stage = dict(selected)
        owner_id, stage_id = str(stage["owner_user_id"]), str(stage["stage_id"])
        ledgers = _lock_ledgers(cur, owner_id, stage_id)
        cur.execute("""
          SELECT s.generation_cursor, s.planned_trial_count,
            r.lifecycle_state, r.terminal_at,
            EXISTS (
              SELECT 1 FROM backtest_experiment_generation_token token
              WHERE token.stage_id=s.id AND token.status='reserved'
            ) AS has_active_token
          FROM backtest_experiment_stage s
          JOIN backtest_experiment_revision r ON r.id=s.revision_id
          WHERE s.id=%s FOR UPDATE OF s, r
        """, (stage_id,))
        locked_stage = cur.fetchone()
        if (
          locked_stage is None
          or locked_stage["lifecycle_state"] != "active"
          or locked_stage["terminal_at"] is not None
          or locked_stage["has_active_token"]
        ):
          return NoWork("candidate_changed")
        remaining = int(locked_stage["planned_trial_count"]) - int(
          locked_stage["generation_cursor"],
        )
        available = min(
          batch_limit,
          remaining,
          limits["stage"] - ledgers["stage_open"] - ledgers["stage_reserved"],
          limits["user"] - ledgers["user_open"] - ledgers["user_reserved"],
          limits["global"] - ledgers["global_open"] - ledgers["global_reserved"],
        )
        if available <= 0:
          return NoWork("rolling_window_full")
        token_id = str(uuid4())
        expires_at = now + timedelta(seconds=settings.EXPERIMENT_COORDINATOR_LEASE_SECONDS)
        cur.execute("""
          INSERT INTO backtest_experiment_generation_token (
            id, stage_id, owner_user_id, expected_cursor,
            reserved_count, expires_at
          ) VALUES (%s,%s,%s,%s,%s,%s)
        """, (
          token_id, stage_id, owner_id, locked_stage["generation_cursor"],
          available, expires_at,
        ))
        _change_reserved_counts(cur, owner_id, stage_id, available)
        cur.execute("""
          UPDATE backtest_experiment_window_user
          SET rotation_sequence=%s,
            next_experiment_sequence=next_experiment_sequence+1,
            row_version=row_version+1, updated_at=now()
          WHERE owner_user_id=%s
        """, (ledgers["next_user_sequence"], owner_id))
        cur.execute("""
          UPDATE backtest_experiment_window_stage
          SET rotation_sequence=%s, row_version=row_version+1, updated_at=now()
          WHERE stage_id=%s
        """, (ledgers["next_experiment_sequence"], stage_id))
        cur.execute("""
          UPDATE backtest_experiment_window_global
          SET next_user_sequence=next_user_sequence+1,
            row_version=row_version+1, updated_at=now()
          WHERE singleton
        """)
        return {
          **stage,
          "token_id": token_id,
          "generation_cursor": int(locked_stage["generation_cursor"]),
          "reserved_count": available,
        }

  def _compile_batch(self, reservation: dict[str, Any]) -> list[dict[str, Any]]:
    template, grid = _restore_grid(reservation, self._generator)
    start = int(reservation["generation_cursor"])
    end = start + int(reservation["reserved_count"])
    return [
      _trial_record(reservation, template, grid, self._compiler, index)
      for index in range(start, end)
    ]

  def _commit(
    self,
    reservation: dict[str, Any],
    trials: list[dict[str, Any]],
  ) -> GeneratedBatch:
    token_id = str(reservation["token_id"])
    owner_id, stage_id = str(reservation["owner_user_id"]), str(reservation["stage_id"])
    with _session() as conn:
      with conn.cursor() as cur:
        _lock_ledgers(cur, owner_id, stage_id)
        cur.execute("""
          SELECT *, expires_at > now() AS is_valid
          FROM backtest_experiment_generation_token
          WHERE id=%s FOR UPDATE
        """, (token_id,))
        token = cur.fetchone()
        if token is None or token["status"] != "reserved" or not token["is_valid"]:
          raise ExperimentError("experiment.generation_lease_lost", "Trial 生成令牌已失效", 409)
        cur.execute("""
          SELECT s.*, r.lifecycle_state, r.terminal_at
          FROM backtest_experiment_stage s
          JOIN backtest_experiment_revision r ON r.id=s.revision_id
          WHERE s.id=%s FOR UPDATE OF s, r
        """, (stage_id,))
        stage = cur.fetchone()
        if (
          stage is None
          or stage["lifecycle_state"] != "active"
          or stage["terminal_at"] is not None
          or int(stage["generation_cursor"]) != int(token["expected_cursor"])
        ):
          release_generation_token(cur, stage_id, "commit_revalidation_failed")
          raise ExperimentError("experiment.generation_lease_lost", "Trial 生成提交条件已变化", 409)
        incremental = str(stage["incremental_collection_digest"])
        for trial in trials:
          _insert_or_verify_trial(cur, dict(stage), trial)
          incremental = extend_collection_digest(incremental, trial["trialFingerprint"])
        next_cursor = int(stage["generation_cursor"]) + len(trials)
        final_digest = incremental if next_cursor == int(stage["planned_trial_count"]) else None
        cur.execute("""
          UPDATE backtest_experiment_stage
          SET generated_trial_count=%s, generation_cursor=%s,
            incremental_collection_digest=%s,
            trial_collection_digest=COALESCE(%s,trial_collection_digest),
            execution_phase=CASE WHEN %s > 0 THEN 'queued' ELSE execution_phase END,
            status=CASE WHEN %s > 0 AND status='preparing' THEN 'queued' ELSE status END,
            row_version=row_version+1, updated_at=now()
          WHERE id=%s
        """, (
          next_cursor, next_cursor, incremental, final_digest,
          len(trials), len(trials), stage_id,
        ))
        cur.execute("""
          UPDATE backtest_experiment_generation_token
          SET status='committed', committed_at=now()
          WHERE id=%s
        """, (token_id,))
        _convert_reserved_to_open(cur, owner_id, stage_id, len(trials))
        return GeneratedBatch(
          stage_id, token_id, int(stage["generation_cursor"]),
          len(trials), next_cursor,
        )

  def _release_token(self, token_id: str, reason: str) -> None:
    with _session() as conn:
      with conn.cursor() as cur:
        cur.execute("""
          SELECT stage_id FROM backtest_experiment_generation_token
          WHERE id=%s
        """, (token_id,))
        row = cur.fetchone()
        if row is not None:
          release_generation_token(cur, str(row["stage_id"]), reason)


def release_generation_token(cur: Any, stage_id: str, reason: str) -> None:
  """生命周期事务参与者：幂等释放 Stage 的有效生成令牌。"""
  cur.execute("""
    SELECT id, owner_user_id, reserved_count
    FROM backtest_experiment_generation_token
    WHERE stage_id=%s AND status='reserved'
  """, (stage_id,))
  token = cur.fetchone()
  if token is None:
    return
  owner_id = str(token["owner_user_id"])
  _lock_ledgers(cur, owner_id, stage_id)
  cur.execute("""
    SELECT id, owner_user_id, reserved_count
    FROM backtest_experiment_generation_token
    WHERE id=%s AND status='reserved'
    FOR UPDATE
  """, (token["id"],))
  token = cur.fetchone()
  if token is None:
    return
  amount = int(token["reserved_count"])
  _change_reserved_counts(cur, owner_id, stage_id, -amount)
  cur.execute("""
    UPDATE backtest_experiment_generation_token
    SET status=CASE WHEN expires_at <= now() THEN 'expired' ELSE 'released' END,
      released_at=now(), release_reason=%s
    WHERE id=%s AND status='reserved'
  """, (reason, token["id"]))


def settle_trial_in_transaction(
  cur: Any,
  trial_id: str,
  terminal_transition: Literal["succeeded", "failed", "cancelled"],
  event_id: str,
  _now: datetime,
) -> SettlementResult:
  replay = _window_event(cur, event_id)
  if replay is not None:
    _validate_window_event(replay, "trial_settled", trial_id)
    return SettlementResult(int(replay["delta_open"]) != 0, True)
  graph = _lock_trial_window_graph(cur, trial_id)
  if graph is None:
    raise ExperimentError("experiment.trial_not_found", "Trial 不存在", 404)
  replay = _window_event(cur, event_id)
  if replay is not None:
    _validate_window_event(replay, "trial_settled", trial_id)
    return SettlementResult(int(replay["delta_open"]) != 0, True)
  if graph["summary_status"] in {"succeeded", "failed", "cancelled"}:
    _insert_window_event(cur, event_id, "trial_settled", graph, 0)
    return SettlementResult(False, False)
  cur.execute("""
    UPDATE backtest_experiment_trial
    SET summary_status=%s, active_execution_id=NULL,
      retry_not_before=NULL, updated_at=now()
    WHERE id=%s
  """, (terminal_transition, trial_id))
  _change_open_counts(cur, graph, -1)
  _insert_window_event(cur, event_id, "trial_settled", graph, -1)
  return SettlementResult(True, False)


def reopen_trial_in_transaction(
  cur: Any,
  trial_id: str,
  event_id: str,
) -> RetryDecision:
  replay = _window_event(cur, event_id)
  if replay is not None:
    _validate_window_event(replay, "trial_reopened", trial_id)
    return RetryDecision(True, True)
  graph = _lock_trial_window_graph(cur, trial_id)
  if graph is None:
    raise ExperimentError("experiment.trial_not_found", "Trial 不存在", 404)
  replay = _window_event(cur, event_id)
  if replay is not None:
    _validate_window_event(replay, "trial_reopened", trial_id)
    return RetryDecision(True, True)
  if graph["summary_status"] != "failed":
    raise ExperimentError("experiment.retry_validation_failed", "Trial 当前不可重试", 409)
  limits = _window_limits(cur)
  if not _has_retry_capacity(graph, limits):
    return RetryDecision(False, False, "rolling_window_full")
  _change_open_counts(cur, graph, 1)
  _insert_window_event(cur, event_id, "trial_reopened", graph, 1)
  return RetryDecision(True, False)


def cancel_pending_trials_in_transaction(
  cur: Any,
  graph: dict[str, Any],
  event_prefix: str,
) -> int:
  """生命周期事务参与者：终结未物化 Trial 并同步释放三级开放占用。"""
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
    RETURNING id
  """, (graph["stage_id"],))
  trial_ids = [str(row["id"]) for row in cur.fetchall()]
  if not trial_ids:
    return 0
  ledger_graph = {
    "owner_user_id": graph["owner_user_id"],
    "stage_id": graph["stage_id"],
  }
  _change_open_counts(cur, ledger_graph, -len(trial_ids))
  cur.executemany("""
    INSERT INTO backtest_experiment_window_event (
      event_id, event_type, stage_id, trial_id, delta_open
    ) VALUES (%s,'trial_settled',%s,%s,-1)
    ON CONFLICT (event_id) DO NOTHING
  """, [
    (f"{event_prefix}:{trial_id}", graph["stage_id"], trial_id)
    for trial_id in trial_ids
  ])
  return len(trial_ids)


def _restore_grid(
  stage: dict[str, Any],
  generator: FiniteGridGenerator,
) -> tuple[ResolvedRunTemplate, GeneratedGrid]:
  experiment_spec = dict(stage["experiment_spec"])
  snapshot = dict(experiment_spec["runTemplateSnapshot"])
  inputs = tuple(snapshot["tunableInputCatalog"]["inputs"])
  defaults = {
    item["sourceName"]: item["defaultValue"]
    for item in inputs if item.get("group") == "strategyParameters"
  }
  template = ResolvedRunTemplate(
    snapshot=snapshot,
    digest=str(stage["run_template_digest"]),
    tunable_inputs=inputs,
    defaults={"strategyParameters": defaults},
  )
  frozen_stage = dict(stage["stage_spec"])
  request_stage = FiniteGridStage.model_validate({
    key: frozen_stage[key]
    for key in (
      "stageType", "generatorVersion", "parameterGrid", "dateScenarios",
      "capitalScenarios", "costScenarios", "executionScenarios",
    )
  })
  grid = generator.normalize(request_stage, template)
  if (
    grid.stage_input_fingerprint != stage["stage_input_fingerprint"]
    or grid.planned_trial_count != int(stage["planned_trial_count"])
  ):
    raise ExperimentError(
      "experiment.generation_identity_conflict", "冻结 Stage 与生成器输出不一致", 409,
    )
  return template, grid


def _trial_record(
  stage: dict[str, Any],
  template: ResolvedRunTemplate,
  grid: GeneratedGrid,
  compiler: TrialCompiler,
  trial_index: int,
) -> dict[str, Any]:
  assignment = grid.assignment_at(trial_index)
  compiled = compiler.compile(template, assignment)
  fingerprint = grid.trial_fingerprint(
    trial_index, assignment, compiler.contract_version,
  )
  trial_spec = with_version({
    "schemaVersion": "trial_spec_v1",
    "experimentId": str(stage["experiment_id"]),
    "experimentRevision": int(stage["revision_no"]),
    "stageId": str(stage["stage_id"]),
    "generatorType": grid.normalized_stage["stageType"],
    "generatorVersion": grid.normalized_stage["generatorVersion"],
    "trialIndex": trial_index,
    "runTemplateDigest": template.digest,
    "stageInputFingerprint": grid.stage_input_fingerprint,
    "dimensionAssignment": assignment,
    "compiledRequest": compiled,
    "generationProvenance": {
      "stageType": "finite_grid_v1", "generatorVersion": "1",
    },
    "candidateKey": _candidate_key(template.digest, assignment),
    "seedBundle": seed_bundle(fingerprint),
    "determinism": stage["determinism"],
  })
  return {
    "trialIndex": trial_index,
    "trialFingerprint": fingerprint,
    "trialSpec": trial_spec,
    "trialSpecDigest": digest(trial_spec),
    "dimensionAssignment": assignment,
  }


def _candidate_key(run_template_digest: str, assignment: dict[str, Any]) -> str:
  return digest(with_version({
    "runTemplateDigest": run_template_digest,
    "strategyParameters": assignment["strategyParameters"],
    "initialCapital": assignment["initialCapital"],
    "costScenario": assignment["costScenario"],
    "executionScenario": assignment["executionScenario"],
  }))


def _insert_or_verify_trial(cur: Any, stage: dict[str, Any], trial: dict[str, Any]) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_trial (
      stage_id, owner_user_id, trial_index, trial_spec,
      trial_spec_digest, trial_fingerprint, dimension_projection
    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT DO NOTHING RETURNING id
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
  rows = [dict(row) for row in cur.fetchall()]
  if len(rows) != 1 or rows[0] != {
    "trial_index": trial["trialIndex"],
    "trial_spec_digest": trial["trialSpecDigest"],
    "trial_fingerprint": trial["trialFingerprint"],
  }:
    raise ExperimentError(
      "experiment.generation_identity_conflict", "Trial 身份与既有记录冲突", 409,
    )


def _lock_ledgers(cur: Any, owner_id: str, stage_id: str) -> dict[str, int]:
  cur.execute("""
    INSERT INTO backtest_experiment_window_user (owner_user_id)
    VALUES (%s) ON CONFLICT DO NOTHING
  """, (owner_id,))
  cur.execute("""
    INSERT INTO backtest_experiment_window_stage (stage_id, owner_user_id)
    VALUES (%s,%s) ON CONFLICT DO NOTHING
  """, (stage_id, owner_id))
  cur.execute("""
    SELECT open_count, reserved_count, next_user_sequence
    FROM backtest_experiment_window_global WHERE singleton FOR UPDATE
  """)
  global_row = cur.fetchone()
  cur.execute("""
    SELECT open_count, reserved_count, next_experiment_sequence
    FROM backtest_experiment_window_user
    WHERE owner_user_id=%s FOR UPDATE
  """, (owner_id,))
  user_row = cur.fetchone()
  cur.execute("""
    SELECT open_count, reserved_count
    FROM backtest_experiment_window_stage WHERE stage_id=%s FOR UPDATE
  """, (stage_id,))
  stage_row = cur.fetchone()
  return {
    "global_open": int(global_row["open_count"]),
    "global_reserved": int(global_row["reserved_count"]),
    "next_user_sequence": int(global_row["next_user_sequence"]),
    "user_open": int(user_row["open_count"]),
    "user_reserved": int(user_row["reserved_count"]),
    "next_experiment_sequence": int(user_row["next_experiment_sequence"]),
    "stage_open": int(stage_row["open_count"]),
    "stage_reserved": int(stage_row["reserved_count"]),
  }


def _window_limits(cur: Any) -> dict[str, int]:
  cur.execute(
    "SELECT * FROM app.experiment_capacity_snapshot(%s)",
    (settings.BACKTEST_BASE_CONCURRENCY,),
  )
  row = cur.fetchone()
  effective = int(row["effective_limit"])
  automation = max(0, effective - settings.EXPERIMENT_MANUAL_RESERVED_PERMITS)
  batch = settings.EXPERIMENT_GENERATION_BATCH_SIZE
  return {
    "stage": batch + settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT,
    "user": batch + settings.EXPERIMENT_MAX_IN_FLIGHT_PER_USER,
    "global": batch + automation,
  }


def _change_reserved_counts(cur: Any, owner_id: str, stage_id: str, delta: int) -> None:
  for table, where, value in (
    ("backtest_experiment_window_global", "singleton", True),
    ("backtest_experiment_window_user", "owner_user_id", owner_id),
    ("backtest_experiment_window_stage", "stage_id", stage_id),
  ):
    cur.execute(f"""
      UPDATE {table}
      SET reserved_count=reserved_count+%s,
        row_version=row_version+1, updated_at=now()
      WHERE {where}=%s AND reserved_count+%s >= 0
    """, (delta, value, delta))
    if cur.rowcount != 1:
      raise ExperimentError("experiment.window_drift", "生成窗口预留账本不一致", 409)


def _convert_reserved_to_open(
  cur: Any,
  owner_id: str,
  stage_id: str,
  amount: int,
) -> None:
  for table, where, value in (
    ("backtest_experiment_window_global", "singleton", True),
    ("backtest_experiment_window_user", "owner_user_id", owner_id),
    ("backtest_experiment_window_stage", "stage_id", stage_id),
  ):
    cur.execute(f"""
      UPDATE {table}
      SET reserved_count=reserved_count-%s, open_count=open_count+%s,
        row_version=row_version+1, updated_at=now()
      WHERE {where}=%s AND reserved_count >= %s
    """, (amount, amount, value, amount))
    if cur.rowcount != 1:
      raise ExperimentError("experiment.window_drift", "生成窗口转换账本不一致", 409)


def _change_open_counts(cur: Any, graph: dict[str, Any], delta: int) -> None:
  for table, where, value in (
    ("backtest_experiment_window_global", "singleton", True),
    ("backtest_experiment_window_user", "owner_user_id", graph["owner_user_id"]),
    ("backtest_experiment_window_stage", "stage_id", graph["stage_id"]),
  ):
    cur.execute(f"""
      UPDATE {table}
      SET open_count=open_count+%s, row_version=row_version+1, updated_at=now()
      WHERE {where}=%s AND open_count+%s >= 0
    """, (delta, value, delta))
    if cur.rowcount != 1:
      raise ExperimentError("experiment.window_drift", "生成窗口开放账本不一致", 409)


def _replace_stage_counts(
  cur: Any,
  owner_id: str,
  stage_id: str,
  open_count: int,
  reserved_count: int,
) -> None:
  cur.execute("""
    UPDATE backtest_experiment_window_stage
    SET open_count=%s, reserved_count=%s,
      row_version=row_version+1, updated_at=now()
    WHERE stage_id=%s
  """, (open_count, reserved_count, stage_id))
  cur.execute("""
    SELECT COALESCE(sum(open_count),0) AS open_count,
      COALESCE(sum(reserved_count),0) AS reserved_count
    FROM backtest_experiment_window_stage WHERE owner_user_id=%s
  """, (owner_id,))
  user = cur.fetchone()
  cur.execute("""
    UPDATE backtest_experiment_window_user
    SET open_count=%s, reserved_count=%s,
      row_version=row_version+1, updated_at=now()
    WHERE owner_user_id=%s
  """, (user["open_count"], user["reserved_count"], owner_id))
  cur.execute("""
    SELECT COALESCE(sum(open_count),0) AS open_count,
      COALESCE(sum(reserved_count),0) AS reserved_count
    FROM backtest_experiment_window_user
  """)
  global_row = cur.fetchone()
  cur.execute("""
    UPDATE backtest_experiment_window_global
    SET open_count=%s, reserved_count=%s,
      row_version=row_version+1, updated_at=now()
    WHERE singleton
  """, (global_row["open_count"], global_row["reserved_count"]))


def _lock_trial_window_graph(cur: Any, trial_id: str) -> dict[str, Any] | None:
  cur.execute("""
    SELECT trial.id AS trial_id, trial.summary_status,
      stage.id AS stage_id, stage.owner_user_id,
      revision.system_block_code, revision.lifecycle_state,
      revision.cancel_requested_at, revision.terminal_at
    FROM backtest_experiment_trial trial
    JOIN backtest_experiment_stage stage ON stage.id=trial.stage_id
    JOIN backtest_experiment_revision revision ON revision.id=stage.revision_id
    WHERE trial.id=%s
  """, (trial_id,))
  row = cur.fetchone()
  if row is None:
    return None
  graph = dict(row)
  graph.update(_lock_ledgers(
    cur, str(graph["owner_user_id"]), str(graph["stage_id"]),
  ))
  cur.execute("SELECT summary_status FROM backtest_experiment_trial WHERE id=%s FOR UPDATE", (trial_id,))
  locked = cur.fetchone()
  graph["summary_status"] = locked["summary_status"]
  return graph


def _has_retry_capacity(graph: dict[str, Any], limits: dict[str, int]) -> bool:
  return all((
    graph.get("lifecycle_state") == "active",
    graph.get("cancel_requested_at") is None,
    graph.get("terminal_at") is None,
    graph.get("system_block_code") is None,
    int(graph.get("stage_open", 0)) < limits["stage"],
    int(graph.get("user_open", 0)) < limits["user"],
    int(graph.get("global_open", 0)) < limits["global"],
  ))


def _window_event(cur: Any, event_id: str) -> dict[str, Any] | None:
  cur.execute("SELECT * FROM backtest_experiment_window_event WHERE event_id=%s", (event_id,))
  row = cur.fetchone()
  return dict(row) if row else None


def _insert_window_event(
  cur: Any,
  event_id: str,
  event_type: str,
  graph: dict[str, Any],
  delta: int,
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_window_event (
      event_id, event_type, stage_id, trial_id, delta_open
    ) VALUES (%s,%s,%s,%s,%s)
  """, (event_id, event_type, graph["stage_id"], graph["trial_id"], delta))


def _validate_window_event(
  event: dict[str, Any],
  expected_type: str,
  trial_id: str,
) -> None:
  if (
    event["event_type"] != expected_type
    or str(event["trial_id"]) != trial_id
  ):
    raise ExperimentError(
      "experiment.window_event_conflict",
      "滚动窗口事件标识已用于其他状态转换",
      409,
    )


def _reclaim_expired_tokens(cur: Any, now: datetime) -> None:
  cur.execute("""
    SELECT stage_id FROM backtest_experiment_generation_token
    WHERE status='reserved' AND expires_at <= %s
    ORDER BY expires_at, id LIMIT 20
  """, (now,))
  for row in cur.fetchall():
    release_generation_token(cur, str(row["stage_id"]), "expired")


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
