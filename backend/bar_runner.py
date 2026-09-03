"""独立标准 Bar 回测 Runner。"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any

from backend import settings
from backend.services import backtest_runner, research_result_outbox, state_store
from backend.services.backtest_errors import error_summary
from backend.services.system_error_log import system_error_log_record

logger = logging.getLogger(__name__)


def _dispatch_research_result_outbox() -> None:
  """发送研究信号回测回流，不影响已落库的回测结果。"""
  try:
    research_result_outbox.dispatch_pending()
  except Exception:
    logger.exception("研究信号回测结果回流派发失败")


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
    lease = _BarJobLeaseKeeper(job)
    stage = "bar.initialize"
    try:
      lease.start()
      payload = dict(job["payload"])
      stage = "bar.backtest"
      result = backtest_runner.run_backtest(payload)
      lease.ensure_active()
      summary = dict(result["summary"])
      summary["warningCount"] = len(result.get("warnings") or [])
      summary["fillModel"] = str(payload.get("fillModel", "bar_next_open"))
      state_store.finish_bar_backtest_job(job["runId"], job["attemptToken"], "succeeded", summary, result)
      _dispatch_research_result_outbox()
    except Exception as error:
      logger.exception("标准 Bar 回测任务失败: %s", job["runId"])
      status = "cancelled" if "租约丢失或已取消" in str(error) else "failed"
      system_error = None
      if status == "failed":
        system_error = system_error_log_record(error, "bar_runner", stage, job["runId"])
      state_store.finish_bar_backtest_job(
        job["runId"], job["attemptToken"], status, error_summary(error, job["runId"]),
        system_error=system_error.to_dict() if system_error else None,
        owner_user_id=job["ownerUserId"],
      )
      _dispatch_research_result_outbox()
    finally:
      lease.stop()


class _BarJobLeaseKeeper:
  """在 Bar 策略调用阻塞时维持租约，并记录取消信号。"""

  def __init__(self, job: dict[str, Any]) -> None:
    self._job = job
    self._stop_event = threading.Event()
    self._lock = threading.Lock()
    self._failure: Exception | None = None
    self._thread = threading.Thread(target=self._run, name="bar-job-lease", daemon=True)

  def start(self) -> None:
    self._thread.start()

  def stop(self) -> None:
    self._stop_event.set()
    self._thread.join(timeout=2)

  def ensure_active(self) -> None:
    with self._lock:
      failure = self._failure
    if failure is not None:
      raise failure

  def _run(self) -> None:
    interval = max(1.0, min(10.0, settings.L2_JOB_LEASE_SECONDS / 3))
    while not self._stop_event.wait(interval):
      if not state_store.heartbeat_bar_backtest_job(
        self._job["runId"], self._job["attemptToken"], settings.L2_JOB_LEASE_SECONDS,
      ):
        with self._lock:
          self._failure = ValueError("Bar 任务租约丢失或已取消")
        return


def main() -> None:
  logging.basicConfig(level=os.getenv("TREND_GRID_LOG_LEVEL", "INFO"))
  BarRunner().run_forever()


if __name__ == "__main__":
  main()
