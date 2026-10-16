"""完整策略生命周期模块：统一网页与开放接口的保存和状态变更语义。"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import math
from typing import Any

from backend import settings
from backend.services import state_store
from backend.services.backtest_errors import StrategyExecutionError
from backend.strategies.runtime_contract import (
  RuntimeDependencyLoadError,
  RuntimeDependencyMissingError,
  StrategyModuleUnavailableError,
  StrategyRuntimePolicyError,
)
from backend.strategies.scripted import (
  script_uses_research_signal,
  strategy_parameters_for_script,
  validate_script_code,
  warmup_trading_days_for_script,
)


class StrategyLifecycleError(ValueError):
  """可稳定映射到 HTTP 错误的策略领域错误。"""

  def __init__(self, code: str, message: str, status_code: int = 422) -> None:
    super().__init__(message)
    self.code = code
    self.status_code = status_code


@dataclass(frozen=True)
class StrategySaveCommand:
  name: str
  description: str
  code: str
  entry_type: str
  signal_required: bool
  strategy: str
  mode: str
  symbol: str
  timeframe: str
  benchmark_symbol: str | None
  account_id: int | None
  params_json: dict[str, Any]
  strategy_parameters: dict[str, Any] | None
  enabled: bool
  script_id: str | None = None
  config_id: str | None = None
  expected_revision: int | None = None


@dataclass(frozen=True)
class PublicWriteContext:
  api_key_id: str
  idempotency_key: str
  request_hash: str
  operation_type: str
  request_id: str | None


def _parameter_value(
  name: str,
  definition: dict[str, Any],
  value: Any,
) -> bool | int | float | str:
  parameter_type = definition.get("type")
  if parameter_type == "boolean" and isinstance(value, bool):
    return value
  if parameter_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
    return value
  if (
    parameter_type == "number"
    and isinstance(value, (int, float))
    and not isinstance(value, bool)
    and math.isfinite(float(value))
  ):
    return float(value)
  if parameter_type == "string" and isinstance(value, str) and len(value) <= 1024:
    return value
  raise StrategyLifecycleError(
    "strategy.parameter_invalid",
    f"策略参数 {name} 的值与声明类型不匹配",
  )


def _saved_parameter_state(config: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
  if not config:
    return {}, {}
  params_json = config.get("paramsJson")
  if not isinstance(params_json, dict):
    return {}, {}
  definitions = params_json.get("strategyParameterDefinitions")
  if not isinstance(definitions, dict):
    definitions = strategy_parameters_for_script(str(config.get("code") or ""))
  saved = params_json.get("strategyParams")
  if not isinstance(saved, dict):
    params = params_json.get("params")
    saved = {
      name: params[name]
      for name in definitions
      if isinstance(params, dict) and name in params
    }
  return definitions, saved


def _coordinate_strategy_parameters(
  definitions: dict[str, dict[str, Any]],
  requested: dict[str, Any],
  previous_definitions: dict[str, Any],
  previous_values: dict[str, Any],
) -> dict[str, Any]:
  unknown = sorted(set(requested) - set(definitions))
  if unknown:
    raise StrategyLifecycleError(
      "strategy.parameter_invalid",
      f"存在未声明的策略参数: {', '.join(unknown)}",
    )
  values: dict[str, Any] = {}
  for name, definition in definitions.items():
    if name in requested:
      candidate = requested[name]
    elif (
      name in previous_values
      and isinstance(previous_definitions.get(name), dict)
      and previous_definitions[name].get("type") == definition.get("type")
    ):
      candidate = previous_values[name]
    else:
      candidate = definition["default"]
    values[name] = _parameter_value(name, definition, candidate)
  return values


def _validated_payloads(command: StrategySaveCommand) -> tuple[dict[str, Any], dict[str, Any]]:
  name = command.name.strip()
  if not name or len(name) > state_store.strategy_name_max_length():
    raise StrategyLifecycleError("strategy.validation_failed", "策略名称长度必须在 1 至 64 之间")
  if command.entry_type != "on_bar" and not (
    command.entry_type.startswith("catalog:") and command.strategy != "user_script"
  ):
    raise StrategyLifecycleError("strategy.validation_failed", "策略入口类型无效")
  if command.mode != "backtest":
    raise StrategyLifecycleError("strategy.validation_failed", "开放策略只支持回测模式")
  try:
    if command.entry_type == "on_bar":
      validate_script_code(command.code)
    else:
      ast.parse(command.code)
  except StrategyRuntimePolicyError as error:
    detail = StrategyExecutionError.from_exception(error).detail
    raise StrategyLifecycleError("strategy.validation_failed", detail.message) from error
  except (
    RuntimeDependencyLoadError,
    RuntimeDependencyMissingError,
    StrategyModuleUnavailableError,
  ) as error:
    detail = StrategyExecutionError.from_exception(error).detail
    status_code = 503 if detail.category == "system_error" else 422
    raise StrategyLifecycleError(
      "strategy.validation_failed", detail.message, status_code,
    ) from error
  except ValueError as error:
    raise StrategyLifecycleError("strategy.validation_failed", str(error)) from error
  except SyntaxError as error:
    raise StrategyLifecycleError(
      "strategy.validation_failed",
      f"Python 语法错误: 第 {error.lineno} 行 {error.msg}",
    ) from error
  if command.signal_required and not script_uses_research_signal(command.code):
    raise StrategyLifecycleError(
      "strategy.validation_failed",
      "研究信号驱动策略必须显式读取 bar['researchSignal']",
    )
  if not settings.validate_symbol(command.symbol):
    raise StrategyLifecycleError("strategy.validation_failed", f"非法标的: {command.symbol}")
  if command.benchmark_symbol and not settings.validate_symbol(command.benchmark_symbol):
    raise StrategyLifecycleError(
      "strategy.validation_failed",
      f"非法买入持有基准: {command.benchmark_symbol}",
    )
  params_json = dict(command.params_json)
  params = params_json.get("params")
  if not isinstance(params, dict):
    raise StrategyLifecycleError("strategy.validation_failed", "完整策略必须包含回测参数")
  params = dict(params)
  if params.get("fillModel") == "bar_close":
    params["fillModel"] = "bar_next_open"
  fill_model = str(params.get("fillModel", "bar_next_open"))
  timeframe_valid = (
    settings.validate_l2_backtest_timeframe(command.timeframe)
    if fill_model == "l2_quote"
    else settings.validate_backtest_timeframe(command.timeframe)
  )
  if not timeframe_valid:
    engine_name = "L2 回测" if fill_model == "l2_quote" else "当前回测引擎"
    raise StrategyLifecycleError(
      "strategy.validation_failed", f"{engine_name}不支持 {command.timeframe}",
    )
  if not params.get("startDate") or not params.get("endDate"):
    raise StrategyLifecycleError(
      "strategy.validation_failed", "完整策略必须包含回测开始日期和结束日期",
    )
  warmup_days = warmup_trading_days_for_script(command.code)
  if fill_model == "l2_quote" and warmup_days != 0:
    raise StrategyLifecycleError(
      "strategy.validation_failed", "L2 盘口回测不支持策略声明预热交易日",
    )
  params["warmupTradingDays"] = warmup_days
  definitions = strategy_parameters_for_script(command.code)
  previous_config = (
    state_store.get_strategy_config(command.config_id)
    if command.config_id is not None
    else None
  )
  previous_definitions, previous_values = _saved_parameter_state(previous_config)
  requested = command.strategy_parameters
  if requested is None:
    requested = {name: params[name] for name in definitions if name in params}
  saved_values = _coordinate_strategy_parameters(
    definitions, requested, previous_definitions, previous_values,
  )
  for key in set(previous_definitions) | set(definitions):
    params.pop(key, None)
  params.update(saved_values)
  params_json.update({
    "params": params,
    "scriptName": name,
    "strategyParameterDefinitions": definitions,
    "strategyParams": saved_values,
  })
  return (
    {
      "name": name,
      "description": command.description,
      "code": command.code,
      "entryType": command.entry_type,
      "signalRequired": command.signal_required,
      "status": "published",
    },
    {
      "name": name,
      "scriptId": command.script_id,
      "strategy": command.strategy,
      "mode": command.mode,
      "symbol": command.symbol,
      "timeframe": command.timeframe,
      "benchmarkSymbol": command.benchmark_symbol,
      "accountId": command.account_id,
      "paramsJson": params_json,
      "enabled": command.enabled,
    },
  )


def _strategy_reference(saved: dict[str, Any]) -> dict[str, Any]:
  config = saved["config"]
  return {
    "config_id": config["id"],
    "script_id": config.get("scriptId"),
    "strategy_version_id": config.get("strategyVersionId"),
    "published_version_id": config.get("publishedVersionId"),
    "revision": config["revision"],
  }


def save_strategy(
  command: StrategySaveCommand,
  write_context: PublicWriteContext | None = None,
) -> dict[str, Any]:
  if write_context is None:
    script_payload, config_payload = _validated_payloads(command)
    return state_store.save_complete_strategy(
      script_payload,
      config_payload,
      command.script_id,
      command.config_id,
      command.expected_revision,
    )

  def operation(cur: Any) -> tuple[str, int, dict[str, Any]]:
    # 幂等层先于业务校验调用 operation，确保资源后续变化不破坏首次结果重放。
    script_payload, config_payload = _validated_payloads(command)
    saved = state_store.save_complete_strategy_in_transaction(
      cur,
      script_payload,
      config_payload,
      command.script_id,
      command.config_id,
      command.expected_revision,
    )
    return str(saved["config"]["id"]), 201 if command.config_id is None else 200, {
      "strategy": _strategy_reference(saved),
    }

  body, replayed, response_status = state_store.execute_public_write(
    write_context.api_key_id,
    write_context.idempotency_key,
    write_context.request_hash,
    write_context.operation_type,
    "strategy",
    write_context.request_id,
    operation,
  )
  return {**body, "replayed": replayed, "response_status": response_status}


def create_strategy(command: StrategySaveCommand) -> dict[str, Any]:
  """按显式新建意图创建独立策略身份和完整版本 V1。"""
  if command.config_id is not None or command.script_id is not None:
    raise StrategyLifecycleError(
      "strategy.create_identity_conflict",
      "新建策略不能携带已有策略身份",
      409,
    )
  return save_strategy(command)


def update_strategy(
  config_id: str,
  expected_revision: int,
  command: StrategySaveCommand,
) -> dict[str, Any]:
  """按配置资源身份更新，不允许调用方改绑脚本身份。"""
  return save_strategy(StrategySaveCommand(
    **{
      **command.__dict__,
      "config_id": config_id,
      "expected_revision": expected_revision,
    },
  ))


def create_strategy_from_copy(command: StrategySaveCommand) -> dict[str, Any]:
  """复制草稿保存仍是显式创建；名称冲突由服务端全量资产判定。"""
  return create_strategy(command)


def _public_backtest_parameters(params: dict[str, Any]) -> dict[str, Any]:
  execution_parameters = {
    "duration_seconds": params.get("twapDurationSeconds"),
    "interval_seconds": params.get("twapIntervalSeconds"),
    "min_child_quantity": params.get("twapMinChildQuantity"),
  }
  return {
    "start_date": params.get("startDate"),
    "end_date": params.get("endDate"),
    "initial_capital": params.get("initialCapital"),
    "commission_rate": params.get("commissionRate"),
    "slippage": params.get("slippage"),
    "allow_long": params.get("allowLong", True),
    "allow_short": params.get("allowShort", True),
    "strategy_category": params.get("strategyCategory", "directional_holding"),
    "holding_policy": params.get("holdingPolicy", "overnight"),
    "overlay_settlement_policy": params.get("overlaySettlementPolicy", "intraday_restore"),
    "fill_model": params.get("fillModel", "bar_next_open"),
    "execution_algorithm": params.get("executionAlgorithm", "immediate"),
    "execution_parameters": {
      key: value for key, value in execution_parameters.items() if value is not None
    },
    "base_quantity": params.get("baseQuantity", 0),
    "base_cost_price": params.get("baseCostPrice", 0),
    "initial_position_enabled": params.get("initialPositionEnabled", False),
    "t0_enabled": params.get("t0Enabled", False),
  }


def _public_strategy(config: dict[str, Any]) -> dict[str, Any]:
  params_json = config.get("paramsJson") if isinstance(config.get("paramsJson"), dict) else {}
  params = params_json.get("params") if isinstance(params_json.get("params"), dict) else {}
  definitions = params_json.get("strategyParameterDefinitions")
  if not isinstance(definitions, dict):
    definitions = strategy_parameters_for_script(str(config.get("code") or ""))
  strategy_parameters = params_json.get("strategyParams")
  if not isinstance(strategy_parameters, dict):
    strategy_parameters = {name: params[name] for name in definitions if name in params}
  return {
    "config_id": config["id"],
    "script_id": config.get("scriptId"),
    "strategy_version_id": config.get("strategyVersionId"),
    "published_version_id": config.get("publishedVersionId"),
    "revision": config.get("revision", 1),
    "name": config["name"],
    "description": config.get("description", ""),
    "code": config.get("code", ""),
    "entry_type": config.get("entryType", "on_bar"),
    "signal_required": bool(config.get("signalRequired", False)),
    "mode": config.get("mode", "backtest"),
    "symbol": config["symbol"],
    "timeframe": config.get("timeframe", "5m"),
    "benchmark_symbol": config.get("benchmarkSymbol"),
    "enabled": bool(config.get("enabled", True)),
    "published": bool(config.get("published", True)),
    "archived": config.get("archivedAt") is not None,
    "archived_at": config.get("archivedAt"),
    "backtest_parameters": _public_backtest_parameters(params),
    "parameter_definitions": definitions,
    "strategy_parameters": strategy_parameters,
    "created_at": config.get("createdAt"),
    "updated_at": config.get("updatedAt"),
  }


def list_strategies(
  limit: int,
  offset: int,
  archived: bool | None = None,
  enabled: bool | None = None,
) -> dict[str, Any]:
  items, total = state_store.list_owned_strategy_configs(limit, offset, archived, enabled)
  return {
    "items": [_public_strategy(item) for item in items],
    "next_cursor": str(offset + len(items)) if offset + len(items) < total else None,
    "total": total,
  }


def get_strategy(config_id: str) -> dict[str, Any]:
  return _public_strategy(state_store.get_strategy_config(config_id, include_archived=True))


def effective_strategy_parameters(
  config: dict[str, Any],
  overrides: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
  """合并已保存默认值与单次覆盖，不修改策略资源。"""
  definitions, saved = _saved_parameter_state(config)
  effective = _coordinate_strategy_parameters(definitions, overrides, definitions, saved)
  return dict(saved), dict(overrides), effective


def _write_action(
  config_id: str,
  expected_revision: int,
  write_context: PublicWriteContext | None,
  action: Any,
) -> dict[str, Any]:
  if write_context is None:
    return action(config_id, expected_revision)

  def operation(cur: Any) -> tuple[str, int, dict[str, Any]]:
    result = action(cur, config_id, expected_revision)
    if isinstance(result, tuple):
      version, created = result
      config = state_store._get_strategy_config_with_cursor(cur, config_id)
      response = {
        "strategy": {
          "config_id": config_id,
          "revision": config["revision"],
          "published_version_id": version["id"],
        },
        "created": created,
      }
    else:
      response = {"strategy": _strategy_reference({"config": result})}
    return config_id, 200, response

  body, replayed, response_status = state_store.execute_public_write(
    write_context.api_key_id,
    write_context.idempotency_key,
    write_context.request_hash,
    write_context.operation_type,
    "strategy",
    write_context.request_id,
    operation,
  )
  return {**body, "replayed": replayed, "response_status": response_status}


def publish_strategy(
  config_id: str,
  expected_revision: int,
  write_context: PublicWriteContext | None = None,
) -> dict[str, Any]:
  if write_context is None:
    version, created = state_store.publish_strategy_config(config_id, expected_revision)
    return {"version": version, "created": created}
  return _write_action(
    config_id, expected_revision, write_context,
    state_store.publish_strategy_config_in_transaction,
  )


def archive_strategy(
  config_id: str,
  expected_revision: int,
  write_context: PublicWriteContext | None = None,
) -> dict[str, Any]:
  if write_context is None:
    return state_store.archive_strategy_config(config_id, expected_revision)
  return _write_action(
    config_id, expected_revision, write_context,
    state_store.archive_strategy_config_in_transaction,
  )


def restore_strategy(
  config_id: str,
  expected_revision: int,
  write_context: PublicWriteContext | None = None,
) -> dict[str, Any]:
  if write_context is None:
    return state_store.restore_strategy_config(config_id, expected_revision)
  return _write_action(
    config_id, expected_revision, write_context,
    state_store.restore_strategy_config_in_transaction,
  )


def list_versions(config_id: str, limit: int, offset: int) -> dict[str, Any]:
  versions, total = state_store.list_strategy_config_versions_page(config_id, limit, offset)
  return {
    "items": [_public_version(item) for item in versions],
    "next_cursor": str(offset + len(versions)) if offset + len(versions) < total else None,
    "total": total,
  }


def _public_version(version: dict[str, Any]) -> dict[str, Any]:
  params_json = version.get("paramsJson") if isinstance(version.get("paramsJson"), dict) else {}
  script = params_json.get("scriptSnapshot") if isinstance(params_json.get("scriptSnapshot"), dict) else {}
  params = params_json.get("params") if isinstance(params_json.get("params"), dict) else {}
  definitions = params_json.get("strategyParameterDefinitions")
  if not isinstance(definitions, dict):
    definitions = strategy_parameters_for_script(str(script.get("code") or ""))
  saved = params_json.get("strategyParams")
  if not isinstance(saved, dict):
    saved = {name: params[name] for name in definitions if name in params}
  return {
    "version_id": version["id"],
    "version_no": version["versionNo"],
    "config_id": version["configId"],
    "strategy_version_id": version.get("strategyVersionId"),
    "name": version["name"],
    "code": script.get("code", ""),
    "entry_type": script.get("entryType", "on_bar"),
    "signal_required": bool(script.get("signalRequired", False)),
    "symbol": version["symbol"],
    "timeframe": version["timeframe"],
    "benchmark_symbol": version.get("benchmarkSymbol"),
    "backtest_parameters": _public_backtest_parameters(params),
    "parameter_definitions": definitions,
    "strategy_parameters": saved,
    "created_at": version.get("createdAt"),
  }


def get_version(config_id: str, version_id: str) -> dict[str, Any]:
  return _public_version(
    state_store.get_strategy_config_version_for_config(config_id, version_id),
  )
