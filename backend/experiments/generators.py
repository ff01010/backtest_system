"""`finite_grid_v1` 规范化与确定性 Trial 生成 Adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
from typing import Any, Callable, Iterable, Iterator

from backend.experiments.canonicalization import canonical_decimal, digest, with_version
from backend.experiments.contracts import FiniteGridStage
from backend.experiments.errors import ExperimentError
from backend.experiments.resolvers import ResolvedRunTemplate


MAX_PLANNED_TRIAL_COUNT = 2**63 - 1


@dataclass(frozen=True)
class GeneratedGrid:
  normalized_stage: dict[str, Any]
  dimensions: tuple[tuple[str, tuple[Any, ...]], ...]
  dimension_counts: dict[str, int]
  dimension_normalization: dict[str, dict[str, int]]
  planned_trial_count: int
  stage_input_fingerprint: str

  def assignments(self) -> Iterator[dict[str, Any]]:
    for trial_index in range(self.planned_trial_count):
      yield self.assignment_at(trial_index)

  def assignment_at(self, trial_index: int) -> dict[str, Any]:
    if not 0 <= trial_index < self.planned_trial_count:
      raise IndexError(trial_index)
    remainder = trial_index
    selected: list[tuple[str, Any]] = []
    for path, values in reversed(self.dimensions):
      remainder, value_index = divmod(remainder, len(values))
      selected.append((path, values[value_index]))
    assignment: dict[str, Any] = {"strategyParameters": {}}
    for path, value in reversed(selected):
      if path.startswith("strategyParameters."):
        assignment["strategyParameters"][path.removeprefix("strategyParameters.")] = value
      else:
        assignment[path] = value
    return assignment

  def trial_fingerprint(
    self,
    trial_index: int,
    assignment: dict[str, Any],
    compiler_contract_version: str = "backtest_run_request_v1",
  ) -> str:
    return digest(with_version({
      "stageInputFingerprint": self.stage_input_fingerprint,
      "trialIndex": trial_index,
      "dimensionAssignment": assignment,
      "trialCompilerContractVersion": compiler_contract_version,
      "generationProvenance": {"stageType": "finite_grid_v1", "generatorVersion": "1"},
    }))


class FiniteGridGenerator:
  stage_type = "finite_grid_v1"
  generator_version = "1"

  def normalize(
    self,
    stage: FiniteGridStage,
    template: ResolvedRunTemplate,
  ) -> GeneratedGrid:
    catalog = {item["inputId"]: item for item in template.tunable_inputs}
    parameter_dimensions = self._parameter_dimensions(stage, catalog)
    date_scenarios = _unique_scenarios(
      [item.model_dump() for item in stage.dateScenarios], "日期场景",
    )
    capitals = _unique_values(
      [_positive_decimal(value, "初始资金") for value in stage.capitalScenarios],
    )
    costs = self._cost_scenarios(stage)
    executions = self._execution_scenarios(stage)
    dimensions = (*parameter_dimensions,
      ("dateScenario", tuple(date_scenarios)),
      ("initialCapital", tuple(capitals)),
      ("costScenario", tuple(costs)),
      ("executionScenario", tuple(executions)),
    )
    count = _safe_product(len(values) for _, values in dimensions)
    normalized_stage = {
      "stageType": self.stage_type,
      "generatorVersion": self.generator_version,
      "parameterGrid": [
        {"inputId": path, "values": list(values)}
        for path, values in parameter_dimensions
      ],
      "dateScenarios": date_scenarios,
      "capitalScenarios": capitals,
      "costScenarios": costs,
      "executionScenarios": executions,
    }
    stage_fingerprint = digest(with_version({
      "stageSpec": normalized_stage,
      "generatorType": self.stage_type,
      "generatorVersion": self.generator_version,
      "runTemplateDigest": template.digest,
      "upstreamArtifacts": [],
    }))
    return GeneratedGrid(
      normalized_stage=normalized_stage,
      dimensions=dimensions,
      dimension_counts={path: len(values) for path, values in dimensions},
      dimension_normalization=self._dimension_normalization(stage, dimensions),
      planned_trial_count=count,
      stage_input_fingerprint=stage_fingerprint,
    )

  def _dimension_normalization(
    self,
    stage: FiniteGridStage,
    dimensions: tuple[tuple[str, tuple[Any, ...]], ...],
  ) -> dict[str, dict[str, int]]:
    submitted = {
      **{item.inputId: len(item.values) for item in stage.parameterGrid},
      "dateScenario": len(stage.dateScenarios),
      "initialCapital": len(stage.capitalScenarios),
      "costScenario": len(stage.costScenarios),
      "executionScenario": len(stage.executionScenarios),
    }
    return {
      path: {"submitted": submitted[path], "normalized": len(values)}
      for path, values in dimensions
    }

  def _parameter_dimensions(
    self,
    stage: FiniteGridStage,
    catalog: dict[str, dict[str, Any]],
  ) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    seen: set[str] = set()
    dimensions: list[tuple[str, tuple[Any, ...]]] = []
    for item in sorted(stage.parameterGrid, key=lambda value: value.inputId):
      if item.inputId in seen:
        raise _invalid("同一策略参数只能配置一个候选值列表", item.inputId)
      definition = catalog.get(item.inputId)
      if definition is None:
        raise _invalid("存在未声明的策略参数", item.inputId)
      seen.add(item.inputId)
      parser = _parameter_parser(definition)
      dimensions.append((item.inputId, tuple(_unique_values([parser(value) for value in item.values]))))
    return tuple(dimensions)

  def _cost_scenarios(self, stage: FiniteGridStage) -> list[dict[str, Any]]:
    costs = [{
      "scenarioId": item.scenarioId,
      "slippage": _nonnegative_decimal(item.slippage, "滑点"),
    } for item in stage.costScenarios]
    return _unique_scenarios(costs, "成本场景")

  def _execution_scenarios(self, stage: FiniteGridStage) -> list[dict[str, Any]]:
    executions: list[dict[str, Any]] = []
    for item in stage.executionScenarios:
      if item.algorithm != "immediate" or item.parameters:
        raise _invalid("标准 Bar 只支持无参数的 immediate 执行算法", item.scenarioId)
      executions.append({
        "scenarioId": item.scenarioId,
        "algorithm": "immediate",
        "parameters": {},
      })
    return _unique_scenarios(executions, "执行场景")


def _parameter_parser(definition: dict[str, Any]) -> Callable[[Any], Any]:
  parameter_type = definition["type"]
  if parameter_type == "boolean":
    base_parser = _boolean
  elif parameter_type == "integer":
    base_parser = _integer
  elif parameter_type == "number":
    base_parser = lambda value: _decimal(value, "策略参数")
  elif parameter_type == "string":
    base_parser = _string
  else:
    raise _invalid("策略参数声明类型不受支持", str(parameter_type))

  def parse(value: Any) -> Any:
    parsed = base_parser(value)
    _validate_parameter_constraints(parsed, definition, base_parser)
    return parsed

  return parse


def _validate_parameter_constraints(
  value: Any,
  definition: dict[str, Any],
  parser: Callable[[Any], Any],
) -> None:
  options = definition.get("options")
  if isinstance(options, list):
    normalized_options = [parser(item) for item in options]
    if all(digest(value) != digest(item) for item in normalized_options):
      raise _invalid("策略参数候选值不在允许列表中")
  if isinstance(value, str) and definition.get("type") == "string":
    if "minLength" in definition and len(value) < int(definition["minLength"]):
      raise _invalid("文本候选值短于声明的最小长度")
    if "maxLength" in definition and len(value) > int(definition["maxLength"]):
      raise _invalid("文本候选值超过声明的最大长度")
    return
  if definition.get("type") not in {"integer", "number"}:
    return
  numeric = Decimal(str(value))
  if "min" in definition and numeric < Decimal(str(definition["min"])):
    raise _invalid("策略参数候选值小于声明的最小值")
  if "max" in definition and numeric > Decimal(str(definition["max"])):
    raise _invalid("策略参数候选值大于声明的最大值")


def _boolean(value: Any) -> bool:
  if isinstance(value, bool):
    return value
  if isinstance(value, str) and value in {"true", "false"}:
    return value == "true"
  raise _invalid("布尔候选值必须为 true 或 false")


def _integer(value: Any) -> int:
  if isinstance(value, bool) or isinstance(value, float):
    raise _invalid("整数候选值格式无效")
  try:
    parsed = int(str(value), 10)
  except (TypeError, ValueError) as error:
    raise _invalid("整数候选值格式无效") from error
  if str(value).strip() not in {str(parsed), f"+{parsed}"}:
    raise _invalid("整数候选值格式无效")
  return parsed


def _string(value: Any) -> str:
  if not isinstance(value, str):
    raise _invalid("文本候选值必须是字符串")
  if len(value) > 1024:
    raise _invalid("文本候选值不能超过 1024 个字符")
  return value


def _decimal(value: Any, label: str) -> str:
  if isinstance(value, (bool, float)):
    raise _invalid(f"{label}必须使用十进制定点字符串")
  try:
    parsed = Decimal(str(value))
  except (InvalidOperation, ValueError) as error:
    raise _invalid(f"{label}格式无效") from error
  if not parsed.is_finite():
    raise _invalid(f"{label}必须是有限值")
  return canonical_decimal(parsed)


def _positive_decimal(value: Any, label: str) -> str:
  normalized = _decimal(value, label)
  if Decimal(normalized) <= 0:
    raise _invalid(f"{label}必须大于 0")
  return normalized


def _nonnegative_decimal(value: Any, label: str) -> str:
  normalized = _decimal(value, label)
  if Decimal(normalized) < 0:
    raise _invalid(f"{label}不能小于 0")
  if Decimal(normalized) >= 1:
    raise _invalid(f"{label}必须小于 1")
  return normalized


def _unique_values(values: list[Any]) -> list[Any]:
  seen: set[str] = set()
  unique: list[Any] = []
  for value in values:
    key = digest(value)
    if key in seen:
      continue
    seen.add(key)
    unique.append(value)
  return unique


def _unique_scenarios(values: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
  scenario_ids: set[str] = set()
  unique: list[dict[str, Any]] = []
  seen: set[str] = set()
  for value in values:
    scenario_id = str(value["scenarioId"])
    if scenario_id in scenario_ids:
      raise _invalid(f"{label} ID 不能重复", scenario_id)
    scenario_ids.add(scenario_id)
    key = digest(value)
    if key not in seen:
      unique.append(value)
      seen.add(key)
  return unique


def _safe_product(counts: Iterable[int]) -> int:
  result = 1
  for count in counts:
    result *= int(count)
    if result > MAX_PLANNED_TRIAL_COUNT:
      raise _invalid("组合数量溢出，请缩小搜索空间")
  return result


def seed_bundle(trial_fingerprint: str) -> dict[str, Any]:
  payload = f"trial-seed-v1{trial_fingerprint}".encode("utf-8")
  base_seed = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=False)
  return {"derivationVersion": "trial-seed-v1", "baseSeed": base_seed}


def initial_collection_digest() -> str:
  return digest(with_version({"trialFingerprints": []}))


def extend_collection_digest(current: str, trial_fingerprint: str) -> str:
  return digest(with_version({
    "previousDigest": current,
    "trialFingerprint": trial_fingerprint,
  }))


def _invalid(message: str, field: str | None = None) -> ExperimentError:
  return ExperimentError(
    "experiment.validation_failed",
    message,
    details={"field": field} if field else {},
  )
