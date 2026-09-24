from __future__ import annotations

import json

import pytest

from backend.services.task_diagnostics import (
  AttemptContext,
  _timeline_sort_key,
  build_task_event,
  exception_diagnostic,
)


def _context() -> AttemptContext:
  return AttemptContext.from_job(
    {
      "runId": "117f14fd-203a-4705-8fe3-c4a3a42d4746",
      "attemptNo": 2,
      "attemptToken": "6e6a7c64-0594-4ff5-a0a8-0477de5c5c59",
    },
    "bar",
    "runner-container:4321",
    service="bar-runner",
  )


def test_task_event_is_single_line_json_with_fixed_attempt_context() -> None:
  event = build_task_event(
    _context(), "attempt.stage_changed", "进入执行阶段", stage="bar.backtest",
  )

  serialized = json.dumps(event, ensure_ascii=False)
  assert "\n" not in serialized
  assert event["schemaVersion"] == "task-log-v1"
  assert event["runId"] == "117f14fd-203a-4705-8fe3-c4a3a42d4746"
  assert event["attemptNo"] == 2
  assert event["workerId"] == "runner-container:4321"
  assert event["containerId"] == "runner-container"
  assert event["processId"] == 4321
  assert event["stage"] == "bar.backtest"


def test_task_event_redacts_credentials_urls_and_truncates_message() -> None:
  event = build_task_event(
    _context(),
    "attempt.failed",
    "password=top-secret token: abc123 postgresql://user:pass@db/app\n" + "x" * 5000,
    level="error",
    stage="bar.backtest",
    status="failed",
    terminationReason="exception",
  )

  message = str(event["message"])
  assert "top-secret" not in message
  assert "abc123" not in message
  assert "user:pass" not in message
  assert "\n" not in message
  assert event["truncated"] is True
  assert event["originalCharacters"] > 4096
  assert len(message) <= 4096


def test_task_event_rejects_unknown_fields() -> None:
  with pytest.raises(ValueError, match="字段"):
    build_task_event(
      _context(), "attempt.started", "开始", sourceCode="private strategy",
    )


def test_exception_diagnostic_keeps_frame_summary_without_source_line() -> None:
  try:
    raise RuntimeError("authorization=Bearer private-value")
  except RuntimeError as error:
    diagnostic = exception_diagnostic(error)

  assert diagnostic["exceptionType"] == "RuntimeError"
  assert "private-value" not in diagnostic["reason"]
  assert "test_task_diagnostics.py" in diagnostic["frameSummary"]
  assert "raise RuntimeError" not in diagnostic["frameSummary"]


def test_timeline_orders_same_second_as_start_error_and_finish() -> None:
  events = [
    {"event": "attempt.failed", "timestamp": "2026-09-24 09:10:53"},
    {"event": "attempt.started", "timestamp": "2026-09-24 09:10:53"},
    {"event": "attempt.error_recorded", "timestamp": "2026-09-24 09:10:53"},
  ]

  events.sort(key=_timeline_sort_key)

  assert [item["event"] for item in events] == [
    "attempt.started", "attempt.error_recorded", "attempt.failed",
  ]


def test_timeline_compares_database_and_loki_timestamps_in_same_timezone() -> None:
  events = [
    {"event": "attempt.failed", "timestamp": "2026-09-24T01:10:54.000Z"},
    {"event": "attempt.started", "timestamp": "2026-09-24 09:10:53"},
  ]

  events.sort(key=_timeline_sort_key)

  assert [item["event"] for item in events] == ["attempt.started", "attempt.failed"]
