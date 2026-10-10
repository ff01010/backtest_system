"""实验 Trial 生成协调器，不执行策略或创建普通回测任务。"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any, Protocol

from backend import settings
from backend.experiments.canonicalization import digest, with_version
from backend.experiments.compiler import LegacySingleBarTrialCompiler, TrialCompiler
from backend.experiments.contracts import FiniteGridStage
from backend.experiments.errors import ExperimentError
from backend.experiments.generators import (
  FiniteGridGenerator,
  GeneratedGrid,
  seed_bundle,
)
from backend.experiments.repository import PostgresExperimentRepository
from backend.experiments.resolvers import ResolvedRunTemplate
from backend.services import state_store


logger = logging.getLogger(__name__)


class GenerationRepository(Protocol):
  def claim_generation_stage(
    self,
    worker_id: str,
    lease_seconds: int,
  ) -> dict[str, Any] | None:
    """领取一个待生成 Stage。"""

  def commit_generation_batch(
    self,
    stage_id: str,
    attempt_token: str,
    expected_cursor: int,
    trials: list[dict[str, Any]],
  ) -> bool:
    """以租约和游标 CAS 提交一个确定性分块。"""

  def block_generation(
    self,
    stage_id: str,
    attempt_token: str,
    code: str,
    message: str,
  ) -> None:
    """记录不可安全自动重试的稳定生成错误。"""

  def materialize_next_trial(self, compiler: TrialCompiler) -> bool:
    """原子物化一个普通回测任务。"""

  def reconcile_next_execution(self) -> bool:
    """从普通回测真源对账一个执行。"""

  def refresh_next_capacity_block(self) -> bool:
    """重新检查一个可恢复的容量阻塞。"""

  def repair_next_aggregate(self) -> bool:
    """从持久化事实修复一个聚合或额度缓存漂移。"""


class ExperimentCoordinator:
  def __init__(
    self,
    *,
    repository: GenerationRepository | None = None,
    generator: FiniteGridGenerator | None = None,
    compiler: TrialCompiler | None = None,
    worker_id: str | None = None,
  ) -> None:
    self._repository = repository or PostgresExperimentRepository()
    self._generator = generator or FiniteGridGenerator()
    self._compiler = compiler or LegacySingleBarTrialCompiler()
    self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

  def run_forever(self) -> None:
    backoff_seconds = 1
    while True:
      try:
        handled = self.run_once()
        backoff_seconds = 1
        if not handled:
          time.sleep(settings.EXPERIMENT_COORDINATOR_POLL_SECONDS)
      except state_store.DatabaseError:
        logger.exception("实验协调器数据库操作失败，%s 秒后重试", backoff_seconds)
        time.sleep(backoff_seconds)
        backoff_seconds = min(backoff_seconds * 2, 30)

  def run_once(self) -> bool:
    if not settings.EXPERIMENTS_ENABLED:
      return False
    handled = self._reconcile_once()
    handled = self._repair_once() or handled
    handled = self._generate_once() or handled
    handled = self._refresh_capacity_block_once() or handled
    handled = self._materialize_once() or handled
    return handled

  def _generate_once(self) -> bool:
    stage = self._repository.claim_generation_stage(
      self._worker_id,
      settings.EXPERIMENT_COORDINATOR_LEASE_SECONDS,
    )
    if stage is None:
      return False
    attempt_token = str(stage["generation_attempt_token"])
    try:
      trials = self._generate_batch(stage)
      self._repository.commit_generation_batch(
        str(stage["id"]),
        attempt_token,
        int(stage["generation_cursor"]),
        trials,
      )
    except state_store.DatabaseError:
      raise
    except Exception as error:
      code = (
        error.code if isinstance(error, ExperimentError)
        else "experiment.generation_failed"
      )
      logger.exception("实验 Trial 生成失败: stage_id=%s code=%s", stage["id"], code)
      self._repository.block_generation(
        str(stage["id"]), attempt_token, code, _public_generation_message(error),
      )
    return True

  def _materialize_once(self) -> bool:
    materialize = getattr(self._repository, "materialize_next_trial", None)
    if not callable(materialize):
      return False
    return bool(materialize(self._compiler))

  def _reconcile_once(self) -> bool:
    reconcile = getattr(self._repository, "reconcile_next_execution", None)
    if not callable(reconcile):
      return False
    return bool(reconcile())

  def _refresh_capacity_block_once(self) -> bool:
    refresh = getattr(self._repository, "refresh_next_capacity_block", None)
    if not callable(refresh):
      return False
    return bool(refresh())

  def _repair_once(self) -> bool:
    repair = getattr(self._repository, "repair_next_aggregate", None)
    if not callable(repair):
      return False
    return bool(repair())

  def _generate_batch(self, stage: dict[str, Any]) -> list[dict[str, Any]]:
    template, grid = self._restore_grid(stage)
    cursor = int(stage["generation_cursor"])
    end = min(
      cursor + settings.EXPERIMENT_GENERATION_BATCH_SIZE,
      grid.planned_trial_count,
    )
    return [
      self._trial_record(stage, template, grid, trial_index)
      for trial_index in range(cursor, end)
    ]

  def _restore_grid(
    self,
    stage: dict[str, Any],
  ) -> tuple[ResolvedRunTemplate, GeneratedGrid]:
    experiment_spec = dict(stage["experiment_spec"])
    snapshot = dict(experiment_spec["runTemplateSnapshot"])
    inputs = tuple(snapshot["tunableInputCatalog"]["inputs"])
    defaults = {
      item["sourceName"]: item["defaultValue"]
      for item in inputs
      if item.get("group") == "strategyParameters"
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
    grid = self._generator.normalize(request_stage, template)
    if (
      grid.stage_input_fingerprint != stage["stage_input_fingerprint"]
      or grid.planned_trial_count != int(stage["planned_trial_count"])
    ):
      raise ExperimentError(
        "experiment.generation_identity_conflict",
        "冻结 Stage 与生成器输出不一致",
        409,
      )
    return template, grid

  def _trial_record(
    self,
    stage: dict[str, Any],
    template: ResolvedRunTemplate,
    grid: GeneratedGrid,
    trial_index: int,
  ) -> dict[str, Any]:
    assignment = grid.assignment_at(trial_index)
    compiled = self._compiler.compile(template, assignment)
    trial_fingerprint = grid.trial_fingerprint(
      trial_index, assignment, self._compiler.contract_version,
    )
    trial_spec = with_version({
      "schemaVersion": "trial_spec_v1",
      "experimentId": str(stage["experiment_id"]),
      "experimentRevision": int(stage["revision_no"]),
      "stageId": str(stage["id"]),
      "generatorType": grid.normalized_stage["stageType"],
      "generatorVersion": grid.normalized_stage["generatorVersion"],
      "trialIndex": trial_index,
      "runTemplateDigest": template.digest,
      "stageInputFingerprint": grid.stage_input_fingerprint,
      "dimensionAssignment": assignment,
      "compiledRequest": compiled,
      "generationProvenance": {
        "stageType": "finite_grid_v1",
        "generatorVersion": "1",
      },
      "candidateKey": _candidate_key(template.digest, assignment),
      "seedBundle": seed_bundle(trial_fingerprint),
      "determinism": stage["determinism"],
    })
    return {
      "trialIndex": trial_index,
      "trialFingerprint": trial_fingerprint,
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


def _public_generation_message(error: Exception) -> str:
  if isinstance(error, ExperimentError):
    return str(error)
  return "Trial 生成失败，请联系管理员并在修复后恢复"
