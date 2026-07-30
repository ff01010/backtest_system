# -*- coding: utf-8 -*-
"""PostgreSQL 状态仓储：仅访问规范表，不在应用启动时建表。"""

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from typing import Any, Iterator

import psycopg
from psycopg.errors import IntegrityError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.services.user_context import get_current_user


class DatabaseError(RuntimeError):
  """数据库连接或执行失败。"""


class ConflictError(DatabaseError):
  """违反唯一约束等写入冲突。"""


def _translate_error(error: Exception) -> DatabaseError:
  if isinstance(error, IntegrityError):
    return ConflictError(str(error))
  return DatabaseError(str(error))


def current_user_id() -> str:
  """返回请求绑定的用户 ID。"""
  return get_current_user().id


def current_user_is_admin() -> bool:
  """返回请求绑定用户是否拥有管理员权限。"""
  return get_current_user().is_admin


@contextmanager
def _db() -> Iterator[psycopg.Connection[Any]]:
  if not settings.DATABASE_URL:
    raise DatabaseError("未设置 TREND_GRID_DATABASE_URL")
  try:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT set_config('app.current_user_id', %s, false)",
          (current_user_id(),),
        )
        cur.execute(
          "SELECT set_config('app.current_is_admin', %s, false)",
          ("true" if current_user_is_admin() else "false",),
        )
      yield conn
  except psycopg.Error as error:
    raise _translate_error(error) from error


def ensure_schema() -> None:
  """确认数据库和迁移已经准备好；DDL 只允许由部署迁移执行。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT to_regclass('public.backtest_run') AS table_name")
      if cur.fetchone()["table_name"] is None:
        raise DatabaseError("数据库迁移尚未执行，请先运行 deploy 的 migrate 服务")


def _to_float(value: Any) -> float | None:
  if value is None:
    return None
  if isinstance(value, Decimal):
    return float(value)
  return float(value)


def _format_time(value: Any) -> str:
  return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _json(value: Any, fallback: Any) -> Any:
  return fallback if value is None else value


def _uuid(value: Any) -> str | None:
  return str(value) if value is not None else None


def _symbol_parts(symbol: str) -> tuple[str, str, str]:
  code, exchange = symbol.split(".", 1)
  secid_prefix = "1" if exchange == "SH" else "0"
  return code, exchange, f"{secid_prefix}.{code}"


def _instrument_id(cur: Any, symbol: str, name: str = "") -> int:
  code, exchange, secid = _symbol_parts(symbol)
  cur.execute("""
    INSERT INTO market_instrument (
      symbol, exchange, display_name, instrument_type, board, eastmoney_secid
    ) VALUES (%s, %s, %s, 'stock', 'unknown', %s)
    ON CONFLICT (exchange, symbol) DO UPDATE
      SET display_name = CASE
        WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.display_name
        ELSE market_instrument.display_name
      END,
      updated_at = now()
    RETURNING id
  """, (code, exchange, name or symbol, secid))
  return int(cur.fetchone()["id"])


def _normalize_account(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": int(row["id"]),
    "name": row["name"],
    "initialCapital": float(row["initial_capital"]),
    "commissionRate": float(row["commission_rate"]),
    "slippage": float(row["slippage"]),
    "positionMode": row["position_mode"],
    "orderCash": _to_float(row["order_cash"]),
    "orderPct": _to_float(row["order_pct"]),
    "allowShort": bool(row["allow_short"]),
    "isDefault": bool(row["is_default"]),
  }


def _normalize_script(row: dict[str, Any]) -> dict[str, Any]:
  runtime = _json(row.get("runtime_config"), {})
  return {
    "id": _uuid(row["id"]),
    "name": row["name"],
    "description": row["description"],
    "language": "python",
    "code": row["source_code"],
    "entryType": runtime.get("entryType", "on_bar"),
    "status": runtime.get("status", "draft"),
    "createdAt": _format_time(row["created_at"]),
    "updatedAt": _format_time(row["updated_at"]),
  }


def _normalize_config(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": _uuid(row["id"]),
    "name": row["name"],
    "scriptId": _uuid(row["strategy_id"]),
    "strategy": row["strategy_key"],
    "mode": row["mode"],
    "symbol": f"{row['symbol']}.{row['exchange']}",
    "timeframe": row["timeframe"],
    "benchmarkSymbol": row.get("benchmark_symbol"),
    "accountId": int(row["account_id"]) if row.get("account_id") is not None else None,
    "paramsJson": _json(row["params"], {}),
    "enabled": bool(row["enabled"]),
    "published": bool(row["published"]),
    "createdAt": _format_time(row["created_at"]),
    "updatedAt": _format_time(row["updated_at"]),
  }


def _normalize_version(row: dict[str, Any]) -> dict[str, Any]:
  snapshot = _json(row["snapshot"], {})
  params = dict(snapshot.get("paramsJson", {}))
  params["strategyVersionId"] = snapshot.get("strategyVersionId")
  return {
    "id": _uuid(row["id"]),
    "configId": _uuid(row["config_id"]),
    "versionNo": int(row["version_no"]),
    "name": snapshot["name"],
    "scriptId": _uuid(snapshot.get("scriptId")),
    "strategy": snapshot["strategy"],
    "mode": snapshot["mode"],
    "symbol": snapshot["symbol"],
    "timeframe": snapshot["timeframe"],
    "benchmarkSymbol": snapshot.get("benchmarkSymbol"),
    "accountId": snapshot.get("accountId"),
    "paramsJson": params,
    "changeNote": row["change_note"],
    "createdAt": _format_time(row["created_at"]),
  }


def _normalize_run(row: dict[str, Any]) -> dict[str, Any]:
  request = _json(row["requested_params"], {})
  return {
    "id": _uuid(row["id"]),
    "configId": _uuid(row.get("config_id")),
    "configVersionId": _uuid(row.get("config_version_id")),
    "scriptId": _uuid(row.get("strategy_id")),
    "accountId": int(row["account_id"]) if row.get("account_id") is not None else None,
    "symbol": row.get("symbol") or request.get("symbol", ""),
    "timeframe": row.get("timeframe") or request.get("timeframe", "5m"),
    "fillModel": row.get("fill_model") or "bar_close_signal_fill",
    "scriptSnapshot": _json(row.get("script_snapshot"), None),
    "paramsSnapshot": _json(row.get("params_snapshot"), {}),
    "dataVersion": _json(row.get("market_data_snapshot"), None),
    "benchmarkSymbol": row.get("benchmark_symbol") or request.get("benchmarkSymbol"),
    "strategy": request.get("strategy", "user_script"),
    "startDate": request.get("startDate", ""),
    "endDate": request.get("endDate", ""),
    "paramsJson": request,
    "status": {"succeeded": "ok", "failed": "error"}.get(row["status"], row["status"]),
    "executionKind": row.get("execution_kind", "published"),
    "summaryJson": _json(row.get("result_summary"), {}),
    "outputDir": row.get("output_dir", ""),
    "note": row.get("note", ""),
    "tags": _json(row.get("tags"), []),
    "resultJson": _json(row.get("result_json"), None),
    "createdAt": _format_time(row["created_at"]),
    "finishedAt": _format_time(row.get("finished_at")),
  }


def seed_default_account_for_user(user_id: str) -> None:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO backtest_account (
          owner_user_id, name, initial_capital, commission_rate, slippage,
          position_mode, order_cash, allow_short, is_default
        ) SELECT %s, 'default', 1000000, 0.0003, 0, 'fixed_cash', 100000, true, true
        WHERE NOT EXISTS (
          SELECT 1 FROM backtest_account WHERE owner_user_id = %s
        )
      """, (user_id, user_id))


def seed_default_account() -> None:
  """为当前用户补齐默认回测账户。"""
  seed_default_account_for_user(current_user_id())


def list_instruments() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT i.symbol, i.exchange, i.display_name, ui.enabled, ui.note
        FROM user_instrument ui
        JOIN market_instrument i ON i.id = ui.instrument_id
        WHERE ui.owner_user_id = %s
        ORDER BY i.exchange, i.symbol
      """, (current_user_id(),))
      return [{
        "symbol": f"{row['symbol']}.{row['exchange']}",
        "name": row["display_name"],
        "market": row["exchange"],
        "enabled": bool(row["enabled"]),
        "note": row["note"],
        "hasKline": False,
        "hasSignal": False,
      } for row in cur.fetchall()]


def upsert_instrument(payload: dict[str, Any]) -> dict[str, Any]:
  symbol = payload["symbol"]
  with _db() as conn:
    with conn.cursor() as cur:
      instrument_id = _instrument_id(cur, symbol, payload.get("name", ""))
      cur.execute("""
        INSERT INTO user_instrument (owner_user_id, instrument_id, enabled, note)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (owner_user_id, instrument_id) DO UPDATE
          SET enabled = EXCLUDED.enabled, note = EXCLUDED.note, updated_at = now()
      """, (current_user_id(), instrument_id, payload.get("enabled", True), payload.get("note", "")))
  return get_instrument(symbol)


def update_instrument(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
  return upsert_instrument({**payload, "symbol": symbol})


def get_instrument(symbol: str) -> dict[str, Any]:
  code, exchange, _ = _symbol_parts(symbol)
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT i.display_name, ui.enabled, ui.note
        FROM user_instrument ui JOIN market_instrument i ON i.id = ui.instrument_id
        WHERE ui.owner_user_id = %s AND i.symbol = %s AND i.exchange = %s
      """, (current_user_id(), code, exchange))
      row = cur.fetchone()
      if not row:
        raise KeyError(symbol)
      return {"symbol": symbol, "name": row["display_name"], "market": exchange,
              "enabled": bool(row["enabled"]), "note": row["note"],
              "hasKline": False, "hasSignal": False}


def delete_instrument(symbol: str) -> bool:
  code, exchange, _ = _symbol_parts(symbol)
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        DELETE FROM user_instrument ui USING market_instrument i
        WHERE ui.instrument_id = i.id AND ui.owner_user_id = %s
          AND i.symbol = %s AND i.exchange = %s
      """, (current_user_id(), code, exchange))
      return cur.rowcount > 0


def list_accounts() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT * FROM backtest_account WHERE owner_user_id = %s ORDER BY is_default DESC, id", (current_user_id(),))
      return [_normalize_account(row) for row in cur.fetchall()]


def _account_values(payload: dict[str, Any]) -> tuple[Any, ...]:
  return (payload["name"], payload["initialCapital"], payload["commissionRate"],
          payload["slippage"], payload["positionMode"], payload.get("orderCash"),
          payload.get("orderPct"), payload.get("allowShort", True), payload.get("isDefault", False))


def create_account(payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      if payload.get("isDefault"):
        cur.execute("UPDATE backtest_account SET is_default = false WHERE owner_user_id = %s", (current_user_id(),))
      cur.execute("""
        INSERT INTO backtest_account (
          owner_user_id, name, initial_capital, commission_rate, slippage, position_mode,
          order_cash, order_pct, allow_short, is_default
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
      """, (current_user_id(), *_account_values(payload)))
      account_id = int(cur.fetchone()["id"])
  return get_account(account_id)


def update_account(account_id: int, payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      if payload.get("isDefault"):
        cur.execute("UPDATE backtest_account SET is_default = false WHERE owner_user_id = %s AND id <> %s", (current_user_id(), account_id))
      cur.execute("""
        UPDATE backtest_account SET name=%s, initial_capital=%s, commission_rate=%s,
          slippage=%s, position_mode=%s, order_cash=%s, order_pct=%s, allow_short=%s,
          is_default=%s, updated_at=now()
        WHERE id=%s AND owner_user_id=%s
      """, (*_account_values(payload), account_id, current_user_id()))
      if cur.rowcount == 0:
        raise KeyError(account_id)
  return get_account(account_id)


def get_account(account_id: int) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT * FROM backtest_account WHERE id=%s AND owner_user_id=%s", (account_id, current_user_id()))
      row = cur.fetchone()
      if not row:
        raise KeyError(account_id)
      return _normalize_account(row)


def delete_account(account_id: int) -> bool:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM backtest_account WHERE id=%s AND owner_user_id=%s", (account_id, current_user_id()))
      return cur.rowcount > 0


def _script_query(where: str = "") -> str:
  return f"""
    SELECT s.id, s.name, s.description, s.created_at, s.updated_at,
      v.source_code, v.runtime_config
    FROM user_strategy s
    JOIN user_strategy_version v ON v.strategy_id=s.id AND v.version_no=s.current_version_no
    WHERE s.owner_user_id=%s {where}
  """


def list_strategy_scripts() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(_script_query("ORDER BY s.updated_at DESC, s.id DESC"), (current_user_id(),))
      return [_normalize_script(row) for row in cur.fetchall()]


def _insert_script_version(cur: Any, script_id: str, version_no: int, payload: dict[str, Any]) -> None:
  source = payload["code"]
  cur.execute("""
    INSERT INTO user_strategy_version (
      id, strategy_id, version_no, source_code, source_checksum, runtime_config, created_by_user_id
    ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s)
  """, (script_id, version_no, source, hashlib.sha256(source.encode("utf-8")).hexdigest(),
         Jsonb({"entryType": payload.get("entryType", "on_bar"), "status": payload.get("status", "draft")}),
         current_user_id()))


def create_strategy_script(payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO user_strategy (id, owner_user_id, name, description, strategy_type, status)
        VALUES (gen_random_uuid(), %s, %s, %s, 'python', 'active') RETURNING id
      """, (current_user_id(), payload["name"], payload.get("description", "")))
      script_id = _uuid(cur.fetchone()["id"])
      _insert_script_version(cur, script_id or "", 1, payload)
  return get_strategy_script(script_id or "")


def update_strategy_script(script_id: str, payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT current_version_no FROM user_strategy WHERE id=%s AND owner_user_id=%s", (script_id, current_user_id()))
      row = cur.fetchone()
      if not row:
        raise KeyError(script_id)
      next_version = int(row["current_version_no"]) + 1
      cur.execute("""
        UPDATE user_strategy SET name=%s, description=%s, current_version_no=%s, updated_at=now()
        WHERE id=%s AND owner_user_id=%s
      """, (payload["name"], payload.get("description", ""), next_version, script_id, current_user_id()))
      _insert_script_version(cur, script_id, next_version, payload)
  return get_strategy_script(script_id)


def get_strategy_script(script_id: str) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(_script_query("AND s.id=%s"), (current_user_id(), script_id))
      row = cur.fetchone()
      if not row:
        raise KeyError(script_id)
      return _normalize_script(row)


def delete_strategy_script(script_id: str) -> bool:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM user_strategy WHERE id=%s AND owner_user_id=%s", (script_id, current_user_id()))
      return cur.rowcount > 0


def _config_query(where: str = "") -> str:
  return f"""
    SELECT c.*, i.symbol, i.exchange,
      CASE WHEN b.id IS NULL THEN NULL ELSE b.symbol || '.' || b.exchange END AS benchmark_symbol
    FROM strategy_config c
    JOIN market_instrument i ON i.id=c.instrument_id
    LEFT JOIN market_instrument b ON b.id=c.benchmark_instrument_id
    WHERE c.owner_user_id=%s {where}
  """


def list_strategy_configs() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(_config_query("ORDER BY c.updated_at DESC, c.id DESC"), (current_user_id(),))
      return [_normalize_config(row) for row in cur.fetchall()]


def _config_snapshot(config: dict[str, Any], strategy_version_id: str | None) -> dict[str, Any]:
  return {
    "name": config["name"], "scriptId": config.get("scriptId"), "strategyVersionId": strategy_version_id,
    "strategy": config["strategy"], "mode": config["mode"], "symbol": config["symbol"],
    "timeframe": config.get("timeframe", "5m"), "benchmarkSymbol": config.get("benchmarkSymbol"),
    "accountId": config.get("accountId"), "paramsJson": _with_script_code_hash(config).get("paramsJson", {}),
  }


def _script_version_id(cur: Any, script_id: str | None) -> str | None:
  if not script_id:
    return None
  cur.execute("""
    SELECT v.id FROM user_strategy s JOIN user_strategy_version v
      ON v.strategy_id=s.id AND v.version_no=s.current_version_no
    WHERE s.id=%s AND s.owner_user_id=%s
  """, (script_id, current_user_id()))
  row = cur.fetchone()
  if not row:
    raise KeyError(script_id)
  return _uuid(row["id"])


def _write_config(cur: Any, config_id: str | None, payload: dict[str, Any]) -> str:
  instrument_id = _instrument_id(cur, payload["symbol"])
  benchmark_id = _instrument_id(cur, payload["benchmarkSymbol"]) if payload.get("benchmarkSymbol") else None
  if config_id is None:
    cur.execute("""
      INSERT INTO strategy_config (
        owner_user_id, name, strategy_id, strategy_key, mode, instrument_id, timeframe,
        benchmark_instrument_id, account_id, params, enabled, published
      ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false) RETURNING id
    """, (current_user_id(), payload["name"], payload.get("scriptId"), payload.get("strategy", "user_script"),
           payload.get("mode", "backtest"), instrument_id, payload.get("timeframe", "5m"), benchmark_id,
           payload.get("accountId"), Jsonb(payload.get("paramsJson", {})), payload.get("enabled", True)))
  else:
    cur.execute("""
      UPDATE strategy_config SET name=%s, strategy_id=%s, strategy_key=%s, mode=%s,
        instrument_id=%s, timeframe=%s, benchmark_instrument_id=%s, account_id=%s, params=%s,
        enabled=%s, published=false, updated_at=now() WHERE id=%s AND owner_user_id=%s RETURNING id
    """, (payload["name"], payload.get("scriptId"), payload.get("strategy", "user_script"),
           payload.get("mode", "backtest"), instrument_id, payload.get("timeframe", "5m"), benchmark_id,
           payload.get("accountId"), Jsonb(payload.get("paramsJson", {})), payload.get("enabled", True),
           config_id, current_user_id()))
  row = cur.fetchone()
  if not row:
    raise KeyError(config_id)
  return _uuid(row["id"]) or ""


def create_strategy_config(payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      config_id = _write_config(cur, None, payload)
  config = get_strategy_config(config_id)
  if payload.get("publish", True):
    publish_strategy_config(config_id)
    return get_strategy_config(config_id)
  return config


def update_strategy_config(config_id: str, payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      _write_config(cur, config_id, payload)
  return get_strategy_config(config_id)


def _version_fingerprint(snapshot: dict[str, Any]) -> str:
  return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def publish_strategy_config(config_id: str) -> tuple[dict[str, Any], bool]:
  config = get_strategy_config(config_id)
  with _db() as conn:
    with conn.cursor() as cur:
      strategy_version_id = _script_version_id(cur, config.get("scriptId"))
      snapshot = _config_snapshot(config, strategy_version_id)
      cur.execute("SELECT id, snapshot FROM strategy_config_version WHERE config_id=%s ORDER BY version_no DESC LIMIT 1", (config_id,))
      previous = cur.fetchone()
      if previous and _version_fingerprint(previous["snapshot"]) == _version_fingerprint(snapshot):
        cur.execute("UPDATE strategy_config SET published=true, updated_at=now() WHERE id=%s", (config_id,))
        return get_strategy_config_version(_uuid(previous["id"]) or ""), False
      cur.execute("SELECT COALESCE(MAX(version_no), 0) + 1 AS value FROM strategy_config_version WHERE config_id=%s", (config_id,))
      version_no = int(cur.fetchone()["value"])
      cur.execute("""
        INSERT INTO strategy_config_version (config_id, version_no, strategy_version_id, snapshot)
        VALUES (%s, %s, %s, %s) RETURNING id
      """, (config_id, version_no, strategy_version_id, Jsonb(snapshot)))
      version_id = _uuid(cur.fetchone()["id"]) or ""
      cur.execute("UPDATE strategy_config SET published=true, updated_at=now() WHERE id=%s", (config_id,))
  return get_strategy_config_version(version_id), True


def get_strategy_config(config_id: str) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(_config_query("AND c.id=%s"), (current_user_id(), config_id))
      row = cur.fetchone()
      if not row:
        raise KeyError(config_id)
      return _normalize_config(row)


def get_strategy_config_version(version_id: str) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT v.* FROM strategy_config_version v
        JOIN strategy_config c ON c.id=v.config_id
        WHERE v.id=%s AND c.owner_user_id=%s
      """, (version_id, current_user_id()))
      row = cur.fetchone()
      if not row:
        raise KeyError(version_id)
      return _normalize_version(row)


def get_latest_strategy_config_version(config_id: str) -> dict[str, Any] | None:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT v.* FROM strategy_config_version v
        JOIN strategy_config c ON c.id=v.config_id
        WHERE v.config_id=%s AND c.owner_user_id=%s
        ORDER BY v.version_no DESC LIMIT 1
      """, (config_id, current_user_id()))
      row = cur.fetchone()
      return _normalize_version(row) if row else None


def list_strategy_config_versions(config_id: str) -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT v.* FROM strategy_config_version v
        JOIN strategy_config c ON c.id=v.config_id
        WHERE v.config_id=%s AND c.owner_user_id=%s ORDER BY v.version_no DESC
      """, (config_id, current_user_id()))
      return [_normalize_version(row) for row in cur.fetchall()]


def delete_strategy_config(config_id: str) -> bool:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM strategy_config WHERE id=%s AND owner_user_id=%s", (config_id, current_user_id()))
      return cur.rowcount > 0


def _with_script_code_hash(payload: dict[str, Any]) -> dict[str, Any]:
  normalized = dict(payload)
  params = dict(normalized.get("paramsJson") or {})
  script_id = normalized.get("scriptId")
  if script_id:
    script = get_strategy_script(str(script_id))
    code_hash = hashlib.sha256(script["code"].encode("utf-8")).hexdigest()
    params["scriptCodeHash"] = code_hash
    params["scriptSnapshot"] = {"id": script["id"], "name": script["name"], "code": script["code"], "entryType": script["entryType"]}
  normalized["paramsJson"] = params
  return normalized


def create_backtest_run(payload: dict[str, Any], status: str = "running") -> str:
  config_id = payload.get("configId")
  version_id = payload.get("configVersionId")
  if config_id and not version_id:
    latest = get_latest_strategy_config_version(str(config_id))
    version_id = latest["id"] if latest else None
  version = get_strategy_config_version(str(version_id)) if version_id else None
  with _db() as conn:
    with conn.cursor() as cur:
      instrument_id = _instrument_id(cur, payload["symbol"])
      benchmark_id = _instrument_id(cur, payload["benchmarkSymbol"]) if payload.get("benchmarkSymbol") else None
      strategy_version_id = version and version["paramsJson"].get("strategyVersionId")
      script_snapshot = (version and version["paramsJson"].get("scriptSnapshot")) or payload.get("draftScriptSnapshot")
      params_snapshot = version and version["paramsJson"]
      db_status = "running" if status == "running" else "queued"
      cur.execute("""
        INSERT INTO backtest_run (
          id, owner_user_id, strategy_version_id, status, requested_params, market_data_snapshot,
          started_at, config_id, config_version_id, account_id, instrument_id,
          benchmark_instrument_id, timeframe, fill_model, script_snapshot, params_snapshot, execution_kind
        ) VALUES (
          gen_random_uuid(), %s, %s, %s, %s, '{}'::jsonb,
          CASE WHEN %s='running' THEN now() ELSE NULL END, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        ) RETURNING id
      """, (current_user_id(), strategy_version_id, db_status, Jsonb(payload), db_status,
             config_id, version_id, payload.get("accountId"), instrument_id, benchmark_id,
             payload.get("timeframe", "5m"), "bar_close_signal_fill", Jsonb(script_snapshot) if script_snapshot else None,
             Jsonb(params_snapshot) if params_snapshot else Jsonb({}), payload.get("executionKind", "published")))
      return _uuid(cur.fetchone()["id"]) or ""


def _run_query(where: str = "") -> str:
  return f"""
    SELECT r.*, s.id AS strategy_id,
      i.symbol || '.' || i.exchange AS symbol,
      b.symbol || '.' || b.exchange AS benchmark_symbol
    FROM backtest_run r
    LEFT JOIN user_strategy_version sv ON sv.id=r.strategy_version_id
    LEFT JOIN user_strategy s ON s.id=sv.strategy_id
    LEFT JOIN market_instrument i ON i.id=r.instrument_id
    LEFT JOIN market_instrument b ON b.id=r.benchmark_instrument_id
    WHERE r.owner_user_id=%s {where}
  """


def list_backtest_runs(limit: int = 30, config_id: str | None = None) -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      query = _run_query("AND r.config_id=%s ORDER BY r.created_at DESC LIMIT %s") if config_id else _run_query("ORDER BY r.created_at DESC LIMIT %s")
      params = (current_user_id(), config_id, limit) if config_id else (current_user_id(), limit)
      cur.execute(query, params)
      return [_normalize_run(row) for row in cur.fetchall()]


def get_backtest_run(run_id: str) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(_run_query("AND r.id=%s"), (current_user_id(), run_id))
      row = cur.fetchone()
      if not row:
        raise KeyError(run_id)
      return _normalize_run(row)


def update_backtest_run_meta(run_id: str, note: str, tags: list[str]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("UPDATE backtest_run SET note=%s, tags=%s WHERE id=%s AND owner_user_id=%s", (note, Jsonb(tags), run_id, current_user_id()))
      if cur.rowcount == 0:
        raise KeyError(run_id)
  return get_backtest_run(run_id)


def finish_backtest_run(run_id: str, status: str, summary: dict[str, Any], output_dir: str, result: dict[str, Any] | None = None) -> None:
  db_status = "succeeded" if status == "ok" else "failed"
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_run SET status=%s, result_summary=%s, result_json=%s,
          output_dir=%s, error_message=%s, finished_at=now()
        WHERE id=%s AND owner_user_id=%s
      """, (db_status, Jsonb(summary), Jsonb(result) if result else None, output_dir,
             summary.get("error") if status != "ok" else None, run_id, current_user_id()))
      if cur.rowcount == 0:
        raise KeyError(run_id)


def _date_value(value: str | None) -> datetime.date:
  return datetime.strptime(value or "19700101", "%Y%m%d").date()


def log_data_sync_run(payload: dict[str, Any], result: dict[str, Any]) -> int:
  with _db() as conn:
    with conn.cursor() as cur:
      instrument_id = _instrument_id(cur, payload["symbol"])
      outcome = result.get("cacheStatus") if result.get("status") == "ok" else "failed"
      if outcome not in {"cache_hit", "fetched", "failed"}:
        outcome = "fetched" if result.get("status") == "ok" else "failed"
      details = {"timeframe": payload.get("timeframe", "5m"), "sourceTimeframe": payload.get("timeframe", "5m"),
                 "mode": payload.get("mode", "incremental"), "newDates": result.get("new_dates", []),
                 "outputPath": "postgresql://market_minute_bar", "elapsedMs": result.get("elapsed_ms", 0),
                 "message": result.get("message", "")}
      cur.execute("""
        INSERT INTO market_fetch_log (
          instrument_id, requested_by_user_id, period, adjustment, source, requested_from,
          requested_to, outcome, row_count, error_message, finished_at, details
        ) VALUES (%s,%s,%s,'none','eastmoney',%s,%s,%s,%s,%s,now(),%s) RETURNING id
      """, (instrument_id, current_user_id(), payload.get("timeframe", "5m"),
             _date_value(payload.get("startDate")), _date_value(payload.get("endDate")), outcome, int(result.get("new_rows", 0)),
             result.get("message") if outcome == "failed" else None, Jsonb(details)))
      return int(cur.fetchone()["id"])


def list_data_sync_runs(limit: int = 20) -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT f.*, i.symbol, i.exchange FROM market_fetch_log f
        LEFT JOIN market_instrument i ON i.id=f.instrument_id
        WHERE f.requested_by_user_id=%s ORDER BY f.started_at DESC LIMIT %s
      """, (current_user_id(), limit))
      rows = []
      for row in cur.fetchall():
        details = _json(row["details"], {})
        rows.append({
          "id": int(row["id"]), "symbol": f"{row['symbol']}.{row['exchange']}",
          "timeframe": details.get("timeframe", "5m"), "sourceTimeframe": details.get("sourceTimeframe", ""),
          "startDate": row["requested_from"].strftime("%Y%m%d"), "endDate": row["requested_to"].strftime("%Y%m%d"),
          "mode": details.get("mode", "incremental"), "status": "ok" if row["outcome"] in {"fetched", "cache_hit"} else "error",
          "newRows": int(row["row_count"]), "newDates": details.get("newDates", []),
          "outputPath": details.get("outputPath", ""), "elapsedMs": int(details.get("elapsedMs", 0)),
          "message": details.get("message", row.get("error_message") or ""), "createdAt": _format_time(row["started_at"]),
        })
      return rows
