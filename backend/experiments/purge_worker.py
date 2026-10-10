"""受审计实验清理 Worker，按冻结依赖图可恢复地推进。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import socket
import time
from typing import Any, Protocol

from backend import settings
from backend.services import state_store


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PurgeProgress:
  phase: str
  completed: bool = False


class PurgeRepository(Protocol):
  def claim_next(self, worker_id: str) -> dict[str, Any] | None: ...

  def heartbeat(self, purge_id: str, attempt_token: str) -> bool: ...

  def load_artifact_batch(
    self, purge_id: str, attempt_token: str,
  ) -> list[dict[str, Any]]: ...

  def mark_artifacts_removed(
    self, purge_id: str, attempt_token: str, item_ids: list[str],
  ) -> None: ...

  def advance_artifact_phase(self, purge_id: str, attempt_token: str) -> bool: ...

  def process_next_batch(
    self, purge_id: str, attempt_token: str,
  ) -> PurgeProgress: ...

  def complete(self, purge_id: str, attempt_token: str) -> None: ...

  def fail(
    self, purge_id: str, attempt_token: str, code: str, message: str,
  ) -> None: ...


class ExperimentPurgeWorker:
  def __init__(
    self,
    *,
    repository: PurgeRepository | None = None,
    artifact_root: Path | None = None,
    worker_id: str | None = None,
  ) -> None:
    if repository is None:
      from backend.experiments.purge_repository import PostgresExperimentPurgeRepository
      repository = PostgresExperimentPurgeRepository()
    self._repository = repository
    self._artifact_root = (artifact_root or Path(settings.OUTPUT_ROOT)).resolve()
    self._worker_id = worker_id or f"{socket.gethostname()}:{time.time_ns()}"

  def run_forever(self) -> None:
    backoff = 1
    while True:
      try:
        if not self.run_once():
          time.sleep(settings.EXPERIMENT_COORDINATOR_POLL_SECONDS)
        backoff = 1
      except state_store.DatabaseError:
        logger.exception("实验清理数据库操作失败")
        time.sleep(backoff)
        backoff = min(backoff * 2, 30)

  def run_once(self) -> bool:
    if not settings.EXPERIMENT_PURGE_ENABLED:
      return False
    claimed = self._repository.claim_next(self._worker_id)
    if claimed is None:
      return False
    purge_id = str(claimed["id"])
    attempt_token = str(claimed["attemptToken"])
    try:
      self._run_claimed(purge_id, attempt_token, str(claimed["phase"]))
    except state_store.DatabaseError:
      logger.exception("实验清理数据库阶段失败: purge_id=%s", purge_id)
      self._repository.fail(
        purge_id, attempt_token, "experiment.purge_database_failed",
        "实验清理数据库阶段失败，将从已记录阶段重试",
      )
    except Exception as error:
      logger.exception("实验清理失败: purge_id=%s", purge_id)
      self._repository.fail(
        purge_id, attempt_token, "experiment.purge_failed",
        "实验清理在当前阶段失败，请检查依赖与存储后重试",
      )
    return True

  def _run_claimed(self, purge_id: str, attempt_token: str, phase: str) -> None:
    while True:
      if not self._repository.heartbeat(purge_id, attempt_token):
        raise RuntimeError("实验清理租约已失效")
      if phase == "artifacts":
        phase = self._process_artifacts(purge_id, attempt_token)
        continue
      progress = self._repository.process_next_batch(purge_id, attempt_token)
      phase = progress.phase
      if progress.completed:
        self._repository.complete(purge_id, attempt_token)
        return

  def _process_artifacts(self, purge_id: str, attempt_token: str) -> str:
    items = self._repository.load_artifact_batch(purge_id, attempt_token)
    if not items:
      advanced = self._repository.advance_artifact_phase(purge_id, attempt_token)
      return "audit" if advanced else "artifacts"
    for item in items:
      self._remove_artifact(str(item.get("storageKey") or ""))
    self._repository.mark_artifacts_removed(
      purge_id, attempt_token, [str(item["itemId"]) for item in items],
    )
    return "artifacts"

  def _remove_artifact(self, storage_key: str) -> None:
    if not storage_key:
      return
    path = (self._artifact_root / storage_key).resolve()
    path.relative_to(self._artifact_root)
    path.unlink(missing_ok=True)
