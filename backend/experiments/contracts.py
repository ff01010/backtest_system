"""实验预览的版本化请求契约。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.date_codec import TradingDateError, format_api_date


class StrictModel(BaseModel):
  model_config = ConfigDict(extra="forbid")


class SubjectRef(StrictModel):
  subjectType: Literal["legacy_single_strategy"]
  subjectId: str = Field(min_length=1, max_length=64)
  subjectVersion: str = Field(min_length=1, max_length=64)


class ParameterGridItem(StrictModel):
  inputId: str = Field(min_length=1, max_length=160)
  values: list[Any] = Field(min_length=1, max_length=1000)


class DateScenario(StrictModel):
  scenarioId: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
  name: str = Field(min_length=1, max_length=80)
  role: Literal["development", "validation", "reference"]
  startDate: str
  endDate: str

  @field_validator("name")
  @classmethod
  def normalize_name(cls, value: str) -> str:
    normalized = value.strip()
    if not normalized:
      raise ValueError("日期场景名称不能为空")
    return normalized

  @field_validator("startDate", "endDate", mode="before")
  @classmethod
  def normalize_date(cls, value: object) -> str:
    try:
      return format_api_date(value)  # type: ignore[arg-type]
    except TradingDateError as error:
      raise ValueError(str(error)) from error

  @model_validator(mode="after")
  def validate_range(self) -> "DateScenario":
    if self.startDate > self.endDate:
      raise ValueError("日期场景开始日期不能晚于结束日期")
    return self


class CostScenario(StrictModel):
  scenarioId: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
  slippage: Any


class ExecutionScenario(StrictModel):
  scenarioId: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
  algorithm: str = Field(min_length=1, max_length=64)
  parameters: dict[str, Any] = Field(default_factory=dict)


class FiniteGridStage(StrictModel):
  stageType: Literal["finite_grid_v1"]
  generatorVersion: Literal["1"]
  parameterGrid: list[ParameterGridItem] = Field(default_factory=list, max_length=64)
  dateScenarios: list[DateScenario] = Field(min_length=1, max_length=100)
  capitalScenarios: list[Any] = Field(min_length=1, max_length=1000)
  costScenarios: list[CostScenario] = Field(min_length=1, max_length=100)
  executionScenarios: list[ExecutionScenario] = Field(min_length=1, max_length=100)


class ExperimentPlan(StrictModel):
  planVersion: Literal["experiment_plan_v1"]
  stages: list[FiniteGridStage] = Field(min_length=1, max_length=1)


class ExperimentPreviewRequest(StrictModel):
  schemaVersion: Literal["experiment_create_v1"]
  name: str = Field(min_length=1, max_length=128)
  description: str = Field(default="", max_length=4000)
  subjectRef: SubjectRef
  plan: ExperimentPlan

  @field_validator("name")
  @classmethod
  def normalize_name(cls, value: str) -> str:
    normalized = value.strip()
    if not normalized:
      raise ValueError("实验名称不能为空")
    return normalized


class ExperimentCreateRequest(StrictModel):
  previewToken: str = Field(
    min_length=36,
    max_length=36,
    pattern=r"^[0-9a-fA-F-]{36}$",
  )
  specHash: str = Field(pattern=r"^[0-9a-f]{64}$")
  experimentSpec: ExperimentPreviewRequest

  @field_validator("previewToken")
  @classmethod
  def normalize_preview_token(cls, value: str) -> str:
    try:
      return str(UUID(value))
    except ValueError as error:
      raise ValueError("previewToken 必须是 UUID") from error


class ExperimentRevisionCreateRequest(ExperimentCreateRequest):
  expectedRevisionId: UUID
  expectedSnapshotVersion: int = Field(ge=1)


class TrialSelection(StrictModel):
  mode: Literal["trial_ids"]
  trialIds: list[UUID] = Field(min_length=1, max_length=200)

  @field_validator("trialIds")
  @classmethod
  def unique_trial_ids(cls, value: list[UUID]) -> list[UUID]:
    if len(set(value)) != len(value):
      raise ValueError("trialIds 不能重复")
    return value


class ExperimentRetryPreviewRequest(StrictModel):
  expectedRevisionId: UUID
  selection: TrialSelection


class ExperimentRetryFailedRequest(ExperimentRetryPreviewRequest):
  expectedSelectedTrialCount: int = Field(ge=1, le=200)
  expectedSelectionDigest: str = Field(pattern=r"^[0-9a-f]{64}$")
  reason: str = Field(default="", max_length=500)

  @field_validator("reason")
  @classmethod
  def normalize_reason(cls, value: str) -> str:
    return value.strip()


class ExperimentResumeRequest(StrictModel):
  expectedRevisionId: UUID
  reason: str = Field(min_length=1, max_length=500)

  @field_validator("reason")
  @classmethod
  def normalize_reason(cls, value: str) -> str:
    normalized = value.strip()
    if not normalized:
      raise ValueError("恢复原因不能为空")
    return normalized


class EvaluationSource(StrictModel):
  experimentRevisionId: UUID
  stageIds: list[UUID] = Field(min_length=1, max_length=1)
  executionSelection: Literal["selected_execution_at_run_creation"]


class EvaluationCatalogs(StrictModel):
  metricCatalogVersion: Literal["metric_catalog_v1"]
  dimensionCatalogVersion: Literal["dimension_catalog_v1"]
  normalizerVersion: None = None


class EvaluationDimensionCoverage(StrictModel):
  dimensionId: str = Field(min_length=1, max_length=160)
  minimumCoverageRate: str = Field(pattern=r"^(0(\.\d{1,4})?|1(\.0{1,4})?)$")


class EvaluationCompleteness(StrictModel):
  allowPartial: bool = False
  minimumCompletionRate: str = Field(
    default="1.0000",
    pattern=r"^(0(\.\d{1,4})?|1(\.0{1,4})?)$",
  )
  requiredDimensionCoverage: list[EvaluationDimensionCoverage] = Field(
    default_factory=list,
    max_length=16,
  )

  @model_validator(mode="after")
  def validate_partial_policy(self) -> "EvaluationCompleteness":
    if not self.allowPartial and (
      self.minimumCompletionRate != "1.0000" or self.requiredDimensionCoverage
    ):
      raise ValueError("完整评价必须使用 100% 完成率且不能设置部分覆盖门槛")
    dimensions = [item.dimensionId for item in self.requiredDimensionCoverage]
    if len(set(dimensions)) != len(dimensions):
      raise ValueError("部分评价的关键维度不能重复")
    return self


class EvaluationFormula(StrictModel):
  type: Literal["row_formula_v1", "group_formula_v1"]
  metricId: str = Field(
    min_length=1,
    max_length=160,
    pattern=r"^[A-Za-z][A-Za-z0-9_]{0,159}$",
  )
  displayName: str = Field(min_length=1, max_length=80)
  formula: str = Field(min_length=1, max_length=1000)
  precision: int = Field(default=8, ge=0, le=18)
  unit: str | None = Field(default=None, max_length=32)
  direction: Literal["higher", "lower", "neutral"] = "neutral"


class EvaluationAggregate(StrictModel):
  type: Literal["metric_aggregate_v1"] = "metric_aggregate_v1"
  aggregateId: str = Field(
    min_length=1,
    max_length=160,
    pattern=r"^[A-Za-z][A-Za-z0-9_]{0,159}$",
  )
  metricId: str = Field(min_length=1, max_length=160)
  function: Literal[
    "count", "mean", "median", "min", "max", "sample_stddev",
    "nearest_rank_percentile",
  ]
  percentile: str | None = Field(
    default=None,
    pattern=r"^(0(\.\d{1,6})?|1(\.0{1,6})?)$",
  )

  @model_validator(mode="after")
  def validate_percentile(self) -> "EvaluationAggregate":
    if self.function == "nearest_rank_percentile":
      if self.percentile is None or self.percentile in {"0", "0.0", "0.00", "0.000", "0.0000", "0.00000", "0.000000"}:
        raise ValueError("最近秩分位数必须提供大于 0 且不超过 1 的 percentile")
    elif self.percentile is not None:
      raise ValueError("仅最近秩分位数可以设置 percentile")
    return self


class EvaluationFilter(StrictModel):
  type: Literal["metric_comparison_v1"] = "metric_comparison_v1"
  metricId: str = Field(min_length=1, max_length=160)
  operator: Literal[">", ">=", "<", "<=", "=", "!=", "is_null", "is_not_null"]
  value: Any | None = None

  @model_validator(mode="after")
  def validate_value(self) -> "EvaluationFilter":
    if self.operator not in {"is_null", "is_not_null"} and self.value is None:
      raise ValueError("非空值过滤必须提供 value")
    if self.operator in {"is_null", "is_not_null"} and self.value is not None:
      raise ValueError("空值过滤不能提供 value")
    return self


class EvaluationGroupFilter(EvaluationFilter):
  type: Literal["group_metric_comparison_v1"] = "group_metric_comparison_v1"


class EvaluationSort(StrictModel):
  type: Literal["metric_sort_v1"] = "metric_sort_v1"
  metricId: str = Field(min_length=1, max_length=160)
  direction: Literal["asc", "desc"]


class ParameterCurveVisualization(StrictModel):
  type: Literal["parameter_curve_v1"] = "parameter_curve_v1"
  parameterDimensionId: str = Field(min_length=1, max_length=160)
  metricId: str = Field(min_length=1, max_length=160)
  seriesBy: list[str] = Field(default_factory=list, max_length=8)


class ParameterHeatmapVisualization(StrictModel):
  type: Literal["parameter_heatmap_v1"] = "parameter_heatmap_v1"
  xDimensionId: str = Field(min_length=1, max_length=160)
  yDimensionId: str = Field(min_length=1, max_length=160)
  metricId: str = Field(min_length=1, max_length=160)
  facetBy: list[str] = Field(default_factory=list, max_length=8)

  @model_validator(mode="after")
  def validate_axes(self) -> "ParameterHeatmapVisualization":
    if self.xDimensionId == self.yDimensionId:
      raise ValueError("热力图横纵轴必须使用不同参数")
    return self


class CostStressVisualization(StrictModel):
  type: Literal["cost_stress_v1"] = "cost_stress_v1"
  metricId: str = Field(min_length=1, max_length=160)
  baselineScenarioId: str = Field(min_length=1, max_length=64)
  degradationThreshold: str | None = Field(
    default=None,
    pattern=r"^(0(\.\d{1,6})?|1(\.0{1,6})?)$",
  )


class TimeStabilityVisualization(StrictModel):
  type: Literal["time_stability_v1"] = "time_stability_v1"
  metricId: str = Field(min_length=1, max_length=160)


EvaluationVisualization = (
  ParameterCurveVisualization
  | ParameterHeatmapVisualization
  | CostStressVisualization
  | TimeStabilityVisualization
)


class EvaluationSpecRequest(StrictModel):
  schemaVersion: Literal["evaluation_spec_v1"]
  name: str = Field(min_length=1, max_length=128)
  source: EvaluationSource
  catalogs: EvaluationCatalogs
  completeness: EvaluationCompleteness = Field(default_factory=EvaluationCompleteness)
  columns: list[str] = Field(min_length=1, max_length=64)
  derivedMetrics: list[EvaluationFormula] = Field(default_factory=list, max_length=32)
  filters: list[EvaluationFilter] = Field(default_factory=list, max_length=32)
  groupBy: list[str] = Field(default_factory=list, max_length=8)
  aggregates: list[EvaluationAggregate] = Field(default_factory=list, max_length=32)
  groupFilters: list[EvaluationGroupFilter] = Field(default_factory=list, max_length=32)
  sort: list[EvaluationSort] = Field(default_factory=list, max_length=8)
  visualizations: list[EvaluationVisualization] = Field(default_factory=list, max_length=16)
  reportSections: list[
    Literal["completeness", "ranking", "groups", "analysis", "exceptions", "dataset_notice"]
  ] = Field(default_factory=list, max_length=6)

  @field_validator("name")
  @classmethod
  def normalize_evaluation_name(cls, value: str) -> str:
    normalized = value.strip()
    if not normalized:
      raise ValueError("评价名称不能为空")
    return normalized

  @field_validator("columns")
  @classmethod
  def unique_columns(cls, value: list[str]) -> list[str]:
    if any(not item or len(item) > 160 for item in value):
      raise ValueError("展示字段 ID 无效")
    if len(set(value)) != len(value):
      raise ValueError("展示字段不能重复")
    return value

  @field_validator("filters")
  @classmethod
  def unique_filters(cls, value: list[EvaluationFilter]) -> list[EvaluationFilter]:
    identities = [(item.metricId, item.operator) for item in value]
    if len(set(identities)) != len(identities):
      raise ValueError("相同指标与操作符的过滤条件不能重复")
    return value

  @field_validator("derivedMetrics")
  @classmethod
  def unique_formula_metrics(
    cls,
    value: list[EvaluationFormula],
  ) -> list[EvaluationFormula]:
    identities = [item.metricId for item in value]
    if len(set(identities)) != len(identities):
      raise ValueError("派生指标 ID 不能重复")
    return value

  @field_validator("groupBy")
  @classmethod
  def unique_group_dimensions(cls, value: list[str]) -> list[str]:
    if len(set(value)) != len(value):
      raise ValueError("分组维度不能重复")
    return value

  @field_validator("aggregates")
  @classmethod
  def unique_aggregate_ids(
    cls,
    value: list[EvaluationAggregate],
  ) -> list[EvaluationAggregate]:
    identities = [item.aggregateId for item in value]
    if len(set(identities)) != len(identities):
      raise ValueError("聚合指标 ID 不能重复")
    return value

  @field_validator("sort")
  @classmethod
  def unique_sort_metrics(cls, value: list[EvaluationSort]) -> list[EvaluationSort]:
    identities = [item.metricId for item in value]
    if len(set(identities)) != len(identities):
      raise ValueError("排序指标不能重复")
    return value


class EvaluationRunCreateRequest(StrictModel):
  evaluationSpecId: UUID


class EvaluationExportCreateRequest(StrictModel):
  format: Literal["csv", "xlsx", "report"]
  columns: list[str] = Field(min_length=1, max_length=64)
  locale: Literal["zh-CN", "en-US"] = "zh-CN"

  @field_validator("columns")
  @classmethod
  def validate_export_columns(cls, value: list[str]) -> list[str]:
    if any(not item or len(item) > 160 for item in value):
      raise ValueError("导出字段 ID 无效")
    if len(set(value)) != len(value):
      raise ValueError("导出字段不能重复")
    return value
