"""受控 Loki 查询适配器；失败只降低诊断证据，不影响回测业务。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import UUID

from backend import settings
from backend.services.task_diagnostics import sanitize_text


class LokiUnavailableError(RuntimeError):
  """集中日志当前不可查询。"""


_SAFE_EVENT_FIELDS = {
  "timestamp", "schemaVersion", "level", "service", "event", "message",
  "runId", "attemptNo", "attemptToken", "jobKind", "workerId",
  "containerId", "processId", "stage", "status", "errorCode",
  "exceptionType", "exitCode", "terminationReason", "durationMs",
  "applicationBuildId", "runtimeVersion", "capabilityDigest", "truncated",
  "originalCharacters", "contentDigest", "frameSummary",
}
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def query_task_events(
  run_id: str,
  attempts: list[dict[str, Any]],
  attempt_token: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
  """仅按 UUID 和固定模板查询，调用方不能传入 LogQL。"""
  _validate_uuid(run_id, "任务 UUID")
  if attempt_token:
    _validate_uuid(attempt_token, "Attempt UUID")
  if not settings.OBSERVABILITY_ENABLED:
    return "disabled", []
  start, end = _query_window(attempts)
  query = (
    f'{{compose_project="{settings.COMPOSE_PROJECT_NAME}"}} | json '
    f'| runId="{run_id}"'
  )
  if attempt_token:
    query += f' | attemptToken="{attempt_token}"'
  params = urlencode({
    "query": query,
    "start": str(int(start.timestamp() * 1_000_000_000)),
    "end": str(int(end.timestamp() * 1_000_000_000)),
    "limit": str(settings.LOG_QUERY_MAX_LINES),
    "direction": "forward",
  })
  request = Request(
    f"{settings.LOKI_URL}/loki/api/v1/query_range?{params}",
    headers={"Accept": "application/json"},
  )
  try:
    with urlopen(request, timeout=settings.LOG_QUERY_TIMEOUT_SECONDS) as response:
      response_body = response.read(_MAX_RESPONSE_BYTES + 1)
      if len(response_body) > _MAX_RESPONSE_BYTES:
        raise LokiUnavailableError("集中日志响应超过安全上限")
      payload = json.loads(response_body)
  except LokiUnavailableError:
    raise
  except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as error:
    raise LokiUnavailableError("集中日志暂不可用") from error
  events = _parse_events(payload, run_id, attempt_token)
  if events:
    return "available", events
  if _logs_expired(attempts):
    return "expired", []
  return "available", []


def health_status() -> dict[str, Any]:
  if not settings.OBSERVABILITY_ENABLED:
    return {"status": "disabled"}
  try:
    with urlopen(
      f"{settings.LOKI_URL}/ready",
      timeout=min(settings.LOG_QUERY_TIMEOUT_SECONDS, 1),
    ) as response:
      healthy = response.status == 200
  except (OSError, TimeoutError):
    healthy = False
  return {"status": "available" if healthy else "unavailable"}


def _parse_events(
  payload: Any,
  run_id: str,
  attempt_token: str | None,
) -> list[dict[str, Any]]:
  if not isinstance(payload, dict) or payload.get("status") != "success":
    raise LokiUnavailableError("集中日志响应无效")
  result = payload.get("data", {}).get("result", [])
  if not isinstance(result, list):
    raise LokiUnavailableError("集中日志响应无效")
  events: list[dict[str, Any]] = []
  for stream in result:
    values = stream.get("values", []) if isinstance(stream, dict) else []
    for value in values if isinstance(values, list) else []:
      if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str):
        continue
      try:
        event = json.loads(value[1])
      except json.JSONDecodeError:
        continue
      if not isinstance(event, dict) or event.get("runId") != run_id:
        continue
      if attempt_token and event.get("attemptToken") != attempt_token:
        continue
      safe = {key: event[key] for key in _SAFE_EVENT_FIELDS if key in event}
      safe["message"] = sanitize_text(str(safe.get("message") or ""), 4096)[0]
      safe["logTimestampNs"] = str(value[0])
      events.append(safe)
  events.sort(key=lambda item: (str(item.get("timestamp") or ""), item["logTimestampNs"]))
  return events[:settings.LOG_QUERY_MAX_LINES]


def _query_window(attempts: list[dict[str, Any]]) -> tuple[datetime, datetime]:
  now = datetime.now(timezone.utc)
  values = [
    parsed
    for attempt in attempts
    for parsed in (
      _parse_time(attempt.get("startedAt")),
      _parse_time(attempt.get("finishedAt")),
    )
    if parsed is not None
  ]
  earliest = min(values) if values else now - timedelta(hours=1)
  latest = max(values) if values else now
  maximum_start = now - timedelta(hours=settings.LOG_QUERY_MAX_HOURS)
  start = max(earliest - timedelta(minutes=5), maximum_start)
  end = min(max(latest + timedelta(minutes=5), start), now + timedelta(minutes=1))
  return start, end


def _logs_expired(attempts: list[dict[str, Any]]) -> bool:
  finished = [
    value for value in (_parse_time(item.get("finishedAt")) for item in attempts)
    if value is not None
  ]
  if not finished:
    return False
  return max(finished) < datetime.now(timezone.utc) - timedelta(
    hours=settings.LOG_RETENTION_HOURS,
  )


def _parse_time(value: Any) -> datetime | None:
  if not isinstance(value, str) or not value:
    return None
  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError:
    return None
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
  return parsed.astimezone(timezone.utc)


def _validate_uuid(value: str, label: str) -> None:
  try:
    UUID(value)
  except ValueError as error:
    raise ValueError(f"{label} 无效") from error
