"""冻结模板到普通单次回测请求的 TrialCompiler Adapter。"""

from __future__ import annotations

from decimal import Decimal
import hashlib
from typing import Any, Protocol

from pydantic import ValidationError

from backend import schemas
from backend.experiments.canonicalization import canonical_decimal, digest, with_version
from backend.experiments.errors import ExperimentError
from backend.experiments.resolvers import ResolvedRunTemplate


class TrialCompiler(Protocol):
  contract_version: str

  def compile(self, template: ResolvedRunTemplate, assignment: dict[str, Any]) -> dict[str, Any]:
    """把冻结模板与维度赋值编译成普通回测请求摘要。"""

  def compile_request(
    self,
    template: ResolvedRunTemplate,
    assignment: dict[str, Any],
    dataset_snapshot: dict[str, Any],
    seed: dict[str, Any],
  ) -> dict[str, Any]:
    """把冻结输入编译成可直接进入普通任务链路的请求。"""


class LegacySingleBarTrialCompiler:
  contract_version = "backtest_run_request_v1"

  def compile(self, template: ResolvedRunTemplate, assignment: dict[str, Any]) -> dict[str, Any]:
    request = self._request_model(template, assignment)
    try:
      summary = with_version({
        "contractVersion": self.contract_version,
        "runTemplateDigest": template.digest,
        "request": _identity_safe_value(request.model_dump()),
      })
      request_digest = digest(summary)
    except (TypeError, ValueError) as error:
      raise ExperimentError(
        "experiment.validation_failed", "网格组合包含不可规范化的数值",
      ) from error
    return {"contractVersion": self.contract_version, "requestDigest": request_digest}

  def compile_request(
    self,
    template: ResolvedRunTemplate,
    assignment: dict[str, Any],
    dataset_snapshot: dict[str, Any],
    seed: dict[str, Any],
  ) -> dict[str, Any]:
    request = self._request_model(template, assignment).model_dump()
    execution = dict(template.snapshot.get("executionSnapshot") or {})
    script = dict(execution.get("script") or {})
    if not script.get("code"):
      raise ExperimentError(
        "experiment.execution_snapshot_invalid", "冻结策略代码快照不完整", 409,
      )
    actual_code_hash = hashlib.sha256(str(script["code"]).encode("utf-8")).hexdigest()
    fixed = template.snapshot["fixedStructure"]
    if actual_code_hash != fixed["scriptCodeDigest"]:
      raise ExperimentError(
        "experiment.execution_snapshot_invalid", "冻结策略代码摘要不一致", 409,
      )
    published = dict(execution.get("publishedConfig") or {})
    effective_parameters = {
      **template.defaults["strategyParameters"],
      **assignment["strategyParameters"],
    }
    request.update({
      "strategyVersionId": fixed["strategyVersionId"],
      "savedStrategyParams": template.defaults["strategyParameters"],
      "effectiveStrategyParams": effective_parameters,
      "feeProfile": _numeric_mapping(fixed["feeProfile"]["rates"]),
      "versionScriptSnapshot": script,
      "savedScriptSnapshot": script,
      "savedConfigSnapshot": {
        **published,
        "runParams": effective_parameters,
        "savedStrategyParams": template.defaults["strategyParameters"],
        "strategyParamOverrides": assignment["strategyParameters"],
        "effectiveStrategyParams": effective_parameters,
        "warmupTradingDays": fixed["warmupTradingDays"],
        "signalRequired": bool(fixed.get("signalRequired", False)),
        "runtimeVersion": fixed["runtimeVersion"],
        "capabilityDigest": fixed["runtimeCapabilityDigest"],
        "execution": assignment["executionScenario"],
      },
      "marketDataSnapshot": {
        "barSnapshot": _bar_snapshot(dataset_snapshot),
      },
      "seedBundle": seed,
    })
    return request

  def _request_model(
    self,
    template: ResolvedRunTemplate,
    assignment: dict[str, Any],
  ) -> schemas.BacktestRunRequest:
    fixed = template.snapshot["fixedStructure"]
    date_scenario = assignment["dateScenario"]
    cost_scenario = assignment["costScenario"]
    execution = assignment["executionScenario"]
    effective_parameters = {
      **template.defaults["strategyParameters"],
      **assignment["strategyParameters"],
    }
    try:
      return schemas.BacktestRunRequest(
        configId=fixed["configId"],
        configVersionId=fixed["configVersionId"],
        scriptId=fixed["scriptId"],
        accountId=fixed["account"].get("accountId"),
        mode="backtest",
        symbol=fixed["symbol"],
        benchmarkSymbol=fixed["benchmarkSymbol"],
        strategy=fixed["strategy"],
        signalDatasetId=fixed.get("signalDatasetId"),
        timeframe=fixed["timeframe"],
        startDate=date_scenario["startDate"],
        endDate=date_scenario["endDate"],
        initialCapital=float(Decimal(assignment["initialCapital"])),
        commissionRate=float(Decimal(fixed["feeProfile"]["rates"]["commissionRate"])),
        slippage=float(Decimal(cost_scenario["slippage"])),
        positionMode=fixed["account"].get("positionMode", "full_available"),
        orderCash=fixed.get("orderCash"),
        orderPct=fixed.get("orderPct"),
        allowLong=fixed["allowLong"],
        allowShort=fixed["allowShort"],
        strategyCategory=fixed["strategyCategory"],
        holdingPolicy=fixed["holdingPolicy"],
        overlaySettlementPolicy=fixed["overlaySettlementPolicy"],
        fillModel=fixed["fillModel"],
        executionAlgorithm=execution["algorithm"],
        executionParameters=execution["parameters"],
        baseQuantity=fixed["baseQuantity"],
        baseCostPrice=fixed["baseCostPrice"],
        initialPositionEnabled=fixed["initialPositionEnabled"],
        t0Enabled=fixed["t0Enabled"],
        warmupTradingDays=fixed["warmupTradingDays"],
        strategyParams=effective_parameters,
        strategyParamOverrides=assignment["strategyParameters"],
      )
    except (ValidationError, ValueError) as error:
      raise ExperimentError(
        "experiment.validation_failed", "网格组合不符合单次回测契约",
        details={"reason": str(error)},
      ) from error


def _numeric_mapping(value: dict[str, Any]) -> dict[str, float]:
  return {key: float(Decimal(str(item))) for key, item in value.items()}


def _bar_snapshot(dataset: dict[str, Any]) -> dict[str, Any]:
  required = (
    "provider", "sourceDatabase", "sourceTable", "sourcePeriod",
    "formalDates", "warmupDates",
  )
  if any(key not in dataset for key in required):
    raise ExperimentError(
      "experiment.execution_snapshot_invalid", "冻结行情快照不完整", 409,
    )
  return {
    "provider": dataset["provider"],
    "symbol": dataset["symbol"],
    "timeframe": dataset["timeframe"],
    "sourceDatabase": dataset["sourceDatabase"],
    "sourceTable": dataset["sourceTable"],
    "sourcePeriod": dataset["sourcePeriod"],
    "derivation": dataset.get("derivationRuleVersion"),
    "formalDates": list(dataset["formalDates"]),
    "warmupDates": list(dataset["warmupDates"]),
    "warmupTradingDays": len(dataset["warmupDates"]),
    "adjustment": dataset.get("adjustmentMode", "none"),
    "manifestHash": dataset.get("releaseMetadataDigest", ""),
    "hash": dataset["contentDigest"],
  }


def _identity_safe_value(value: Any) -> Any:
  """旧请求契约使用 float；实验身份只接收确定性的十进制文本。"""
  if isinstance(value, float):
    return canonical_decimal(Decimal(str(value)))
  if isinstance(value, dict):
    return {key: _identity_safe_value(item) for key, item in value.items()}
  if isinstance(value, list):
    return [_identity_safe_value(item) for item in value]
  return value
