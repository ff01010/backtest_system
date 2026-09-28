"""只读观察本 Compose 项目的 Docker 容器生命周期事件。"""

from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any, Iterator, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from backend import settings
from backend.services import state_store
from backend.services.task_diagnostics import emit_external_event


logger = logging.getLogger(__name__)
_ACTIONS = {"oom", "die", "restart", "destroy"}


def observe_forever() -> None:
  backoff_seconds = 1.0
  degraded = False
  while True:
    try:
      for event in _observe_stream():
        if degraded:
          emit_external_event({
            "service": "container-event-observer",
            "event": "log_pipeline.recovered",
            "message": "容器事件观察已恢复",
            "stage": "observability.pipeline",
          })
          degraded = False
          backoff_seconds = 1.0
        if event is not None:
          _handle_event(event)
      raise OSError("Docker 事件流意外结束")
    except (OSError, TimeoutError, json.JSONDecodeError) as error:
      if "timed out" in str(error).lower() and _proxy_healthy():
        logger.info("Docker 容器事件流空闲超时，重新连接")
        continue
      logger.exception("Docker 容器事件观察器连接中断")
      if not degraded:
        emit_external_event({
          "service": "container-event-observer",
          "event": "log_pipeline.degraded",
          "message": "容器事件观察暂不可用",
          "stage": "observability.pipeline",
          "terminationReason": "unknown_interruption",
        }, level="error")
        degraded = True
      time.sleep(backoff_seconds)
      backoff_seconds = min(backoff_seconds * 2, 30.0)


def _observe_stream() -> Iterator[dict[str, Any] | None]:
  filters = json.dumps({
    "type": ["container"],
    "event": sorted(_ACTIONS),
    "label": [f"com.docker.compose.project={settings.COMPOSE_PROJECT_NAME}"],
  }, separators=(",", ":"))
  request = Request(f"{settings.DOCKER_EVENTS_URL}/events?{urlencode({'filters': filters})}")
  with urlopen(request, timeout=300) as response:
    yield None
    for raw_line in response:
      if not raw_line.strip():
        continue
      event = json.loads(raw_line)
      if isinstance(event, dict):
        yield event


def _handle_event(event: dict[str, Any]) -> None:
  action = str(event.get("Action") or event.get("status") or "")
  if action not in _ACTIONS:
    return
  actor = event.get("Actor") if isinstance(event.get("Actor"), dict) else {}
  attributes = actor.get("Attributes") if isinstance(actor.get("Attributes"), dict) else {}
  container_id = str(actor.get("ID") or event.get("id") or "")
  if not container_id:
    return
  exit_code = _optional_int(attributes.get("exitCode"))
  event_name = {
    "oom": "container.oom",
    "die": "container.died",
    "destroy": "container.died",
    "restart": "container.restarted",
  }[action]
  reason = "container_oom" if action == "oom" else "container_died"
  attempts: list[dict[str, Any]] = []
  if action != "restart":
    try:
      attempts = state_store.interrupt_container_attempts(
        container_id, reason, exit_code,
      )
    except state_store.DatabaseError:
      logger.exception("容器事件关联 Attempt 失败，将由租约过期兜底")
  base = {
    "service": "container-event-observer",
    "event": event_name,
    "message": "Docker 已确认容器生命周期事件",
    "containerId": container_id[:64],
    "stage": "container.lifecycle",
    "exitCode": exit_code,
  }
  if action != "restart":
    base["terminationReason"] = reason
  if not attempts:
    emit_external_event(base, level="error" if action != "restart" else "warning")
    return
  for attempt in attempts:
    emit_external_event({
      **base,
      **attempt,
      "status": "interrupted",
    }, level="error")


def _proxy_healthy() -> bool:
  try:
    with urlopen(f"{settings.DOCKER_EVENTS_URL}/_ping", timeout=3) as response:
      return response.status == 200 and response.read(16).strip() == b"OK"
  except (OSError, TimeoutError):
    return False


def _optional_int(value: Any) -> int | None:
  try:
    return int(value) if value is not None else None
  except (TypeError, ValueError):
    return None


def main(argv: Sequence[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--healthcheck", action="store_true")
  args = parser.parse_args(argv)
  if args.healthcheck:
    if not _proxy_healthy():
      raise SystemExit(1)
    return
  logging.basicConfig(level=logging.INFO)
  settings.validate_observability_configuration()
  state_store.ensure_schema()
  observe_forever()


if __name__ == "__main__":
  main()
