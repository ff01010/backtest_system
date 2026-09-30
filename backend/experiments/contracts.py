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


class EvaluationCompleteness(StrictModel):
  allowPartial: Literal[False] = False
  minimumCompletionRate: Literal["1.0000"] = "1.0000"
  requiredDimensionCoverage: list[Any] = Field(default_factory=list, max_length=0)


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


class EvaluationSort(StrictModel):
  type: Literal["metric_sort_v1"] = "metric_sort_v1"
  metricId: str = Field(min_length=1, max_length=160)
  direction: Literal["asc", "desc"]


class EvaluationSpecRequest(StrictModel):
  schemaVersion: Literal["evaluation_spec_v1"]
  name: str = Field(min_length=1, max_length=128)
  source: EvaluationSource
  catalogs: EvaluationCatalogs
  completeness: EvaluationCompleteness = Field(default_factory=EvaluationCompleteness)
  columns: list[str] = Field(min_length=1, max_length=64)
  derivedMetrics: list[Any] = Field(default_factory=list, max_length=0)
  filters: list[EvaluationFilter] = Field(default_factory=list, max_length=32)
  groupBy: list[Any] = Field(default_factory=list, max_length=0)
  aggregates: list[Any] = Field(default_factory=list, max_length=0)
  groupFilters: list[Any] = Field(default_factory=list, max_length=0)
  sort: list[EvaluationSort] = Field(default_factory=list, max_length=8)
  visualizations: list[Any] = Field(default_factory=list, max_length=0)
  reportSections: list[Any] = Field(default_factory=list, max_length=0)

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

  @field_validator("sort")
  @classmethod
  def unique_sort_metrics(cls, value: list[EvaluationSort]) -> list[EvaluationSort]:
    identities = [item.metricId for item in value]
    if len(set(identities)) != len(identities):
      raise ValueError("排序指标不能重复")
    return value


class EvaluationRunCreateRequest(StrictModel):
  evaluationSpecId: UUID
