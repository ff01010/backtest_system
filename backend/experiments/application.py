"""实验预览外部 Interface。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from backend import settings
from backend.experiments.canonicalization import (
  CANONICALIZATION_VERSION,
  canonical_value,
  digest,
  with_version,
)
from backend.experiments.compiler import LegacySingleBarTrialCompiler, TrialCompiler
from backend.experiments.contracts import (
  ExperimentCreateRequest,
  ExperimentLifecycleRequest,
  ExperimentPurgeRequest,
  ExperimentPreviewRequest,
  ExperimentRetryFailedRequest,
  ExperimentRetryPreviewRequest,
  ExperimentRevisionCreateRequest,
  SubjectRef,
)
from backend.experiments.errors import ExperimentError
from backend.experiments.estimator import ResourceEstimator, SingleBarResourceEstimator
from backend.experiments.generators import (
  MAX_PLANNED_TRIAL_COUNT,
  FiniteGridGenerator,
  GeneratedGrid,
  seed_bundle,
)
from backend.experiments.lifecycle import (
  ExperimentLifecycleCommands,
  LifecycleCommand,
)
from backend.experiments.recovery import default_failure_policy
from backend.experiments.rollout import ContractEpoch, current_contract_epoch
from backend.experiments.repository import (
  ExperimentRepository,
  PostgresExperimentRepository,
  PreviewRepository,
)
from backend.experiments.resolvers import (
  DatasetSnapshotResolver,
  LegacySingleStrategyResolver,
  QmtBarDatasetSnapshotResolver,
  ResolvedRunTemplate,
  RunTemplateResolver,
)


_EXPERIMENT_LIST_STATUSES = frozenset({
  "preparing",
  "queued",
  "running",
  "completed",
  "partially_completed",
  "failed",
  "cancelling",
  "cancelled",
  "generation_blocked",
})


class ExperimentApplication:
  """在一个深 Module 内封装预览权限、规范化、快照与预算。"""

  def __init__(
    self,
    *,
    resolver: RunTemplateResolver | None = None,
    dataset_resolver: DatasetSnapshotResolver | None = None,
    generator: FiniteGridGenerator | None = None,
    compiler: TrialCompiler | None = None,
    estimator: ResourceEstimator | None = None,
    repository: PreviewRepository | None = None,
    lifecycle_commands: ExperimentLifecycleCommands | None = None,
    clock: Callable[[], datetime] | None = None,
  ) -> None:
    self._resolver = resolver or LegacySingleStrategyResolver()
    self._dataset_resolver = dataset_resolver or QmtBarDatasetSnapshotResolver()
    self._generator = generator or FiniteGridGenerator()
    self._compiler = compiler or LegacySingleBarTrialCompiler()
    self._estimator = estimator or SingleBarResourceEstimator()
    self._repository = repository or PostgresExperimentRepository()
    self._lifecycle_commands = lifecycle_commands or ExperimentLifecycleCommands()
    self._clock = clock or (lambda: datetime.now(timezone.utc))

  def resolve_subject(self, subject_ref: SubjectRef) -> dict[str, Any]:
    self._require_capability()
    template = self._resolver.resolve(subject_ref)
    fixed = template.snapshot["fixedStructure"]
    return {
      "subjectRef": subject_ref.model_dump(),
      "displayName": fixed["strategyName"],
      "templateType": template.snapshot["templateType"],
      "runTemplateDigest": template.digest,
      "fixedStructure": fixed,
      "tunableInputCatalog": template.snapshot["tunableInputCatalog"],
      "assetManifestSummary": template.snapshot["assetManifestSnapshot"],
      "contracts": template.snapshot["contracts"],
      "defaultExperimentInputs": template.defaults,
      "capability": self._capability_summary(),
    }

  def preview(self, request: ExperimentPreviewRequest) -> dict[str, Any]:
    response = self._calculate_preview(request)
    self._repository.save_preview(response)
    public = {key: value for key, value in response.items() if key != "previewSummary"}
    normalized_spec = deepcopy(public["normalizedSpec"])
    normalized_spec["runTemplateSnapshot"].pop("executionSnapshot", None)
    public["normalizedSpec"] = normalized_spec
    return public

  def create(
    self,
    request: ExperimentCreateRequest,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    response = self._calculate_preview(request.experimentSpec)
    if not response["limitDecision"]["allowed"]:
      self._raise_create_blocked(response)
    if request.specHash != response["specHash"]:
      raise ExperimentError(
        "experiment.preview_stale", "实验定义已变化，请重新预览", 409,
      )
    repository = self._experiment_repository()
    result = repository.create_experiment(
      idempotency_key=idempotency_key,
      request_hash=digest(with_version({
        "operationType": "experiment.create.v1",
        "request": request.model_dump(mode="json"),
      })),
      preview_token=request.previewToken,
      expected_bindings=self._preview_bindings(response),
      normalized_spec=response["normalizedSpec"],
      source=request.source.model_dump(mode="json") if request.source else None,
      request_id=request_id,
    )
    return result.response, result.status_code

  def find_created(self, idempotency_key: str) -> tuple[dict[str, Any], int]:
    result = self._experiment_repository().find_created_experiment(idempotency_key)
    return result.response, result.status_code

  def create_revision(
    self,
    experiment_id: str,
    request: ExperimentRevisionCreateRequest,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    response = self._calculate_preview(request.experimentSpec)
    self._validate_create_preview(request, response)
    result = self._experiment_repository().create_revision(
      experiment_id=experiment_id,
      expected_revision_id=str(request.expectedRevisionId),
      expected_snapshot_version=request.expectedSnapshotVersion,
      idempotency_key=idempotency_key,
      request_hash=digest(with_version({
        "operationType": "experiment.revision.create.v1",
        "experimentId": experiment_id,
        "request": request.model_dump(mode="json"),
      })),
      preview_token=request.previewToken,
      expected_bindings=self._preview_bindings(response),
      normalized_spec=response["normalizedSpec"],
      request_id=request_id,
    )
    return result.response, result.status_code

  def detail(self, experiment_id: str) -> dict[str, Any]:
    return self._experiment_repository().get_experiment(experiment_id)

  def list_experiments(
    self,
    *,
    limit: int,
    cursor: str | None,
    archived: bool = False,
    query: str = "",
    status: str | None = None,
  ) -> dict[str, Any]:
    normalized_query = query.strip()
    normalized_status = status.strip() if status else None
    if len(normalized_query) > 100:
      raise ExperimentError(
        "experiment.list_filter_invalid", "实验搜索内容不能超过 100 个字符", 400,
      )
    if normalized_status and normalized_status not in _EXPERIMENT_LIST_STATUSES:
      raise ExperimentError(
        "experiment.list_filter_invalid", "实验状态筛选无效", 400,
      )
    return self._experiment_repository().list_experiments(
      limit=limit,
      cursor=cursor,
      archived=archived,
      query=normalized_query,
      status=normalized_status,
    )

  def list_admin_archived_experiments(
    self,
    *,
    limit: int,
    cursor: str | None,
  ) -> dict[str, Any]:
    return self._experiment_repository().list_admin_archived_experiments(
      limit=limit, cursor=cursor,
    )

  def list_revisions(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
  ) -> dict[str, Any]:
    return self._experiment_repository().list_revisions(
      experiment_id, limit=limit, cursor=cursor,
    )

  def list_trials(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
    status: str | None,
    revision_id: str | None = None,
    query: str = "",
    date_scenario_id: str | None = None,
  ) -> dict[str, Any]:
    return self._experiment_repository().list_trials(
      experiment_id,
      limit=limit,
      cursor=cursor,
      status=status,
      revision_id=revision_id,
      query=query,
      date_scenario_id=date_scenario_id,
    )

  def list_related_runs(
    self,
    experiment_id: str,
    *,
    limit: int,
    cursor: str | None,
    query: str,
  ) -> dict[str, Any]:
    return self._experiment_repository().list_related_runs(
      experiment_id,
      limit=limit,
      cursor=cursor,
      query=query,
    )

  def preview_retry(
    self,
    experiment_id: str,
    request: ExperimentRetryPreviewRequest,
  ) -> dict[str, Any]:
    return self._experiment_repository().preview_retry_selection(
      experiment_id,
      str(request.expectedRevisionId),
      request.selection.model_dump(mode="json"),
    )

  def retry_failed(
    self,
    experiment_id: str,
    request: ExperimentRetryFailedRequest,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    request_hash = digest(with_version({
      "operationType": "experiment.retry_failed.v1",
      "experimentId": experiment_id,
      "request": request.model_dump(mode="json"),
    }))
    result = self._experiment_repository().retry_failed_trials(
      experiment_id=experiment_id,
      expected_revision_id=str(request.expectedRevisionId),
      selection=request.selection.model_dump(mode="json"),
      expected_count=request.expectedSelectedTrialCount,
      expected_digest=request.expectedSelectionDigest,
      reason=request.reason,
      compiler=self._compiler,
      idempotency_key=idempotency_key,
      request_hash=request_hash,
      request_id=request_id,
    )
    return result.response, result.status_code

  def cancel(
    self,
    experiment_id: str,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    if current_contract_epoch() >= ContractEpoch.LIFECYCLE_V2:
      detail = self.detail(experiment_id)
      return self.cancel_lifecycle(
        experiment_id,
        str(detail["currentRevisionId"]),
        int(detail["lifecycleVersion"]),
        "旧版取消接口请求",
        idempotency_key,
        request_id,
      )
    result = self._experiment_repository().cancel_experiment(
      experiment_id,
      idempotency_key,
      digest(with_version({
        "operationType": "experiment.cancel.v1",
        "experimentId": experiment_id,
      })),
      request_id,
    )
    return result.response, result.status_code

  def pause_lifecycle(
    self,
    experiment_id: str,
    expected_revision_id: str,
    expected_lifecycle_version: int,
    reason: str,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    result = self._lifecycle_commands.pause(LifecycleCommand(
      experiment_id=experiment_id,
      expected_revision_id=expected_revision_id,
      expected_lifecycle_version=expected_lifecycle_version,
      idempotency_key=idempotency_key,
      reason=reason,
      request_id=request_id,
    ))
    return result.response, result.status_code

  def resume_lifecycle(
    self,
    experiment_id: str,
    expected_revision_id: str,
    expected_lifecycle_version: int,
    resume_mode: str,
    reason: str,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    if resume_mode not in {"user_pause", "execution_circuit"}:
      raise ExperimentError("experiment.command_invalid", "恢复模式无效", 409)
    result = self._lifecycle_commands.resume(LifecycleCommand(
      experiment_id=experiment_id,
      expected_revision_id=expected_revision_id,
      expected_lifecycle_version=expected_lifecycle_version,
      idempotency_key=idempotency_key,
      reason=reason,
      request_id=request_id,
      resume_mode=resume_mode,  # type: ignore[arg-type]
    ))
    return result.response, result.status_code

  def cancel_lifecycle(
    self,
    experiment_id: str,
    expected_revision_id: str,
    expected_lifecycle_version: int,
    reason: str,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    result = self._lifecycle_commands.cancel(LifecycleCommand(
      experiment_id=experiment_id,
      expected_revision_id=expected_revision_id,
      expected_lifecycle_version=expected_lifecycle_version,
      idempotency_key=idempotency_key,
      reason=reason,
      request_id=request_id,
    ))
    return result.response, result.status_code

  def resume(
    self,
    experiment_id: str,
    expected_revision_id: str,
    reason: str,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    if current_contract_epoch() >= ContractEpoch.LIFECYCLE_V2:
      detail = self.detail(experiment_id)
      return self.resume_lifecycle(
        experiment_id,
        expected_revision_id,
        int(detail["lifecycleVersion"]),
        "execution_circuit",
        reason,
        idempotency_key,
        request_id,
      )
    repository = self._experiment_repository()
    if not hasattr(repository, "resume_experiment"):
      raise RuntimeError("当前实验仓储不支持恢复操作")
    request_hash = digest(with_version({
      "operationType": "experiment.resume.v1",
      "experimentId": experiment_id,
      "expectedRevisionId": expected_revision_id,
      "reason": reason,
    }))
    result = repository.resume_experiment(
      experiment_id,
      expected_revision_id,
      reason,
      idempotency_key,
      request_hash,
      request_id,
    )
    return result.response, result.status_code

  def archive(
    self,
    experiment_id: str,
    request: ExperimentLifecycleRequest,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    request_hash = digest(with_version({
      "operationType": "experiment.archive.v1",
      "experimentId": experiment_id,
      "request": request.model_dump(mode="json"),
    }))
    result = self._experiment_repository().archive_experiment(
      experiment_id=experiment_id,
      expected_revision_id=str(request.expectedRevisionId),
      expected_snapshot_version=request.expectedSnapshotVersion,
      reason=request.reason,
      idempotency_key=idempotency_key,
      request_hash=request_hash,
      request_id=request_id,
    )
    return result.response, result.status_code

  def restore_archive(
    self,
    experiment_id: str,
    request: ExperimentLifecycleRequest,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    request_hash = digest(with_version({
      "operationType": "experiment.archive.restore.v1",
      "experimentId": experiment_id,
      "request": request.model_dump(mode="json"),
    }))
    result = self._experiment_repository().restore_archived_experiment(
      experiment_id=experiment_id,
      expected_revision_id=str(request.expectedRevisionId),
      expected_snapshot_version=request.expectedSnapshotVersion,
      reason=request.reason,
      idempotency_key=idempotency_key,
      request_hash=request_hash,
      request_id=request_id,
    )
    return result.response, result.status_code

  def purge(
    self,
    experiment_id: str,
    request: ExperimentPurgeRequest,
    idempotency_key: str,
    request_id: str | None = None,
  ) -> tuple[dict[str, Any], int]:
    request_hash = digest(with_version({
      "operationType": "experiment.purge.create.v1",
      "experimentId": experiment_id,
      "request": request.model_dump(mode="json"),
    }))
    result = self._experiment_repository().request_purge(
      experiment_id=experiment_id,
      expected_revision_id=str(request.expectedRevisionId),
      expected_snapshot_version=request.expectedSnapshotVersion,
      reason=request.reason,
      idempotency_key=idempotency_key,
      request_hash=request_hash,
      request_id=request_id,
    )
    return result.response, result.status_code

  def _experiment_repository(self) -> ExperimentRepository:
    required = (
      "create_experiment", "find_created_experiment",
      "get_experiment", "cancel_experiment",
    )
    if not all(hasattr(self._repository, name) for name in required):
      raise RuntimeError("当前实验仓储不支持创建操作")
    return self._repository  # type: ignore[return-value]

  @staticmethod
  def _validate_create_preview(
    request: ExperimentCreateRequest,
    response: dict[str, Any],
  ) -> None:
    if not response["limitDecision"]["allowed"]:
      ExperimentApplication._raise_create_blocked(response)
    if request.specHash != response["specHash"]:
      raise ExperimentError(
        "experiment.preview_stale", "实验定义已变化，请重新预览", 409,
      )

  @staticmethod
  def _raise_create_blocked(response: dict[str, Any]) -> None:
    legacy = current_contract_epoch() < ContractEpoch.ROLLING_WINDOW_V1
    raise ExperimentError(
      "experiment.plan_limit_exceeded" if legacy else "experiment.validation_failed",
      (
        "实验计划超过当前硬上限，请重新调整后预览"
        if legacy else "实验输入不符合创建要求，请重新调整后预览"
      ),
      429 if legacy else 422,
      details={"blockingReasons": response["limitDecision"]["blockingReasons"]},
    )

  def _calculate_preview(self, request: ExperimentPreviewRequest) -> dict[str, Any]:
    self._require_capability()
    base_template = self._resolver.resolve(request.subjectRef)
    stage_request = request.plan.stages[0]
    datasets = self._dataset_resolver.resolve(
      base_template, tuple(stage_request.dateScenarios),
    )
    template = replace(base_template, digest=digest(with_version({
      "runTemplateSnapshot": base_template.snapshot,
      "datasetSnapshots": list(datasets),
    })))
    grid = self._generator.normalize(stage_request, template)
    estimate = self._estimator.estimate(
      template, datasets, grid.planned_trial_count, len(stage_request.dateScenarios),
    )
    if (
      current_contract_epoch() < ContractEpoch.ROLLING_WINDOW_V1
      and estimate["estimateStatus"] == "unavailable"
    ):
      raise ExperimentError(
        "experiment.capacity_unavailable",
        "当前数据或周期元数据不足，无法形成可靠资源估算",
        503,
        retryable=True,
      )
    limit_decision = self._limit_decision(grid, estimate)
    examples = self._examples(template, grid)
    normalized_spec = self._normalized_spec(request, template, grid, datasets, estimate)
    spec_hash = self._spec_hash(normalized_spec)
    response = self._preview_response(
      normalized_spec, template, grid, datasets, estimate,
      limit_decision, examples, spec_hash,
    )
    return response

  @staticmethod
  def _preview_bindings(response: dict[str, Any]) -> dict[str, str]:
    return {
      "specHash": response["specHash"],
      "runTemplateDigest": response["runTemplateDigest"],
      "datasetSnapshotDigest": response["datasetSnapshotDigest"],
      "capabilityDigest": response["capabilityDigest"],
      "budgetDigest": response["budgetDigest"],
    }

  def _require_capability(self) -> None:
    if not settings.EXPERIMENTS_ENABLED or not settings.EXPERIMENT_BAR_ENABLED:
      raise ExperimentError(
        "experiment.capacity_unavailable",
        "标准 Bar 自动化回测尚未启用",
        503,
        retryable=True,
      )

  def _limit_decision(
    self,
    grid: GeneratedGrid,
    estimate: dict[str, Any],
  ) -> dict[str, Any]:
    reasons: list[dict[str, Any]] = []
    oversized = {
      path: count for path, count in grid.dimension_counts.items()
      if count > settings.EXPERIMENT_MAX_VALUES_PER_DIMENSION
    }
    if oversized:
      reasons.append({
        "code": "dimension_limit_exceeded",
        "message": "单个维度候选值数量超过上限",
        "dimensions": oversized,
      })
    if current_contract_epoch() < ContractEpoch.ROLLING_WINDOW_V1:
      if grid.planned_trial_count > settings.EXPERIMENT_MAX_TRIALS:
        reasons.append({
          "code": "trial_limit_exceeded",
          "message": "Trial 总数超过单实验上限",
          "actual": grid.planned_trial_count,
          "limit": settings.EXPERIMENT_MAX_TRIALS,
        })
      estimated_bars = estimate.get("estimatedBarCount")
      if (
        estimated_bars is not None
        and int(estimated_bars) > settings.EXPERIMENT_BAR_MAX_ESTIMATED_BARS
      ):
        reasons.append({
          "code": "bar_budget_exceeded",
          "message": "预计 Bar 总量超过标准 Bar 资源预算",
          "actual": estimated_bars,
          "limit": settings.EXPERIMENT_BAR_MAX_ESTIMATED_BARS,
        })
    warnings = []
    if estimate["estimateStatus"] == "unavailable":
      warnings.append({
        "code": "resource_estimate_unavailable",
        "message": "当前覆盖元数据不足，暂时无法估算耗时",
      })
    elif estimate["resourceClass"] == "heavy":
      warnings.append({
        "code": "high_resource_estimate",
        "message": "该实验预计资源消耗较高，创建后可能长时间排队",
      })
    return {"allowed": not reasons, "warnings": warnings, "blockingReasons": reasons}

  def _examples(
    self,
    template: ResolvedRunTemplate,
    grid: GeneratedGrid,
  ) -> list[dict[str, Any]]:
    indexes = _sample_indexes(grid.planned_trial_count)
    examples: list[dict[str, Any]] = []
    for index in indexes:
      assignment = grid.assignment_at(index)
      compiled = self._compiler.compile(template, assignment)
      trial_fingerprint = grid.trial_fingerprint(
        index, assignment, self._compiler.contract_version,
      )
      examples.append({
        "trialIndex": index,
        "trialFingerprint": trial_fingerprint,
        "seedBundle": seed_bundle(trial_fingerprint),
        "dimensionAssignment": assignment,
        "compiledRequestDigest": compiled["requestDigest"],
      })
    return examples

  def _normalized_spec(
    self,
    request: ExperimentPreviewRequest,
    template: ResolvedRunTemplate,
    grid: GeneratedGrid,
    datasets: tuple[dict[str, Any], ...],
    estimate: dict[str, Any],
  ) -> dict[str, Any]:
    budget = self._budget_summary()
    policy = (
      {"admissionPolicy": budget["admissionPolicy"]}
      if budget["budgetVersion"] == "experiment_budget_v3"
      else {"capacityPolicy": budget}
    )
    return canonical_value(with_version({
      "schemaVersion": request.schemaVersion,
      "name": request.name.strip(),
      "description": request.description,
      "subjectRef": request.subjectRef.model_dump(),
      "runTemplateSnapshot": template.snapshot,
      "plan": {
        "planVersion": request.plan.planVersion,
        "stages": [{
          **grid.normalized_stage,
          "stageInputFingerprint": grid.stage_input_fingerprint,
          "plannedTrialCount": grid.planned_trial_count,
        }],
      },
      "datasetSnapshots": list(datasets),
      "resourceEstimate": estimate,
      **policy,
      "failurePolicy": default_failure_policy(),
      "determinism": {
        "seedDerivation": "trial-seed-v1",
        "deterministicLevel": "input_deterministic_only",
        "tolerancePolicy": "none",
      },
    }))

  def _spec_hash(self, normalized_spec: dict[str, Any]) -> str:
    semantic = {
      key: value for key, value in normalized_spec.items()
      if key not in {"name", "description"}
    }
    return digest(semantic)

  def _preview_response(
    self,
    normalized_spec: dict[str, Any],
    template: ResolvedRunTemplate,
    grid: GeneratedGrid,
    datasets: tuple[dict[str, Any], ...],
    estimate: dict[str, Any],
    limit_decision: dict[str, Any],
    examples: list[dict[str, Any]],
    spec_hash: str,
  ) -> dict[str, Any]:
    now = self._clock()
    capability = self._capability_summary()
    budget = self._budget_summary()
    response = {
      "previewToken": str(uuid4()),
      "expiresAt": now + timedelta(seconds=settings.EXPERIMENT_PREVIEW_TTL_SECONDS),
      "specHash": spec_hash,
      "canonicalizationVersion": CANONICALIZATION_VERSION,
      "runTemplateDigest": template.digest,
      "datasetSnapshotDigest": digest(list(datasets)),
      "capabilityDigest": digest(capability),
      "budgetDigest": digest(budget),
      "budgetVersion": budget["budgetVersion"],
      "normalizedSpec": normalized_spec,
      "plannedTrialCount": grid.planned_trial_count,
      "dimensionCounts": grid.dimension_counts,
      "dimensionNormalization": grid.dimension_normalization,
      "resourceEstimate": estimate,
      "limitDecision": limit_decision,
      "examples": examples,
    }
    response["previewSummary"] = {
      "specHash": spec_hash,
      "plannedTrialCount": grid.planned_trial_count,
      "limitDecision": limit_decision,
      "exampleFingerprints": [item["trialFingerprint"] for item in examples],
    }
    return response

  def _capability_summary(self) -> dict[str, Any]:
    return with_version({
      "capabilityVersion": "experiment_bar_execution_v1",
      "templateTypes": ["legacy_single_strategy_v1"],
      "stageTypes": ["finite_grid_v1"],
      "runTypes": ["standard_bar"],
      "trialCompiler": self._compiler.contract_version,
      "resourceEstimator": self._estimator.version,
    })

  def _budget_summary(self) -> dict[str, Any]:
    if current_contract_epoch() < ContractEpoch.ROLLING_WINDOW_V1:
      return with_version({
        "budgetVersion": "experiment_budget_v2",
        "maxValuesPerDimension": settings.EXPERIMENT_MAX_VALUES_PER_DIMENSION,
        "maxTrials": settings.EXPERIMENT_MAX_TRIALS,
        "maxUserPlannedTrials": settings.EXPERIMENT_MAX_USER_PLANNED_TRIALS,
        "maxEstimatedBars": settings.EXPERIMENT_BAR_MAX_ESTIMATED_BARS,
        "maxInFlightPerExperiment": settings.EXPERIMENT_MAX_IN_FLIGHT_PER_EXPERIMENT,
        "maxInFlightPerUser": settings.EXPERIMENT_MAX_IN_FLIGHT_PER_USER,
        "schedulerCapacity": settings.BACKTEST_BASE_CONCURRENCY,
      })
    return with_version({
      "budgetVersion": "experiment_budget_v3",
      "admissionPolicy": {
        "maxValuesPerDimension": settings.EXPERIMENT_MAX_VALUES_PER_DIMENSION,
        "maxExactPlannedTrialCount": MAX_PLANNED_TRIAL_COUNT,
      },
    })


def _sample_indexes(count: int) -> list[int]:
  indexes = [*range(min(3, count)), *range(max(0, count - 3), count)]
  return list(dict.fromkeys(indexes))
