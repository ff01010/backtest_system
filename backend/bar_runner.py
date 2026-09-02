"""独立标准 Bar 回测 Runner。"""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Any

from backend import settings
from backend.services import backtest_runner, research_result_outbox, state_store
from backend.services.backtest_errors import error_summary
from backend.services.system_error_log import system_error_log_record

logger = logging.getLogger(__name__)


class BarRunner:
  """以 PostgreSQL 租约执行标准 Bar 回测。"""

  def __init__(self, worker_id: str | None = None) -> None:
    self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"

  def run_forever(self) -> None:
    while True:
      job = state_store.claim_bar_backtest_job(self._worker_id, settings.L2_JOB_LEASE_SECONDS)
      if job is None:
        time.sleep(settings.L2_RUNNER_POLL_SECONDS)
        continue
      self._run_job(job)

  def run_once(self) -> bool:
    job = state_store.claim_bar_backtest_job(self._worker_id, settings.L2_JOB_LEASE_SECONDS)
    if job is None:
      return False
    self._run_job(job)
    return True

  def _run_job(self, job: dict[str, Any]) -> None:
    stage = "bar.initialize"
    try:
      payload = dict(job["payload"])
      stage = "bar.backtest"
      result = backtest_runner.run_backtest(payload)
      if not state_store.heartbeat_bar_backtest_job(job["runId"], job["attemptToken"], settings.L2_JOB_LEASE_SECONDS):
        raise ValueError("Bar 回测任务已取消或租约丢失")
      summary = dict(result["summary"])
      summary["warningCount"] = len(result.get("warnings") or [])
      summary["fillModel"] = str(payload.get("fillModel", "bar_next_open"))
      state_store.finish_bar_backtest_job(job["runId"], job["attemptToken"], "succeeded", summary, result)
      _dispatch_research_result_outbox()
    except Exception as error:
      logger.exception("标准 Bar 回测任务失败: %s", job["runId"])
      status = "cancelled" if "已取消或租约丢失" in str(error) else "failed"
      system_error = None
      if status == "failed":
        system_error = system_error_log_record(error, "bar_runner", stage, job["runId"])
      state_store.finish_bar_backtest_job(
        job["runId"], job["attemptToken"], status, error_summary(error, job["runId"]),
        system_error=system_error.to_dict() if system_error else None,
        owner_user_id=job["ownerUserId"],
      )
      _dispatch_research_result_outbox()


def main() -> None:
  logging.basicConfig(level=os.getenv("TREND_GRID_LOG_LEVEL", "INFO"))
  BarRunner().run_forever()


if __name__ == "__main__":
  main()
