"""基础自定义评价的应用层 Interface。"""

from __future__ import annotations

from typing import Any

from backend.experiments.canonicalization import canonical_value, digest, with_version
from backend.experiments.contracts import EvaluationSpecRequest
from backend.experiments.errors import ExperimentError
from backend.experiments.evaluation import (
  DIMENSION_CATALOG_VERSION,
  EVALUATION_PIPELINE_VERSION,
  METRIC_CATALOG_VERSION,
  CANDIDATE_RATE_DISTRIBUTION_NORMALIZER_VERSION,
  RESULT_ADAPTER_TYPE,
  RESULT_ADAPTER_VERSION,
  catalog_snapshot,
  validate_evaluation_spec,
)
from backend.experiments.evaluation_repository import PostgresEvaluationRepository


class EvaluationApplication:
  def __init__(self, repository: PostgresEvaluationRepository | None = None) -> None:
    self._repository = repository or PostgresEvaluationRepository()

  def catalogs(self, experiment_id: str) -> dict[str, Any]:
    context = self._repository.experiment_context(experiment_id)
    catalogs = catalog_snapshot(dict(context["experiment_spec"]))
    return {
      "experimentId": str(context["experiment_id"]),
      "experimentRevisionId": str(context["current_revision_id"]),
      "stages": [
        {
          "stageId": str(stage["id"]),
          "stageIndex": int(stage["stage_index"]),
          "stageType": stage["stage_type"],
          "status": stage["status"],
          "plannedTrialCount": int(stage["planned_trial_count"]),
        }
        for stage in context["stages"]
      ],
      **catalogs,
      "pipelineVersion": EVALUATION_PIPELINE_VERSION,
      "adapter": {"type": RESULT_ADAPTER_TYPE, "version": RESULT_ADAPTER_VERSION},
      "normalizers": [{
        "version": CANDIDATE_RATE_DISTRIBUTION_NORMALIZER_VERSION,
        "name": "候选率指标跨场景分布",
        "supportedUnits": ["ratio"],
      }],
    }

  def save_spec(
    self,
    experiment_id: str,
    request: EvaluationSpecRequest,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    context = self._repository.experiment_context(experiment_id)
    normalized = canonical_value(request.model_dump(mode="json"))
    self._validate_source(context, normalized)
    catalogs = catalog_snapshot(dict(context["experiment_spec"]))
    validate_evaluation_spec(normalized, catalogs)
    spec_hash = digest(normalized)
    result = self._repository.save_spec(
      experiment_id=experiment_id,
      evaluation_spec=normalized,
      spec_hash=spec_hash,
      idempotency_key=idempotency_key,
      request_hash=digest(with_version({
        "operationType": "experiment.evaluation_spec.save.v1",
        "experimentId": experiment_id,
        "evaluationSpec": normalized,
      })),
      request_id=request_id,
    )
    return result.response, result.status_code

  def list_specs(self, experiment_id: str) -> dict[str, Any]:
    return self._repository.list_specs(experiment_id)

  def create_run(
    self,
    experiment_id: str,
    evaluation_spec_id: str,
    idempotency_key: str,
    request_id: str | None = None,
    force_recompute: bool = False,
  ) -> tuple[dict[str, Any], int]:
    stored = self._repository.get_spec(experiment_id, evaluation_spec_id)
    evaluation_spec = dict(stored["evaluation_spec"])
    catalogs = catalog_snapshot(dict(stored["revision_experiment_spec"]))
    validate_evaluation_spec(evaluation_spec, catalogs)
    result = self._repository.create_run(
      experiment_id=experiment_id,
      evaluation_spec_id=evaluation_spec_id,
      catalog_snapshot=catalogs,
      pipeline_version=EVALUATION_PIPELINE_VERSION,
      adapter_type=RESULT_ADAPTER_TYPE,
      adapter_version=RESULT_ADAPTER_VERSION,
      force_recompute=force_recompute,
      idempotency_key=idempotency_key,
      request_hash=digest(with_version({
        "operationType": "experiment.evaluation_run.create.v1",
        "experimentId": experiment_id,
        "evaluationSpecId": evaluation_spec_id,
        "forceRecompute": force_recompute,
      })),
      request_id=request_id,
    )
    return result.response, result.status_code

  def list_runs(self, experiment_id: str) -> dict[str, Any]:
    return self._repository.list_runs(experiment_id)

  def run_detail(self, experiment_id: str, run_id: str) -> dict[str, Any]:
    return self._repository.get_run(experiment_id, run_id)

  def retry_run(
    self,
    experiment_id: str,
    run_id: str,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    result = self._repository.retry_run(
      experiment_id=experiment_id,
      run_id=run_id,
      idempotency_key=idempotency_key,
      request_hash=digest(with_version({
        "operationType": "experiment.evaluation_run.retry.v1",
        "experimentId": experiment_id,
        "evaluationRunId": run_id,
      })),
      request_id=request_id,
    )
    return result.response, result.status_code

  @staticmethod
  def _validate_source(context: dict[str, Any], evaluation_spec: dict[str, Any]) -> None:
    source = evaluation_spec["source"]
    if source["experimentRevisionId"] != str(context["current_revision_id"]):
      raise ExperimentError(
        "experiment.evaluation_source_invalid", "评价来源必须是当前实验修订", 409,
      )
    stage_ids = {str(stage["id"]) for stage in context["stages"]}
    if any(str(stage_id) not in stage_ids for stage_id in source["stageIds"]):
      raise ExperimentError(
        "experiment.evaluation_source_invalid", "评价来源包含未知 Stage", 422,
      )
    catalog_versions = evaluation_spec["catalogs"]
    if (
      catalog_versions["metricCatalogVersion"] != METRIC_CATALOG_VERSION
      or catalog_versions["dimensionCatalogVersion"] != DIMENSION_CATALOG_VERSION
      or catalog_versions["normalizerVersion"] not in {
        None, CANDIDATE_RATE_DISTRIBUTION_NORMALIZER_VERSION,
      }
    ):
      raise ExperimentError(
        "experiment.evaluation_catalog_unsupported", "评价目录版本不受支持", 422,
      )
