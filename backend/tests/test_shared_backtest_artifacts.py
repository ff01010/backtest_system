from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from backend import app as backend_app
from backend.services import (
  backtest_runner,
  shared_backtest_artifacts,
  state_store,
)


def _signal_row() -> dict[str, Any]:
  return {
    "signalId": "signal-1",
    "instrument": "688981.SH",
    "timeframe": "5m",
    "signalTime": "2026-01-02T09:30:00+08:00",
    "availableAt": "2026-01-02T09:30:00+08:00",
    "effectiveAt": "2026-01-02T09:30:00+08:00",
    "sequence": 1,
    "predictedLabelId": "UP",
    "action": "long",
    "modelVersionId": "model-1",
    "sourceHash": "a" * 64,
    "diagnostic": {},
  }


def _source(root: Path, name: str = "signals.ndjson") -> tuple[Path, dict[str, Any]]:
  path = root / "research_signals" / name
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(_signal_row(), ensure_ascii=False) + "\n", encoding="utf-8")
  digest = hashlib.sha256(path.read_bytes()).hexdigest()
  return path, {
    "storage_key": f"research_signals/{name}",
    "original_filename": name,
    "artifact_type": "research_signal",
    "content_type": "application/x-ndjson",
    "byte_size": path.stat().st_size,
    "content_sha256": digest,
  }


def test_publication_artifact_survives_source_deletion(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  monkeypatch.setenv("TREND_GRID_OUTPUT_ROOT", str(tmp_path))
  source_path, source = _source(tmp_path)

  copied = shared_backtest_artifacts.copy_artifacts(
    [source], "shared_backtests/publication-1",
  )
  source_path.unlink()

  target = shared_backtest_artifacts.verified_artifact_path(
    copied[0]["storage_key"], copied[0]["content_sha256"], copied[0]["byte_size"],
  )
  assert target.is_file()
  assert target.read_text(encoding="utf-8").strip()


def test_copy_digest_failure_cleans_prior_files(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  monkeypatch.setenv("TREND_GRID_OUTPUT_ROOT", str(tmp_path))
  _, first = _source(tmp_path, "first.ndjson")
  _, second = _source(tmp_path, "second.ndjson")
  second["content_sha256"] = "0" * 64

  with pytest.raises(shared_backtest_artifacts.SharedArtifactError, match="摘要"):
    shared_backtest_artifacts.copy_artifacts(
      [first, second], "shared_backtests/publication-1",
    )

  shared_dir = tmp_path / "shared_backtests"
  assert not shared_dir.exists() or not list(shared_dir.rglob("*.ndjson"))


def test_shared_metadata_exposes_download_but_not_storage_key() -> None:
  metadata = state_store._shared_artifact_metadata({
    "id": "11111111-1111-4111-8111-111111111111",
    "publication_id": "22222222-2222-4222-8222-222222222222",
    "artifact_type": "research_signal",
    "display_name": "signals.ndjson",
    "content_type": "application/x-ndjson",
    "byte_size": 42,
    "content_sha256": "a" * 64,
    "storage_key": "private/path/signals.ndjson",
    "snapshot_schema_version": 1,
    "reproduction_metadata": {},
  }, include_download=True)

  assert metadata["downloadUrl"].endswith(
    "/artifacts/11111111-1111-4111-8111-111111111111"
  )
  assert "storageKey" not in metadata
  assert "storage_key" not in metadata


def test_publication_rejects_missing_research_signal_artifact() -> None:
  class EmptyCursor:
    def execute(self, _query: str, _params: object) -> None:
      return None

    def fetchall(self) -> list[dict[str, Any]]:
      return []

  snapshot = {
    "data": {
      "dependencies": [{
        "type": "research_signal",
        "summary": {"snapshotHash": "a" * 64},
      }],
    },
  }

  with pytest.raises(ValueError, match="研究信号快照不完整"):
    state_store._shared_artifact_sources(EmptyCursor(), "run-1", snapshot)


def test_imported_dataset_uses_normal_attachment_preparation_after_withdrawal(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  monkeypatch.setenv("TREND_GRID_OUTPUT_ROOT", str(tmp_path))
  _, source = _source(tmp_path)
  imported = shared_backtest_artifacts.copy_artifacts(
    [source], "shared_imports/user-2/publication-1",
  )[0]
  Path(tmp_path / source["storage_key"]).unlink()
  dataset_id = "33333333-3333-4333-8333-333333333333"
  dataset = {
    "id": dataset_id,
    "timeframe": "5m",
    "manifest": {"version": 1, "timeframe": "5m"},
    "storage_key": imported["storage_key"],
    "content_sha256": imported["content_sha256"],
    "byte_size": imported["byte_size"],
  }
  monkeypatch.setattr(
    backend_app.state_store,
    "get_imported_research_signal_dataset",
    lambda requested_id: dataset if requested_id == dataset_id else None,
  )
  monkeypatch.setattr(
    backend_app.research_signal_snapshot,
    "freeze_signal_snapshot",
    lambda *_: (_ for _ in ()).throw(AssertionError("不应访问原作者研究信号服务")),
  )
  bars = pd.DataFrame([{"time": "20260102093000"}])
  monkeypatch.setattr(
    backend_app.backtest_runner,
    "_load_user_script_bars",
    lambda _request: (bars, 0, {}),
  )
  request = {
    "signalDatasetId": dataset_id,
    "symbol": "688981.SH",
    "startDate": "2026-01-02",
    "endDate": "2026-01-02",
    "timeframe": "5m",
    "savedConfigSnapshot": {},
  }

  backend_app._prepare_research_signal_attachment(request)

  snapshot = request["researchSignalSnapshot"]
  assert snapshot["datasetId"] == dataset_id
  signals = backtest_runner._research_signal_by_bar_time(
    snapshot, ["20260102093000"],
  )
  assert signals["20260102093000"]["action"] == "long"


def test_artifact_migration_enforces_independent_ownership_and_rls() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "033_shared_backtest_publication_artifacts.sql"
  ).read_text(encoding="utf-8")

  assert "CREATE TABLE IF NOT EXISTS backtest_publication_artifact" in migration
  assert "CREATE TABLE IF NOT EXISTS imported_research_signal_dataset" in migration
  assert "CREATE TABLE IF NOT EXISTS strategy_config_artifact" in migration
  assert migration.count("FORCE ROW LEVEL SECURITY") == 3
  assert "backtest_publication_artifact_read" in migration
  assert "owner_user_id=app.current_user_id()" in migration
  assert "REVOKE UPDATE, DELETE ON backtest_publication_artifact" in migration
