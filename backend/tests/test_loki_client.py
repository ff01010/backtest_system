from __future__ import annotations

import json
from typing import Any

from backend import settings
from backend.services import loki_client


class _Response:
  status = 200

  def __init__(self, payload: dict[str, Any]) -> None:
    self._content = json.dumps(payload).encode("utf-8")

  def __enter__(self) -> "_Response":
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def read(self, _limit: int) -> bytes:
    return self._content


def test_loki_query_uses_fixed_template_and_returns_only_safe_fields(monkeypatch) -> None:
  captured_url = ""
  event = {
    "timestamp": "2026-09-23T01:02:03Z",
    "event": "attempt.failed",
    "message": "safe",
    "runId": "117f14fd-203a-4705-8fe3-c4a3a42d4746",
    "attemptToken": "6e6a7c64-0594-4ff5-a0a8-0477de5c5c59",
    "sourceCode": "private strategy",
  }
  payload = {
    "status": "success",
    "data": {"result": [{"values": [["1", json.dumps(event)]]}]},
  }

  def fake_urlopen(request: Any, timeout: int) -> _Response:
    nonlocal captured_url
    captured_url = request.full_url
    assert timeout == settings.LOG_QUERY_TIMEOUT_SECONDS
    return _Response(payload)

  monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", True)
  monkeypatch.setattr(loki_client, "urlopen", fake_urlopen)

  status, events = loki_client.query_task_events(
    "117f14fd-203a-4705-8fe3-c4a3a42d4746",
    [],
    "6e6a7c64-0594-4ff5-a0a8-0477de5c5c59",
  )

  assert status == "available"
  assert "query_range" in captured_url
  assert "compose_project" in captured_url
  assert events[0]["message"] == "safe"
  assert "sourceCode" not in events[0]


def test_disabled_loki_does_not_make_network_request(monkeypatch) -> None:
  monkeypatch.setattr(settings, "OBSERVABILITY_ENABLED", False)
  monkeypatch.setattr(
    loki_client,
    "urlopen",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应访问网络")),
  )

  assert loki_client.query_task_events(
    "117f14fd-203a-4705-8fe3-c4a3a42d4746", [],
  ) == ("disabled", [])
