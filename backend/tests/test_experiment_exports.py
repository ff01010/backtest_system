from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend import app as app_module
from backend import settings
from backend.experiments.contracts import EvaluationExportCreateRequest
from backend.experiments.export_repository import ClaimedExport
from backend.experiments.export_worker import (
  EvaluationExportLease,
  EvaluationExportWorker,
)
from backend.experiments.exports import (
  column_labels,
  iter_csv_chunks,
  safe_spreadsheet_value,
  write_report,
  write_xlsx,
)


ROOT = Path(__file__).resolve().parents[2]


def _output(row_count: int = 2) -> dict[str, object]:
  rows = [{
    "trialId": f"trial-{index}",
    "runId": f"run-{index}",
    "stageIndex": 0,
    "trialIndex": index,
    "values": {
      "label": "=HYPERLINK(\"https://attacker.invalid\")" if index == 0 else "normal",
      "totalReturn": f"0.{index + 1}",
    },
    "unsupportedReasons": {},
  } for index in range(row_count)]
  return {
    "schemaVersion": "advanced_evaluation_output_v1",
    "pipelineVersion": "advanced_evaluation_pipeline_v1",
    "statisticsVersion": "evaluation_statistics_v1",
    "resultSetHash": "b" * 64,
    "isPartial": True,
    "columns": ["label", "totalReturn"],
    "rows": rows,
    "visualizations": [{
      "type": "time_stability_v1",
      "metricId": "totalReturn",
      "periods": [{"dateScenarioId": "development-1", "mean": "0.10"}],
    }],
    "completeness": {
      "plannedTrialCount": row_count + 2,
      "succeededCount": row_count,
      "failedCount": 1,
      "cancelledCount": 1,
      "missingCount": 0,
      "completionRate": "0.5000",
      "exceptions": [{
        "stageId": "stage-1",
        "trialIndex": row_count,
        "status": "failed",
        "materializationStatus": "materialized",
        "error": {"code": "strategy.failed", "message": "私有错误原文"},
      }],
    },
  }


def _source(output: dict[str, object]) -> dict[str, object]:
  return {
    "output": output,
    "evaluationSpec": {
      "columns": ["label", "totalReturn"],
      "derivedMetrics": [],
      "filters": [],
      "groupBy": [],
      "aggregates": [],
      "sort": [{"metricId": "totalReturn", "direction": "desc"}],
      "visualizations": [],
    },
    "catalogSnapshot": {
      "metricCatalog": {"metrics": [{
        "metricId": "totalReturn", "displayName": "总收益率",
        "dataType": "decimal", "unit": "ratio", "direction": "higher",
        "grain": "trial_execution", "windowSemantics": "frozen_date_scenario",
      }]},
      "dimensionCatalog": {"dimensions": [{
        "dimensionId": "label", "displayName": "候选标签",
        "dataType": "string", "unit": None,
      }]},
    },
    "experiment": {
      "subject": {
        "subjectType": "legacy_single_strategy",
        "subjectId": "strategy-1",
        "subjectVersion": "version-1",
      },
      "templateType": "legacy_single_strategy_v1",
      "runPath": "strategy",
      "scopeType": "single",
      "fixedStructure": {
        "symbol": "688981.SH", "timeframe": "5m", "fillModel": "bar_next_open",
      },
      "searchSpace": [{
        "stageType": "finite_grid_v1",
        "generatorVersion": "1",
        "plannedTrialCount": 4,
        "parameterGrid": [{"inputId": "lookback", "values": [10, 20]}],
      }],
      "datasetSnapshots": [{
        "provider": "qmt_kline",
        "datasetFreezeLevel": "query_definition_frozen",
        "contentDigest": "f" * 64,
      }],
    },
    "assetVersions": [{
      "assetType": "strategy_script_version",
      "assetId": "script-1",
      "exactVersion": "v1",
      "contentDigest": "a" * 64,
    }],
    "evidence": {
      "evaluationName": "稳健性评价",
      "evaluationRunId": "run-1",
      "evaluationSpecId": "spec-1",
      "evaluationSpecVersion": 1,
      "outputHash": "c" * 64,
      "resultSetHash": "b" * 64,
      "pipelineVersion": "advanced_evaluation_pipeline_v1",
      "metricCatalogVersion": "metric_catalog_v1",
      "dimensionCatalogVersion": "dimension_catalog_v1",
      "runTemplateDigest": "d" * 64,
      "datasetSnapshotDigest": "e" * 64,
    },
  }


def test_spreadsheet_formula_prefixes_are_escaped() -> None:
  for prefix in ("=", "+", "-", "@", "  ="):
    escaped = safe_spreadsheet_value(f"{prefix}attack")
    assert isinstance(escaped, str)
    assert escaped.startswith("'")
  assert safe_spreadsheet_value("normal") == "normal"


def test_csv_is_lazy_and_formula_safe_for_large_output() -> None:
  output = _output(20_000)
  chunks = iter_csv_chunks(
    output,
    ["label", "totalReturn"],
    {"label": "候选标签", "totalReturn": "总收益率"},
  )
  first = next(chunks)
  second = next(chunks)
  third = next(chunks)

  assert first == "\ufeff".encode("utf-8")
  assert "候选标签" in second.decode("utf-8")
  assert "'=HYPERLINK" in third.decode("utf-8")
  assert len(third) < 1024
  assert sum(1 for _ in chunks) == 19_999


def test_xlsx_uses_inline_strings_without_formula_nodes(tmp_path: Path) -> None:
  target = tmp_path / "evaluation.xlsx"
  output = _output()
  labels = column_labels(_source(output)["catalogSnapshot"])  # type: ignore[arg-type]

  write_xlsx(target, output, ["label", "totalReturn"], labels)

  with ZipFile(target) as archive:
    worksheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
  assert "<f>" not in worksheet
  assert "&#x27;=HYPERLINK" in worksheet
  assert "评价结果" in ZipFile(target).read("xl/workbook.xml").decode("utf-8")


def test_report_lists_partial_failures_and_version_evidence_without_secrets(
  tmp_path: Path,
) -> None:
  target = tmp_path / "report.html"
  output = _output()
  source = _source(output)
  source["strategyCode"] = "raise RuntimeError('secret')"
  source["privatePath"] = "C:/private/token.txt"
  source["experiment"]["searchSpace"][0]["parameterGrid"].append({  # type: ignore[index,union-attr]
    "inputId": "logPath",
    "values": ["C:/private/token.txt"],
  })
  source["evaluationSpec"]["filters"] = [{  # type: ignore[index]
    "metricId": "label",
    "operator": "equals",
    "value": "postgresql://user:password@database/private",
  }]
  output["rows"][1]["values"]["label"] = "/srv/private/result.csv"  # type: ignore[index]
  labels = column_labels(source["catalogSnapshot"])  # type: ignore[arg-type]

  write_report(target, source, ["label", "totalReturn"], labels)
  report = target.read_text(encoding="utf-8")

  assert "不构成系统投资建议" in report
  assert "provisional（部分评价）" in report
  assert "strategy.failed" in report
  assert "评价规则与指标口径" in report
  assert "windowSemantics" in report
  assert "finite_grid_v1" in report
  assert "query_definition_frozen" in report
  assert "time_stability_v1" in report
  assert "/?workspace=backtest&amp;runId=run-0" in report
  assert "版本证据" in report
  assert "raise RuntimeError" not in report
  assert "C:/private" not in report
  assert "/srv/private" not in report
  assert "postgresql://" not in report
  assert "[REDACTED]" in report
  assert "私有错误原文" not in report


def test_export_migration_has_rls_lease_ttl_and_owner_scoped_relations() -> None:
  migration = (
    ROOT / "db" / "migrations" / "051_experiment_evaluation_exports.sql"
  ).read_text(encoding="utf-8")

  assert "CREATE TABLE IF NOT EXISTS backtest_experiment_evaluation_export" in migration
  assert "output_hash char(64) NOT NULL" in migration
  assert "content_sha256 char(64)" in migration
  assert "lease_expires_at timestamptz" in migration
  assert "expires_at timestamptz" in migration
  assert "backtest_evaluation_export_run_owner_fk" in migration
  assert "ENABLE ROW LEVEL SECURITY" in migration
  assert "backtest_evaluation_export_owner" in migration
  assert "ux_backtest_evaluation_export_owner_active" in migration


def test_create_export_route_forwards_frozen_request_and_idempotency(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  payload = EvaluationExportCreateRequest(
    format="xlsx", columns=["totalReturn"], locale="zh-CN",
  )

  class FakeApplication:
    def create(
      self,
      run_id: str,
      request_payload: EvaluationExportCreateRequest,
      key: str,
      request_id: str,
    ) -> tuple[dict[str, Any], int]:
      assert run_id == "60000000-0000-4000-8000-000000000001"
      assert request_payload == payload
      assert key == "create-export-1"
      assert request_id == "req-export"
      return {"exportId": "export-1", "status": "queued"}, 202

  monkeypatch.setattr(app_module, "_evaluation_export_application", FakeApplication())
  request = Request({
    "type": "http",
    "method": "POST",
    "path": "/api/backtest/evaluation-runs/test/exports",
    "headers": [(b"idempotency-key", b"create-export-1")],
  })
  request.state.request_id = "req-export"

  response = app_module.create_evaluation_export(
    UUID("60000000-0000-4000-8000-000000000001"), payload, request,
  )

  assert response.status_code == 202
  assert json.loads(bytes(response.body))["status"] == "queued"


def test_expired_download_returns_410_without_exposing_another_state(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class FakeApplication:
    def download_source(self, _export_id: str) -> dict[str, Any]:
      return {"status": "expired"}

  monkeypatch.setattr(app_module, "_evaluation_export_application", FakeApplication())

  with pytest.raises(HTTPException) as caught:
    app_module.download_evaluation_export(
      UUID("60000000-0000-4000-8000-000000000001"),
    )

  assert caught.value.status_code == 410
  assert caught.value.detail["code"] == "experiment.export_expired"


class _WorkerRepository:
  def __init__(self, source: dict[str, Any]) -> None:
    self.source = source
    self.completed: dict[str, Any] | None = None
    self.failed: list[tuple[str, bool]] = []
    self.health: list[str] = []
    self.health_event = Event()
    self.claimed = False
    self.expired_records: list[dict[str, Any]] = []
    self.cleared_storage: list[tuple[str, str]] = []

  def claim_next_export(self, _worker_id: str) -> ClaimedExport | None:
    if self.claimed:
      return None
    self.claimed = True
    return ClaimedExport({
      "id": "70000000-0000-4000-8000-000000000001",
      "ownerUserId": "10000000-0000-4000-8000-000000000001",
      "format": "csv",
      "locale": "zh-CN",
      "columns": ["label", "totalReturn"],
      "outputHash": "c" * 64,
      "createdAt": datetime.now(timezone.utc).isoformat(),
    }, "80000000-0000-4000-8000-000000000001")

  def load_source(self, _export_id: str, _token: str) -> dict[str, Any]:
    return self.source

  def heartbeat(
    self, _export_id: str, _token: str, _completed: int | None = None,
  ) -> bool:
    return True

  def should_cancel(self, _export_id: str, _token: str) -> bool:
    return False

  def complete_export(self, _export_id: str, _token: str, **values: Any) -> bool:
    self.completed = values
    return True

  def finish_cancelled(self, _export_id: str, _token: str) -> bool:
    return True

  def fail_export(
    self,
    _export_id: str,
    _token: str,
    error_code: str,
    _message: str,
    *,
    retryable: bool,
  ) -> bool:
    self.failed.append((error_code, retryable))
    return True

  def record_health(
    self,
    _worker_id: str,
    status: str,
    _export_id: str | None,
    _error: str | None = None,
  ) -> None:
    self.health.append(status)
    self.health_event.set()

  def expire_records(self) -> list[dict[str, Any]]:
    return self.expired_records

  def clear_expired_storage_key(self, export_id: str, storage_key: str) -> None:
    self.cleared_storage.append((export_id, storage_key))


def test_worker_generates_hash_and_size_without_loading_artifact_bytes(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  source = _source(_output(1000))
  repository = _WorkerRepository(source)
  monkeypatch.setattr(settings, "OUTPUT_ROOT", str(tmp_path))
  monkeypatch.setattr(settings, "EXPERIMENT_EXPORT_MIN_FREE_BYTES", 0)
  worker = EvaluationExportWorker(repository=repository, worker_id="test-worker")  # type: ignore[arg-type]
  worker._last_cleanup = float("inf")

  assert worker.run_once() is True
  assert repository.failed == []
  assert repository.completed is not None
  assert repository.completed["byte_size"] > 0
  assert len(repository.completed["content_sha256"]) == 64
  assert (
    "80000000-0000-4000-8000-000000000001"
    in repository.completed["storage_key"]
  )
  artifact = tmp_path / repository.completed["storage_key"]
  assert artifact.is_file()


def test_export_lease_refreshes_worker_health_during_long_render(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  repository = _WorkerRepository(_source(_output()))
  monkeypatch.setattr(settings, "EXPERIMENT_EXPORT_HEARTBEAT_SECONDS", 0.01)
  lease = EvaluationExportLease(
    repository,  # type: ignore[arg-type]
    "70000000-0000-4000-8000-000000000001",
    "80000000-0000-4000-8000-000000000001",
    "test-worker",
  )

  lease.start()
  try:
    assert repository.health_event.wait(0.5)
    assert "running" in repository.health
  finally:
    lease.stop()


def test_worker_deletes_expired_artifact_and_records_storage_cleanup(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  repository = _WorkerRepository(_source(_output()))
  storage_key = "evaluation-exports/owner/export.attempt.csv"
  artifact = tmp_path / storage_key
  artifact.parent.mkdir(parents=True)
  artifact.write_text("expired", encoding="utf-8")
  repository.expired_records = [{"id": "export-1", "storage_key": storage_key}]
  monkeypatch.setattr(settings, "OUTPUT_ROOT", str(tmp_path))
  worker = EvaluationExportWorker(
    repository=repository, worker_id="test-worker",  # type: ignore[arg-type]
  )
  worker._last_cleanup = float("-inf")

  worker._cleanup_if_due()

  assert not artifact.exists()
  assert repository.cleared_storage == [("export-1", storage_key)]


def test_worker_classifies_invalid_frozen_source_as_non_retryable(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  source = _source(_output())
  source["output"] = {"columns": ["label"], "rows": []}
  repository = _WorkerRepository(source)
  monkeypatch.setattr(settings, "OUTPUT_ROOT", str(tmp_path))
  monkeypatch.setattr(settings, "EXPERIMENT_EXPORT_MIN_FREE_BYTES", 0)
  worker = EvaluationExportWorker(repository=repository, worker_id="test-worker")  # type: ignore[arg-type]
  worker._last_cleanup = float("inf")

  assert worker.run_once() is True
  assert repository.completed is None
  assert repository.failed == [("source_invalid", False)]
