# -*- coding: utf-8 -*-
"""进程内数据同步任务管理。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import threading
import uuid
from typing import Any, Callable

_executor = ThreadPoolExecutor(max_workers=2)
_lock = threading.Lock()
_tasks: dict[str, dict[str, Any]] = {}


def start_task(
  request: dict[str, Any],
  worker: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> dict[str, Any]:
  task_id = uuid.uuid4().hex
  task = {
    "id": task_id,
    "status": "queued",
    "request": request,
    "results": [],
    "error": "",
    "createdAt": _now(),
    "startedAt": None,
    "finishedAt": None,
  }
  with _lock:
    _tasks[task_id] = task
  _executor.submit(_run_task, task_id, request, worker)
  return task.copy()


def get_task(task_id: str) -> dict[str, Any] | None:
  with _lock:
    task = _tasks.get(task_id)
    return task.copy() if task else None


def _run_task(
  task_id: str,
  request: dict[str, Any],
  worker: Callable[[dict[str, Any]], list[dict[str, Any]]],
) -> None:
  _update(task_id, status="running", startedAt=_now())
  try:
    results = worker(request)
    has_error = any(item.get("status") == "error" for item in results)
    _update(
      task_id,
      status="partial_error" if has_error else "ok",
      results=results,
      finishedAt=_now(),
    )
  except Exception as e:
    _update(task_id, status="error", error=str(e), finishedAt=_now())


def _update(task_id: str, **updates: Any) -> None:
  with _lock:
    if task_id in _tasks:
      _tasks[task_id].update(updates)


def _now() -> str:
  return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
