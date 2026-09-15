from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from backend import app as backend_app
from backend.services import shared_backtest_copy, state_store


def publication_row() -> dict[str, Any]:
  return {
    "id": "11111111-1111-4111-8111-111111111111",
    "title": "均线策略",
    "author_name_snapshot": "原作者",
    "status": "active",
    "snapshot_schema_version": 1,
    "snapshot_json": {
      "strategy": {
        "description": "共享策略说明",
        "code": "def on_bar(bar, state):\n  return []",
        "entryType": "on_bar",
        "signalRequired": False,
      },
      "configuration": {
        "strategy": "user_script",
        "symbol": "688981.SH",
        "timeframe": "5m",
        "benchmarkSymbol": "000300.SH",
        "startDate": "2026-01-01",
        "endDate": "2026-01-31",
        "initialCapital": 1_000_000,
        "commissionRate": 0.0003,
        "slippage": 0.0001,
        "strategyCategory": "directional_holding",
        "executionAlgorithm": "twap",
        "executionParameters": {
          "durationSeconds": 300,
          "intervalSeconds": 30,
          "minChildQuantity": 100,
        },
        "params": {"window": 20},
      },
    },
  }


def copied_config() -> dict[str, Any]:
  return {
    "id": "22222222-2222-4222-8222-222222222222",
    "scriptId": "33333333-3333-4333-8333-333333333333",
    "name": "均线策略-副本",
    "accountId": None,
    "strategyVersionId": "44444444-4444-4444-8444-444444444444",
    "sharedSource": {
      "publicationId": "11111111-1111-4111-8111-111111111111",
      "authorName": "原作者",
      "title": "均线策略",
    },
  }


class CopyCursor:
  def __init__(
    self,
    existing: bool = False,
    publication: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
  ) -> None:
    self.existing = existing
    self.publication = publication or publication_row()
    self.artifacts = artifacts or []
    self.query = ""
    self.statements: list[str] = []

  def __enter__(self) -> "CopyCursor":
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def execute(self, query: str, _params: Any = None) -> None:
    self.query = " ".join(query.split())
    self.statements.append(self.query)

  def fetchone(self) -> dict[str, Any] | None:
    if "FROM backtest_publication WHERE" in self.query:
      return self.publication
    if "FROM backtest_publication_copy" in self.query:
      return {
        "strategy_config_id": "22222222-2222-4222-8222-222222222222",
      } if self.existing else None
    if "record_backtest_publication_copy" in self.query:
      return {"created": True}
    raise AssertionError(f"unexpected fetchone: {self.query}")

  def fetchall(self) -> list[dict[str, Any]]:
    if "FROM backtest_publication_artifact" in self.query:
      return self.artifacts
    raise AssertionError(f"unexpected fetchall: {self.query}")


class CopyConnection:
  def __init__(self, cursor: CopyCursor) -> None:
    self.cursor_value = cursor
    self.rolled_back = False

  def cursor(self) -> CopyCursor:
    return self.cursor_value


@contextmanager
def copy_db(connection: CopyConnection) -> Iterator[CopyConnection]:
  try:
    yield connection
  except Exception:
    connection.rolled_back = True
    raise


def install_created_copy_mocks(monkeypatch: pytest.MonkeyPatch, cursor: CopyCursor) -> dict[str, Any]:
  captured: dict[str, Any] = {}
  connection = CopyConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: copy_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-2")
  monkeypatch.setattr(state_store, "_resolve_unique_strategy_name", lambda *_: "均线策略-副本")

  def write_script(_cur: Any, script_id: str | None, payload: dict[str, Any]) -> tuple[str, str, int]:
    captured["scriptIdInput"] = script_id
    captured["script"] = payload
    return "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444", 1

  def write_config(_cur: Any, config_id: str | None, payload: dict[str, Any], *_args: Any) -> str:
    captured["configIdInput"] = config_id
    captured["config"] = payload
    return "22222222-2222-4222-8222-222222222222"

  monkeypatch.setattr(state_store, "_write_strategy_script", write_script)
  monkeypatch.setattr(state_store, "_write_config", write_config)
  monkeypatch.setattr(state_store, "_get_strategy_config_with_cursor", lambda *_: copied_config())
  monkeypatch.setattr(state_store, "_publish_strategy_config_with_cursor", lambda *_: ("version", True))
  captured["connection"] = connection
  return captured


def test_copy_creates_independent_strategy_and_clears_account(monkeypatch: pytest.MonkeyPatch) -> None:
  cursor = CopyCursor()
  captured = install_created_copy_mocks(monkeypatch, cursor)

  response = shared_backtest_copy.copy_shared_backtest(publication_row()["id"])

  assert response["created"] is True
  assert response["accountCleared"] is True
  assert captured["scriptIdInput"] is None
  assert captured["configIdInput"] is None
  config = captured["config"]
  assert config["accountId"] is None
  assert config["benchmarkSymbol"] == "000300.SH"
  assert config["paramsJson"]["params"]["window"] == 20
  assert config["paramsJson"]["params"]["twapDurationSeconds"] == 300
  assert config["sourcePublicationId"] == publication_row()["id"]
  assert sum("record_backtest_publication_copy" in item for item in cursor.statements) == 1


def test_copy_name_keeps_suffix_with_long_shared_title() -> None:
  name = shared_backtest_copy._copy_name("长" * 128)

  assert len(name) == 64
  assert name.endswith("-副本")


def test_repeat_copy_returns_existing_without_increment(monkeypatch: pytest.MonkeyPatch) -> None:
  cursor = CopyCursor(existing=True)
  connection = CopyConnection(cursor)
  monkeypatch.setattr(state_store, "_db", lambda: copy_db(connection))
  monkeypatch.setattr(state_store, "current_user_id", lambda: "user-2")
  monkeypatch.setattr(state_store, "_get_strategy_config_with_cursor", lambda *_: copied_config())

  response = shared_backtest_copy.copy_shared_backtest(publication_row()["id"])

  assert response["created"] is False
  assert any("FOR UPDATE" in item for item in cursor.statements)
  assert not any("record_backtest_publication_copy" in item for item in cursor.statements)
  assert not any("copy_count" in item for item in cursor.statements)


def test_copy_failure_rolls_back_without_increment(monkeypatch: pytest.MonkeyPatch) -> None:
  cursor = CopyCursor()
  captured = install_created_copy_mocks(monkeypatch, cursor)
  monkeypatch.setattr(
    state_store,
    "_write_config",
    lambda *_: (_ for _ in ()).throw(state_store.DatabaseError("write failed")),
  )

  with pytest.raises(state_store.DatabaseError, match="write failed"):
    shared_backtest_copy.copy_shared_backtest(publication_row()["id"])

  assert captured["connection"].rolled_back is True
  assert not any("copy_count" in item for item in cursor.statements)


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


def _research_publication_artifact(root: Path) -> dict[str, Any]:
  source = root / "shared_backtests" / "publication" / "signals.ndjson"
  source.parent.mkdir(parents=True)
  source.write_text(json.dumps(_signal_row(), ensure_ascii=False) + "\n", encoding="utf-8")
  digest = hashlib.sha256(source.read_bytes()).hexdigest()
  snapshot_hash = hashlib.sha256(
    json.dumps(_signal_row(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
  ).hexdigest()
  return {
    "id": "55555555-5555-4555-8555-555555555555",
    "publication_id": publication_row()["id"],
    "artifact_type": "research_signal",
    "display_name": "signals.ndjson",
    "content_type": "application/x-ndjson",
    "byte_size": source.stat().st_size,
    "content_sha256": digest,
    "storage_key": "shared_backtests/publication/signals.ndjson",
    "snapshot_schema_version": 1,
    "reproduction_metadata": {
      "datasetVersion": 3,
      "snapshotHash": snapshot_hash,
      "sourceTimeframe": "5m",
      "rowCount": 1,
      "manifest": {"version": 3, "timeframe": "5m"},
      "sourcePartitions": [],
    },
  }


def test_copy_imports_research_signal_with_new_dataset_identity(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  monkeypatch.setenv("TREND_GRID_OUTPUT_ROOT", str(tmp_path))
  publication = publication_row()
  publication["snapshot_json"]["strategy"]["signalRequired"] = True
  artifact = _research_publication_artifact(tmp_path)
  cursor = CopyCursor(publication=publication, artifacts=[artifact])
  captured = install_created_copy_mocks(monkeypatch, cursor)

  response = shared_backtest_copy.copy_shared_backtest(publication["id"])

  imported_dataset_id = captured["config"]["paramsJson"]["signalDatasetId"]
  assert response["created"] is True
  assert imported_dataset_id != artifact["reproduction_metadata"].get("datasetId")
  assert any("INSERT INTO imported_research_signal_dataset" in item for item in cursor.statements)
  assert any("INSERT INTO strategy_config_artifact" in item for item in cursor.statements)
  imported_files = list((tmp_path / "shared_imports").rglob("*.ndjson"))
  assert len(imported_files) == 1
  Path(tmp_path / artifact["storage_key"]).unlink()
  assert imported_files[0].is_file()


def test_research_import_failure_cleans_file_and_does_not_increment(
  monkeypatch: pytest.MonkeyPatch,
  tmp_path: Path,
) -> None:
  monkeypatch.setenv("TREND_GRID_OUTPUT_ROOT", str(tmp_path))
  publication = publication_row()
  publication["snapshot_json"]["strategy"]["signalRequired"] = True
  artifact = _research_publication_artifact(tmp_path)
  cursor = CopyCursor(publication=publication, artifacts=[artifact])
  install_created_copy_mocks(monkeypatch, cursor)
  monkeypatch.setattr(
    state_store,
    "_write_config",
    lambda *_: (_ for _ in ()).throw(state_store.DatabaseError("write failed")),
  )

  with pytest.raises(state_store.DatabaseError, match="write failed"):
    shared_backtest_copy.copy_shared_backtest(publication["id"])

  assert not (tmp_path / "shared_imports").exists() or not list(
    (tmp_path / "shared_imports").rglob("*.ndjson")
  )
  assert not any("record_backtest_publication_copy" in item for item in cursor.statements)


def test_copy_endpoint_maps_unavailable_publication(monkeypatch: pytest.MonkeyPatch) -> None:
  monkeypatch.setattr(
    backend_app.shared_backtest_copy,
    "copy_shared_backtest",
    lambda *_: (_ for _ in ()).throw(KeyError("missing")),
  )

  with pytest.raises(HTTPException) as error:
    backend_app.copy_shared_backtest("missing")

  assert error.value.status_code == 404


def test_copied_strategy_source_is_frozen_into_backtest_snapshot(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  source = copied_config()["sharedSource"]
  config = {
    **copied_config(),
    "symbol": "688981.SH",
    "timeframe": "5m",
    "benchmarkSymbol": "000300.SH",
    "strategy": "user_script",
    "mode": "backtest",
    "paramsJson": {"params": {}},
  }
  script = {
    "id": config["scriptId"],
    "versionId": config["strategyVersionId"],
    "name": config["name"],
    "description": "说明",
    "code": "def on_bar(bar, state):\n  return []",
    "entryType": "on_bar",
    "signalRequired": False,
  }
  monkeypatch.setattr(backend_app.state_store, "get_strategy_config", lambda *_: config)
  monkeypatch.setattr(backend_app.state_store, "get_strategy_script_version", lambda *_: script)
  request = {
    "configId": config["id"],
    "accountId": None,
    "symbol": config["symbol"],
    "timeframe": config["timeframe"],
    "benchmarkSymbol": config["benchmarkSymbol"],
    "strategy": config["strategy"],
    "strategyParams": {},
    "executionAlgorithm": "immediate",
    "executionParameters": {},
    "fillModel": "bar_next_open",
  }

  backend_app._prepare_saved_strategy_snapshot(request)

  assert request["savedConfigSnapshot"]["sharedSource"] == source


def test_copy_migration_enforces_idempotency_and_source_immutability() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "032_shared_backtest_copies.sql"
  ).read_text(encoding="utf-8")

  assert "UNIQUE (publication_id, recipient_user_id)" in migration
  assert "UNIQUE (strategy_config_id)" in migration
  assert "FORCE ROW LEVEL SECURITY" in migration
  assert "prevent_strategy_config_shared_source_update" in migration
  assert "prevent_backtest_publication_source_update" in migration
  assert "record_backtest_publication_copy" in migration
  assert "REVOKE INSERT, UPDATE, DELETE ON backtest_publication_copy" in migration
