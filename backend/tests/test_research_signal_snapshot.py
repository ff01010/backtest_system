from __future__ import annotations

from email.message import Message
import json
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request

import pytest

from backend.services import research_signal_snapshot


class FakeResponse:
  def __init__(
    self,
    payload: Any,
    content_type: str = "application/json",
    next_cursor: str | None = None,
  ) -> None:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    self.body = body.encode("utf-8")
    self.headers = Message()
    self.headers["Content-Type"] = content_type
    if next_cursor is not None:
      self.headers["X-Next-Cursor"] = next_cursor

  def __enter__(self) -> FakeResponse:
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def read(self) -> bytes:
    return self.body


def _install_transport(
  monkeypatch: pytest.MonkeyPatch,
  responses: list[FakeResponse],
) -> list[Request]:
  requests: list[Request] = []

  def open_url(request: Request, timeout: int) -> FakeResponse:
    assert timeout == 30
    requests.append(request)
    return responses.pop(0)

  monkeypatch.setattr(research_signal_snapshot, "_SIGNAL_API_URL", "https://signals.test")
  monkeypatch.setattr(research_signal_snapshot, "_SERVICE_TOKEN", "service-token")
  monkeypatch.setattr(research_signal_snapshot, "urlopen", open_url)
  return requests


def _row(signal_id: str, effective_at: str = "2026-01-05T09:30:00+08:00") -> dict[str, object]:
  return {
    "signal_id": signal_id,
    "instrument": "000001.SZ",
    "timeframe": "1d",
    "signal_time": "2026-01-02T15:00:00+08:00",
    "available_at": "2026-01-02T15:00:00+08:00",
    "effective_at": effective_at,
    "sequence": 1,
    "predicted_label_id": "UP",
    "action": "long",
    "model_version_id": "model-1",
    "source_hash": "a" * 64,
    "diagnostic": {},
  }


def test_freeze_snapshot_canonicalizes_and_writes_exact_rows(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  requests = _install_transport(monkeypatch, [
    FakeResponse({
      "status": "published",
      "partitions": [{
        "partitionKey": "1d/000001.SZ/2026-01",
        "contentHash": "a" * 64,
        "rowCount": 2,
      }],
    }),
    FakeResponse([_row("b"), _row("a", "2026-01-06T09:30:00+08:00")]),
  ])

  snapshot = research_signal_snapshot.freeze_signal_snapshot(
    "dataset-1", "user-1", "000001.SZ", "2026-01-05", "2026-01-06", "1d", str(tmp_path),
  )

  assert snapshot["rowCount"] == 2
  lines = Path(snapshot["artifactFile"]).read_text(encoding="utf-8").splitlines()
  assert '"signalId":"b"' in lines[0]
  assert len(snapshot["snapshotHash"]) == 64
  assert snapshot["alignmentPlan"] == []

  aligned = research_signal_snapshot.align_frozen_signal_snapshot(
    snapshot, ["20260105093000", "20260106093000"],
  )

  assert [item["signalId"] for item in aligned["alignmentPlan"]] == ["b", "a"]
  assert aligned["alignment"]["alignedCount"] == 2
  assert aligned["alignment"]["unmatchedCount"] == 0
  assert requests[0].full_url.endswith("/dataset-1/manifest")
  assert requests[0].get_header("X-service-token") == "service-token"
  assert requests[0].get_header("X-user-id") == "user-1"


def test_freeze_snapshot_rejects_duplicate_event_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  _install_transport(monkeypatch, [
    FakeResponse({
      "status": "published",
      "partitions": [{
        "partitionKey": "1d/000001.SZ/2026-01",
        "contentHash": "a" * 64,
        "rowCount": 2,
      }],
    }),
    FakeResponse([_row("a"), _row("b")]),
  ])

  with pytest.raises(research_signal_snapshot.ResearchSignalSnapshotError, match="冲突"):
    research_signal_snapshot.freeze_signal_snapshot(
      "dataset-1", "user-1", "000001.SZ", "2026-01-05", "2026-01-05", "1d", str(tmp_path),
    )


def test_signal_rows_follow_ndjson_pagination_at_the_transport_boundary(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  requests = _install_transport(monkeypatch, [
    FakeResponse(
      json.dumps(_row("a")) + "\n",
      content_type="application/x-ndjson",
      next_cursor="2",
    ),
    FakeResponse(
      json.dumps(_row("b", "2026-01-06T09:30:00+08:00")) + "\n",
      content_type="application/x-ndjson",
    ),
  ])

  rows = research_signal_snapshot._request_signal_rows(
    "dataset-1", "user-1", "000001.SZ", "2026-01-05", "2026-01-06", "1d",
  )

  assert [row["signal_id"] for row in rows] == ["a", "b"]
  assert len(requests) == 2
  assert "cursor=2" in requests[1].full_url


def test_signal_transport_error_is_exposed_as_domain_error(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(research_signal_snapshot, "_SIGNAL_API_URL", "https://signals.test")
  monkeypatch.setattr(research_signal_snapshot, "_SERVICE_TOKEN", "service-token")
  monkeypatch.setattr(
    research_signal_snapshot,
    "urlopen",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
  )

  with pytest.raises(
    research_signal_snapshot.ResearchSignalSnapshotError,
    match="无法读取已发布研究信号",
  ):
    research_signal_snapshot.list_published_signal_datasets("user-1")


def test_alignment_uses_first_bar_at_or_after_effective_time() -> None:
  row = research_signal_snapshot._canonical_signal(_row("a", "2026-01-05T09:31:00+08:00"))

  plan = research_signal_snapshot._build_alignment_plan(
    [row], ["20260105093000", "20260105093500"],
  )

  assert plan == [{
    "signalId": "a",
    "action": "long",
    "effectiveAt": "2026-01-05T09:31:00.000000+08:00",
    "executionBarTime": "20260105093500",
  }]


def test_alignment_keeps_last_ordered_signal_when_events_share_one_bar() -> None:
  rows = [
    research_signal_snapshot._canonical_signal(_row("first", "2026-01-05T09:31:00+08:00")),
    research_signal_snapshot._canonical_signal({
      **_row("last", "2026-01-05T09:31:00+08:00"),
      "sequence": 2,
      "action": "flat",
    }),
  ]

  plan = research_signal_snapshot._build_alignment_plan(rows, ["20260105093000", "20260105093500"])

  assert plan == [{
    "signalId": "last",
    "action": "flat",
    "effectiveAt": "2026-01-05T09:31:00.000000+08:00",
    "executionBarTime": "20260105093500",
  }]


def test_rejects_signal_timeframe_finer_than_backtest() -> None:
  with pytest.raises(research_signal_snapshot.ResearchSignalSnapshotError, match="不能细于"):
    research_signal_snapshot._validate_timeframe_alignment("1m", "5m")
