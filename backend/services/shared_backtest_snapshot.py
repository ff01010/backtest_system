"""构建和读取共享回测快照的显式安全契约。"""

from __future__ import annotations

from typing import Any


INCOMPLETE_HISTORY_MESSAGE = "历史任务缺少可共享快照字段（{fields}），请重新运行该任务后共享"

STRATEGY_FIELDS = (
  "name", "description", "entryType", "code", "codeHash", "signalRequired",
  "runtimeVersion", "capabilityDigest",
)
CONFIGURATION_FIELDS = (
  "strategyName", "strategy", "strategyCategory", "symbol", "timeframe",
  "benchmarkSymbol", "startDate", "endDate", "params", "runParams", "execution",
  "initialCapital", "commissionRate", "slippage", "feeProfile", "allowLong",
  "allowShort", "holdingPolicy", "overlaySettlementPolicy", "fillModel",
  "executionAlgorithm", "executionParameters", "baseQuantity", "baseCostPrice",
  "initialPositionEnabled", "t0Enabled", "warmupTradingDays", "strategyParams",
  "savedStrategyParams", "strategyParamOverrides", "effectiveStrategyParams",
)
ORIGIN_FIELDS = ("publicationId", "authorName", "title")
MARKET_DATA_FIELDS = (
  "provider", "symbol", "timeframe", "sourcePeriod", "derivation",
  "warmupTradingDays", "barCount", "lastImportedAt", "adjustment",
  "manifestHash", "hash",
)
RESULT_DATA_FIELDS = (
  "provider", "symbol", "timeframe", "startDate", "endDate", "actualStartDate",
  "actualEndDate", "barCount", "sourcePeriod", "aggregationVersion",
  "mappingVersion", "quality", "hash",
)
DEPENDENCY_FIELDS = (
  "datasetId", "datasetVersion", "symbol", "sourceTimeframe", "timeframe",
  "startDate", "endDate", "rowCount", "snapshotHash", "signalOrder", "alignment",
)
MANIFEST_FIELDS = (
  "experimentHash", "engineVersion", "strategyRuntimeVersion",
  "strategyRuntimeCapabilityDigest", "signalModel", "fillModel",
  "executionAlgorithm", "executionParameters", "randomSeed", "strategyCodeHash",
  "configHash", "dataHash", "barCount", "barRange", "costModel",
  "holdingPolicy", "overlaySettlementPolicy", "strategyCategory",
  "initialPositionEnabled", "baseQuantity", "baseCostPrice", "t0Enabled",
  "resultSchemaVersion",
)
SUMMARY_FIELDS = (
  "totalPnl", "accountPnl", "accountRealizedPnl", "totalReturn", "baseHoldingPnl",
  "baseHoldingReturn", "t0ExcessPnl", "t0ExcessReturn", "baseBenchmarkPnl",
  "overlayPnl", "realizedOverlayPnl", "unrealizedOverlayPnl", "completedCyclePnl",
  "completedCycleCount", "openCycleCount", "winningCycles", "losingCycles",
  "breakevenCycles", "cycleWinRate", "tradeMetricVersion", "completedTradeCount",
  "openTradeCount", "breakevenTrades", "maxDrawdown", "maxDrawdownRate",
  "annualVolatility", "winRate", "dailyWinRate", "tradeCount", "orderCount",
  "fillCount", "winningTrades", "losingTrades", "avgWin", "avgLoss",
  "profitLossRatio", "finalEquity", "annualReturn", "annualSharpe", "sortino",
)
STRATEGY_METRIC_FIELDS = ("t0Metrics", "holdingMetrics")
EQUITY_FIELDS = ("date", "pnl", "value")
BENCHMARK_FIELDS = ("date", "close", "value", "returnRate")
HOLDING_FIELDS = (
  "date", "close", "longQty", "shortQty", "cash", "longMarketValue",
  "shortMarketValue", "marketValue", "totalEquity", "dailyPnl",
  "targetBaseQuantity", "actualQuantity", "overlayQuantity", "sellableQuantity",
  "dailyAccountPnl", "dailyBaseBenchmarkPnl", "dailyOverlayPnl",
  "dailyRealizedOverlayPnl", "unrealizedOverlayPnl",
)
TRADE_FIELDS = (
  "time", "signalTime", "executionTime", "parentOrderId", "childIndex",
  "executionAlgorithm", "priceSource", "fundingLimited", "depthQuantity",
  "affordableQuantity", "remainingQuantity", "availableCashBefore", "action",
  "status", "direction", "side", "anchor", "fillId", "positionKey",
  "inventorySource", "cycleId", "cycleEffect", "overlayQuantityBefore",
  "overlayQuantityAfter", "accountRealizedPnl", "realizedOverlayPnl", "totalFee",
  "attributionSegments", "price", "quantity", "pnl", "note", "strategyModule",
  "researchSignalId", "researchSignalAction", "researchSignalEffectiveAt",
)
SIGNAL_FIELDS = (
  "time", "signalTime", "executionTime", "parentOrderId", "action", "status",
  "direction", "side", "anchor", "price", "quantity", "pnl", "note",
  "strategyModule", "researchSignalId", "researchSignalAction",
  "researchSignalEffectiveAt",
)
PLAN_FIELDS = (
  "parentOrderId", "algorithm", "signalTime", "action", "requestedQuantity",
  "filledQuantity", "cancelledQuantity", "rejectedQuantity", "remainingQuantity",
  "status", "reason", "plannedChildCount",
)
CYCLE_FIELDS = (
  "cycleId", "symbol", "direction", "openedAt", "closedAt", "openedQuantity",
  "closedQuantity", "remainingQuantity", "realizedPnl", "unrealizedPnl", "netPnl",
  "fillIds", "status",
)
ATTRIBUTION_FIELDS = (
  "inventorySource", "quantity", "entryPrice", "exitPrice", "grossPnl", "fee",
  "netPnl", "positionKey", "cycleId",
)
BAR_RANGE_FIELDS = ("start", "end", "timeframe")
COST_MODEL_FIELDS = (
  "commissionRate", "slippage", "stampDutyRate", "transferFeeRate", "minCommission",
)
EXECUTION_PARAMETER_FIELDS = (
  "childQuantity", "intervalSeconds", "participationRate", "maxChildQuantity",
)
RESEARCH_MANIFEST_FIELDS = (
  "version", "datasetId", "datasetVersion", "snapshotHash", "symbol", "timeframe",
  "sourceTimeframe", "startDate", "endDate", "rowCount", "contentSha256",
)
SOURCE_PARTITION_FIELDS = (
  "date", "rowCount", "contentSha256", "firstTime", "lastTime",
)

_RESERVED_AUTHOR_KEYS = {
  "accountid", "accountname", "apikey", "artifactfile", "authorization",
  "credential", "diagnostic", "email", "errormessage", "exception", "internalpath",
  "internalserviceurl", "loginname", "outputdir", "owneruserid", "requestid",
  "requestkey", "sessionid", "stacktrace", "storagekey", "token", "traceback",
  "userid", "username", "workerid",
}
_RESERVED_AUTHOR_SUFFIXES = (
  "accountid", "accountname", "apikey", "credential", "diagnostic", "dir",
  "email", "error", "exception", "path", "requestid", "requestkey",
  "secret", "sessionid", "stacktrace", "storagekey", "token", "traceback",
  "userid", "username", "workerid",
)


def project_fields(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
  if not isinstance(value, dict):
    return {}
  return {key: value[key] for key in fields if key in value}


def _normalized_key(value: object) -> str:
  return "".join(character for character in str(value).lower() if character.isalnum())


def _is_reserved_author_key(value: object) -> bool:
  normalized = _normalized_key(value)
  return normalized in _RESERVED_AUTHOR_KEYS or normalized.endswith(
    _RESERVED_AUTHOR_SUFFIXES,
  )


def project_author_json(value: Any) -> Any:
  """投影作者业务参数；系统保留键永远不进入公开快照。"""
  if isinstance(value, dict):
    return {
      str(key): project_author_json(item)
      for key, item in value.items()
      if not _is_reserved_author_key(key)
    }
  if isinstance(value, list):
    return [project_author_json(item) for item in value]
  if value is None or isinstance(value, (str, int, float, bool)):
    return value
  raise ValueError("策略参数包含不支持的值，请重新保存并运行后共享")


def _records(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
  if not isinstance(value, list):
    return []
  return [project_fields(item, fields) for item in value if isinstance(item, dict)]


def _required(mapping: Any, names: tuple[str, ...], prefix: str) -> dict[str, Any]:
  if not isinstance(mapping, dict):
    raise ValueError(INCOMPLETE_HISTORY_MESSAGE.format(fields=prefix))
  missing = [name for name in names if name not in mapping or mapping[name] is None]
  if missing:
    fields = "、".join(f"{prefix}.{name}" for name in missing)
    raise ValueError(INCOMPLETE_HISTORY_MESSAGE.format(fields=fields))
  return mapping


def _require_type(value: Any, expected: type, field: str) -> Any:
  if not isinstance(value, expected):
    raise ValueError(INCOMPLETE_HISTORY_MESSAGE.format(fields=field))
  return value


def _project_manifest(value: Any) -> dict[str, Any]:
  manifest = project_fields(value, MANIFEST_FIELDS)
  manifest["barRange"] = project_fields(manifest.get("barRange"), BAR_RANGE_FIELDS)
  manifest["costModel"] = project_fields(manifest.get("costModel"), COST_MODEL_FIELDS)
  manifest["executionParameters"] = project_fields(
    manifest.get("executionParameters"), EXECUTION_PARAMETER_FIELDS,
  )
  return manifest


def _project_trades(value: Any) -> list[dict[str, Any]]:
  trades = _records(value, TRADE_FIELDS)
  for trade in trades:
    trade["attributionSegments"] = _records(
      trade.get("attributionSegments"), ATTRIBUTION_FIELDS,
    )
  return trades


def project_result(value: Any, *, detail: bool = False) -> dict[str, Any]:
  result = value if isinstance(value, dict) else {}
  projected: dict[str, Any] = {}
  for key in ("strategyCategory", "resultSchemaVersion", "benchmarkSymbol", "benchmarkReturn", "excessReturn"):
    if key in result:
      projected[key] = result[key]
  if "strategyMetrics" in result:
    projected["strategyMetrics"] = {
      key: project_fields(group, SUMMARY_FIELDS)
      for key, group in project_fields(result.get("strategyMetrics"), STRATEGY_METRIC_FIELDS).items()
    }
  if "summary" in result:
    projected["summary"] = project_fields(result.get("summary"), SUMMARY_FIELDS)
  if "experimentManifest" in result:
    projected["experimentManifest"] = _project_manifest(result.get("experimentManifest"))
  if "warnings" in result:
    projected["warnings"] = [item for item in result.get("warnings", []) if isinstance(item, str)]
  if detail:
    return projected
  projected.update({
    "equity": _records(result.get("equity"), EQUITY_FIELDS),
    "dailyHoldings": _records(result.get("dailyHoldings"), HOLDING_FIELDS),
    "cycles": _records(result.get("cycles"), CYCLE_FIELDS),
    "executionPlans": _records(result.get("executionPlans"), PLAN_FIELDS),
    "benchmarkEquity": _records(result.get("benchmarkEquity"), BENCHMARK_FIELDS),
    "trades": _project_trades(result.get("trades")),
    "skippedSignals": _records(result.get("skippedSignals"), SIGNAL_FIELDS),
    "dataSnapshot": project_fields(result.get("dataSnapshot"), RESULT_DATA_FIELDS),
  })
  return projected


def project_data(value: Any) -> dict[str, Any]:
  data = value if isinstance(value, dict) else {}
  dependencies = []
  for dependency in data.get("dependencies", []):
    if not isinstance(dependency, dict):
      continue
    dependencies.append({
      "type": dependency.get("type"),
      "summary": project_fields(
        dependency.get("summary"), tuple(
          field for field in DEPENDENCY_FIELDS
          if field not in {"datasetId", "datasetVersion"}
        ),
      ),
    })
  return {
    "marketData": project_fields(data.get("marketData"), MARKET_DATA_FIELDS),
    "resultData": project_fields(data.get("resultData"), RESULT_DATA_FIELDS),
    "dependencies": dependencies,
  }


def project_reproduction(value: Any) -> dict[str, Any]:
  result = project_fields(value, DEPENDENCY_FIELDS)
  result["manifest"] = project_fields(
    value.get("manifest") if isinstance(value, dict) else None,
    RESEARCH_MANIFEST_FIELDS,
  )
  raw_partitions = value.get("sourcePartitions") if isinstance(value, dict) else None
  result["sourcePartitions"] = _records(raw_partitions, SOURCE_PARTITION_FIELDS)
  return result


def build_snapshot(run: dict[str, Any]) -> dict[str, Any]:
  """仅从明确存在的保存证据构造 v1 快照，不为旧任务补猜语义。"""
  if run.get("status") not in {"ok", "succeeded"}:
    raise ValueError("只有成功完成的回测可以共享")
  script = _required(
    run.get("scriptSnapshot"),
    ("name", "code", "entryType", "codeHash", "signalRequired", "runtimeVersion"),
    "scriptSnapshot",
  )
  if not str(script["code"]).strip():
    raise ValueError(INCOMPLETE_HISTORY_MESSAGE.format(fields="scriptSnapshot.code"))
  code_hash = str(script["codeHash"])
  if len(code_hash) != 64 or any(character not in "0123456789abcdef" for character in code_hash):
    raise ValueError(INCOMPLETE_HISTORY_MESSAGE.format(fields="scriptSnapshot.codeHash"))
  saved = _required(
    run.get("paramsSnapshot"),
    ("name", "strategy", "symbol", "timeframe", "params", "runParams", "execution"),
    "paramsSnapshot",
  )
  for field in ("params", "runParams", "execution"):
    _require_type(saved[field], dict, f"paramsSnapshot.{field}")
  request = _required(
    run.get("paramsJson"),
    (
      "strategyCategory", "startDate", "endDate", "initialCapital", "commissionRate",
      "slippage", "fillModel", "executionAlgorithm", "strategyParams",
    ),
    "paramsJson",
  )
  _require_type(request["strategyParams"], dict, "paramsJson.strategyParams")
  result = _required(
    run.get("resultJson"),
    (
      "resultSchemaVersion", "summary", "equity", "dailyHoldings", "trades",
      "skippedSignals", "experimentManifest", "dataSnapshot", "warnings",
    ),
    "resultJson",
  )
  _required(result.get("summary"), ("totalReturn", "maxDrawdownRate", "annualSharpe"), "resultJson.summary")
  for field in ("equity", "dailyHoldings", "trades", "skippedSignals", "warnings"):
    _require_type(result[field], list, f"resultJson.{field}")
  for field in ("experimentManifest", "dataSnapshot"):
    _require_type(result[field], dict, f"resultJson.{field}")
  data_version = _required(run.get("dataVersion"), ("barSnapshot",), "dataVersion")
  market_data = _required(data_version["barSnapshot"], ("provider", "hash"), "dataVersion.barSnapshot")
  configuration = {
    "strategyName": saved["name"],
    "strategy": saved["strategy"],
    "strategyCategory": request["strategyCategory"],
    "symbol": saved["symbol"],
    "timeframe": saved["timeframe"],
    "benchmarkSymbol": request.get("benchmarkSymbol"),
    "startDate": request["startDate"],
    "endDate": request["endDate"],
    "params": project_author_json(saved["params"]),
    "runParams": project_author_json(saved["runParams"]),
    "execution": project_author_json(saved["execution"]),
  }
  for key in CONFIGURATION_FIELDS[11:]:
    if key in request:
      configuration[key] = project_author_json(request[key])
  dependencies: list[dict[str, Any]] = []
  research_signal_source = (
    result.get("researchSignalSnapshot")
    if isinstance(result.get("researchSignalSnapshot"), dict)
    else saved.get("researchSignal")
  )
  if research_signal_source is not None:
    research_signal = _required(
      research_signal_source, ("snapshotHash",), "researchSignalSnapshot",
    )
    dependencies.append({"type": "research_signal", "summary": project_fields(research_signal, DEPENDENCY_FIELDS)})
  return {
    "strategy": project_fields(script, STRATEGY_FIELDS),
    "configuration": configuration,
    "data": {
      "marketData": project_fields(market_data, MARKET_DATA_FIELDS),
      "resultData": project_fields(result["dataSnapshot"], RESULT_DATA_FIELDS),
      "dependencies": dependencies,
    },
    "result": project_result(result),
    "origin": project_fields(saved.get("sharedSource"), ORIGIN_FIELDS),
  }
