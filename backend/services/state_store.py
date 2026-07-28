# -*- coding: utf-8 -*-
"""MySQL 状态库：保存 viewer 自定义标的、回测账号和回测记录。"""

from decimal import Decimal
import hashlib
import json
import re
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from backend import settings

_SCHEMA_READY = False


def _validate_database_name(name: str) -> None:
  """避免把环境变量里的库名直接拼进 SQL。"""
  if not re.match(r"^[A-Za-z0-9_]+$", name):
    raise RuntimeError(f"非法数据库名: {name}")


def _connect(database: str | None = None) -> pymysql.connections.Connection:
  return pymysql.connect(
    host=settings.MYSQL_HOST,
    port=settings.MYSQL_PORT,
    user=settings.MYSQL_USER,
    password=settings.MYSQL_PASSWORD,
    database=database,
    charset="utf8mb4",
    cursorclass=DictCursor,
    autocommit=True,
    connect_timeout=5,
  )


def ensure_schema() -> None:
  """创建数据库和基础表；可重复调用。"""
  global _SCHEMA_READY
  if _SCHEMA_READY:
    return

  _validate_database_name(settings.MYSQL_DATABASE)
  with _connect() as conn:
    with conn.cursor() as cur:
      cur.execute(
        f"CREATE DATABASE IF NOT EXISTS `{settings.MYSQL_DATABASE}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
      )

  with _connect(settings.MYSQL_DATABASE) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        CREATE TABLE IF NOT EXISTS viewer_instruments (
          symbol VARCHAR(16) PRIMARY KEY,
          name VARCHAR(64) NOT NULL DEFAULT '',
          market VARCHAR(8) NOT NULL,
          enabled TINYINT(1) NOT NULL DEFAULT 1,
          note VARCHAR(255) NOT NULL DEFAULT '',
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      """)
      cur.execute("""
        CREATE TABLE IF NOT EXISTS viewer_accounts (
          id INT AUTO_INCREMENT PRIMARY KEY,
          name VARCHAR(64) NOT NULL UNIQUE,
          initial_capital DECIMAL(18, 2) NOT NULL,
          commission_rate DECIMAL(10, 6) NOT NULL DEFAULT 0,
          slippage DECIMAL(10, 4) NOT NULL DEFAULT 0,
          position_mode VARCHAR(32) NOT NULL DEFAULT 'fixed_cash',
          order_cash DECIMAL(18, 2) NULL,
          order_pct DECIMAL(10, 6) NULL,
          allow_short TINYINT(1) NOT NULL DEFAULT 1,
          is_default TINYINT(1) NOT NULL DEFAULT 0,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      """)
      cur.execute("""
        CREATE TABLE IF NOT EXISTS viewer_backtest_runs (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          account_id INT NULL,
          symbol VARCHAR(16) NOT NULL,
          timeframe VARCHAR(16) NOT NULL DEFAULT '5m',
          strategy VARCHAR(64) NOT NULL,
          fill_model VARCHAR(64) NOT NULL DEFAULT 'bar_close_signal_fill',
          script_snapshot_json JSON NULL,
          params_snapshot_json JSON NULL,
          data_version_json JSON NULL,
          start_date VARCHAR(8) NOT NULL,
          end_date VARCHAR(8) NOT NULL,
          params_json JSON NOT NULL,
          status VARCHAR(16) NOT NULL,
          summary_json JSON NULL,
          output_dir VARCHAR(255) NOT NULL DEFAULT '',
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          finished_at TIMESTAMP NULL,
          INDEX idx_symbol_created (symbol, created_at),
          INDEX idx_account_created (account_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      """)
      cur.execute("""
        CREATE TABLE IF NOT EXISTS viewer_data_sync_runs (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          symbol VARCHAR(16) NOT NULL,
          timeframe VARCHAR(16) NOT NULL,
          source_timeframe VARCHAR(16) NOT NULL DEFAULT '',
          start_date VARCHAR(8) NOT NULL DEFAULT '',
          end_date VARCHAR(8) NOT NULL DEFAULT '',
          mode VARCHAR(16) NOT NULL DEFAULT 'incremental',
          status VARCHAR(16) NOT NULL,
          new_rows INT NOT NULL DEFAULT 0,
          new_dates_json JSON NULL,
          output_path VARCHAR(500) NOT NULL DEFAULT '',
          elapsed_ms INT NOT NULL DEFAULT 0,
          message VARCHAR(500) NOT NULL DEFAULT '',
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_symbol_created (symbol, created_at),
          INDEX idx_status_created (status, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      """)
      cur.execute("""
        CREATE TABLE IF NOT EXISTS viewer_strategy_scripts (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          name VARCHAR(64) NOT NULL UNIQUE,
          description VARCHAR(500) NOT NULL DEFAULT '',
          language VARCHAR(16) NOT NULL DEFAULT 'python',
          code MEDIUMTEXT NOT NULL,
          entry_type VARCHAR(32) NOT NULL DEFAULT 'on_bar',
          status VARCHAR(16) NOT NULL DEFAULT 'draft',
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,
          INDEX idx_status_updated (status, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      """)
      cur.execute("""
        CREATE TABLE IF NOT EXISTS viewer_strategy_configs (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          name VARCHAR(64) NOT NULL UNIQUE,
          script_id BIGINT NULL,
          strategy VARCHAR(64) NOT NULL DEFAULT 'trend_grid',
          mode VARCHAR(16) NOT NULL DEFAULT 'backtest',
          symbol VARCHAR(16) NOT NULL,
          timeframe VARCHAR(16) NOT NULL DEFAULT '5m',
          benchmark_symbol VARCHAR(16) NULL,
          account_id INT NULL,
          params_json JSON NOT NULL,
          enabled TINYINT(1) NOT NULL DEFAULT 1,
          published TINYINT(1) NOT NULL DEFAULT 1,
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            ON UPDATE CURRENT_TIMESTAMP,
          INDEX idx_mode_updated (mode, updated_at),
          INDEX idx_symbol_updated (symbol, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      """)
      cur.execute("""
        CREATE TABLE IF NOT EXISTS viewer_strategy_config_versions (
          id BIGINT AUTO_INCREMENT PRIMARY KEY,
          config_id BIGINT NOT NULL,
          version_no INT NOT NULL,
          name VARCHAR(64) NOT NULL,
          script_id BIGINT NULL,
          strategy VARCHAR(64) NOT NULL DEFAULT 'trend_grid',
          mode VARCHAR(16) NOT NULL DEFAULT 'backtest',
          symbol VARCHAR(16) NOT NULL,
          timeframe VARCHAR(16) NOT NULL DEFAULT '5m',
          benchmark_symbol VARCHAR(16) NULL,
          account_id INT NULL,
          params_json JSON NOT NULL,
          change_note VARCHAR(500) NOT NULL DEFAULT '',
          created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_config_version (config_id, version_no),
          INDEX idx_config_created (config_id, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
      """)
      _ensure_column(cur, "viewer_backtest_runs", "config_id", "BIGINT NULL")
      _ensure_column(cur, "viewer_backtest_runs", "config_version_id", "BIGINT NULL")
      _ensure_column(cur, "viewer_backtest_runs", "script_id", "BIGINT NULL")
      _ensure_column(cur, "viewer_backtest_runs", "benchmark_symbol", "VARCHAR(16) NULL")
      _ensure_column(cur, "viewer_backtest_runs", "timeframe", "VARCHAR(16) NOT NULL DEFAULT '5m'")
      _ensure_column(cur, "viewer_backtest_runs", "fill_model",
                     "VARCHAR(64) NOT NULL DEFAULT 'bar_close_signal_fill'")
      _ensure_column(cur, "viewer_backtest_runs", "script_snapshot_json", "JSON NULL")
      _ensure_column(cur, "viewer_backtest_runs", "params_snapshot_json", "JSON NULL")
      _ensure_column(cur, "viewer_backtest_runs", "data_version_json", "JSON NULL")
      _ensure_column(cur, "viewer_backtest_runs", "note", "VARCHAR(500) NOT NULL DEFAULT ''")
      _ensure_column(cur, "viewer_backtest_runs", "tags_json", "JSON NULL")
      _ensure_column(cur, "viewer_backtest_runs", "result_json", "JSON NULL")
      _ensure_column(cur, "viewer_strategy_configs", "timeframe",
                     "VARCHAR(16) NOT NULL DEFAULT '5m'")
      _ensure_column(cur, "viewer_strategy_configs", "published",
                     "TINYINT(1) NOT NULL DEFAULT 1")
      _ensure_column(cur, "viewer_strategy_config_versions", "timeframe",
                     "VARCHAR(16) NOT NULL DEFAULT '5m'")
      _ensure_column(cur, "viewer_data_sync_runs", "source_timeframe",
                     "VARCHAR(16) NOT NULL DEFAULT ''")
      _ensure_column(cur, "viewer_data_sync_runs", "output_path",
                     "VARCHAR(500) NOT NULL DEFAULT ''")
      _ensure_column(cur, "viewer_data_sync_runs", "elapsed_ms",
                     "INT NOT NULL DEFAULT 0")
  _SCHEMA_READY = True
  seed_default_account()
  backfill_strategy_config_versions()


def _db() -> pymysql.connections.Connection:
  ensure_schema()
  return _connect(settings.MYSQL_DATABASE)


def _ensure_column(cur: Any, table: str, column: str, ddl: str) -> None:
  cur.execute("""
    SELECT COUNT(*) AS count
    FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
  """, (settings.MYSQL_DATABASE, table, column))
  if int(cur.fetchone()["count"]) == 0:
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _to_float(value: Any) -> float | None:
  if value is None:
    return None
  if isinstance(value, Decimal):
    return float(value)
  return float(value)


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


def _format_time(value: Any) -> str:
  return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _json_loads(value: Any, fallback: Any) -> Any:
  if value is None:
    return fallback
  if isinstance(value, str):
    return json.loads(value)
  return value


def _normalize_strategy_script(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": int(row["id"]),
    "name": row["name"],
    "description": row["description"],
    "language": row["language"],
    "code": row["code"],
    "entryType": row["entry_type"],
    "status": row["status"],
    "createdAt": _format_time(row["created_at"]),
    "updatedAt": _format_time(row["updated_at"]),
  }


def _normalize_strategy_config(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": int(row["id"]),
    "name": row["name"],
    "scriptId": int(row["script_id"]) if row["script_id"] is not None else None,
    "strategy": row["strategy"],
    "mode": row["mode"],
    "symbol": row["symbol"],
    "timeframe": row.get("timeframe", "5m"),
    "benchmarkSymbol": row["benchmark_symbol"],
    "accountId": int(row["account_id"]) if row["account_id"] is not None else None,
    "paramsJson": _json_loads(row["params_json"], {}),
    "enabled": bool(row["enabled"]),
    "published": bool(row.get("published", 1)),
    "createdAt": _format_time(row["created_at"]),
    "updatedAt": _format_time(row["updated_at"]),
  }


def _normalize_strategy_config_version(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": int(row["id"]),
    "configId": int(row["config_id"]),
    "versionNo": int(row["version_no"]),
    "name": row["name"],
    "scriptId": int(row["script_id"]) if row["script_id"] is not None else None,
    "strategy": row["strategy"],
    "mode": row["mode"],
    "symbol": row["symbol"],
    "timeframe": row.get("timeframe", "5m"),
    "benchmarkSymbol": row["benchmark_symbol"],
    "accountId": int(row["account_id"]) if row["account_id"] is not None else None,
    "paramsJson": _json_loads(row["params_json"], {}),
    "changeNote": row["change_note"],
    "createdAt": _format_time(row["created_at"]),
  }


def _normalize_backtest_run(row: dict[str, Any]) -> dict[str, Any]:
  summary = _json_loads(row.get("summary_json"), {})
  params = _json_loads(row.get("params_json"), {})
  tags = _json_loads(row.get("tags_json"), [])
  result = _json_loads(row.get("result_json"), None)
  return {
    "id": int(row["id"]),
    "configId": int(row["config_id"]) if row.get("config_id") is not None else None,
    "configVersionId": (
      int(row["config_version_id"]) if row.get("config_version_id") is not None else None
    ),
    "scriptId": int(row["script_id"]) if row.get("script_id") is not None else None,
    "accountId": int(row["account_id"]) if row.get("account_id") is not None else None,
    "symbol": row["symbol"],
    "timeframe": row.get("timeframe", "5m"),
    "fillModel": row.get("fill_model", "bar_close_signal_fill"),
    "scriptSnapshot": _json_loads(row.get("script_snapshot_json"), None),
    "paramsSnapshot": _json_loads(row.get("params_snapshot_json"), {}),
    "dataVersion": _json_loads(row.get("data_version_json"), None),
    "benchmarkSymbol": row.get("benchmark_symbol"),
    "strategy": row["strategy"],
    "startDate": row["start_date"],
    "endDate": row["end_date"],
    "paramsJson": params,
    "status": row["status"],
    "summaryJson": summary,
    "outputDir": row["output_dir"],
    "note": row.get("note", ""),
    "tags": tags if isinstance(tags, list) else [],
    "resultJson": result if isinstance(result, dict) else None,
    "createdAt": _format_time(row["created_at"]),
    "finishedAt": _format_time(row["finished_at"]),
  }


def seed_default_account() -> None:
  """没有账号时创建一个本机默认回测账号。"""
  with _connect(settings.MYSQL_DATABASE) as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT COUNT(*) AS count FROM viewer_accounts")
      if int(cur.fetchone()["count"]) > 0:
        return
      cur.execute("""
        INSERT INTO viewer_accounts (
          name, initial_capital, commission_rate, slippage,
          position_mode, order_cash, order_pct, allow_short, is_default
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
      """, (
        "default", 1000000, 0.0003, 0,
        "fixed_cash", 100000, None, 1, 1,
      ))


def backfill_strategy_config_versions() -> None:
  """旧配置没有版本记录时，补一条初始化版本。"""
  with _connect(settings.MYSQL_DATABASE) as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT c.id, c.name, c.script_id, c.strategy, c.mode, c.symbol, c.timeframe,
               c.benchmark_symbol, c.account_id, c.params_json
        FROM viewer_strategy_configs c
        LEFT JOIN viewer_strategy_config_versions v ON v.config_id = c.id
        WHERE v.id IS NULL AND c.published = 1
      """)
      rows = cur.fetchall()
      for row in rows:
        cur.execute("""
          INSERT INTO viewer_strategy_config_versions (
            config_id, version_no, name, script_id, strategy, mode, symbol, timeframe,
            benchmark_symbol, account_id, params_json, change_note
          ) VALUES (%s, 1, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), %s)
        """, (
          row["id"],
          row["name"],
          row["script_id"],
          row["strategy"],
          row["mode"],
          row["symbol"],
          row["timeframe"],
          row["benchmark_symbol"],
          row["account_id"],
          json.dumps(_json_loads(row["params_json"], {}), ensure_ascii=False),
          "初始化历史版本",
        ))


def list_instruments() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT symbol, name, market, enabled, note
        FROM viewer_instruments
        ORDER BY symbol
      """)
      return [
        {
          "symbol": row["symbol"],
          "name": row["name"],
          "market": row["market"],
          "enabled": bool(row["enabled"]),
          "note": row["note"],
          "hasKline": False,
          "hasSignal": False,
        }
        for row in cur.fetchall()
      ]


def upsert_instrument(payload: dict[str, Any]) -> dict[str, Any]:
  symbol = payload["symbol"]
  market = symbol.split(".")[1]
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO viewer_instruments (symbol, name, market, enabled, note)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          name = VALUES(name),
          market = VALUES(market),
          enabled = VALUES(enabled),
          note = VALUES(note)
      """, (
        symbol,
        payload.get("name", ""),
        market,
        1 if payload.get("enabled", True) else 0,
        payload.get("note", ""),
      ))
  return get_instrument(symbol)


def update_instrument(symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
  market = symbol.split(".")[1]
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO viewer_instruments (symbol, name, market, enabled, note)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          name = VALUES(name),
          enabled = VALUES(enabled),
          note = VALUES(note)
      """, (
        symbol,
        payload.get("name", ""),
        market,
        1 if payload.get("enabled", True) else 0,
        payload.get("note", ""),
      ))
  return get_instrument(symbol)


def get_instrument(symbol: str) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT symbol, name, market, enabled, note
        FROM viewer_instruments
        WHERE symbol = %s
      """, (symbol,))
      row = cur.fetchone()
      if not row:
        raise KeyError(symbol)
      return {
        "symbol": row["symbol"],
        "name": row["name"],
        "market": row["market"],
        "enabled": bool(row["enabled"]),
        "note": row["note"],
        "hasKline": False,
        "hasSignal": False,
      }


def delete_instrument(symbol: str) -> bool:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM viewer_instruments WHERE symbol = %s", (symbol,))
      return cur.rowcount > 0


def list_accounts() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, name, initial_capital, commission_rate, slippage,
               position_mode, order_cash, order_pct, allow_short, is_default
        FROM viewer_accounts
        ORDER BY is_default DESC, id
      """)
      return [_normalize_account(row) for row in cur.fetchall()]


def create_account(payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      if payload.get("isDefault"):
        cur.execute("UPDATE viewer_accounts SET is_default = 0")
      cur.execute("""
        INSERT INTO viewer_accounts (
          name, initial_capital, commission_rate, slippage,
          position_mode, order_cash, order_pct, allow_short, is_default
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
      """, (
        payload["name"],
        payload["initialCapital"],
        payload["commissionRate"],
        payload["slippage"],
        payload["positionMode"],
        payload.get("orderCash"),
        payload.get("orderPct"),
        1 if payload.get("allowShort", True) else 0,
        1 if payload.get("isDefault", False) else 0,
      ))
      account_id = int(cur.lastrowid)
  return get_account(account_id)


def update_account(account_id: int, payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      if payload.get("isDefault"):
        cur.execute("UPDATE viewer_accounts SET is_default = 0 WHERE id <> %s", (account_id,))
      cur.execute("""
        UPDATE viewer_accounts
        SET name = %s,
            initial_capital = %s,
            commission_rate = %s,
            slippage = %s,
            position_mode = %s,
            order_cash = %s,
            order_pct = %s,
            allow_short = %s,
            is_default = %s
        WHERE id = %s
      """, (
        payload["name"],
        payload["initialCapital"],
        payload["commissionRate"],
        payload["slippage"],
        payload["positionMode"],
        payload.get("orderCash"),
        payload.get("orderPct"),
        1 if payload.get("allowShort", True) else 0,
        1 if payload.get("isDefault", False) else 0,
        account_id,
      ))
      if cur.rowcount == 0:
        raise KeyError(account_id)
  return get_account(account_id)


def get_account(account_id: int) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, name, initial_capital, commission_rate, slippage,
               position_mode, order_cash, order_pct, allow_short, is_default
        FROM viewer_accounts
        WHERE id = %s
      """, (account_id,))
      row = cur.fetchone()
      if not row:
        raise KeyError(account_id)
      return _normalize_account(row)


def delete_account(account_id: int) -> bool:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM viewer_accounts WHERE id = %s", (account_id,))
      return cur.rowcount > 0


def list_strategy_scripts() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, name, description, language, code, entry_type, status,
               created_at, updated_at
        FROM viewer_strategy_scripts
        ORDER BY updated_at DESC, id DESC
      """)
      return [_normalize_strategy_script(row) for row in cur.fetchall()]


def create_strategy_script(payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO viewer_strategy_scripts (
          name, description, language, code, entry_type, status
        ) VALUES (%s, %s, %s, %s, %s, %s)
      """, (
        payload["name"],
        payload.get("description", ""),
        "python",
        payload["code"],
        payload.get("entryType", "on_bar"),
        payload.get("status", "draft"),
      ))
      script_id = int(cur.lastrowid)
  return get_strategy_script(script_id)


def update_strategy_script(script_id: int, payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE viewer_strategy_scripts
        SET name = %s,
            description = %s,
            code = %s,
            entry_type = %s,
            status = %s
        WHERE id = %s
      """, (
        payload["name"],
        payload.get("description", ""),
        payload["code"],
        payload.get("entryType", "on_bar"),
        payload.get("status", "draft"),
        script_id,
      ))
  return get_strategy_script(script_id)


def get_strategy_script(script_id: int) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, name, description, language, code, entry_type, status,
               created_at, updated_at
        FROM viewer_strategy_scripts
        WHERE id = %s
      """, (script_id,))
      row = cur.fetchone()
      if not row:
        raise KeyError(script_id)
      return _normalize_strategy_script(row)


def delete_strategy_script(script_id: int) -> bool:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM viewer_strategy_scripts WHERE id = %s", (script_id,))
      return cur.rowcount > 0


def list_strategy_configs() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, name, script_id, strategy, mode, symbol, timeframe, benchmark_symbol,
               account_id, params_json, enabled, published, created_at, updated_at
        FROM viewer_strategy_configs
        ORDER BY updated_at DESC, id DESC
      """)
      return [_normalize_strategy_config(row) for row in cur.fetchall()]


def _create_strategy_config_version(
  config_id: int,
  payload: dict[str, Any],
  change_note: str,
) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version
        FROM viewer_strategy_config_versions
        WHERE config_id = %s
      """, (config_id,))
      version_no = int(cur.fetchone()["next_version"])
      cur.execute("""
        INSERT INTO viewer_strategy_config_versions (
          config_id, version_no, name, script_id, strategy, mode, symbol, timeframe,
          benchmark_symbol, account_id, params_json, change_note
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), %s)
      """, (
        config_id,
        version_no,
        payload["name"],
        payload.get("scriptId"),
        payload.get("strategy", "trend_grid"),
        payload.get("mode", "backtest"),
        payload["symbol"],
        payload.get("timeframe", "5m"),
        payload.get("benchmarkSymbol"),
        payload.get("accountId"),
        json.dumps(payload.get("paramsJson", {}), ensure_ascii=False),
        change_note,
      ))
      version_id = int(cur.lastrowid)
  return get_strategy_config_version(version_id)


def _with_script_code_hash(payload: dict[str, Any]) -> dict[str, Any]:
  """由后端保存脚本快照和代码指纹，避免草稿覆盖已发布版本。"""
  normalized = dict(payload)
  params_json = dict(payload.get("paramsJson", {}))
  script_id = payload.get("scriptId")
  if script_id is not None:
    script = get_strategy_script(int(script_id))
    code_hash = hashlib.sha256(script["code"].encode("utf-8")).hexdigest()
    params_json["scriptCodeHash"] = code_hash
    params_json["scriptSnapshot"] = {
      "id": script.get("id", int(script_id)),
      "name": script.get("name", f"策略脚本 {script_id}"),
      "description": script.get("description", ""),
      "code": script["code"],
      "entryType": script.get("entryType", "on_bar"),
      "codeHash": code_hash,
    }
  else:
    params_json.pop("scriptCodeHash", None)
    params_json.pop("scriptSnapshot", None)
  normalized["paramsJson"] = params_json
  return normalized


def _version_fingerprint(snapshot: dict[str, Any]) -> str:
  """仅按影响执行结果的字段判断是否需要发布新版本。"""
  params_json = dict(snapshot.get("paramsJson", {}))
  code_snapshot = params_json.get("scriptSnapshot", {})
  code_hash = params_json.get("scriptCodeHash")
  if isinstance(code_snapshot, dict):
    code_hash = code_snapshot.get("codeHash", code_hash)
  effective = {
    "strategy": snapshot.get("strategy"),
    "mode": snapshot.get("mode"),
    "symbol": snapshot.get("symbol"),
    "timeframe": snapshot.get("timeframe", "5m"),
    "benchmarkSymbol": snapshot.get("benchmarkSymbol"),
    "accountId": snapshot.get("accountId"),
    "enabled": bool(snapshot.get("enabled", True)),
    "params": params_json.get("params", {}),
    "scriptCodeHash": code_hash,
  }
  serialized = json.dumps(effective, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def create_strategy_config(payload: dict[str, Any]) -> dict[str, Any]:
  publish = bool(payload.pop("publish", True))
  payload = _with_script_code_hash(payload)
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO viewer_strategy_configs (
          name, script_id, strategy, mode, symbol, timeframe, benchmark_symbol,
          account_id, params_json, enabled, published
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), %s, %s)
      """, (
        payload["name"],
        payload.get("scriptId"),
        payload.get("strategy", "trend_grid"),
        payload.get("mode", "backtest"),
        payload["symbol"],
        payload.get("timeframe", "5m"),
        payload.get("benchmarkSymbol"),
        payload.get("accountId"),
        json.dumps(payload.get("paramsJson", {}), ensure_ascii=False),
        1 if payload.get("enabled", True) else 0,
        1 if publish else 0,
      ))
      config_id = int(cur.lastrowid)
  if publish:
    _create_strategy_config_version(config_id, payload, "创建配置")
  return get_strategy_config(config_id)


def update_strategy_config(config_id: int, payload: dict[str, Any]) -> dict[str, Any]:
  payload = _with_script_code_hash(payload)
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE viewer_strategy_configs
        SET name = %s,
            script_id = %s,
            strategy = %s,
            mode = %s,
            symbol = %s,
            timeframe = %s,
            benchmark_symbol = %s,
            account_id = %s,
            params_json = CAST(%s AS JSON),
            enabled = %s
        WHERE id = %s
      """, (
        payload["name"],
        payload.get("scriptId"),
        payload.get("strategy", "trend_grid"),
        payload.get("mode", "backtest"),
        payload["symbol"],
        payload.get("timeframe", "5m"),
        payload.get("benchmarkSymbol"),
        payload.get("accountId"),
        json.dumps(payload.get("paramsJson", {}), ensure_ascii=False),
        1 if payload.get("enabled", True) else 0,
        config_id,
      ))
  return get_strategy_config(config_id)


def publish_strategy_config(config_id: int) -> tuple[dict[str, Any], bool]:
  """将当前草稿发布为不可变版本；等价内容不会重复生成版本。"""
  config = get_strategy_config(config_id)
  payload = _with_script_code_hash(config)
  latest_version = get_latest_strategy_config_version(config_id)
  if latest_version and _version_fingerprint(latest_version) == _version_fingerprint(payload):
    _set_strategy_config_published(config_id)
    return latest_version, False
  version = _create_strategy_config_version(config_id, payload, "发布新版本")
  _set_strategy_config_published(config_id)
  return version, True


def _set_strategy_config_published(config_id: int) -> None:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("UPDATE viewer_strategy_configs SET published = 1 WHERE id = %s", (config_id,))


def get_strategy_config(config_id: int) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, name, script_id, strategy, mode, symbol, timeframe, benchmark_symbol,
               account_id, params_json, enabled, published, created_at, updated_at
        FROM viewer_strategy_configs
        WHERE id = %s
      """, (config_id,))
      row = cur.fetchone()
      if not row:
        raise KeyError(config_id)
      return _normalize_strategy_config(row)


def get_strategy_config_version(version_id: int) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, config_id, version_no, name, script_id, strategy, mode,
               symbol, timeframe, benchmark_symbol, account_id, params_json,
               change_note, created_at
        FROM viewer_strategy_config_versions
        WHERE id = %s
      """, (version_id,))
      row = cur.fetchone()
      if not row:
        raise KeyError(version_id)
      return _normalize_strategy_config_version(row)


def get_latest_strategy_config_version(config_id: int) -> dict[str, Any] | None:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, config_id, version_no, name, script_id, strategy, mode,
               symbol, timeframe, benchmark_symbol, account_id, params_json,
               change_note, created_at
        FROM viewer_strategy_config_versions
        WHERE config_id = %s
        ORDER BY version_no DESC
        LIMIT 1
      """, (config_id,))
      row = cur.fetchone()
      return _normalize_strategy_config_version(row) if row else None


def list_strategy_config_versions(config_id: int) -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, config_id, version_no, name, script_id, strategy, mode,
               symbol, timeframe, benchmark_symbol, account_id, params_json,
               change_note, created_at
        FROM viewer_strategy_config_versions
        WHERE config_id = %s
        ORDER BY version_no DESC
      """, (config_id,))
      return [_normalize_strategy_config_version(row) for row in cur.fetchall()]


def delete_strategy_config(config_id: int) -> bool:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("DELETE FROM viewer_strategy_config_versions WHERE config_id = %s", (config_id,))
      cur.execute("DELETE FROM viewer_strategy_configs WHERE id = %s", (config_id,))
      return cur.rowcount > 0


def create_backtest_run(payload: dict[str, Any], status: str = "running") -> int:
  config_id = payload.get("configId")
  config_version_id = payload.get("configVersionId")
  if config_id and not config_version_id:
    latest_version = get_latest_strategy_config_version(int(config_id))
    config_version_id = latest_version["id"] if latest_version else None

  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO viewer_backtest_runs (
          config_id, config_version_id, script_id, account_id,
          symbol, timeframe, benchmark_symbol, strategy, fill_model,
          script_snapshot_json, params_snapshot_json, data_version_json,
          start_date, end_date, params_json, status, output_dir, note, tags_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), CAST(%s AS JSON), CAST(%s AS JSON), %s, %s, CAST(%s AS JSON), %s, %s, %s, CAST(%s AS JSON))
      """, (
        config_id,
        config_version_id,
        payload.get("scriptId"),
        payload.get("accountId"),
        payload["symbol"],
        payload.get("timeframe", "5m"),
        payload.get("benchmarkSymbol"),
        payload["strategy"],
        "bar_close_signal_fill",
        json.dumps({}, ensure_ascii=False),
        json.dumps(payload.get("strategyParams", {}), ensure_ascii=False),
        json.dumps({}, ensure_ascii=False),
        payload["startDate"],
        payload["endDate"],
        json.dumps(payload, ensure_ascii=False),
        status,
        "",
        "",
        json.dumps([], ensure_ascii=False),
      ))
      return int(cur.lastrowid)


def list_backtest_runs(limit: int = 30, config_id: int | None = None) -> list[dict[str, Any]]:
  safe_limit = max(1, min(limit, 100))
  where = ""
  params: list[Any] = []
  if config_id is not None:
    where = "WHERE config_id = %s"
    params.append(config_id)
  params.append(safe_limit)
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(f"""
        SELECT id, config_id, config_version_id, script_id, account_id,
               symbol, timeframe, benchmark_symbol, strategy, fill_model,
               script_snapshot_json, params_snapshot_json, data_version_json,
               start_date, end_date, params_json, status, summary_json, result_json, output_dir, note, tags_json,
               created_at, finished_at
        FROM viewer_backtest_runs
        {where}
        ORDER BY id DESC
        LIMIT %s
      """, params)
      return [_normalize_backtest_run(row) for row in cur.fetchall()]


def get_backtest_run(run_id: int) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, config_id, config_version_id, script_id, account_id,
               symbol, timeframe, benchmark_symbol, strategy, fill_model,
               script_snapshot_json, params_snapshot_json, data_version_json,
               start_date, end_date, params_json, status, summary_json, result_json, output_dir, note, tags_json,
               created_at, finished_at
        FROM viewer_backtest_runs
        WHERE id = %s
      """, (run_id,))
      row = cur.fetchone()
      if not row:
        raise KeyError(run_id)
      return _normalize_backtest_run(row)


def update_backtest_run_meta(run_id: int, note: str, tags: list[str]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE viewer_backtest_runs
        SET note = %s,
            tags_json = CAST(%s AS JSON)
        WHERE id = %s
      """, (
        note,
        json.dumps(tags, ensure_ascii=False),
        run_id,
      ))
      if cur.rowcount == 0:
        raise KeyError(run_id)
  return get_backtest_run(run_id)


def finish_backtest_run(
  run_id: int,
  status: str,
  summary: dict[str, Any] | None,
  output_dir: str,
  result: dict[str, Any] | None = None,
) -> None:
  manifest = (result or {}).get("experimentManifest", {})
  data_version = {
    "dataHash": manifest.get("dataHash"),
    "dataFiles": manifest.get("dataFiles", []),
  }
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE viewer_backtest_runs
        SET status = %s,
            summary_json = CAST(%s AS JSON),
            result_json = CAST(%s AS JSON),
            output_dir = %s,
            timeframe = %s,
            fill_model = %s,
            script_snapshot_json = CAST(%s AS JSON),
            data_version_json = CAST(%s AS JSON),
            finished_at = CURRENT_TIMESTAMP
        WHERE id = %s
      """, (
        status,
        json.dumps(summary or {}, ensure_ascii=False),
        json.dumps(result or {}, ensure_ascii=False),
        output_dir,
        manifest.get("barRange", {}).get("timeframe", "5m"),
        manifest.get("fillModel", "bar_close_signal_fill"),
        json.dumps(manifest.get("strategySource", {}), ensure_ascii=False),
        json.dumps(data_version, ensure_ascii=False),
        run_id,
      ))


def log_data_sync_run(payload: dict[str, Any], result: dict[str, Any]) -> int:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        INSERT INTO viewer_data_sync_runs (
          symbol, timeframe, source_timeframe, start_date, end_date,
          mode, status, new_rows, new_dates_json, output_path,
          elapsed_ms, message
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CAST(%s AS JSON), %s, %s, %s)
      """, (
        payload.get("symbol", result.get("symbol", "")),
        payload.get("timeframe", result.get("timeframe", "5m")),
        result.get("source_timeframe", ""),
        payload.get("startDate") or result.get("from", ""),
        payload.get("endDate") or result.get("to", ""),
        payload.get("mode", "incremental"),
        result.get("status", "error"),
        int(result.get("new_rows", 0) or 0),
        json.dumps(result.get("new_dates", []), ensure_ascii=False),
        result.get("path", ""),
        int(result.get("elapsed_ms", 0) or 0),
        result.get("message", ""),
      ))
      return int(cur.lastrowid)


def list_data_sync_runs(limit: int = 20) -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id, symbol, timeframe, source_timeframe, start_date, end_date,
               mode, status, new_rows, new_dates_json, output_path,
               elapsed_ms, message, created_at
        FROM viewer_data_sync_runs
        ORDER BY id DESC
        LIMIT %s
      """, (limit,))
      rows = cur.fetchall()

  result = []
  for row in rows:
    raw_dates = row.get("new_dates_json")
    if isinstance(raw_dates, str):
      new_dates = json.loads(raw_dates)
    else:
      new_dates = raw_dates or []
    result.append({
      "id": int(row["id"]),
      "symbol": row["symbol"],
      "timeframe": row["timeframe"],
      "sourceTimeframe": row["source_timeframe"],
      "startDate": row["start_date"],
      "endDate": row["end_date"],
      "mode": row["mode"],
      "status": row["status"],
      "newRows": int(row["new_rows"]),
      "newDates": new_dates,
      "outputPath": row["output_path"],
      "elapsedMs": int(row["elapsed_ms"]),
      "message": row["message"],
      "createdAt": row["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
    })
  return result
