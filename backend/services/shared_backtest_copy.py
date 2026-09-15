from __future__ import annotations

from typing import Any

from backend.services import state_store


_COPIED_PARAM_KEYS = (
  "startDate",
  "endDate",
  "initialCapital",
  "commissionRate",
  "slippage",
  "allowLong",
  "allowShort",
  "strategyCategory",
  "holdingPolicy",
  "overlaySettlementPolicy",
  "fillModel",
  "executionAlgorithm",
  "baseQuantity",
  "baseCostPrice",
  "initialPositionEnabled",
  "t0Enabled",
)


def _copy_name(title: str) -> str:
  suffix = "-副本"
  base_length = state_store._STRATEGY_NAME_MAX_LENGTH - len(suffix)
  return f"{title.strip()[:base_length]}{suffix}"


def _copy_params(configuration: dict[str, Any]) -> dict[str, Any]:
  params = dict(configuration.get("params") or {})
  for key in _COPIED_PARAM_KEYS:
    value = configuration.get(key)
    if value is not None:
      params[key] = value
  execution = configuration.get("executionParameters")
  if isinstance(execution, dict):
    params["twapDurationSeconds"] = execution.get("durationSeconds", 300)
    params["twapIntervalSeconds"] = execution.get("intervalSeconds", 30)
    params["twapMinChildQuantity"] = execution.get("minChildQuantity", 100)
  return params


def _copy_payloads(publication: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
  version = int(publication["snapshot_schema_version"])
  if version != 1:
    raise ValueError(f"暂不支持共享快照版本: {version}")
  snapshot = state_store._json(publication["snapshot_json"], {})
  strategy = snapshot.get("strategy")
  configuration = snapshot.get("configuration")
  if not isinstance(strategy, dict) or not isinstance(configuration, dict):
    raise ValueError("共享回测快照不完整，暂时无法复制")
  code = str(strategy.get("code") or "")
  symbol = str(configuration.get("symbol") or "")
  if not code.strip() or not symbol:
    raise ValueError("共享回测快照不完整，暂时无法复制")
  name = _copy_name(str(publication["title"]))
  script_payload = {
    "name": name,
    "description": str(strategy.get("description") or ""),
    "code": code,
    "entryType": str(strategy.get("entryType") or "on_bar"),
    "signalRequired": bool(strategy.get("signalRequired", False)),
  }
  config_payload = {
    "name": name,
    "strategy": str(configuration.get("strategy") or "user_script"),
    "mode": "backtest",
    "symbol": symbol,
    "timeframe": str(configuration.get("timeframe") or "5m"),
    "benchmarkSymbol": configuration.get("benchmarkSymbol"),
    "accountId": None,
    "paramsJson": {
      "params": _copy_params(configuration),
      "scriptName": name,
    },
    "enabled": True,
    "sourcePublicationId": state_store._uuid(publication["id"]),
    "sourceAuthorName": publication["author_name_snapshot"],
    "sourceTitle": publication["title"],
  }
  return script_payload, config_payload


def _copy_response(
  config: dict[str, Any],
  created: bool,
) -> dict[str, Any]:
  return {
    "configId": config["id"],
    "scriptId": config["scriptId"],
    "name": config["name"],
    "created": created,
    "accountCleared": config.get("accountId") is None,
    "sharedSource": config.get("sharedSource"),
  }


def _existing_copy(cur: Any, publication_id: str, recipient_user_id: str) -> dict[str, Any] | None:
  cur.execute("""
    SELECT strategy_config_id
    FROM backtest_publication_copy
    WHERE publication_id=%s AND recipient_user_id=%s
  """, (publication_id, recipient_user_id))
  existing = cur.fetchone()
  if not existing:
    return None
  try:
    return state_store._get_strategy_config_with_cursor(
      cur, str(existing["strategy_config_id"]),
    )
  except KeyError as error:
    raise state_store.ConflictError("已有策略副本当前不可用") from error


def _create_copy(cur: Any, publication: dict[str, Any]) -> dict[str, Any]:
  script_payload, config_payload = _copy_payloads(publication)
  resolved_name = state_store._resolve_unique_strategy_name(
    cur, str(config_payload["name"]), None,
  )
  script_payload["name"] = resolved_name
  config_payload["name"] = resolved_name
  config_payload["paramsJson"]["scriptName"] = resolved_name
  script_id, strategy_version_id, _ = state_store._write_strategy_script(
    cur, None, script_payload,
  )
  config_id = state_store._write_config(
    cur, None, config_payload, script_id, strategy_version_id,
  )
  config = state_store._get_strategy_config_with_cursor(cur, config_id)
  state_store._publish_strategy_config_with_cursor(cur, config)
  cur.execute(
    "SELECT app.record_backtest_publication_copy(%s, %s) AS created",
    (publication["id"], config_id),
  )
  recorded = cur.fetchone()
  if not recorded or not bool(recorded["created"]):
    raise state_store.DatabaseError("策略副本关系创建失败")
  return config


def copy_shared_backtest(publication_id: str) -> dict[str, Any]:
  """幂等创建当前用户独立拥有的普通策略副本。"""
  recipient_user_id = state_store.current_user_id()
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT * FROM backtest_publication WHERE id=%s AND status='active' FOR UPDATE",
        (publication_id,),
      )
      publication = cur.fetchone()
      if not publication:
        raise KeyError(publication_id)
      existing = _existing_copy(cur, publication_id, recipient_user_id)
      if existing:
        return _copy_response(existing, False)
      return _copy_response(_create_copy(cur, publication), True)
