"""在单个容器内监督多个相互独立的回测 Runner 进程。"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Sequence

from backend.services import state_store
from backend.services.task_diagnostics import emit_external_event


logger = logging.getLogger(__name__)
_RUNNER_MODULES = {"bar": "backend.bar_runner", "l2": "backend.l2_runner"}


def _process_count(value: str) -> int:
  try:
    count = int(value)
  except ValueError as error:
    raise argparse.ArgumentTypeError("Runner进程数必须是整数") from error
  if not 1 <= count <= 8:
    raise argparse.ArgumentTypeError("Runner进程数必须在1到8之间")
  return count


def _start_worker(kind: str) -> subprocess.Popen[bytes]:
  module = _RUNNER_MODULES[kind]
  return subprocess.Popen(
    [sys.executable, "-m", module],
    start_new_session=os.name == "posix",
  )


def _signal_worker_group(
  worker: subprocess.Popen[bytes],
  *,
  force: bool = False,
) -> None:
  if os.name == "posix":
    try:
      os.killpg(worker.pid, signal.SIGKILL if force else signal.SIGTERM)
      return
    except ProcessLookupError:
      return
  if force:
    worker.kill()
  else:
    worker.terminate()


def _reap_exited_worker_group(worker: subprocess.Popen[bytes]) -> None:
  """Runner异常退出时清理仍留在同一进程组的策略子进程。"""
  if os.name != "posix":
    return
  try:
    os.killpg(worker.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass


def _record_worker_exit(kind: str, worker_pid: int, exit_code: int) -> None:
  worker_id = f"{socket.gethostname()}:{worker_pid}"
  reason = "runner_exit_nonzero" if exit_code != 0 else "unknown_interruption"
  attempts: list[dict[str, object]] = []
  try:
    attempts = state_store.interrupt_worker_attempts(worker_id, reason, exit_code)
  except state_store.DatabaseError:
    logger.exception("Runner 退出关联 Attempt 失败，将由租约过期兜底")
  if not attempts:
    emit_external_event({
      "service": "runner-supervisor",
      "event": "runner_process.exited",
      "message": "Runner 进程已退出，未能立即关联执行尝试",
      "jobKind": kind,
      "workerId": worker_id,
      "containerId": socket.gethostname(),
      "processId": worker_pid,
      "stage": "runner.supervisor",
      "exitCode": exit_code,
      "terminationReason": reason,
    }, level="error")
    return
  for attempt in attempts:
    emit_external_event({
      "service": "runner-supervisor",
      "event": "runner_process.exited",
      "message": "Runner 进程异常退出",
      **attempt,
      "stage": "runner.supervisor",
      "status": "interrupted",
      "exitCode": exit_code,
      "terminationReason": reason,
    }, level="error")


def _stop_workers(workers: list[subprocess.Popen[bytes]]) -> None:
  for worker in workers:
    if worker.poll() is None:
      _signal_worker_group(worker)
  deadline = time.monotonic() + 10
  for worker in workers:
    if worker.poll() is not None:
      continue
    try:
      worker.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
      _signal_worker_group(worker, force=True)
      worker.wait(timeout=5)


def run(kind: str, count: int) -> None:
  stopping = False

  def request_stop(_signum: int, _frame: object) -> None:
    nonlocal stopping
    stopping = True

  signal.signal(signal.SIGTERM, request_stop)
  signal.signal(signal.SIGINT, request_stop)
  workers = [_start_worker(kind) for _ in range(count)]
  logger.info("已启动 %s 个 %s Runner进程", count, kind)
  try:
    while not stopping:
      for index, worker in enumerate(workers):
        exit_code = worker.poll()
        if exit_code is None:
          continue
        logger.error("%s Runner进程异常退出: code=%s", kind, exit_code)
        _record_worker_exit(kind, worker.pid, exit_code)
        _reap_exited_worker_group(worker)
        workers[index] = _start_worker(kind)
      time.sleep(0.5)
  finally:
    _stop_workers(workers)


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("kind", choices=sorted(_RUNNER_MODULES))
  parser.add_argument("--processes", type=_process_count, default=1)
  return parser


def main(argv: Sequence[str] | None = None) -> None:
  logging.basicConfig(level=logging.INFO)
  args = _parser().parse_args(argv)
  run(args.kind, args.processes)


if __name__ == "__main__":
  main()
