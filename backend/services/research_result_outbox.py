# -*- coding: utf-8 -*-
"""Reliably deliver research-signal backtest summaries after run persistence."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from psycopg.types.json import Jsonb

from backend import settings
from backend.services import state_store


def dispatch_pending(limit: int = 10) -> int:
  """Attempt pending deliveries without allowing failures to affect stored runs."""
  if not settings.RESEARCH_RESULT_REPORT_ENABLED:
    return 0
  delivered = 0
  for _ in range(limit):
    item = _claim_one()
    if item is None:
      return delivered
    try:
      _post_result(item["payload"])
    except (HTTPError, URLError, ValueError) as error:
      _mark_failed(item["runId"], item["attemptCount"], str(error))
    else:
      _mark_delivered(item["runId"])
      delivered += 1
  return delivered


def _claim_one() -> dict[str, Any] | None:
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT run_id, payload, attempt_count FROM research_signal_result_outbox
        WHERE status IN ('pending', 'delivering') AND next_attempt_at <= now()
        ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
      """)
      row = cur.fetchone()
      if row is None:
        return None
      cur.execute("""
        UPDATE research_signal_result_outbox
        SET status='delivering', attempt_count=attempt_count + 1, updated_at=now()
        WHERE run_id=%s
      """, (row["run_id"],))
      return {
        "runId": str(row["run_id"]),
        "payload": dict(row["payload"]),
        "attemptCount": int(row["attempt_count"]) + 1,
      }


def _post_result(payload: dict[str, Any]) -> None:
  if not settings.RESEARCH_SIGNAL_API_URL or not settings.RESEARCH_SIGNAL_SERVICE_TOKEN:
    raise ValueError("研究回流服务尚未配置")
  compatible_payload = dict(payload)
  compatible_payload.setdefault("backtestInstanceId", settings.BACKTEST_INSTANCE_ID)
  request = Request(
    f"{settings.RESEARCH_SIGNAL_API_URL}/internal/v1/backtest-links",
    data=json.dumps(compatible_payload, separators=(",", ":")).encode("utf-8"),
    headers={
      "Content-Type": "application/json",
      "X-Service-Token": settings.RESEARCH_SIGNAL_SERVICE_TOKEN,
      "X-User-Id": "backtest-service",
    },
    method="POST",
  )
  with urlopen(request, timeout=15) as response:
    if response.status < 200 or response.status >= 300:
      raise ValueError("研究回流服务返回失败状态")


def _mark_delivered(run_id: str) -> None:
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE research_signal_result_outbox
        SET status='delivered', delivered_at=now(), last_error=NULL, updated_at=now()
        WHERE run_id=%s
      """, (run_id,))


def _mark_failed(run_id: str, attempt_count: int, error: str) -> None:
  delay = min(3_600, 30 * (2 ** min(attempt_count, 6)))
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE research_signal_result_outbox
        SET status='pending', next_attempt_at=now() + make_interval(secs => %s),
          last_error=%s, updated_at=now()
        WHERE run_id=%s
      """, (delay, error[:500], run_id))
