"""首期实验对象与标准 Bar 数据集解析 Adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from backend import settings
from backend.experiments.canonicalization import canonical_decimal, digest, with_version
from backend.experiments.contracts import DateScenario, SubjectRef
from backend.experiments.errors import ExperimentError
from backend.services import market_data, research_signal_snapshot, state_store
from backend.strategies.scripted import strategy_parameters_for_script


@dataclass(frozen=True)
class ResolvedRunTemplate:
  snapshot: dict[str, Any]
  digest: str
  tunable_inputs: tuple[dict[str, Any], ...]
  defaults: dict[str, Any]


class RunTemplateResolver(Protocol):
  def resolve(self, subject_ref: SubjectRef) -> ResolvedRunTemplate:
    """解析当前用户可运行的不可变实验对象。"""


class DatasetSnapshotResolver(Protocol):
  def resolve(
    self,
    template: ResolvedRunTemplate,
    scenarios: tuple[DateScenario, ...],
  ) -> tuple[dict[str, Any], ...]:
    """冻结全部日期场景的数据查询定义。"""


class LegacySingleStrategyResolver:
  template_type = "legacy_single_strategy_v1"
  schema_version = "run_template_v1"

  def resolve(self, subject_ref: SubjectRef) -> ResolvedRunTemplate:
    config, version = self._load_subject(subject_ref)
    params_json = dict(version.get("paramsJson") or {})
    params = dict(params_json.get("params") or {})
    script = dict(params_json.get("scriptSnapshot") or {})
    definitions = self._parameter_definitions(params_json, script)
    saved_parameters = self._saved_parameters(params_json, params, definitions)
    fee_profile = _fee_profile_snapshot()
    account = self._account_snapshot(version.get("accountId"), params)
    fixed = self._fixed_structure(
      subject_ref,
      version,
      params,
      script,
      account,
      fee_profile,
      params_json.get("scriptCodeHash"),
    )
    execution_snapshot = self._execution_snapshot(
      version, params, script, saved_parameters, fixed,
    )
    inputs = tuple(
      {
        "inputId": f"strategyParameters.{name}",
        "sourceName": name,
        "type": definition["type"],
        "group": "strategyParameters",
        "defaultValue": saved_parameters[name],
        "overridePolicy": "finite_candidates",
        **_parameter_constraints(definition),
      }
      for name, definition in definitions.items()
    )
    snapshot = with_version({
      "templateType": self.template_type,
      "templateSchemaVersion": self.schema_version,
      "subjectRef": subject_ref.model_dump(),
      "runPath": "strategy",
      "scopeType": "single",
      "fixedStructure": fixed,
      "executionSnapshot": execution_snapshot,
      "tunableInputCatalog": {
        "schemaVersion": "tunable_input_catalog_v1",
        "inputs": list(inputs),
        "scenarioEditors": _scenario_editors(),
      },
      "assetManifestSnapshot": _asset_manifest(subject_ref, fixed),
      "contracts": {
        "submission": "backtest_run_request_v1",
        "execution": "bar_execution_v1",
        "resultSchema": "single_bar_result_v1",
        "trialCompiler": "legacy_single_bar_v1",
        "resourceEstimator": "single_bar_estimator_v1",
      },
    })
    return ResolvedRunTemplate(snapshot, digest(snapshot), inputs, {
      "strategyParameters": saved_parameters,
      "dateScenario": _default_date_scenario(params),
      "initialCapital": _decimal_text(params.get("initialCapital"), "初始资金"),
      "slippage": _decimal_text(params.get("slippage", 0), "滑点"),
    })

  def _execution_snapshot(
    self,
    version: dict[str, Any],
    params: dict[str, Any],
    script: dict[str, Any],
    saved_parameters: dict[str, Any],
    fixed: dict[str, Any],
  ) -> dict[str, Any]:
    code = str(script.get("code") or "")
    if not code:
      raise ExperimentError("experiment.preview_stale", "发布版本缺少策略代码", 409)
    return {
      "script": {
        "id": script["id"],
        "versionId": script["versionId"],
        "name": str(script.get("name") or version["name"]),
        "description": str(script.get("description") or ""),
        "code": code,
        "entryType": str(script.get("entryType") or "on_bar"),
        "signalRequired": bool(script.get("signalRequired", False)),
        "codeHash": fixed["scriptCodeDigest"],
        "runtimeVersion": fixed["runtimeVersion"],
        "capabilityDigest": fixed["runtimeCapabilityDigest"],
      },
      "publishedConfig": {
        "name": version["name"],
        "strategy": version["strategy"],
        "mode": version["mode"],
        "symbol": version["symbol"],
        "timeframe": version["timeframe"],
        "benchmarkSymbol": version.get("benchmarkSymbol"),
        "accountId": version.get("accountId"),
        "params": _identity_value(params),
        "savedStrategyParams": saved_parameters,
        "sharedSource": version.get("sharedSource"),
      },
    }

  def _load_subject(self, subject_ref: SubjectRef) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
      config = state_store.get_strategy_config(subject_ref.subjectId, include_archived=True)
      version = state_store.get_strategy_config_version_for_config(
        subject_ref.subjectId, subject_ref.subjectVersion,
      )
    except KeyError as error:
      raise ExperimentError("experiment.not_found", "策略版本不存在", 404) from error
    if (
      config.get("archivedAt")
      or not config.get("enabled")
      or not config.get("published")
      or config.get("publishedVersionId") != subject_ref.subjectVersion
      or version.get("mode") != "backtest"
    ):
      raise ExperimentError("experiment.forbidden", "当前策略版本不可用于自动化回测", 403)
    return config, version

  def _parameter_definitions(
    self,
    params_json: dict[str, Any],
    script: dict[str, Any],
  ) -> dict[str, dict[str, Any]]:
    definitions = params_json.get("strategyParameterDefinitions")
    if not isinstance(definitions, dict):
      definitions = strategy_parameters_for_script(str(script.get("code") or ""))
    return {
      str(name): dict(definition)
      for name, definition in definitions.items()
      if isinstance(definition, dict)
    }

  def _saved_parameters(
    self,
    params_json: dict[str, Any],
    params: dict[str, Any],
    definitions: dict[str, dict[str, Any]],
  ) -> dict[str, Any]:
    saved = params_json.get("strategyParams")
    source = saved if isinstance(saved, dict) else params
    return {
      name: _saved_parameter_value(
        source.get(name, definition.get("default")), definition, name,
      )
      for name, definition in definitions.items()
    }

  def _account_snapshot(
    self,
    account_id: Any,
    params: dict[str, Any],
  ) -> dict[str, Any]:
    if account_id is None:
      return {
        "accountId": None,
        "positionMode": params.get("positionMode", "full_available"),
        "source": "published_strategy_snapshot",
      }
    try:
      account = state_store.get_account(int(account_id))
    except (KeyError, TypeError, ValueError) as error:
      raise ExperimentError("experiment.preview_stale", "策略绑定的回测账户已不可用", 409) from error
    return {
      "accountId": account["id"],
      "positionMode": account["positionMode"],
      "accountDigest": digest(with_version({
        "id": account["id"],
        "positionMode": account["positionMode"],
        "orderCash": _optional_decimal_text(account.get("orderCash"), "账户下单金额"),
        "orderPct": _optional_decimal_text(account.get("orderPct"), "账户下单比例"),
        "allowShort": account.get("allowShort"),
      })),
      "source": "backtest_account",
    }

  def _fixed_structure(
    self,
    subject_ref: SubjectRef,
    version: dict[str, Any],
    params: dict[str, Any],
    script: dict[str, Any],
    account: dict[str, Any],
    fee_profile: dict[str, Any],
    saved_script_code_digest: Any,
  ) -> dict[str, Any]:
    fill_model = str(params.get("fillModel", "bar_next_open"))
    if fill_model != "bar_next_open":
      raise ExperimentError(
        "experiment.capacity_unavailable", "当前 ticket 仅开放标准 Bar 自动化预览", 503,
      )
    if not script.get("id") or not script.get("versionId"):
      raise ExperimentError("experiment.preview_stale", "发布版本缺少不可变策略代码身份", 409)
    script_code_digest = script.get("codeHash") or saved_script_code_digest
    if not script_code_digest:
      raise ExperimentError("experiment.preview_stale", "发布版本缺少策略代码摘要", 409)
    signal_required = bool(script.get("signalRequired", False))
    if signal_required and not params.get("signalDatasetId"):
      raise ExperimentError("experiment.preview_stale", "发布版本缺少必需的研究信号数据集", 409)
    signal_asset = _signal_asset_snapshot(params.get("signalDatasetId"))
    return {
      "configId": subject_ref.subjectId,
      "configVersionId": subject_ref.subjectVersion,
      "strategyName": version["name"],
      "scriptId": script["id"],
      "strategyVersionId": script["versionId"],
      "scriptCodeDigest": script_code_digest,
      "signalRequired": signal_required,
      "runtimeVersion": script.get("runtimeVersion"),
      "runtimeCapabilityDigest": script.get("capabilityDigest"),
      "symbol": version["symbol"],
      "benchmarkSymbol": version.get("benchmarkSymbol") or version["symbol"],
      "timeframe": version["timeframe"],
      "mode": "backtest",
      "account": account,
      "strategy": version["strategy"],
      "strategyCategory": params.get("strategyCategory", "directional_holding"),
      "holdingPolicy": params.get("holdingPolicy", "overnight"),
      "overlaySettlementPolicy": params.get("overlaySettlementPolicy", "intraday_restore"),
      "fillModel": fill_model,
      "allowLong": _saved_bool(params.get("allowLong", True), "允许做多"),
      "allowShort": _saved_bool(params.get("allowShort", True), "允许做空"),
      "orderCash": _optional_decimal_text(params.get("orderCash"), "下单金额"),
      "orderPct": _optional_decimal_text(params.get("orderPct"), "下单比例"),
      "initialPositionEnabled": _saved_bool(
        params.get("initialPositionEnabled", False), "启用初始持仓",
      ),
      "baseQuantity": _nonnegative_int(params.get("baseQuantity", 0), "底仓数量"),
      "baseCostPrice": _decimal_text(params.get("baseCostPrice", 0), "底仓成本价"),
      "t0Enabled": _saved_bool(params.get("t0Enabled", False), "启用日内增强"),
      "warmupTradingDays": _nonnegative_int(
        params.get("warmupTradingDays", 0), "预热交易日",
      ),
      "signalDatasetId": params.get("signalDatasetId"),
      "signalAssetSnapshot": signal_asset,
      "feeProfile": fee_profile,
    }


class QmtBarDatasetSnapshotResolver:
  adapter_type = "qmt_bar_query_definition_v1"

  def resolve(
    self,
    template: ResolvedRunTemplate,
    scenarios: tuple[DateScenario, ...],
  ) -> tuple[dict[str, Any], ...]:
    fixed = template.snapshot["fixedStructure"]
    snapshots: list[dict[str, Any]] = []
    for scenario in scenarios:
      try:
        resolution = market_data.resolve_backtest_data(
          str(fixed["symbol"]),
          str(fixed["timeframe"]),
          scenario.startDate,
          scenario.endDate,
          warmup_trading_days=int(fixed.get("warmupTradingDays") or 0),
        )
      except market_data.MarketDataError as error:
        raise ExperimentError(
          "experiment.validation_failed",
          str(error),
          details={"field": "dateScenarios", "reason": error.reason},
        ) from error
      source = market_data.snapshot_for_resolution(str(fixed["symbol"]), resolution)
      snapshot = with_version({
        "datasetSnapshotSchemaVersion": "dataset_snapshot_v1",
        "resolverType": self.adapter_type,
        "scenario": scenario.model_dump(),
        "symbol": fixed["symbol"],
        "timeframe": fixed["timeframe"],
        "adjustmentMode": source.get("adjustment", "none"),
        "formalDatePolicy": "bar_coverage_v1",
        "warmupPolicy": "preceding_complete_bar_dates_v1",
        "provider": "qmt_kline",
        "sourceDatabase": source["sourceDatabase"],
        "sourceTable": source["sourceTable"],
        "sourcePeriod": source["sourcePeriod"],
        "derivationRuleVersion": source.get("derivation"),
        "sourceFirstDate": resolution.all_dates[0],
        "sourceLastDate": resolution.formal_dates[-1],
        "formalDateCount": len(resolution.formal_dates),
        "formalDates": list(resolution.formal_dates),
        "formalDatesDigest": digest(list(resolution.formal_dates)),
        "warmupDateCount": len(resolution.warmup_dates),
        "warmupDates": list(resolution.warmup_dates),
        "releaseMetadataDigest": source.get("hash") or digest({
          "formalDates": list(resolution.formal_dates),
          "source": source["sourceTable"],
        }),
        "datasetFreezeLevel": "query_definition_frozen",
      })
      snapshots.append({**snapshot, "contentDigest": digest(snapshot)})
    return tuple(snapshots)


def _decimal_text(value: Any, label: str) -> str:
  try:
    parsed = Decimal(str(value))
  except Exception as error:
    raise ExperimentError("experiment.preview_stale", f"发布版本的{label}无效", 409) from error
  if not parsed.is_finite():
    raise ExperimentError("experiment.preview_stale", f"发布版本的{label}无效", 409)
  return canonical_decimal(parsed)


def _default_date_scenario(params: dict[str, Any]) -> dict[str, Any]:
  try:
    return DateScenario(
      scenarioId="development-1",
      name="默认回测区间",
      role="development",
      startDate=params.get("startDate"),
      endDate=params.get("endDate"),
    ).model_dump()
  except ValueError as error:
    raise ExperimentError("experiment.preview_stale", "发布版本的回测日期无效", 409) from error


def _optional_decimal_text(value: Any, label: str) -> str | None:
  if value is None:
    return None
  return _decimal_text(value, label)


def _nonnegative_int(value: Any, label: str) -> int:
  try:
    parsed = int(value)
    if isinstance(value, bool) or Decimal(str(value)) != parsed or parsed < 0:
      raise ValueError
    return parsed
  except (TypeError, ValueError, ArithmeticError) as error:
    raise ExperimentError("experiment.preview_stale", f"发布版本的{label}无效", 409) from error


def _saved_bool(value: Any, label: str) -> bool:
  if isinstance(value, bool):
    return value
  raise ExperimentError("experiment.preview_stale", f"发布版本的{label}无效", 409)


def _identity_value(value: Any) -> Any:
  if isinstance(value, float):
    return canonical_decimal(Decimal(str(value)))
  if isinstance(value, dict):
    return {key: _identity_value(item) for key, item in value.items()}
  if isinstance(value, list):
    return [_identity_value(item) for item in value]
  return value


def _saved_parameter_value(
  value: Any,
  definition: dict[str, Any],
  name: str,
) -> Any:
  parameter_type = definition.get("type")
  try:
    if parameter_type == "number":
      return _decimal_text(value, f"策略参数 {name}")
    if parameter_type == "integer" and not isinstance(value, bool):
      parsed = int(value)
      if Decimal(str(value)) == parsed:
        return parsed
    if parameter_type == "boolean" and isinstance(value, bool):
      return value
    if parameter_type == "string" and isinstance(value, str):
      return value
  except (TypeError, ValueError, ArithmeticError) as error:
    raise ExperimentError(
      "experiment.preview_stale", f"发布版本的策略参数 {name} 无效", 409,
    ) from error
  raise ExperimentError("experiment.preview_stale", f"发布版本的策略参数 {name} 无效", 409)


def _fee_profile_snapshot() -> dict[str, Any]:
  rates = {
    key: _decimal_text(value, key)
    for key, value in settings.default_fee_profile().items()
  }
  content_digest = digest(with_version({"rates": rates}))
  return {
    "profileId": "default_a_share",
    "version": f"fee_profile_v1:{content_digest[:16]}",
    "rates": rates,
    "contentDigest": content_digest,
  }


def _asset_manifest(subject_ref: SubjectRef, fixed: dict[str, Any]) -> dict[str, Any]:
  assets = [
    {
      "assetType": "strategy_config_version",
      "assetId": subject_ref.subjectId,
      "exactVersion": subject_ref.subjectVersion,
      "contentDigest": digest(fixed),
    },
    {
      "assetType": "strategy_script_version",
      "assetId": fixed["scriptId"],
      "exactVersion": fixed["strategyVersionId"],
      "contentDigest": fixed["scriptCodeDigest"],
    },
    {
      "assetType": "fee_profile",
      "assetId": fixed["feeProfile"]["profileId"],
      "exactVersion": fixed["feeProfile"]["version"],
      "contentDigest": fixed["feeProfile"]["contentDigest"],
    },
  ]
  signal_asset = fixed.get("signalAssetSnapshot")
  if isinstance(signal_asset, dict):
    assets.append(signal_asset)
  return {"schemaVersion": "asset_manifest_v1", "assets": assets}


def _signal_asset_snapshot(dataset_id: Any) -> dict[str, Any] | None:
  if not isinstance(dataset_id, str) or not dataset_id:
    return None
  imported = state_store.get_imported_research_signal_dataset(dataset_id)
  if imported:
    return {
      "assetType": "research_signal_dataset",
      "assetId": dataset_id,
      "exactVersion": str(imported["dataset_version"]),
      "contentDigest": str(imported["content_sha256"]),
      "sourceTimeframe": str(imported["timeframe"]),
      "sourceType": "imported_artifact",
    }
  try:
    datasets = research_signal_snapshot.list_published_signal_datasets(
      str(state_store.current_user_id()),
    )
  except research_signal_snapshot.ResearchSignalSnapshotError as error:
    raise ExperimentError(
      "experiment.capacity_unavailable",
      "研究信号资产暂时无法冻结",
      503,
      retryable=True,
    ) from error
  matched = next((item for item in datasets if item.get("id") == dataset_id), None)
  if matched is None:
    raise ExperimentError("experiment.preview_stale", "发布版本引用的研究信号已不可用", 409)
  identity = with_version({
    "datasetId": dataset_id,
    "version": matched.get("version"),
    "timeframe": matched.get("timeframe"),
    "recordCount": matched.get("recordCount"),
  })
  return {
    "assetType": "research_signal_dataset",
    "assetId": dataset_id,
    "exactVersion": str(matched.get("version") or "published"),
    "contentDigest": digest(identity),
    "sourceTimeframe": matched.get("timeframe"),
    "sourceType": "published_manifest",
  }


def _scenario_editors() -> list[dict[str, Any]]:
  return [
    {"inputId": "dateScenarios", "type": "date_scenario", "atomic": True},
    {"inputId": "capitalScenarios", "type": "decimal", "atomic": False},
    {"inputId": "costScenarios", "type": "slippage_scenario", "atomic": True},
    {"inputId": "executionScenarios", "type": "execution_scenario", "atomic": True},
  ]


def _parameter_constraints(definition: dict[str, Any]) -> dict[str, Any]:
  constraints: dict[str, Any] = {}
  for key in ("min", "max"):
    if key in definition:
      constraints[key] = _decimal_text(definition[key], f"参数约束 {key}")
  for key in ("minLength", "maxLength"):
    if key in definition:
      constraints[key] = _nonnegative_int(definition[key], f"参数约束 {key}")
  options = definition.get("options")
  if isinstance(options, list):
    constraints["options"] = [
      _saved_parameter_value(value, definition, "候选项")
      for value in options
    ]
  return constraints
