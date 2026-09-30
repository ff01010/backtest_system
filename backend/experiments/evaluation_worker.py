"""带租约的基础评价 Worker。"""

from __future__ import annotations

import logging
import os
import socket
import time

from backend import settings
from backend.experiments.errors import ExperimentError
from backend.experiments.evaluation import evaluate, output_hash
from backend.experiments.evaluation_repository import PostgresEvaluationRepository


logger = logging.getLogger(__name__)


class EvaluationWorker:
  def __init__(
    self,
    *,
    repository: PostgresEvaluationRepository | None = None,
    worker_id: str | None = None,
    lease_seconds: int | None = None,
  ) -> None:
    self._repository = repository or PostgresEvaluationRepository()
    self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    self._lease_seconds = lease_seconds or settings.EXPERIMENT_COORDINATOR_LEASE_SECONDS

  def run_once(self) -> bool:
    claimed = self._repository.claim_next_run(self._worker_id, self._lease_seconds)
    if claimed is None:
      return False
    run_id = str(claimed.run["id"])
    try:
      sources = self._repository.load_run_sources(claimed.run)
      output = evaluate(
        dict(claimed.run["evaluation_spec"]),
        dict(claimed.run["catalog_snapshot"]),
        sources,
        str(claimed.run["result_set_hash"]),
      )
      self._repository.complete_run(
        run_id, claimed.attempt_token, output, output_hash(output),
      )
    except ExperimentError as error:
      self._repository.fail_run(
        run_id, claimed.attempt_token, error.code, str(error),
      )
    except Exception:
      logger.exception("评价运行失败: %s", run_id)
      self._repository.fail_run(
        run_id,
        claimed.attempt_token,
        "experiment.evaluation_internal_error",
        "评价计算失败，请重试或联系管理员",
      )
    return True

  def run_forever(self, poll_seconds: float = 1.0) -> None:
    while True:
      if not self.run_once():
        time.sleep(poll_seconds)
