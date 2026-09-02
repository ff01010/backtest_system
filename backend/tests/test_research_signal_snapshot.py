from pathlib import Path

import pytest

from backend.services import research_signal_snapshot


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
  monkeypatch.setattr(research_signal_snapshot, "_request_json", lambda path, _: {"status": "published", "partitions": [{"partitionKey": "1d/000001.SZ/2026-01", "contentHash": "a" * 64, "rowCount": 2}]} if path.endswith("manifest") else [_row("b"), _row("a", "2026-01-06T09:30:00+08:00")])

  snapshot = research_signal_snapshot.freeze_signal_snapshot(
    "dataset-1", "user-1", "000001.SZ", "2026-01-05", "2026-01-06", "1d", str(tmp_path),
  )

  assert snapshot["rowCount"] == 2
  lines = Path(snapshot["artifactFile"]).read_text(encoding="utf-8").splitlines()
  assert '"signalId":"b"' in lines[0]
  assert len(snapshot["snapshotHash"]) == 64


def test_freeze_snapshot_rejects_duplicate_event_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
  monkeypatch.setattr(research_signal_snapshot, "_request_json", lambda path, _: {"status": "published", "partitions": [{"partitionKey": "1d/000001.SZ/2026-01", "contentHash": "a" * 64, "rowCount": 2}]} if path.endswith("manifest") else [_row("a"), _row("b")])

  with pytest.raises(research_signal_snapshot.ResearchSignalSnapshotError, match="冲突"):
    research_signal_snapshot.freeze_signal_snapshot(
      "dataset-1", "user-1", "000001.SZ", "2026-01-05", "2026-01-05", "1d", str(tmp_path),
    )
