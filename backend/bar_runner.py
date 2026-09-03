"""独立标准 Bar 回测 Runner。"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any

from backend import settings
from backend.bar_strategy_session import BarStrategySession
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
    self._health_signature: tuple[str, str | None, str | None] | None = None
    self._health_written_at = 0.0

  def run_forever(self) -> None:
    backoff_seconds = 1.0
    while True:
      try:
        job = state_store.claim_bar_backtest_job(self._worker_id, settings.L2_JOB_LEASE_SECONDS)
      except state_store.DatabaseError as error:
        self._handle_database_error(error, backoff_seconds)
        backoff_seconds = min(
          backoff_seconds * 2, settings.BACKTEST_RUNNER_ERROR_BACKOFF_MAX_SECONDS,
        )
        continue
      backoff_seconds = 1.0
      if job is None:
        self._record_health("ready")
        time.sleep(settings.L2_RUNNER_POLL_SECONDS)
        continue
      self._record_health("running", job["runId"])
      try:
        self._run_job(job)
      except state_store.DatabaseError as error:
        self._handle_database_error(error, backoff_seconds)

  def _handle_database_error(self, error: Exception, backoff_seconds: float) -> None:
    logger.exception("Bar Runner 数据库操作失败，%.1f 秒后重试", backoff_seconds)
    self._record_health("error", error_code=type(error).__name__)
    time.sleep(backoff_seconds)

  def _record_health(
    self,
    status: str,
    current_run_id: str | None = None,
    error_code: str | None = None,
  ) -> None:
    signature = (status, current_run_id, error_code)
    now = time.monotonic()
    if (
      signature == self._health_signature
      and now - self._health_written_at < settings.BACKTEST_RUNNER_HEARTBEAT_WRITE_SECONDS
    ):
      return
    try:
      state_store.record_backtest_runner_heartbeat(
        self._worker_id, "bar", status, current_run_id, error_code,
      )
      self._health_signature = signature
      self._health_written_at = now
    except state_store.DatabaseError:
      logger.exception("Bar Runner 健康心跳写入失败")

  def run_once(self) -> bool:
    job = state_store.claim_bar_backtest_job(self._worker_id, settings.L2_JOB_LEASE_SECONDS)
    if job is None:
      return False
    self._run_job(job)
    return True

  def _run_job(self, job: dict[str, Any]) -> None:
    lease = _BarJobLeaseKeeper(job, self._worker_id)
    stage = "bar.initialize"
    try:
      lease.start()
      payload = dict(job["payload"])
      stage = "bar.backtest"
      if payload.get("strategy") in {"user_script", "research_signal"}:
        session = BarStrategySession(lease.ensure_active, settings.BACKTEST_CANCEL_GRACE_SECONDS)
        result = backtest_runner.run_backtest(payload, session.run_script)
      else:
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

  def __init__(self, job: dict[str, Any], worker_id: str = "unknown") -> None:
    self._job = job
    self._worker_id = worker_id
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
    interval = min(settings.BACKTEST_CANCEL_POLL_SECONDS, settings.L2_JOB_LEASE_SECONDS / 3)
    while not self._stop_event.wait(interval):
      try:
        renewed = state_store.heartbeat_bar_backtest_job(
          self._job["runId"], self._job["attemptToken"], settings.L2_JOB_LEASE_SECONDS,
          self._worker_id,
        )
      except Exception as error:
        with self._lock:
          self._failure = error
        return
      if not renewed:
        with self._lock:
          self._failure = ValueError("Bar 任务租约丢失或已取消")
        return


def main() -> None:
  logging.basicConfig(level=os.getenv("TREND_GRID_LOG_LEVEL", "INFO"))
  state_store.ensure_schema()
  BarRunner().run_forever()


if __name__ == "__main__":
  main()
