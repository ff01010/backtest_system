# -*- coding: utf-8 -*-
"""测试研究结果回传的实例隔离与兼容行为。"""

import json
from typing import Any

from backend.services import research_result_outbox


def test_dispatch_is_noop_when_reporting_disabled(monkeypatch: Any) -> None:
  monkeypatch.setattr(research_result_outbox.settings, "RESEARCH_RESULT_REPORT_ENABLED", False)
  monkeypatch.setattr(
    research_result_outbox,
    "_claim_one",
    lambda: (_ for _ in ()).throw(AssertionError("不应领取回传任务")),
  )

  assert research_result_outbox.dispatch_pending() == 0


def test_post_result_adds_instance_to_historical_payload(monkeypatch: Any) -> None:
  captured: dict[str, Any] = {}

  class Response:
    status = 204

    def __enter__(self) -> "Response":
      return self

    def __exit__(self, *_args: object) -> None:
      return None

  def fake_urlopen(request: Any, timeout: int) -> Response:
    captured["url"] = request.full_url
    captured["payload"] = json.loads(request.data)
    captured["timeout"] = timeout
    return Response()

  monkeypatch.setattr(research_result_outbox.settings, "BACKTEST_INSTANCE_ID", "server")
  monkeypatch.setattr(research_result_outbox.settings, "RESEARCH_SIGNAL_API_URL", "http://research:8000")
  monkeypatch.setattr(research_result_outbox.settings, "RESEARCH_SIGNAL_SERVICE_TOKEN", "test-token")
  monkeypatch.setattr(research_result_outbox, "urlopen", fake_urlopen)

  research_result_outbox._post_result({"backtestRunId": "run-1"})

  assert captured["url"] == "http://research:8000/internal/v1/backtest-links"
  assert captured["payload"]["backtestInstanceId"] == "server"
  assert captured["timeout"] == 15
