from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi import HTTPException

from backend import app as backend_app
from backend.services import state_store


def complete_run() -> dict[str, Any]:
  return {
    "id": "run-1",
    "status": "ok",
    "symbol": "688981.SH",
    "timeframe": "5m",
    "benchmarkSymbol": "000300.SH",
    "strategy": "user_script",
    "strategyCategory": "directional_holding",
    "startDate": "2026-01-01",
    "endDate": "2026-01-31",
    "scriptSnapshot": {
      "name": "均线策略",
      "description": "示例",
      "code": "def on_bar(bar, state):\n  return []",
      "entryType": "on_bar",
      "codeHash": "a" * 64,
      "signalRequired": False,
      "runtimeVersion": "1",
    },
    "paramsSnapshot": {
      "name": "均线方案",
      "strategy": "user_script",
      "symbol": "688981.SH",
      "timeframe": "5m",
      "accountId": 42,
      "params": {"window": 20},
      "runParams": {"window": 20},
      "execution": {"algorithm": "immediate"},
      "researchSignal": {
        "snapshotHash": "b" * 64,
        "artifactFile": "C:/private/signals.ndjson",
      },
    },
    "paramsJson": {
      "accountId": 42,
      "strategyCategory": "directional_holding",
      "symbol": "688981.SH",
      "timeframe": "5m",
      "startDate": "2026-01-01",
      "endDate": "2026-01-31",
      "initialCapital": 1_000_000,
      "commissionRate": 0.0003,
      "slippage": 0.0001,
      "fillModel": "bar_next_open",
      "executionAlgorithm": "immediate",
      "strategyParams": {"window": 20},
      "requestKey": "private-request",
    },
    "dataVersion": {"barSnapshot": {
      "provider": "qmt_kline", "hash": "c" * 64,
      "sourcePath": "C:/private/bars.parquet",
    }},
    "resultJson": {
      "runId": "run-1",
      "outputDir": "C:/private/output",
      "resultSchemaVersion": 3,
      "summary": {
        "totalReturn": 0.12,
        "maxDrawdownRate": 0.04,
        "annualSharpe": 1.5,
      },
      "equity": [{"date": "2026-01-02", "value": 1_010_000, "pnl": 10_000}],
      "dailyHoldings": [],
      "trades": [{"time": "2026-01-02", "price": 10, "accountId": 42}],
      "skippedSignals": [],
      "experimentManifest": {
        "resultSchemaVersion": 3,
        "attemptToken": "private-attempt",
      },
      "dataSnapshot": {"provider": "qmt_kline", "hash": "d" * 64},
      "warnings": [],
    },
  }


def publication_row(snapshot: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": "publication-1",
    "title": "均线示例",
    "description": "用于震荡行情",
    "author_name_snapshot": "测试作者",
    "tags": ["均线"],
    "symbol": "688981.SH",
    "timeframe": "5m",
    "strategy_category": "directional_holding",
    "start_date": "2026-01-01",
    "end_date": "2026-01-31",
    "total_return": 0.12,
    "max_drawdown_rate": 0.04,
    "annual_sharpe": 1.5,
    "copy_count": 0,
    "first_published_at": datetime(2026, 9, 15, tzinfo=timezone.utc),
    "snapshot_schema_version": 1,
    "snapshot_digest": state_store._shared_snapshot_digest(snapshot),
    "snapshot_json": snapshot,
  }


def test_snapshot_uses_business_whitelist_and_removes_system_fields() -> None:
  snapshot = state_store._build_shared_backtest_snapshot(complete_run())
  serialized = str(snapshot)

  assert snapshot["strategy"]["code"].startswith("def on_bar")
  assert snapshot["configuration"]["initialCapital"] == 1_000_000
  assert snapshot["result"]["summary"]["totalReturn"] == 0.12
  assert snapshot["data"]["dependencies"][0]["summary"]["snapshotHash"] == "b" * 64
  for forbidden in (
    "accountId", "outputDir", "sourcePath", "artifactFile", "requestKey", "attemptToken",
  ):
    assert forbidden not in serialized


def test_snapshot_nested_contract_does_not_leak_system_diagnostics() -> None:
  run = complete_run()
  run["paramsJson"]["strategyParams"] = {
    "window": 20,
    "nested": {"loginName": "secret", "sessionId": "secret", "kept": 1},
  }
  run["resultJson"]["summary"]["diagnostic"] = {"rawException": "secret"}
  run["resultJson"]["experimentManifest"]["internalServiceUrl"] = "http://private"
  run["resultJson"]["trades"][0]["attributionSegments"] = [{
    "quantity": 1, "path": "C:/private", "rawException": "secret",
  }]

  snapshot = state_store._build_shared_backtest_snapshot(run)

  assert snapshot["configuration"]["strategyParams"]["nested"] == {"kept": 1}
  assert snapshot["result"]["trades"][0]["attributionSegments"] == [{"quantity": 1}]
  serialized = str(snapshot)
  for forbidden in ("loginName", "sessionId", "diagnostic", "internalServiceUrl", "rawException", "C:/private"):
    assert forbidden not in serialized


def test_derived_snapshot_keeps_immutable_shared_origin() -> None:
  run = complete_run()
  run["paramsSnapshot"]["sharedSource"] = {
    "publicationId": "publication-1",
    "authorName": "原作者",
    "title": "原共享策略",
  }

  snapshot = state_store._build_shared_backtest_snapshot(run)

  assert snapshot["origin"] == run["paramsSnapshot"]["sharedSource"]


@pytest.mark.parametrize("field", ["scriptSnapshot", "paramsSnapshot", "dataVersion", "resultJson"])
def test_snapshot_rejects_incomplete_history(field: str) -> None:
  run = complete_run()
  run[field] = None

  with pytest.raises(ValueError, match="请重新运行该任务后共享"):
    state_store._build_shared_backtest_snapshot(run)


@pytest.mark.parametrize(
  ("container", "field"),
  [
    ("scriptSnapshot", "entryType"),
    ("scriptSnapshot", "codeHash"),
    ("paramsSnapshot", "strategy"),
    ("paramsSnapshot", "timeframe"),
    ("paramsJson", "strategyCategory"),
    ("resultJson", "resultSchemaVersion"),
  ],
)
def test_snapshot_rejects_legacy_run_without_explicit_contract_field(
  container: str,
  field: str,
) -> None:
  run = complete_run()
  del run[container][field]

  with pytest.raises(ValueError, match=rf"{container}\.{field}"):
    state_store._build_shared_backtest_snapshot(run)


@pytest.mark.parametrize(
  ("container", "field", "invalid_value"),
  [
    ("paramsSnapshot", "params", []),
    ("paramsJson", "strategyParams", []),
    ("resultJson", "equity", {}),
    ("resultJson", "experimentManifest", []),
    ("resultJson", "warnings", {}),
  ],
)
def test_snapshot_rejects_history_with_invalid_contract_shape(
  container: str,
  field: str,
  invalid_value: object,
) -> None:
  run = complete_run()
  run[container][field] = invalid_value

  with pytest.raises(ValueError, match=rf"{container}\.{field}"):
    state_store._build_shared_backtest_snapshot(run)


def test_snapshot_rejects_non_successful_run() -> None:
  run = complete_run()
  run["status"] = "error"

  with pytest.raises(ValueError, match="只有成功完成"):
    state_store._build_shared_backtest_snapshot(run)


def test_detail_is_projected_instead_of_returning_snapshot_json() -> None:
  snapshot = state_store._build_shared_backtest_snapshot(complete_run())
  detail = state_store._publication_detail(publication_row(snapshot))

  assert detail["riskNotice"] == "历史回测结果不代表未来表现"
  assert detail["strategy"] == snapshot["strategy"]
  assert "snapshot_json" not in detail
  assert "owner_user_id" not in detail


def test_publish_endpoint_maps_missing_owner_run_to_not_found(monkeypatch: Any) -> None:
  def missing(*_args: object) -> tuple[dict[str, Any], bool]:
    raise KeyError("run-1")

  monkeypatch.setattr(backend_app.state_store, "create_shared_backtest_publication", missing)
  payload = backend_app.schemas.SharedBacktestPublicationCreate(
    title="共享策略",
    description="策略说明",
  )

  with pytest.raises(HTTPException) as error:
    backend_app.publish_backtest_run("run-1", payload)

  assert error.value.status_code == 404


def test_publish_endpoint_is_idempotent(monkeypatch: Any) -> None:
  snapshot = state_store._build_shared_backtest_snapshot(complete_run())
  publication = state_store._publication_detail(publication_row(snapshot))
  monkeypatch.setattr(
    backend_app.state_store,
    "create_shared_backtest_publication",
    lambda *_args: (publication, False),
  )
  payload = backend_app.schemas.SharedBacktestPublicationCreate(
    title="共享策略",
    description="策略说明",
  )

  response = backend_app.publish_backtest_run("run-1", payload)

  assert response == {"publication": publication, "created": False}


class ListCursor:
  def __init__(self, row: dict[str, Any]) -> None:
    self.row = row
    self.query = ""
    self.params: tuple[Any, ...] = ()

  def __enter__(self) -> "ListCursor":
    return self

  def __exit__(self, *_args: object) -> None:
    return None

  def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
    self.query = query
    self.params = params

  def fetchone(self) -> dict[str, Any]:
    return {"value": 1}

  def fetchall(self) -> list[dict[str, Any]]:
    return [self.row]


class ListConnection:
  def __init__(self, cursor: ListCursor) -> None:
    self.cursor_value = cursor

  def cursor(self) -> ListCursor:
    return self.cursor_value


@contextmanager
def list_db(connection: ListConnection) -> Iterator[ListConnection]:
  yield connection


def test_list_explicitly_filters_active_publications(monkeypatch: Any) -> None:
  snapshot = state_store._build_shared_backtest_snapshot(complete_run())
  cursor = ListCursor(publication_row(snapshot))
  monkeypatch.setattr(state_store, "_db", lambda: list_db(ListConnection(cursor)))

  page = state_store.list_shared_backtests(limit=10, offset=0)

  assert page["total"] == 1
  assert page["items"][0]["title"] == "均线示例"
  assert "WHERE status='active'" in cursor.query
  assert cursor.params == (10, 0)


def test_migration_enforces_shared_boundary() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "030_shared_backtest_publications.sql"
  ).read_text(encoding="utf-8")

  assert "ON DELETE SET NULL" in migration
  assert "ux_backtest_publication_source_run" in migration
  assert "ENABLE ROW LEVEL SECURITY" in migration
  assert "FORCE ROW LEVEL SECURITY" in migration
  assert "backtest_publication_read" in migration
  assert "backtest_publication_create" in migration
  assert "prevent_backtest_publication_snapshot_update" in migration
  assert "REVOKE UPDATE, DELETE ON backtest_publication" in migration
  assert "GRANT SELECT, INSERT ON backtest_publication" in migration
  assert "GRANT SELECT, INSERT, UPDATE" not in migration
