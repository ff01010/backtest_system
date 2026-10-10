"""评价导出应用层 Interface。"""

from __future__ import annotations

from typing import Any

from backend.experiments.canonicalization import digest, with_version
from backend.experiments.contracts import EvaluationExportCreateRequest
from backend.experiments.export_repository import PostgresEvaluationExportRepository


class EvaluationExportApplication:
  def __init__(
    self,
    repository: PostgresEvaluationExportRepository | None = None,
  ) -> None:
    self._repository = repository or PostgresEvaluationExportRepository()

  def create(
    self,
    evaluation_run_id: str,
    request: EvaluationExportCreateRequest,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    payload = request.model_dump(mode="json")
    result = self._repository.create_export(
      evaluation_run_id=evaluation_run_id,
      export_format=payload["format"],
      columns=payload["columns"],
      locale=payload["locale"],
      idempotency_key=idempotency_key,
      request_hash=digest(with_version({
        "operationType": "experiment.evaluation_export.create.v1",
        "evaluationRunId": evaluation_run_id,
        **payload,
      })),
      request_id=request_id,
    )
    return result.response, result.status_code

  def list_exports(self, evaluation_run_id: str) -> dict[str, Any]:
    return self._repository.list_exports(evaluation_run_id)

  def detail(self, export_id: str) -> dict[str, Any]:
    return self._repository.get_export(export_id)

  def cancel(
    self,
    export_id: str,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    result = self._repository.cancel_export(
      export_id=export_id,
      idempotency_key=idempotency_key,
      request_hash=digest(with_version({
        "operationType": "experiment.evaluation_export.cancel.v1",
        "exportId": export_id,
      })),
      request_id=request_id,
    )
    return result.response, result.status_code

  def retry(
    self,
    export_id: str,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    result = self._repository.retry_export(
      export_id=export_id,
      idempotency_key=idempotency_key,
      request_hash=digest(with_version({
        "operationType": "experiment.evaluation_export.retry.v1",
        "exportId": export_id,
      })),
      request_id=request_id,
    )
    return result.response, result.status_code

  def download_source(self, export_id: str) -> dict[str, Any]:
    return self._repository.get_download(export_id)

  def mark_downloaded(self, export_id: str) -> None:
    self._repository.mark_downloaded(export_id)

  def expire_artifact(self, export_id: str, error_code: str | None = None) -> None:
    self._repository.expire_artifact(export_id, error_code)

  def worker_available(self) -> bool:
    return self._repository.worker_available()
