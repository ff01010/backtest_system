from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from backend.services import shared_backtest_artifacts, state_store


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
  base_length = state_store.strategy_name_max_length() - len(suffix)
  return f"{title.strip()[:base_length]}{suffix}"


def _json_object(value: Any) -> dict[str, Any]:
  if isinstance(value, dict):
    return value
  if isinstance(value, str):
    try:
      decoded = json.loads(value)
    except json.JSONDecodeError:
      return {}
    return decoded if isinstance(decoded, dict) else {}
  return {}


def _uuid_text(value: Any) -> str | None:
  return str(value) if value is not None else None


def _publication_artifact_rows(cur: Any, publication_id: str) -> list[dict[str, Any]]:
  cur.execute("""
    SELECT * FROM backtest_publication_artifact
    WHERE publication_id=%s ORDER BY artifact_type, id
  """, (publication_id,))
  return list(cur.fetchall())


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


def _copy_payloads(
  publication: dict[str, Any],
  signal_dataset_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
  version = int(publication["snapshot_schema_version"])
  if version != 1:
    raise ValueError(f"暂不支持共享快照版本: {version}")
  snapshot = _json_object(publication["snapshot_json"])
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
      **({"signalDatasetId": signal_dataset_id} if signal_dataset_id else {}),
    },
    "enabled": True,
    "sourcePublicationId": _uuid_text(publication["id"]),
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
    return state_store.get_strategy_config_in_transaction(
      cur, str(existing["strategy_config_id"]),
    )
  except KeyError as error:
    raise state_store.ConflictError("已有策略副本当前不可用") from error


def _prepare_imported_artifacts(
  cur: Any,
  publication: dict[str, Any],
  recipient_user_id: str,
) -> list[dict[str, Any]]:
  sources = _publication_artifact_rows(cur, str(publication["id"]))
  prepared = shared_backtest_artifacts.copy_artifacts(
    [{
      **source,
      "original_filename": source["display_name"],
      "source_publication_artifact_id": _uuid_text(source["id"]),
    } for source in sources],
    f"shared_imports/{recipient_user_id}/{publication['id']}",
  )
  for artifact in prepared:
    if artifact["artifact_type"] == "research_signal":
      artifact["imported_dataset_id"] = str(uuid4())
  return prepared


def _research_dataset_id(
  publication: dict[str, Any],
  artifacts: list[dict[str, Any]],
) -> str | None:
  research_artifacts = [
    artifact for artifact in artifacts
    if artifact["artifact_type"] == "research_signal"
  ]
  snapshot = _json_object(publication["snapshot_json"])
  strategy = snapshot.get("strategy", {})
  if bool(strategy.get("signalRequired")) and len(research_artifacts) != 1:
    raise ValueError("共享回测的研究信号附件不完整，暂时无法复制")
  if len(research_artifacts) > 1:
    raise ValueError("共享回测包含多个研究信号附件，暂时无法复制")
  return (
    str(research_artifacts[0]["imported_dataset_id"])
    if research_artifacts else None
  )


def _persist_imported_artifacts(
  cur: Any,
  publication: dict[str, Any],
  config_id: str,
  recipient_user_id: str,
  artifacts: list[dict[str, Any]],
) -> None:
  snapshot = _json_object(publication["snapshot_json"])
  configuration = snapshot.get("configuration", {})
  for artifact in artifacts:
    cur.execute("""
      INSERT INTO artifact_file (
        id, owner_user_id, run_id, artifact_type, storage_key,
        original_filename, content_type, byte_size, content_sha256
      ) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s)
    """, (
      artifact["id"], recipient_user_id, artifact["artifact_type"],
      artifact["storage_key"], artifact["original_filename"],
      artifact.get("content_type"), artifact["byte_size"], artifact["content_sha256"],
    ))
    source_artifact_id = artifact.get("source_publication_artifact_id")
    cur.execute("""
      INSERT INTO strategy_config_artifact (
        strategy_config_id, artifact_file_id,
        source_publication_artifact_id, artifact_role
      ) VALUES (%s, %s, %s, %s)
    """, (config_id, artifact["id"], source_artifact_id, artifact["artifact_type"]))
    if artifact["artifact_type"] != "research_signal":
      continue
    metadata = _json_object(artifact.get("reproduction_metadata"))
    snapshot_hash = shared_backtest_artifacts.research_signal_snapshot_hash(
      artifact["storage_key"],
    )
    if metadata.get("snapshotHash") != snapshot_hash:
      raise ValueError("共享回测的研究信号内容摘要不一致，暂时无法复制")
    manifest = _json_object(metadata.get("manifest"))
    dataset_version = int(metadata.get("datasetVersion") or manifest.get("version") or 1)
    source_timeframe = str(
      metadata.get("sourceTimeframe") or configuration.get("timeframe") or "5m"
    )
    cur.execute("""
      INSERT INTO imported_research_signal_dataset (
        id, owner_user_id, artifact_file_id, source_publication_artifact_id,
        display_name, dataset_key, dataset_version, symbol, timeframe,
        start_date, end_date, row_count, snapshot_hash, manifest, source_partitions
      ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
      artifact["imported_dataset_id"], recipient_user_id, artifact["id"],
      source_artifact_id, f"{publication['title']}-研究信号"[:255],
      f"shared:{source_artifact_id}", dataset_version,
      configuration.get("symbol") or publication["symbol"], source_timeframe,
      configuration.get("startDate") or publication["start_date"],
      configuration.get("endDate") or publication["end_date"],
      int(metadata.get("rowCount") or 0),
      snapshot_hash,
      Jsonb(manifest), Jsonb(metadata.get("sourcePartitions") or []),
    ))


def _create_copy(
  cur: Any,
  publication: dict[str, Any],
  recipient_user_id: str,
  imported_artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
  signal_dataset_id = _research_dataset_id(publication, imported_artifacts)
  script_payload, config_payload = _copy_payloads(publication, signal_dataset_id)
  config = state_store.create_published_strategy_in_transaction(
    cur, script_payload, config_payload,
  )
  config_id = str(config["id"])
  _persist_imported_artifacts(
    cur, publication, config_id, recipient_user_id, imported_artifacts,
  )
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
  imported_artifacts: list[dict[str, Any]] = []
  try:
    with state_store.database_session() as conn:
      with conn.cursor() as cur:
        lock_key = f"shared-backtest-copy:{publication_id}:{recipient_user_id}"
        cur.execute(
          "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
          (lock_key,),
        )
        cur.execute(
          "SELECT * FROM backtest_publication WHERE id=%s AND status='active'",
          (publication_id,),
        )
        publication = cur.fetchone()
        if not publication:
          raise KeyError(publication_id)
        existing = _existing_copy(cur, publication_id, recipient_user_id)
        if existing:
          return _copy_response(existing, False)
        imported_artifacts = _prepare_imported_artifacts(
          cur, publication, recipient_user_id,
        )
        config = _create_copy(
          cur, publication, recipient_user_id, imported_artifacts,
        )
        return _copy_response(config, True)
  except Exception:
    shared_backtest_artifacts.cleanup_artifacts(imported_artifacts)
    raise
