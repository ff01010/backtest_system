"""实验生命周期命令、乐观并发、幂等与旧状态投影。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from psycopg.types.json import Jsonb

from backend.experiments.canonicalization import digest, with_version
from backend.experiments.errors import ExperimentError
from backend.experiments.rollout import ContractEpoch, require_epoch
from backend.services import state_store


LifecycleState = Literal["active", "pausing", "paused", "cancelling", "terminal"]
ExecutionPhase = Literal["preparing", "queued", "running", "blocked", "settled"]
TerminalOutcome = Literal[
  "completed", "partially_completed", "failed", "cancelled",
]


@dataclass(frozen=True)
class LifecycleCommand:
  experiment_id: str
  expected_revision_id: str
  expected_lifecycle_version: int
  idempotency_key: str
  reason: str
  request_id: str | None = None
  resume_mode: Literal["user_pause", "execution_circuit"] | None = None


@dataclass(frozen=True)
class LifecycleCommandResult:
  response: dict[str, Any]
  status_code: int


class LifecycleStore(Protocol):
  def execute(
    self,
    operation: Literal["pause", "resume", "cancel"],
    command: LifecycleCommand,
  ) -> LifecycleCommandResult:
    """在单一事务中执行生命周期命令。"""


class ExperimentLifecycleCommands:
  def __init__(self, store: LifecycleStore | None = None) -> None:
    self._store = store or PostgresLifecycleStore()

  def pause(self, command: LifecycleCommand) -> LifecycleCommandResult:
    return self._store.execute("pause", command)

  def resume(self, command: LifecycleCommand) -> LifecycleCommandResult:
    return self._store.execute("resume", command)

  def cancel(self, command: LifecycleCommand) -> LifecycleCommandResult:
    return self._store.execute("cancel", command)


class PostgresLifecycleStore:
  def execute(
    self,
    operation: Literal["pause", "resume", "cancel"],
    command: LifecycleCommand,
  ) -> LifecycleCommandResult:
    require_epoch(ContractEpoch.LIFECYCLE_V2)
    owner_id = state_store.current_user_id()
    operation_type = f"experiment.lifecycle.{operation}.v2"
    request_hash = _request_hash(operation, command)
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_is_admin', 'true', true)")
        cur.execute(
          "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
          (f"{owner_id}:{operation_type}:{command.idempotency_key}",),
        )
        replay = _load_replay(
          cur, owner_id, operation_type, command.idempotency_key, request_hash,
        )
        if replay is not None:
          return replay
        _lock_window_before_graph(cur, command.experiment_id, owner_id)
        graph = _lock_graph(cur, command.experiment_id, owner_id)
        _validate_command(graph, command)
        if operation == "pause":
          response, status_code = _pause(cur, graph, command)
        elif operation == "resume":
          response, status_code = _resume(cur, graph, command)
        else:
          response, status_code = _cancel(cur, graph, command)
        _save_result(
          cur, owner_id, operation_type, command.idempotency_key,
          request_hash, command.experiment_id, status_code, response,
        )
        return LifecycleCommandResult(response, status_code)


def project_legacy_status(
  lifecycle_state: LifecycleState,
  execution_phase: ExecutionPhase,
  system_block_code: str | None,
  terminal_outcome: TerminalOutcome | None,
) -> str:
  """把正交状态投影到旧客户端已经认识的枚举。"""
  if lifecycle_state == "terminal":
    return terminal_outcome or "failed"
  if lifecycle_state == "cancelling":
    return "cancelling"
  if lifecycle_state == "paused":
    return "generation_blocked"
  if lifecycle_state == "pausing" and execution_phase in {"queued", "running"}:
    return execution_phase
  if system_block_code is not None or execution_phase == "blocked":
    return "generation_blocked"
  if execution_phase == "settled":
    return "queued"
  return execution_phase


def _request_hash(operation: str, command: LifecycleCommand) -> str:
  return digest(with_version({
    "operationType": f"experiment.lifecycle.{operation}.v2",
    "experimentId": command.experiment_id,
    "expectedRevisionId": command.expected_revision_id,
    "expectedLifecycleVersion": command.expected_lifecycle_version,
    "reason": command.reason,
    "resumeMode": command.resume_mode,
  }))


def _lock_graph(cur: Any, experiment_id: str, owner_id: str) -> dict[str, Any]:
  cur.execute("""
    SELECT e.id AS experiment_id, e.owner_user_id, e.current_revision_id,
      e.archived_at, e.purge_job_id, e.row_version,
      r.id AS revision_id, r.lifecycle_state, r.lifecycle_version,
      r.terminal_outcome, r.pause_requested_at, r.paused_at,
      r.cancel_requested_at, r.terminal_at,
      r.generation_policy_version, r.system_block_code,
      s.id AS stage_id, s.status AS legacy_stage_status, s.execution_phase,
      s.blocked_reason_code AS stage_blocked_reason_code, s.in_flight_count,
      s.planned_trial_count, s.generated_trial_count,
      s.cancelled_before_generation
    FROM backtest_experiment e
    JOIN backtest_experiment_revision r ON r.id=e.current_revision_id
    JOIN backtest_experiment_stage s ON s.revision_id=r.id AND s.stage_index=0
    WHERE e.id=%s AND e.owner_user_id=%s
    FOR UPDATE OF e, r, s
  """, (experiment_id, owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  graph = dict(row)
  if graph["purge_job_id"] is not None:
    raise ExperimentError("experiment.purge_in_progress", "实验已经进入清理流程", 409)
  if graph["archived_at"] is not None:
    raise ExperimentError("experiment.archived_read_only", "归档实验只允许读取或恢复", 409)
  return graph


def _lock_window_before_graph(cur: Any, experiment_id: str, owner_id: str) -> None:
  cur.execute("""
    SELECT stage.id AS stage_id, revision.generation_policy_version
    FROM backtest_experiment experiment
    JOIN backtest_experiment_revision revision
      ON revision.id=experiment.current_revision_id
    JOIN backtest_experiment_stage stage
      ON stage.revision_id=revision.id AND stage.stage_index=0
    WHERE experiment.id=%s AND experiment.owner_user_id=%s
  """, (experiment_id, owner_id))
  row = cur.fetchone()
  if row is None:
    raise ExperimentError("experiment.not_found", "实验不存在", 404)
  if row["generation_policy_version"] != "rolling_window_v1":
    return
  from backend.experiments.rolling_window import _lock_ledgers

  _lock_ledgers(cur, owner_id, str(row["stage_id"]))


def _validate_command(graph: dict[str, Any], command: LifecycleCommand) -> None:
  if str(graph["revision_id"]) != command.expected_revision_id:
    raise ExperimentError("experiment.revision_conflict", "实验修订已经变化", 409)
  if int(graph["lifecycle_version"]) != command.expected_lifecycle_version:
    raise ExperimentError(
      "experiment.lifecycle_conflict", "实验生命周期状态已经变化", 409,
      details={"actualLifecycleVersion": int(graph["lifecycle_version"])},
    )


def _pause(
  cur: Any,
  graph: dict[str, Any],
  command: LifecycleCommand,
) -> tuple[dict[str, Any], int]:
  if graph["lifecycle_state"] in {"pausing", "paused"}:
    accepted_at = (
      graph.get("pause_requested_at") or graph.get("paused_at") or _database_now(cur)
    )
    status = project_legacy_status(
      graph["lifecycle_state"], graph["execution_phase"],
      graph["system_block_code"], None,
    )
    return _response(
      graph, graph["lifecycle_state"], int(graph["lifecycle_version"]),
      status, accepted_at, False,
    ), 200
  if graph["lifecycle_state"] != "active" or graph["terminal_at"] is not None:
    raise ExperimentError("experiment.command_invalid", "当前实验不能暂停", 409)
  state = "pausing" if int(graph["in_flight_count"]) > 0 else "paused"
  accepted_at = _database_now(cur)
  cur.execute("""
    UPDATE backtest_experiment_revision
    SET lifecycle_state=%s, lifecycle_version=lifecycle_version+1,
      pause_requested_at=%s, paused_at=CASE WHEN %s='paused' THEN %s ELSE NULL END,
      pause_reason=%s, updated_at=now()
    WHERE id=%s
    RETURNING lifecycle_version
  """, (state, accepted_at, state, accepted_at, command.reason, graph["revision_id"]))
  lifecycle_version = int(cur.fetchone()["lifecycle_version"])
  _release_generation_token(cur, graph["stage_id"], "user_pause")
  status = project_legacy_status(
    state, graph["execution_phase"], graph["system_block_code"], None,
  )
  _write_projection(cur, graph, status)
  _audit(cur, graph, "experiment.pause_requested", command, {
    "lifecycleState": state,
    "inFlightTrialCount": int(graph["in_flight_count"]),
  })
  return _response(graph, state, lifecycle_version, status, accepted_at, False), 202


def _resume(
  cur: Any,
  graph: dict[str, Any],
  command: LifecycleCommand,
) -> tuple[dict[str, Any], int]:
  if graph["cancel_requested_at"] is not None or graph["terminal_at"] is not None:
    raise ExperimentError("experiment.command_invalid", "当前实验不能恢复", 409)
  if command.resume_mode == "user_pause":
    if graph["lifecycle_state"] not in {"pausing", "paused"}:
      raise ExperimentError("experiment.command_invalid", "当前实验没有用户暂停", 409)
    cur.execute("""
      UPDATE backtest_experiment_revision
      SET lifecycle_state='active', lifecycle_version=lifecycle_version+1,
        pause_requested_at=NULL, paused_at=NULL, pause_reason=NULL,
        updated_at=now()
      WHERE id=%s RETURNING lifecycle_version
    """, (graph["revision_id"],))
    lifecycle_version = int(cur.fetchone()["lifecycle_version"])
    event_type = "experiment.user_pause_resumed"
  elif command.resume_mode == "execution_circuit":
    if graph["system_block_code"] != "execution_circuit" and (
      graph["stage_blocked_reason_code"] != "experiment.execution_blocked"
    ):
      raise ExperimentError("experiment.command_invalid", "当前实验没有执行熔断", 409)
    cur.execute("""
      UPDATE backtest_experiment_revision
      SET lifecycle_version=lifecycle_version+1,
        system_block_code=NULL, system_block_message=NULL, updated_at=now()
      WHERE id=%s RETURNING lifecycle_version
    """, (graph["revision_id"],))
    lifecycle_version = int(cur.fetchone()["lifecycle_version"])
    cur.execute("""
      UPDATE backtest_experiment_stage
      SET blocked_reason_code=NULL, blocked_reason_message=NULL,
        blocked_scope=NULL, blocked_at=NULL,
        circuit_reset_sequence=next_terminal_sequence-1,
        consecutive_failure_fingerprint=NULL, consecutive_failure_count=0,
        execution_phase=CASE
          WHEN in_flight_count > 0 THEN 'running'
          WHEN generated_trial_count < planned_trial_count THEN 'preparing'
          ELSE 'queued'
        END,
        row_version=row_version+1, updated_at=now()
      WHERE id=%s
    """, (graph["stage_id"],))
    event_type = "experiment.execution_circuit_resumed"
  else:
    raise ExperimentError("experiment.command_invalid", "恢复模式无效", 409)
  accepted_at = _database_now(cur)
  phase = _active_phase(graph)
  status = project_legacy_status("active", phase, None, None)
  _write_projection(cur, graph, status)
  _audit(cur, graph, event_type, command, {"resumeMode": command.resume_mode})
  return _response(graph, "active", lifecycle_version, status, accepted_at, False), 200


def _cancel(
  cur: Any,
  graph: dict[str, Any],
  command: LifecycleCommand,
) -> tuple[dict[str, Any], int]:
  if graph["lifecycle_state"] == "terminal":
    status = project_legacy_status(
      "terminal", "settled", None, graph["terminal_outcome"],
    )
    return _response(
      graph, "terminal", int(graph["lifecycle_version"]), status,
      _database_now(cur), False,
    ), 200
  remaining = max(
    0, int(graph["planned_trial_count"]) - int(graph["generated_trial_count"]),
  )
  _release_generation_token(cur, graph["stage_id"], "user_cancel")
  if graph["generation_policy_version"] == "rolling_window_v1":
    from backend.experiments.rolling_window import cancel_pending_trials_in_transaction

    cancelled_waiting = cancel_pending_trials_in_transaction(
      cur, graph, f"lifecycle-cancel:{graph['revision_id']}",
    )
  else:
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
    """, (graph["stage_id"],))
    cancelled_waiting = cur.rowcount
  _request_active_cancellation(cur, graph)
  terminal = int(graph["in_flight_count"]) == 0
  state: LifecycleState = "terminal" if terminal else "cancelling"
  outcome: TerminalOutcome | None = "cancelled" if terminal else None
  accepted_at = _database_now(cur)
  cur.execute("""
    UPDATE backtest_experiment_revision
    SET lifecycle_state=%s, lifecycle_version=lifecycle_version+1,
      terminal_outcome=%s, cancel_requested_at=COALESCE(cancel_requested_at,%s),
      cancel_reason=COALESCE(cancel_reason,%s),
      terminal_at=CASE WHEN %s THEN COALESCE(terminal_at,%s) ELSE terminal_at END,
      updated_at=now()
    WHERE id=%s RETURNING lifecycle_version
  """, (
    state, outcome, accepted_at, command.reason, terminal, accepted_at,
    graph["revision_id"],
  ))
  lifecycle_version = int(cur.fetchone()["lifecycle_version"])
  cur.execute("""
    WITH counts AS (
      SELECT
        count(*) FILTER (WHERE summary_status='succeeded') AS succeeded,
        count(*) FILTER (WHERE summary_status='failed') AS failed,
        count(*) FILTER (WHERE summary_status='cancelled') AS cancelled
      FROM backtest_experiment_trial
      WHERE stage_id=%s
    )
    UPDATE backtest_experiment_stage stage
    SET cancelled_before_generation=%s,
      terminal_count=counts.succeeded + counts.failed + counts.cancelled + %s,
      succeeded_count=counts.succeeded,
      failed_count=counts.failed,
      cancelled_count=counts.cancelled + %s,
      execution_phase=CASE WHEN %s THEN 'settled' ELSE 'running' END,
      row_version=row_version+1, updated_at=now()
    FROM counts
    WHERE stage.id=%s
  """, (
    graph["stage_id"], remaining, remaining, remaining, terminal,
    graph["stage_id"],
  ))
  status = project_legacy_status(
    state, "settled" if terminal else "running", None, outcome,
  )
  _write_projection(cur, graph, status)
  _audit(cur, graph, "experiment.cancel_requested", command, {
    "lifecycleState": state,
    "cancelledBeforeGeneration": remaining,
    "cancelledBeforeMaterialization": cancelled_waiting,
    "inFlightTrialCount": int(graph["in_flight_count"]),
  })
  response = _response(graph, state, lifecycle_version, status, accepted_at, False)
  response["terminalOutcome"] = outcome
  return response, 200 if terminal else 202


def _request_active_cancellation(cur: Any, graph: dict[str, Any]) -> None:
  cur.execute("""
    SELECT execution.run_id
    FROM backtest_experiment_trial trial
    JOIN backtest_experiment_trial_execution execution
      ON execution.id=trial.active_execution_id
    WHERE trial.stage_id=%s
    ORDER BY trial.trial_index
    FOR UPDATE OF trial, execution
  """, (graph["stage_id"],))
  for row in cur.fetchall():
    state_store.request_backtest_cancel_in_transaction(
      cur, str(row["run_id"]), str(graph["owner_user_id"]),
    )


def _release_generation_token(cur: Any, stage_id: str, reason: str) -> None:
  from backend.experiments.rolling_window import release_generation_token

  release_generation_token(cur, stage_id, reason)


def _write_projection(cur: Any, graph: dict[str, Any], status: str) -> None:
  cur.execute(
    "UPDATE backtest_experiment_stage SET status=%s, updated_at=now() WHERE id=%s",
    (status, graph["stage_id"]),
  )
  cur.execute(
    "UPDATE backtest_experiment_revision SET status=%s, updated_at=now() WHERE id=%s",
    (status, graph["revision_id"]),
  )
  cur.execute("""
    UPDATE backtest_experiment
    SET status=%s, row_version=row_version+1, updated_at=now()
    WHERE id=%s
  """, (status, graph["experiment_id"]))


def _active_phase(graph: dict[str, Any]) -> ExecutionPhase:
  if int(graph["in_flight_count"]) > 0:
    return "running"
  if int(graph["generated_trial_count"]) < int(graph["planned_trial_count"]):
    return "preparing"
  return "queued"


def _database_now(cur: Any) -> Any:
  cur.execute("SELECT now() AS value")
  return cur.fetchone()["value"]


def _audit(
  cur: Any,
  graph: dict[str, Any],
  event_type: str,
  command: LifecycleCommand,
  detail: dict[str, Any],
) -> None:
  cur.execute("""
    INSERT INTO backtest_experiment_audit (
      owner_user_id, experiment_id, revision_id, stage_id,
      event_type, request_id, detail
    ) VALUES (%s,%s,%s,%s,%s,%s,%s)
  """, (
    graph["owner_user_id"], graph["experiment_id"], graph["revision_id"],
    graph["stage_id"], event_type, command.request_id,
    Jsonb({"reason": command.reason, **detail}),
  ))


def _response(
  graph: dict[str, Any],
  state: LifecycleState,
  lifecycle_version: int,
  status: str,
  accepted_at: Any,
  replayed: bool,
) -> dict[str, Any]:
  return {
    "experimentId": str(graph["experiment_id"]),
    "revisionId": str(graph["revision_id"]),
    "lifecycleState": state,
    "lifecycleVersion": lifecycle_version,
    "status": status,
    "acceptedAt": accepted_at.isoformat(),
    "replayed": replayed,
  }


def _load_replay(
  cur: Any,
  owner_id: str,
  operation_type: str,
  key: str,
  request_hash: str,
) -> LifecycleCommandResult | None:
  cur.execute("""
    SELECT request_hash, status, response_status, response_body
    FROM backtest_internal_idempotency
    WHERE owner_user_id=%s AND operation_type=%s AND idempotency_key=%s
  """, (owner_id, operation_type, key))
  row = cur.fetchone()
  if row is None:
    return None
  if row["request_hash"] != request_hash:
    raise ExperimentError("idempotency.conflict", "同一幂等键已用于不同请求", 409)
  body = dict(row.get("response_body") or {})
  if row["status"] != "completed" or not body:
    return LifecycleCommandResult({"status": "processing", "replayed": True}, 202)
  body["replayed"] = True
  return LifecycleCommandResult(body, int(row["response_status"]))


def _save_result(
  cur: Any,
  owner_id: str,
  operation_type: str,
  key: str,
  request_hash: str,
  experiment_id: str,
  status_code: int,
  response: dict[str, Any],
) -> None:
  cur.execute("""
    INSERT INTO backtest_internal_idempotency (
      owner_user_id, operation_type, idempotency_key, request_hash,
      status, resource_type, resource_id, response_status,
      response_body, completed_at
    ) VALUES (%s,%s,%s,%s,'completed','experiment',%s,%s,%s,now())
  """, (
    owner_id, operation_type, key, request_hash, experiment_id,
    status_code, Jsonb(response),
  ))
