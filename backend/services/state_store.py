# -*- coding: utf-8 -*-
"""PostgreSQL 状态仓储：仅访问规范表，不在应用启动时建表。"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.errors import IntegrityError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.services.user_context import get_current_user
from backend.time_utils import format_shanghai_datetime


class DatabaseError(RuntimeError):
  """数据库连接或执行失败。"""


class ConflictError(DatabaseError):
  """违反唯一约束等写入冲突。"""


_L2_FAIR_CLAIM_LOCK_KEY = 314159265


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


def replace_trade_calendar(
  exchange: str, start_date: str, end_date: str, open_dates: list[str], source: str,
) -> None:
  """按自然日覆盖写入交易日历，确保区间内开市和休市状态都可判定。"""
  start = date.fromisoformat(start_date)
  end = date.fromisoformat(end_date)
  all_dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
  open_set = set(open_dates)
  rows = [(exchange, value, value.isoformat() in open_set, source) for value in all_dates]
  with _db() as conn:
    with conn.cursor() as cur:
      cur.executemany("""
        INSERT INTO market_trade_calendar (exchange, trade_date, is_open, source)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (exchange, trade_date) DO UPDATE SET
          is_open=EXCLUDED.is_open, source=EXCLUDED.source, updated_at=now()
      """, rows)


def latest_trade_calendar_date(exchange: str = "SSE") -> str | None:
  """返回交易日历已写入的最新自然日，供增量同步确定起点。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT max(trade_date) AS latest_date
        FROM market_trade_calendar
        WHERE exchange=%s
      """, (exchange,))
      value = cur.fetchone()["latest_date"]
      return str(value) if value else None


def list_open_trade_dates(start_date: str, end_date: str, exchange: str = "SSE") -> list[str]:
  """返回指定交易所的开市日期。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT trade_date
        FROM market_trade_calendar
        WHERE exchange=%s AND trade_date BETWEEN %s AND %s AND is_open=true
        ORDER BY trade_date
      """, (exchange, start_date, end_date))
      return [str(row["trade_date"]) for row in cur.fetchall()]


def is_trade_calendar_range_covered(start_date: str, end_date: str, exchange: str = "SSE") -> bool:
  """仅当每个自然日都有日历记录时才允许预检使用该区间。"""
  start = date.fromisoformat(start_date)
  end = date.fromisoformat(end_date)
  expected_count = (end - start).days + 1
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT count(*) AS date_count
        FROM market_trade_calendar
        WHERE exchange=%s AND trade_date BETWEEN %s AND %s
      """, (exchange, start_date, end_date))
      row = cur.fetchone()
      return int(row["date_count"] or 0) == expected_count


def _to_float(value: Any) -> float | None:
  if value is None:
    return None
  if isinstance(value, Decimal):
    return float(value)
  return float(value)

def list_l2_quote_daily_availability(
  source_key: str, symbol: str, trade_dates: list[str],
) -> dict[str, dict[str, Any]]:
  """读取日级十档可用性摘要；未命中日期由调用方补齐。"""
  dates = sorted(set(trade_dates))
  if not dates:
    return {}
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT trade_date, status, snapshot_count, first_quote_time, last_quote_time
        FROM l2_quote_daily_availability
        WHERE source_key=%s AND symbol=%s AND trade_date = ANY(%s::date[])
      """, (source_key, symbol, dates))
      return {
        str(row["trade_date"]): {
          "status": str(row["status"]),
          "snapshotCount": int(row["snapshot_count"]),
          "firstQuoteTime": str(row["first_quote_time"] or ""),
          "lastQuoteTime": str(row["last_quote_time"] or ""),
        }
        for row in cur.fetchall()
      }


def upsert_l2_quote_daily_availability(
  source_key: str, symbol: str, records: list[dict[str, Any]],
) -> None:
  """写入按标的、交易日和数据源版本隔离的十档摘要。"""
  if not records:
    return
  rows = [
    (
      source_key, symbol, item["tradeDate"], item["status"], int(item.get("snapshotCount") or 0),
      str(item.get("firstQuoteTime") or "") or None, str(item.get("lastQuoteTime") or "") or None,
    )
    for item in records
  ]
  with _db() as conn:
    with conn.cursor() as cur:
      cur.executemany("""
        INSERT INTO l2_quote_daily_availability (
          source_key, symbol, trade_date, status, snapshot_count, first_quote_time, last_quote_time
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_key, symbol, trade_date) DO UPDATE SET
          status=EXCLUDED.status,
          snapshot_count=EXCLUDED.snapshot_count,
          first_quote_time=EXCLUDED.first_quote_time,
          last_quote_time=EXCLUDED.last_quote_time,
          checked_at=now()
      """, rows)


def _format_time(value: Any) -> str:
  return format_shanghai_datetime(value) if value else ""


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
  normalized_name = name.strip()
  cur.execute("""
    INSERT INTO market_instrument (
      symbol, exchange, display_name, instrument_type, board, eastmoney_secid
    ) VALUES (%s, %s, %s, 'stock', 'unknown', %s)
    ON CONFLICT (exchange, symbol) DO UPDATE
      SET display_name = CASE
        WHEN %s <> '' THEN EXCLUDED.display_name
        ELSE market_instrument.display_name
      END,
      updated_at = now()
    RETURNING id
  """, (code, exchange, normalized_name or symbol, secid, normalized_name))
  return int(cur.fetchone()["id"])


def search_market_instruments(query: str, limit: int = 20) -> list[dict[str, Any]]:
  """按代码或名称搜索全局证券目录，不受当前用户范围限制。"""
  keyword = query.strip()
  pattern = f"%{keyword}%"
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT symbol, exchange, display_name, instrument_type, board
        FROM market_instrument
        WHERE is_active=true
          AND (%s = '' OR symbol ILIKE %s OR display_name ILIKE %s)
        ORDER BY CASE WHEN symbol = %s THEN 0 WHEN symbol ILIKE %s THEN 1 ELSE 2 END,
                 exchange, symbol
        LIMIT %s
      """, (keyword, pattern, pattern, keyword, f"{keyword}%", limit))
      return [{
        "symbol": f"{row['symbol']}.{row['exchange']}",
        "name": row["display_name"],
        "market": row["exchange"],
        "instrumentType": row["instrument_type"],
        "board": row["board"],
      } for row in cur.fetchall()]


def upsert_market_instruments(items: list[Any]) -> int:
  """幂等更新全局证券目录，仅由管理员刷新入口调用。"""
  if not items:
    return 0
  rows = [(item.symbol, item.exchange, item.display_name, item.board, item.eastmoney_secid) for item in items]
  with _db() as conn:
    with conn.cursor() as cur:
      cur.executemany("""
        INSERT INTO market_instrument (
          symbol, exchange, display_name, instrument_type, board, eastmoney_secid, is_active
        ) VALUES (%s, %s, %s, 'stock', %s, %s, true)
        ON CONFLICT (exchange, symbol) DO UPDATE SET
          display_name=EXCLUDED.display_name,
          instrument_type=EXCLUDED.instrument_type,
          board=EXCLUDED.board,
          eastmoney_secid=EXCLUDED.eastmoney_secid,
          is_active=true,
          updated_at=now()
      """, rows)
  return len(rows)


def replace_market_instruments_from_qmt(items: list[dict[str, str | None]]) -> int:
  """以完整 QMT 快照替换 PostgreSQL 中的有效股票目录。"""
  if not items:
    return 0
  rows = [
    (
      item["symbol"], item["exchange"], item["display_name"], item["instrument_type"],
      item["board"], _qmt_secid(item["symbol"], item["exchange"]), item["list_date"],
      item["delist_date"], Jsonb({"source": "qmt"}),
    )
    for item in items
  ]
  if not settings.DATABASE_URL:
    raise DatabaseError("未设置 TREND_GRID_DATABASE_URL")
  try:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
      with conn.cursor() as cur:
        cur.execute("UPDATE market_instrument SET is_active=false WHERE instrument_type='stock'")
        cur.executemany("""
          INSERT INTO market_instrument (
            symbol, exchange, display_name, instrument_type, board, eastmoney_secid,
            list_date, delist_date, source_metadata, is_active
          ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)
          ON CONFLICT (exchange, symbol) DO UPDATE SET
            display_name=EXCLUDED.display_name,
            instrument_type=EXCLUDED.instrument_type,
            board=EXCLUDED.board,
            eastmoney_secid=EXCLUDED.eastmoney_secid,
            list_date=EXCLUDED.list_date,
            delist_date=EXCLUDED.delist_date,
            source_metadata=EXCLUDED.source_metadata,
            is_active=true,
            updated_at=now()
        """, rows)
    return len(rows)
  except psycopg.Error as error:
    raise DatabaseError(str(error)) from error


def _qmt_secid(symbol: str | None, exchange: str | None) -> str:
  if not symbol or not exchange:
    raise DatabaseError("QMT 证券目录缺少代码或交易所")
  return f"{'1' if exchange == 'SH' else '0'}.{symbol}"


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
  fill_model = row.get("fill_model") or "bar_close_signal_fill"
  raw_result = _json(row.get("result_json"), None)
  result = raw_result
  summary = _json(row.get("result_summary"), {})
  if fill_model == "l2_quote" and isinstance(raw_result, dict):
    from backend.l2.result_adapter import normalize_l2_result

    result = normalize_l2_result(raw_result, request)
    summary = result["summary"]
  category = request.get("strategyCategory")
  if category not in {"core_position_overlay", "directional_holding"}:
    category = "core_position_overlay" if int(request.get("baseQuantity") or 0) > 0 or bool(request.get("t0Enabled")) else "directional_holding"
  return {
    "id": _uuid(row["id"]),
    "displayNo": int(row["display_no"]),
    "configId": _uuid(row.get("config_id")),
    "configVersionId": _uuid(row.get("config_version_id")),
    "scriptId": _uuid(row.get("strategy_id")) or request.get("scriptId"),
    "accountId": int(row["account_id"]) if row.get("account_id") is not None else None,
    "symbol": row.get("symbol") or request.get("symbol", ""),
    "timeframe": row.get("timeframe") or request.get("timeframe", "5m"),
    "fillModel": fill_model,
    "scriptSnapshot": _json(row.get("script_snapshot"), None),
    "paramsSnapshot": _json(row.get("params_snapshot"), {}),
    "dataVersion": _json(row.get("market_data_snapshot"), None),
    "benchmarkSymbol": row.get("benchmark_symbol") or request.get("benchmarkSymbol"),
    "strategy": request.get("strategy", "user_script"),
    "strategyCategory": category,
    "startDate": request.get("startDate", ""),
    "endDate": request.get("endDate", ""),
    "paramsJson": request,
    "status": {"succeeded": "ok", "failed": "error"}.get(row["status"], row["status"]),
    "executionKind": row.get("execution_kind", "published"),
    "summaryJson": summary,
    "outputDir": row.get("output_dir", ""),
    "note": row.get("note", ""),
    "tags": _json(row.get("tags"), []),
    "resultJson": result,
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


def _write_strategy_script(cur: Any, script_id: str | None, payload: dict[str, Any]) -> str:
  if script_id is None:
    cur.execute("""
      INSERT INTO user_strategy (id, owner_user_id, name, description, strategy_type, status)
      VALUES (gen_random_uuid(), %s, %s, %s, 'python', 'active') RETURNING id
    """, (current_user_id(), payload["name"], payload.get("description", "")))
    saved_id = _uuid(cur.fetchone()["id"]) or ""
    _insert_script_version(cur, saved_id, 1, payload)
    return saved_id
  cur.execute("SELECT current_version_no FROM user_strategy WHERE id=%s AND owner_user_id=%s", (script_id, current_user_id()))
  row = cur.fetchone()
  if not row:
    raise KeyError(script_id)
  cur.execute("""
    UPDATE user_strategy SET name=%s, description=%s, updated_at=now()
    WHERE id=%s AND owner_user_id=%s
  """, (payload["name"], payload.get("description", ""), script_id, current_user_id()))
  cur.execute("""
    UPDATE user_strategy_version SET source_code=%s, source_checksum=%s, runtime_config=%s
    WHERE strategy_id=%s AND version_no=%s
  """, (
    payload["code"], hashlib.sha256(payload["code"].encode("utf-8")).hexdigest(),
    Jsonb({"entryType": payload.get("entryType", "on_bar"), "status": "active"}),
    script_id, int(row["current_version_no"]),
  ))
  if cur.rowcount == 0:
    _insert_script_version(cur, script_id, int(row["current_version_no"]), payload)
  return script_id


def create_strategy_script(payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      script_id = _write_strategy_script(cur, None, payload)
  return get_strategy_script(script_id)


def update_strategy_script(script_id: str, payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      _write_strategy_script(cur, script_id, payload)
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
      ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true) RETURNING id
    """, (current_user_id(), payload["name"], payload.get("scriptId"), payload.get("strategy", "user_script"),
           payload.get("mode", "backtest"), instrument_id, payload.get("timeframe", "5m"), benchmark_id,
           payload.get("accountId"), Jsonb(payload.get("paramsJson", {})), payload.get("enabled", True)))
  else:
    cur.execute("""
      UPDATE strategy_config SET name=%s, strategy_id=%s, strategy_key=%s, mode=%s,
        instrument_id=%s, timeframe=%s, benchmark_instrument_id=%s, account_id=%s, params=%s,
        enabled=%s, published=true, updated_at=now() WHERE id=%s AND owner_user_id=%s RETURNING id
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



def save_complete_strategy(
  script_payload: dict[str, Any], config_payload: dict[str, Any], script_id: str | None, config_id: str | None,
) -> dict[str, dict[str, Any]]:
  """在同一事务中保存脚本与完整回测策略，避免预检通过后出现半完成数据。"""
  with _db() as conn:
    with conn.cursor() as cur:
      saved_script_id = _write_strategy_script(cur, script_id, script_payload)
      config = {**config_payload, "scriptId": saved_script_id}
      saved_config_id = _write_config(cur, config_id, config)
  return {
    "script": get_strategy_script(saved_script_id),
    "config": get_strategy_config(saved_config_id),
  }
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


def _next_backtest_display_no(cur: Any, owner_user_id: str) -> int:
  cur.execute("""
    INSERT INTO user_backtest_counter (owner_user_id, next_display_no)
    VALUES (%s, 2)
    ON CONFLICT (owner_user_id) DO UPDATE
    SET next_display_no = user_backtest_counter.next_display_no + 1
    RETURNING next_display_no - 1 AS display_no
  """, (owner_user_id,))
  row = cur.fetchone()
  return int(row["display_no"])


def _create_backtest_run_row(
  cur: Any,
  payload: dict[str, Any],
  status: str,
  owner_user_id: str,
) -> tuple[str, int]:
  config_id = payload.get("configId")
  display_no = _next_backtest_display_no(cur, owner_user_id)
  instrument_id = _instrument_id(cur, payload["symbol"])
  benchmark_id = _instrument_id(cur, payload["benchmarkSymbol"]) if payload.get("benchmarkSymbol") else None
  script_snapshot = payload.get("savedScriptSnapshot")
  params_snapshot = payload.get("savedConfigSnapshot", {})
  db_status = "running" if status == "running" else "queued"
  cur.execute("""
    INSERT INTO backtest_run (
      id, owner_user_id, display_no, strategy_version_id, status, requested_params, market_data_snapshot,
      started_at, config_id, config_version_id, account_id, instrument_id,
      benchmark_instrument_id, timeframe, fill_model, script_snapshot, params_snapshot, execution_kind
    ) VALUES (
      gen_random_uuid(), %s, %s, %s, %s, %s, %s,
      CASE WHEN %s='running' THEN now() ELSE NULL END, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    ) RETURNING id, display_no
  """, (owner_user_id, display_no, None, db_status, Jsonb(payload), Jsonb(payload.get("marketDataSnapshot", {})), db_status,
         config_id, None, payload.get("accountId"), instrument_id, benchmark_id,
         payload.get("timeframe", "5m"), "bar_close_signal_fill", Jsonb(script_snapshot) if script_snapshot else None,
         Jsonb(params_snapshot), payload.get("executionKind", "published")))
  row = cur.fetchone()
  return _uuid(row["id"]) or "", int(row["display_no"])


def create_backtest_run(payload: dict[str, Any], status: str = "running") -> tuple[str, int]:
  owner_user_id = current_user_id()
  with _db() as conn:
    with conn.cursor() as cur:
      return _create_backtest_run_row(cur, payload, status, owner_user_id)


def _create_queued_backtest_job(
  cur: Any,
  payload: dict[str, Any],
  owner_user_id: str,
  job_table: str,
  fill_model: str,
) -> tuple[str, int]:
  run_id, display_no = _create_backtest_run_row(cur, payload, "queued", owner_user_id)
  cur.execute("UPDATE backtest_run SET fill_model=%s WHERE id=%s AND owner_user_id=%s", (
    fill_model, run_id, owner_user_id,
  ))
  cur.execute(f"""
    INSERT INTO {job_table} (run_id, owner_user_id, payload)
    VALUES (%s, %s, %s)
  """, (run_id, owner_user_id, Jsonb(payload)))
  _save_research_signal_snapshot(cur, run_id, owner_user_id, payload.get("researchSignalSnapshot"))
  return run_id, display_no


def _save_research_signal_snapshot(cur: Any, run_id: str, owner_user_id: str, snapshot: object) -> None:
  if not isinstance(snapshot, dict):
    return
  artifact_path = snapshot.get("artifactFile")
  manifest = snapshot.get("manifest")
  if not isinstance(artifact_path, str) or not isinstance(manifest, dict):
    raise DatabaseError("研究信号冻结快照元数据无效")
  path = Path(artifact_path)
  if not path.is_file():
    raise DatabaseError("研究信号冻结文件不存在")
  content_sha256 = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
  cur.execute(
    """INSERT INTO artifact_file
      (id, owner_user_id, run_id, artifact_type, storage_key, original_filename, content_type, byte_size, content_sha256)
      VALUES (gen_random_uuid(), %s, %s, 'research_signal', %s, %s, 'application/x-ndjson', %s, %s)
      RETURNING id""",
    (owner_user_id, run_id, f"research_signals/{path.name}", path.name, path.stat().st_size, content_sha256),
  )
  artifact_id = cur.fetchone()["id"]
  cur.execute(
    """INSERT INTO research_signal_snapshot
      (run_id, dataset_id, dataset_version, snapshot_hash, artifact_file_id, manifest, source_partitions)
      VALUES (%s, %s::uuid, %s, %s, %s, %s, %s)""",
    (run_id, snapshot["datasetId"], manifest.get("version"), snapshot["snapshotHash"], artifact_id,
     Jsonb(manifest), Jsonb(snapshot.get("sourcePartitions") or [])),
  )


def create_l2_backtest_job(payload: dict[str, Any]) -> tuple[str, int]:
  """创建异步 L2 回测任务；PostgreSQL 是其唯一状态真源。"""
  owner_user_id = current_user_id()
  with _db() as conn:
    with conn.cursor() as cur:
      return _create_queued_backtest_job(cur, payload, owner_user_id, "l2_backtest_job", "l2_quote")


def create_bar_backtest_job(payload: dict[str, Any]) -> tuple[str, int]:
  """创建异步标准 Bar 回测任务；任务状态由 PostgreSQL 持久化。"""
  owner_user_id = current_user_id()
  with _db() as conn:
    with conn.cursor() as cur:
      return _create_queued_backtest_job(
        cur, payload, owner_user_id, "bar_backtest_job", payload.get("fillModel", "bar_next_open"),
      )


def create_api_backtest_job(
  payload: dict[str, Any],
  api_key_id: str,
  idempotency_key: str,
  request_hash: str,
) -> tuple[str, int, bool, str, str]:
  """在同一事务内处理 API 限额、幂等记录和回测任务创建。"""
  owner_user_id = current_user_id()
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT id FROM api_access_key
        WHERE id=%s AND owner_user_id=%s AND revoked_at IS NULL
          AND (expires_at IS NULL OR expires_at > now())
        FOR UPDATE
      """, (api_key_id, owner_user_id))
      if cur.fetchone() is None:
        raise KeyError("api_key")
      cur.execute("""
        SELECT request_hash, run_id FROM api_idempotency_record
        WHERE api_key_id=%s AND idempotency_key=%s
      """, (api_key_id, idempotency_key))
      existing = cur.fetchone()
      if existing:
        if existing["request_hash"] != request_hash:
          raise ConflictError("同一幂等键不能用于不同请求")
        if existing["run_id"] is None:
          raise DatabaseError("幂等请求尚未完成，请重试")
        cur.execute("SELECT display_no, status, fill_model FROM backtest_run WHERE id=%s", (existing["run_id"],))
        run = cur.fetchone()
        if run is None:
          raise DatabaseError("幂等请求关联的回测记录不存在")
        return (
          str(existing["run_id"]),
          int(run["display_no"]),
          True,
          str(run["status"]),
          str(run["fill_model"] or payload.get("fillModel", "bar_next_open")),
        )
      cur.execute("""
        SELECT count(*) AS value
        FROM api_idempotency_record record
        JOIN backtest_run run ON run.id=record.run_id
        WHERE record.api_key_id=%s AND run.status IN ('queued', 'running')
      """, (api_key_id,))
      if int(cur.fetchone()["value"]) >= settings.PUBLIC_API_MAX_INFLIGHT:
        raise ValueError("API Key 正在排队或运行的回测已达上限")
      cur.execute("""
        SELECT count(*) AS value FROM api_idempotency_record
        WHERE api_key_id=%s AND created_at >= current_date
      """, (api_key_id,))
      if int(cur.fetchone()["value"]) >= settings.PUBLIC_API_DAILY_SUBMIT_LIMIT:
        raise ValueError("API Key 今日回测提交次数已达上限")
      cur.execute("""
        INSERT INTO api_idempotency_record (
          api_key_id, owner_user_id, idempotency_key, request_hash
        ) VALUES (%s, %s, %s, %s)
      """, (api_key_id, owner_user_id, idempotency_key, request_hash))
      if payload.get("fillModel") == "l2_quote":
        run_id, display_no = _create_queued_backtest_job(
          cur, payload, owner_user_id, "l2_backtest_job", "l2_quote",
        )
      else:
        run_id, display_no = _create_queued_backtest_job(
          cur, payload, owner_user_id, "bar_backtest_job", payload.get("fillModel", "bar_next_open"),
        )
      cur.execute("""
        UPDATE api_idempotency_record SET run_id=%s
        WHERE api_key_id=%s AND idempotency_key=%s
      """, (run_id, api_key_id, idempotency_key))
      cur.execute("""
        INSERT INTO api_access_audit (api_key_id, owner_user_id, event_type, detail)
        VALUES (%s, %s, 'backtest_submitted', %s)
      """, (api_key_id, owner_user_id, Jsonb({"runId": run_id, "fillModel": payload.get("fillModel")})))
      return run_id, display_no, False, "queued", str(payload.get("fillModel", "bar_next_open"))


def _claim_fair_l2_job_row(cur: Any, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
  """在短事务调度锁内按用户运行数领取一条 L2 任务。"""
  cur.execute("SELECT pg_advisory_xact_lock(%s)", (_L2_FAIR_CLAIM_LOCK_KEY,))
  cur.execute("""
    WITH candidate AS (
      SELECT job.run_id
      FROM l2_backtest_job AS job
      WHERE (job.status='queued' AND NOT job.cancel_requested)
         OR (job.status='running' AND job.lease_expires_at < now() AND NOT job.cancel_requested)
      ORDER BY (
        SELECT count(*)
        FROM l2_backtest_job AS active
        WHERE active.owner_user_id=job.owner_user_id
          AND active.status='running'
          AND active.lease_expires_at >= now()
      ), job.created_at, job.run_id
      FOR UPDATE OF job SKIP LOCKED
      LIMIT 1
    )
    UPDATE l2_backtest_job AS job
    SET status='running', attempt_no=attempt_no + 1, attempt_token=gen_random_uuid(),
      lease_expires_at=now() + make_interval(secs => %s), worker_id=%s,
      started_at=COALESCE(started_at, now()), updated_at=now()
    FROM candidate
    WHERE job.run_id=candidate.run_id
    RETURNING job.run_id, job.owner_user_id, job.payload, job.attempt_no, job.attempt_token
  """, (lease_seconds, worker_id))
  return cur.fetchone()


def claim_l2_backtest_job(worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
  """原子公平领取一个任务，并为本次尝试生成不可复用令牌。"""
  if not worker_id.strip() or lease_seconds <= 0:
    raise ValueError("L2 runner 标识和租约时长必须有效")
  with _db() as conn:
    with conn.cursor() as cur:
      row = _claim_fair_l2_job_row(cur, worker_id, lease_seconds)
      if not row:
        return None
      cur.execute("UPDATE backtest_run SET status='running', started_at=COALESCE(started_at, now()) WHERE id=%s", (row["run_id"],))
      return {
        "runId": _uuid(row["run_id"]),
        "ownerUserId": _uuid(row["owner_user_id"]),
        "payload": _json(row["payload"], {}),
        "attemptNo": int(row["attempt_no"]),
        "attemptToken": _uuid(row["attempt_token"]),
      }


def claim_bar_backtest_job(worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
  """原子领取一个标准 Bar 回测任务。"""
  if not worker_id.strip() or lease_seconds <= 0:
    raise ValueError("Bar runner 标识和租约时长必须有效")
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        WITH candidate AS (
          SELECT run_id
          FROM bar_backtest_job
          WHERE (status='queued' AND NOT cancel_requested)
             OR (status='running' AND lease_expires_at < now() AND NOT cancel_requested)
          ORDER BY created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        UPDATE bar_backtest_job AS job
        SET status='running', attempt_no=attempt_no + 1, attempt_token=gen_random_uuid(),
          lease_expires_at=now() + make_interval(secs => %s), worker_id=%s,
          started_at=COALESCE(started_at, now()), updated_at=now()
        FROM candidate
        WHERE job.run_id=candidate.run_id
        RETURNING job.run_id, job.owner_user_id, job.payload, job.attempt_no, job.attempt_token
      """, (lease_seconds, worker_id))
      row = cur.fetchone()
      if not row:
        return None
      cur.execute("UPDATE backtest_run SET status='running', started_at=COALESCE(started_at, now()) WHERE id=%s", (row["run_id"],))
      return {
        "runId": _uuid(row["run_id"]),
        "ownerUserId": _uuid(row["owner_user_id"]),
        "payload": _json(row["payload"], {}),
        "attemptNo": int(row["attempt_no"]),
        "attemptToken": _uuid(row["attempt_token"]),
      }


def heartbeat_l2_backtest_job(run_id: str, attempt_token: str, lease_seconds: int) -> bool:
  """仅持有当前 attempt_token 的 runner 可以续租。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE l2_backtest_job
        SET lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
        WHERE run_id=%s AND attempt_token=%s AND status='running' AND NOT cancel_requested
      """, (lease_seconds, run_id, attempt_token))
      return cur.rowcount == 1


def heartbeat_bar_backtest_job(run_id: str, attempt_token: str, lease_seconds: int) -> bool:
  """仅持有当前租约的 Bar runner 可以续租。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
        WHERE run_id=%s AND attempt_token=%s AND status='running' AND NOT cancel_requested
      """, (lease_seconds, run_id, attempt_token))
      return cur.rowcount == 1


def finish_l2_backtest_job(
  run_id: str,
  attempt_token: str,
  status: str,
  summary: dict[str, Any],
  result: dict[str, Any] | None = None,
  system_error: dict[str, Any] | None = None,
  owner_user_id: str | None = None,
) -> bool:
  """按尝试令牌提交终态，旧 runner 无法覆盖新的运行结果。"""
  if status not in {"succeeded", "failed", "cancelled"}:
    raise ValueError("L2 任务终态无效")
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE l2_backtest_job
        SET status=%s, error_message=%s, lease_expires_at=NULL, finished_at=now(), updated_at=now()
        WHERE run_id=%s AND attempt_token=%s AND status='running'
        RETURNING run_id
      """, (status, summary.get("error") if status != "succeeded" else None, run_id, attempt_token))
      if cur.fetchone() is None:
        return False
      cur.execute("""
        UPDATE backtest_run
        SET status=%s, result_summary=%s, result_json=%s, error_message=%s, finished_at=now()
        WHERE id=%s
      """, (status, Jsonb(summary), Jsonb(result) if result else None,
             summary.get("error") if status != "succeeded" else None, run_id))
      if status == "failed" and system_error and owner_user_id:
        _insert_system_error(cur, run_id, owner_user_id, system_error)
      return True


def finish_bar_backtest_job(
  run_id: str,
  attempt_token: str,
  status: str,
  summary: dict[str, Any],
  result: dict[str, Any] | None = None,
  system_error: dict[str, Any] | None = None,
  owner_user_id: str | None = None,
) -> bool:
  """提交标准 Bar 回测终态，避免过期 worker 覆盖新执行尝试。"""
  if status not in {"succeeded", "failed", "cancelled"}:
    raise ValueError("Bar 任务终态无效")
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET status=%s, error_message=%s, lease_expires_at=NULL, finished_at=now(), updated_at=now()
        WHERE run_id=%s AND attempt_token=%s AND status='running'
        RETURNING run_id
      """, (status, summary.get("error") if status != "succeeded" else None, run_id, attempt_token))
      if cur.fetchone() is None:
        return False
      cur.execute("""
        UPDATE backtest_run
        SET status=%s, result_summary=%s, result_json=%s, error_message=%s, finished_at=now()
        WHERE id=%s
      """, (status, Jsonb(summary), Jsonb(result) if result else None,
             summary.get("error") if status != "succeeded" else None, run_id))
      _enqueue_research_signal_result(cur, run_id, status, summary)
      if status == "failed" and system_error and owner_user_id:
        _insert_system_error(cur, run_id, owner_user_id, system_error)
      return True


def _enqueue_research_signal_result(cur: Any, run_id: str, status: str, summary: dict[str, Any]) -> None:
  cur.execute(
    """INSERT INTO research_signal_result_outbox (run_id, payload)
      SELECT snapshot.run_id, jsonb_build_object(
        'backtestRunId', snapshot.run_id::text,
        'signalDatasetId', snapshot.dataset_id::text,
        'signalDatasetVersion', snapshot.dataset_version,
        'snapshotHash', snapshot.snapshot_hash,
        'status', %s,
        'performanceSummary', %s::jsonb
      )
      FROM research_signal_snapshot AS snapshot
      WHERE snapshot.run_id = %s
      ON CONFLICT (run_id) DO NOTHING""",
    (status, Jsonb(summary), run_id),
  )


def request_l2_backtest_cancel(run_id: str) -> bool:
  """请求取消尚未结束的 L2 任务。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE l2_backtest_job
        SET cancel_requested=true,
          status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
          finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END,
          updated_at=now()
        WHERE run_id=%s AND status IN ('queued', 'running')
        RETURNING status
      """, (run_id,))
      row = cur.fetchone()
      if row is None:
        return False
      if row["status"] == "cancelled":
        cur.execute("UPDATE backtest_run SET status='cancelled', finished_at=now() WHERE id=%s", (run_id,))
      return True


def request_bar_backtest_cancel(run_id: str) -> bool:
  """请求取消标准 Bar 回测；运行中的任务会在当前策略调用返回后结束。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE bar_backtest_job
        SET cancel_requested=true,
          status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
          finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END,
          updated_at=now()
        WHERE run_id=%s AND status IN ('queued', 'running')
        RETURNING status
      """, (run_id,))
      row = cur.fetchone()
      if row is None:
        return False
      if row["status"] == "cancelled":
        cur.execute("UPDATE backtest_run SET status='cancelled', finished_at=now() WHERE id=%s", (run_id,))
      return True


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


def list_backtest_runs(
  limit: int = 30,
  config_id: str | None = None,
  offset: int = 0,
) -> list[dict[str, Any]]:
  if offset < 0:
    raise ValueError("分页偏移量无效")
  with _db() as conn:
    with conn.cursor() as cur:
      query = (
        _run_query("AND r.config_id=%s ORDER BY r.created_at DESC LIMIT %s OFFSET %s")
        if config_id
        else _run_query("ORDER BY r.created_at DESC LIMIT %s OFFSET %s")
      )
      params = (
        (current_user_id(), config_id, limit, offset)
        if config_id
        else (current_user_id(), limit, offset)
      )
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


def delete_backtest_run(run_id: str) -> bool:
  """永久删除当前用户已结束的回测及由外键关联的数据库记录。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT status FROM backtest_run WHERE id=%s AND owner_user_id=%s FOR UPDATE",
        (run_id, current_user_id()),
      )
      row = cur.fetchone()
      if row is None:
        raise KeyError(run_id)
      if row["status"] not in {"succeeded", "failed", "cancelled"}:
        raise ValueError("排队或运行中的回测不能删除，请先取消任务")
      cur.execute(
        "DELETE FROM backtest_run WHERE id=%s AND owner_user_id=%s",
        (run_id, current_user_id()),
      )
      return cur.rowcount == 1


def delete_backtest_runs(run_ids: list[str]) -> int:
  """原子删除当前用户选中的多条已结束回测。"""
  unique_run_ids = list(dict.fromkeys(run_ids))
  if not unique_run_ids:
    raise ValueError("请至少选择一条回测记录")
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT id, status FROM backtest_run WHERE id=ANY(%s) AND owner_user_id=%s FOR UPDATE",
        (unique_run_ids, current_user_id()),
      )
      rows = cur.fetchall()
      if len(rows) != len(unique_run_ids):
        raise KeyError("部分回测记录不存在或无删除权限")
      if any(row["status"] not in {"succeeded", "failed", "cancelled"} for row in rows):
        raise ValueError("选中的回测包含排队或运行中任务，请先取消任务")
      cur.execute(
        "DELETE FROM backtest_run WHERE id=ANY(%s) AND owner_user_id=%s",
        (unique_run_ids, current_user_id()),
      )
      return cur.rowcount


def get_backtest_result_page(
  run_id: str,
  result_key: str,
  cursor: int,
  limit: int,
) -> dict[str, Any]:
  """分页读取已完成任务的不可变结果快照，避免开放 API 返回超大 JSON。"""
  if cursor < 0 or limit < 1 or limit > 200:
    raise ValueError("分页参数无效")
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT result_json FROM backtest_run
        WHERE id=%s AND owner_user_id=%s
      """, (run_id, current_user_id()))
      row = cur.fetchone()
      if not row:
        raise KeyError(run_id)
      result = _json(row["result_json"], {})
      if not isinstance(result, dict):
        result = {}
      if result_key == "signals":
        items = [
          {"type": "execution_plan", **item}
          for item in result.get("executionPlans", []) if isinstance(item, dict)
        ] + [
          {"type": "skipped_signal", **item}
          for item in result.get("skippedSignals", []) if isinstance(item, dict)
        ]
        items.sort(key=lambda item: str(item.get("signalTime") or item.get("time") or ""))
      else:
        items = [item for item in result.get("trades", []) if isinstance(item, dict)]
      page = items[cursor:cursor + limit]
      next_cursor = cursor + len(page)
      return {
        "items": page,
        "nextCursor": str(next_cursor) if next_cursor < len(items) else None,
        "total": len(items),
      }


def list_system_error_logs(
  limit: int = 100,
  user_id: str | None = None,
  run_id: str | None = None,
  error_code: str | None = None,
  from_time: str | None = None,
  to_time: str | None = None,
) -> list[dict[str, Any]]:
  """仅管理员调用的跨用户系统错误列表。"""
  clauses: list[str] = []
  params: list[Any] = []
  for value, clause in (
    (user_id, "e.owner_user_id=%s"),
    (run_id, "e.run_id=%s"),
    (error_code, "e.error_code=%s"),
    (from_time, "e.occurred_at >= %s::timestamptz"),
    (to_time, "e.occurred_at <= %s::timestamptz"),
  ):
    if value:
      clauses.append(clause)
      params.append(value)
  where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
  params.append(limit)
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(f"""
        SELECT e.*, u.username, u.display_name, r.display_no,
          i.symbol || '.' || i.exchange AS symbol, r.timeframe
        FROM system_error_log e
        JOIN app_user u ON u.id=e.owner_user_id
        JOIN backtest_run r ON r.id=e.run_id
        LEFT JOIN market_instrument i ON i.id=r.instrument_id
        {where}
        ORDER BY e.occurred_at DESC, e.id DESC
        LIMIT %s
      """, params)
      return [_normalize_system_error(row) for row in cur.fetchall()]


def update_backtest_run_meta(run_id: str, note: str, tags: list[str]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("UPDATE backtest_run SET note=%s, tags=%s WHERE id=%s AND owner_user_id=%s", (note, Jsonb(tags), run_id, current_user_id()))
      if cur.rowcount == 0:
        raise KeyError(run_id)
  return get_backtest_run(run_id)


def finish_backtest_run(
  run_id: str,
  status: str,
  summary: dict[str, Any],
  output_dir: str,
  result: dict[str, Any] | None = None,
  system_error: dict[str, Any] | None = None,
) -> None:
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
      if db_status == "failed" and system_error:
        _insert_system_error(cur, run_id, current_user_id(), system_error)


def _insert_system_error(
  cur: Any,
  run_id: str,
  owner_user_id: str,
  record: dict[str, Any],
) -> None:
  cur.execute("""
    INSERT INTO system_error_log (
      run_id, owner_user_id, source, stage, error_code, reason,
      exception_type, context_json, category, retryable, raw_log_ref
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
  """, (
    run_id, owner_user_id, record["source"], record["stage"], record["code"],
    record["reason"], record["exception_type"], Jsonb(record["context"]),
    record["category"], record["retryable"], record["raw_log_ref"],
  ))


def _normalize_system_error(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": int(row["id"]),
    "runId": _uuid(row["run_id"]),
    "displayNo": int(row["display_no"]),
    "userId": _uuid(row["owner_user_id"]),
    "username": row["username"],
    "displayName": row["display_name"],
    "symbol": row.get("symbol") or "",
    "timeframe": row.get("timeframe") or "",
    "source": row["source"],
    "stage": row["stage"],
    "code": row["error_code"],
    "category": row.get("category") or "internal",
    "retryable": bool(row.get("retryable")),
    "reason": row["reason"],
    "exceptionType": row["exception_type"],
    "context": _public_system_error_context(_json(row.get("context_json"), {})),
    "rawLogRef": row.get("raw_log_ref") or "",
    "occurredAt": _format_time(row["occurred_at"]),
  }


def _public_system_error_context(context: dict[str, Any]) -> dict[str, Any]:
  return {
    key: value
    for key, value in context.items()
    if not key.startswith("__internal_")
  }
