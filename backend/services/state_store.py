# -*- coding: utf-8 -*-
"""PostgreSQL 状态仓储：仅访问规范表，不在应用启动时建表。"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

import psycopg
from psycopg.errors import IntegrityError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend import settings
from backend.services import shared_backtest_artifacts, shared_backtest_snapshot
from backend.services.user_context import get_current_user
from backend.strategies.runtime_contract import (
  RUNTIME_CAPABILITY_DIGEST,
  SCRIPT_RUNTIME_VERSION,
  SUPPORTED_QUANT_IMPORTS,
  SUPPORTED_QUANT_PACKAGE_VERSIONS,
  SUPPORTED_STANDARD_LIBRARY_MODULES,
  is_safe_module_name,
  is_standard_builtin_name,
)
from backend.time_utils import format_shanghai_datetime


class DatabaseError(RuntimeError):
  """数据库连接或执行失败。"""


class ConflictError(DatabaseError):
  """违反唯一约束等写入冲突。"""


class RunnerUnavailableError(DatabaseError):
  """指定类型的回测执行器暂时不可用。"""


_BACKTEST_SCHEDULER_LOCK_KEY = 314159265
_STRATEGY_NAME_MAX_LENGTH = 64
# 保留旧名称，避免外部诊断脚本在迁移期间失效。
_L2_FAIR_CLAIM_LOCK_KEY = _BACKTEST_SCHEDULER_LOCK_KEY
logger = logging.getLogger(__name__)


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


def strategy_name_max_length() -> int:
  """返回完整策略名称的持久化长度上限。"""
  return _STRATEGY_NAME_MAX_LENGTH


@contextmanager
def _db() -> Iterator[psycopg.Connection[Any]]:
  if not settings.DATABASE_URL:
    raise DatabaseError("未设置 TREND_GRID_DATABASE_URL")
  try:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as conn:
      with conn.cursor() as cur:
        cur.execute(
          "SELECT set_config('app.current_user_id', %s, true)",
          (current_user_id(),),
        )
        cur.execute(
          "SELECT set_config('app.current_is_admin', %s, true)",
          ("true" if current_user_is_admin() else "false",),
        )
      yield conn
  except psycopg.Error as error:
    raise _translate_error(error) from error


@contextmanager
def database_session() -> Iterator[psycopg.Connection[Any]]:
  """为需要跨仓储原子写入的领域服务提供用户上下文连接。"""
  with _db() as conn:
    yield conn


def ensure_schema() -> None:
  """确认数据库和迁移已经准备好；DDL 只允许由部署迁移执行。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT to_regclass('public.backtest_run') AS run_table,
          to_regclass('public.backtest_execution_slot') AS slot_table
      """)
      row = cur.fetchone()
      if row["run_table"] is None:
        raise DatabaseError("数据库迁移尚未执行，请先运行 deploy 的 migrate 服务")
      if row["slot_table"] is None:
        raise DatabaseError("数据库缺少统一回测调度迁移 021，请先运行 deploy 的 migrate 服务")
      cur.execute("SELECT to_regclass('public.backtest_runner_health') AS health_table")
      if cur.fetchone()["health_table"] is None:
        raise DatabaseError("数据库缺少 Runner 健康迁移 022，请先运行 deploy 的 migrate 服务")
      cur.execute("SELECT to_regclass('public.app_release') AS release_table")
      if cur.fetchone()["release_table"] is None:
        raise DatabaseError("数据库缺少发布通知迁移 027，请先运行 deploy 的 migrate 服务")
      cur.execute("""
        SELECT to_regprocedure(
          'app.acknowledge_release(uuid,uuid,character varying)'
        ) AS acknowledge_function
      """)
      if cur.fetchone()["acknowledge_function"] is None:
        raise DatabaseError("数据库缺少发布权限迁移 028，请先运行 deploy 的 migrate 服务")
      cur.execute("""
        SELECT EXISTS (
          SELECT 1 FROM information_schema.columns
          WHERE table_schema='public' AND table_name='strategy_config'
            AND column_name='strategy_version_id'
        ) AS has_strategy_ownership
      """)
      if not cur.fetchone()["has_strategy_ownership"]:
        raise DatabaseError("数据库缺少策略归属迁移 023，请先运行 deploy 的 migrate 服务")
      cur.execute("SELECT to_regclass('public.strategy_runtime_error_stat') AS runtime_error_table")
      if cur.fetchone()["runtime_error_table"] is None:
        raise DatabaseError("数据库缺少策略运行时错误统计迁移 025，请先运行 deploy 的 migrate 服务")
def get_release_status_record() -> dict[str, Any] | None:
  """返回当前部署、有效公告和当前用户回执。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT d.current_build_id, d.required_client_version,
          d.current_notice_id, d.activation_kind, d.generation, d.activated_at,
          r.release_no, r.version, r.title, r.summary, r.highlights,
          r.level, r.status, r.manifest_digest, r.published_at,
          (receipt.user_id IS NOT NULL) AS acknowledged
        FROM app_deployment_state d
        LEFT JOIN app_release r ON r.id=d.current_notice_id
        LEFT JOIN app_release_receipt receipt
          ON receipt.release_id=r.id AND receipt.user_id=%s
        WHERE d.singleton_key=true
      """, (current_user_id(),))
      return cur.fetchone()


def acknowledge_release(release_id: str, client_build_id: str) -> bool:
  """确认当前有效公告；公告已切换时返回 False。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT app.acknowledge_release(%s, %s, %s) AS acknowledged",
        (release_id, current_user_id(), client_build_id),
      )
      return bool(cur.fetchone()["acknowledged"])


def list_release_records(
  limit: int, cursor: tuple[datetime, int] | None,
) -> list[dict[str, Any]]:
  """按发布序号倒序返回用户可见公告。"""
  with _db() as conn:
    with conn.cursor() as cur:
      if cursor is None:
        cur.execute("""
          SELECT id, release_no, version, title, summary, highlights, level, published_at
          FROM app_release
          WHERE status='published' AND notify_users
          ORDER BY published_at DESC, release_no DESC LIMIT %s
        """, (limit,))
      else:
        cur.execute("""
          SELECT id, release_no, version, title, summary, highlights, level, published_at
          FROM app_release
          WHERE status='published' AND notify_users
            AND (published_at, release_no) < (%s, %s)
          ORDER BY published_at DESC, release_no DESC LIMIT %s
        """, (cursor[0], cursor[1], limit))
      return list(cur.fetchall())


def bootstrap_deployment(build_id: str, client_version: int, deployment_id: str) -> dict[str, Any]:
  """首次上线时创建唯一部署状态。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT app.bootstrap_deployment(%s, %s, %s) AS result",
        (build_id, client_version, deployment_id),
      )
      return dict(cur.fetchone()["result"])


def activate_release_record(
  manifest: dict[str, Any], build_id: str, manifest_digest: str,
  deployment_id: str, expected_generation: int,
) -> dict[str, Any]:
  """幂等登记公告，并用代际 CAS 激活部署。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT app.activate_release(
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        ) AS result
      """, (
        str(uuid4()), build_id, manifest_digest, manifest["version"],
        manifest["clientCompatibilityVersion"], manifest["title"],
        manifest["summary"], Jsonb(manifest["highlights"]), manifest["level"],
        deployment_id, expected_generation,
      ))
      return dict(cur.fetchone()["result"])


def activate_deployment_without_notice(
  build_id: str, client_version: int, deployment_id: str,
  expected_generation: int, activation_kind: str, keep_notice: bool,
) -> dict[str, Any]:
  """激活静默构建或回滚构建。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT app.activate_deployment_without_notice(
          %s, %s, %s, %s, %s, %s
        ) AS result
      """, (
        build_id, client_version, deployment_id, expected_generation,
        activation_kind, keep_notice,
      ))
      return dict(cur.fetchone()["result"])


def rollback_deployment(
  build_id: str, client_version: int, deployment_id: str,
  expected_generation: int, release_id: str | None,
) -> dict[str, Any]:
  """回滚到显式构建和公告，不根据历史顺序猜测。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT app.rollback_deployment(%s, %s, %s, %s, %s) AS result",
        (build_id, client_version, deployment_id, expected_generation, release_id),
      )
      return dict(cur.fetchone()["result"])


def withdraw_release_record(
  release_id: str, deployment_id: str, expected_generation: int,
) -> dict[str, Any]:
  """撤回公告，并在其当前生效时清空公告指针。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "SELECT app.withdraw_release(%s, %s, %s) AS result",
        (release_id, deployment_id, expected_generation),
      )
      return dict(cur.fetchone()["result"])


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
  with _db() as conn:
    with conn.cursor() as cur:
      _execute_instrument_search(cur, keyword, limit)
      return [{
        "symbol": f"{row['symbol']}.{row['exchange']}",
        "name": row["display_name"],
        "market": row["exchange"],
        "instrumentType": row["instrument_type"],
        "board": row["board"],
      } for row in cur.fetchall()]


def _execute_instrument_search(cur: Any, keyword: str, limit: int) -> None:
  code_match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", keyword.upper())
  if code_match:
    code, exchange = code_match.groups()
    if exchange:
      cur.execute("""
        SELECT symbol, exchange, display_name, instrument_type, board
        FROM market_instrument
        WHERE is_active=true AND symbol=%s AND exchange=%s
        ORDER BY exchange, symbol
        LIMIT %s
      """, (code, exchange, limit))
      return
    cur.execute("""
      SELECT symbol, exchange, display_name, instrument_type, board
      FROM market_instrument
      WHERE is_active=true AND symbol=%s
      ORDER BY exchange, symbol
      LIMIT %s
    """, (code, limit))
    return
  pattern = f"%{keyword}%"
  cur.execute("""
    SELECT symbol, exchange, display_name, instrument_type, board
    FROM market_instrument
    WHERE is_active=true
      AND (%s = '' OR symbol ILIKE %s OR display_name ILIKE %s)
    ORDER BY CASE WHEN symbol = %s THEN 0 WHEN symbol ILIKE %s THEN 1 ELSE 2 END,
             exchange, symbol
    LIMIT %s
  """, (keyword, pattern, pattern, keyword, f"{keyword}%", limit))


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


def replace_market_instruments_from_directory(items: list[dict[str, str | None]]) -> int:
  """以完整证券主数据快照替换 PostgreSQL 中的有效股票目录。"""
  if not items:
    return 0
  rows = [
    (
      item["symbol"], item["exchange"], item["display_name"], item["instrument_type"],
      item["board"], _legacy_secid(item["symbol"], item["exchange"]), item["list_date"],
      item["delist_date"], Jsonb({"source": item.get("source") or "unknown"}),
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


def _legacy_secid(symbol: str | None, exchange: str | None) -> str:
  if not symbol or not exchange:
    raise DatabaseError("证券目录缺少代码或交易所")
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
    "versionId": _uuid(row.get("version_id")),
    "versionNo": int(row.get("version_no") or 1),
    "name": row["name"],
    "description": row["description"],
    "language": "python",
    "code": row["source_code"],
    "entryType": runtime.get("entryType", "on_bar"),
    "signalRequired": bool(runtime.get("signalRequired", False)),
    "status": runtime.get("status", "draft"),
    "contentFingerprint": row.get("content_fingerprint", ""),
    "createdAt": _format_time(row["created_at"]),
    "updatedAt": _format_time(row["updated_at"]),
  }


def _normalize_config(row: dict[str, Any]) -> dict[str, Any]:
  runtime = _json(row.get("runtime_config"), {})
  source_publication_id = _uuid(row.get("source_publication_id"))
  return {
    "id": _uuid(row["id"]),
    "name": row["name"],
    "scriptId": _uuid(row["strategy_id"]),
    "strategyVersionId": _uuid(row.get("strategy_version_id")),
    "scriptVersionNo": int(row.get("script_version_no") or 1),
    "description": row.get("script_description", ""),
    "code": row.get("source_code", ""),
    "entryType": runtime.get("entryType", "on_bar"),
    "signalRequired": bool(runtime.get("signalRequired", False)),
    "strategy": row["strategy_key"],
    "mode": row["mode"],
    "symbol": f"{row['symbol']}.{row['exchange']}",
    "timeframe": row["timeframe"],
    "benchmarkSymbol": row.get("benchmark_symbol"),
    "accountId": int(row["account_id"]) if row.get("account_id") is not None else None,
    "paramsJson": _json(row["params"], {}),
    "enabled": bool(row["enabled"]),
    "published": bool(row["published"]),
    "publishedVersionId": _uuid(row.get("published_version_id")),
    "revision": int(row.get("revision") or 1),
    "sharedSource": {
      "publicationId": source_publication_id,
      "authorName": row.get("source_author_name_snapshot"),
      "title": row.get("source_title_snapshot"),
    } if source_publication_id else None,
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
    "strategyVersionId": _uuid(row.get("strategy_version_id")) or _uuid(snapshot.get("strategyVersionId")),
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
    "isFavorited": row.get("favorited_at") is not None,
    "favoritedAt": (
      _format_time(row["favorited_at"]) if row.get("favorited_at") is not None else None
    ),
    "resultJson": result,
    "queuedAt": _format_time(row.get("queued_at")),
    "startedAt": _format_time(row.get("started_at")),
    "cancelRequested": bool(row.get("cancel_requested", False)),
    "createdAt": _format_time(row["created_at"]),
    "finishedAt": _format_time(row.get("finished_at")),
    "publicationId": _uuid(row.get("publication_id")),
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
      v.id AS version_id, v.version_no, v.source_code, v.runtime_config,
      v.content_fingerprint
    FROM user_strategy s
    JOIN user_strategy_version v ON v.strategy_id=s.id AND v.version_no=s.current_version_no
    WHERE s.owner_user_id=%s AND s.status='active' {where}
  """


def list_strategy_scripts() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(_script_query("ORDER BY s.updated_at DESC, s.id DESC"), (current_user_id(),))
      return [_normalize_script(row) for row in cur.fetchall()]


def _script_runtime_config(payload: dict[str, Any], status: str = "active") -> dict[str, Any]:
  return {
    "entryType": payload.get("entryType", "on_bar"),
    "signalRequired": bool(payload.get("signalRequired", False)),
    "status": status,
  }


def _script_content_fingerprint(source: str, runtime: dict[str, Any]) -> str:
  entry_type = str(runtime.get("entryType", "on_bar"))
  signal_required = "1" if bool(runtime.get("signalRequired", False)) else "0"
  value = f"{len(source)}:{source}{len(entry_type)}:{entry_type}{signal_required}"
  return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_script_version(
  cur: Any,
  script_id: str,
  version_no: int,
  payload: dict[str, Any],
  status: str = "active",
) -> tuple[str, str]:
  source = str(payload["code"])
  runtime = _script_runtime_config(payload, status)
  fingerprint = _script_content_fingerprint(source, runtime)
  cur.execute("""
    INSERT INTO user_strategy_version (
      id, strategy_id, version_no, source_code, source_checksum, runtime_config,
      content_fingerprint, created_by_user_id
    ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s)
    RETURNING id
  """, (script_id, version_no, source, hashlib.sha256(source.encode("utf-8")).hexdigest(),
         Jsonb(runtime), fingerprint, current_user_id()))
  return _uuid(cur.fetchone()["id"]) or "", fingerprint


def _script_version_state(cur: Any, script_id: str, fingerprint: str) -> dict[str, Any]:
  cur.execute("""
    SELECT strategy.current_version_no, version.id AS version_id,
      version.content_fingerprint,
      matching_version.id AS matching_version_id,
      matching_version.version_no AS matching_version_no,
      (SELECT max(candidate.version_no)
       FROM user_strategy_version candidate
       WHERE candidate.strategy_id=strategy.id) AS max_version_no
    FROM user_strategy strategy
    JOIN user_strategy_version version
      ON version.strategy_id=strategy.id
      AND version.version_no=strategy.current_version_no
    LEFT JOIN user_strategy_version matching_version
      ON matching_version.strategy_id=strategy.id
      AND matching_version.content_fingerprint=%s
    WHERE strategy.id=%s AND strategy.owner_user_id=%s
    FOR UPDATE OF strategy
  """, (fingerprint, script_id, current_user_id()))
  row = cur.fetchone()
  if not row:
    raise KeyError(script_id)
  return row


def _write_strategy_script(
  cur: Any,
  script_id: str | None,
  payload: dict[str, Any],
) -> tuple[str, str, int]:
  if script_id is None:
    cur.execute("""
      INSERT INTO user_strategy (id, owner_user_id, name, description, strategy_type, status)
      VALUES (gen_random_uuid(), %s, %s, %s, 'python', 'active') RETURNING id
    """, (current_user_id(), payload["name"], payload.get("description", "")))
    saved_id = _uuid(cur.fetchone()["id"]) or ""
    version_id, _ = _insert_script_version(cur, saved_id, 1, payload)
    return saved_id, version_id, 1
  runtime = _script_runtime_config(payload)
  fingerprint = _script_content_fingerprint(str(payload["code"]), runtime)
  row = _script_version_state(cur, script_id, fingerprint)
  version_no = int(row["current_version_no"])
  version_id = _uuid(row["version_id"]) or ""
  matching_version_id = _uuid(row.get("matching_version_id"))
  if row.get("content_fingerprint") == fingerprint:
    pass
  elif matching_version_id:
    version_id = matching_version_id
    version_no = int(row["matching_version_no"])
  else:
    version_no = int(row.get("max_version_no") or version_no) + 1
    version_id, _ = _insert_script_version(cur, script_id, version_no, payload)
  cur.execute("""
    UPDATE user_strategy
    SET name=%s, description=%s, current_version_no=%s, status='active', updated_at=now()
    WHERE id=%s AND owner_user_id=%s
  """, (
    payload["name"], payload.get("description", ""), version_no,
    script_id, current_user_id(),
  ))
  return script_id, version_id, version_no


def _strategy_name_candidate(requested_name: str, suffix: int) -> str:
  if suffix == 0:
    return requested_name
  suffix_text = f"-{suffix}"
  return f"{requested_name[:_STRATEGY_NAME_MAX_LENGTH - len(suffix_text)]}{suffix_text}"


def _resolve_unique_strategy_name(cur: Any, requested_name: str, config_id: str | None) -> str:
  """在用户级事务锁内分配名称，避免并发保存绕过自动编号。"""
  normalized_name = requested_name.strip()
  if not normalized_name:
    raise ConflictError("策略名称不能为空")
  cur.execute(
    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
    (f"strategy-name:{current_user_id()}",),
  )
  cur.execute("""
    SELECT COALESCE(array_agg(name::text), ARRAY[]::text[]) AS names
    FROM strategy_config
    WHERE owner_user_id=%s AND archived_at IS NULL
      AND (%s::uuid IS NULL OR id<>%s::uuid)
  """, (current_user_id(), config_id, config_id))
  existing_names = set(cur.fetchone()["names"])
  suffix = 0
  while True:
    candidate = _strategy_name_candidate(normalized_name, suffix)
    if candidate not in existing_names:
      return candidate
    suffix += 1


def create_strategy_script(payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      script_id, _, _ = _write_strategy_script(cur, None, payload)
  return get_strategy_script(script_id)


def update_strategy_script(script_id: str, payload: dict[str, Any]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT 1 FROM strategy_config WHERE strategy_id=%s", (script_id,))
      if cur.fetchone():
        raise ConflictError("该脚本已归属完整策略，请通过策略方案保存")
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


def get_strategy_script_version(version_id: str, script_id: str | None = None) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT strategy.id, strategy.name, strategy.description,
          strategy.created_at, strategy.updated_at,
          version.id AS version_id, version.version_no, version.source_code,
          version.runtime_config, version.content_fingerprint
        FROM user_strategy_version version
        JOIN user_strategy strategy ON strategy.id=version.strategy_id
        WHERE version.id=%s AND strategy.owner_user_id=%s
          AND (%s::uuid IS NULL OR strategy.id=%s::uuid)
      """, (version_id, current_user_id(), script_id, script_id))
      row = cur.fetchone()
      if not row:
        raise KeyError(version_id)
      return _normalize_script(row)


def delete_strategy_script(script_id: str) -> bool:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE user_strategy SET status='archived', updated_at=now()
        WHERE id=%s AND owner_user_id=%s
          AND NOT EXISTS (SELECT 1 FROM strategy_config WHERE strategy_id=%s)
      """, (script_id, current_user_id(), script_id))
      return cur.rowcount > 0


def _config_query(where: str = "") -> str:
  return f"""
    SELECT c.*, i.symbol, i.exchange,
      CASE WHEN b.id IS NULL THEN NULL ELSE b.symbol || '.' || b.exchange END AS benchmark_symbol,
      strategy.description AS script_description,
      version.version_no AS script_version_no,
      version.source_code, version.runtime_config,
      version.content_fingerprint,
      latest_version.id AS published_version_id
    FROM strategy_config c
    JOIN market_instrument i ON i.id=c.instrument_id
    LEFT JOIN market_instrument b ON b.id=c.benchmark_instrument_id
    LEFT JOIN user_strategy strategy ON strategy.id=c.strategy_id
    LEFT JOIN user_strategy_version current_version
      ON current_version.strategy_id=strategy.id
      AND current_version.version_no=strategy.current_version_no
    LEFT JOIN user_strategy_version version
      ON version.id=COALESCE(c.strategy_version_id, current_version.id)
    LEFT JOIN LATERAL (
      SELECT config_version.id
      FROM strategy_config_version config_version
      WHERE config_version.config_id=c.id
      ORDER BY config_version.version_no DESC
      LIMIT 1
    ) latest_version ON true
    WHERE c.owner_user_id=%s AND c.archived_at IS NULL {where}
  """


def list_strategy_configs() -> list[dict[str, Any]]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(_config_query("ORDER BY c.updated_at DESC, c.id DESC"), (current_user_id(),))
      return [_normalize_config(row) for row in cur.fetchall()]


def _config_snapshot(config: dict[str, Any]) -> dict[str, Any]:
  params_json = dict(config.get("paramsJson") or {})
  code = str(config.get("code") or "")
  params_json["scriptCodeHash"] = hashlib.sha256(code.encode("utf-8")).hexdigest()
  params_json["scriptSnapshot"] = {
    "id": config.get("scriptId"),
    "versionId": config.get("strategyVersionId"),
    "name": config["name"],
    "description": config.get("description", ""),
    "code": code,
    "entryType": config.get("entryType", "on_bar"),
    "signalRequired": bool(config.get("signalRequired", False)),
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
  }
  return {
    "name": config["name"], "scriptId": config.get("scriptId"),
    "strategyVersionId": config.get("strategyVersionId"),
    "strategy": config["strategy"], "mode": config["mode"], "symbol": config["symbol"],
    "timeframe": config.get("timeframe", "5m"), "benchmarkSymbol": config.get("benchmarkSymbol"),
    "accountId": config.get("accountId"), "paramsJson": params_json,
    "sharedSource": config.get("sharedSource"),
  }


def _write_config(
  cur: Any,
  config_id: str | None,
  payload: dict[str, Any],
  strategy_id: str,
  strategy_version_id: str,
) -> str:
  instrument_id = _instrument_id(cur, payload["symbol"])
  benchmark_id = _instrument_id(cur, payload["benchmarkSymbol"]) if payload.get("benchmarkSymbol") else None
  if config_id is None:
    cur.execute("""
      INSERT INTO strategy_config (
        owner_user_id, name, strategy_id, strategy_version_id, strategy_key, mode, instrument_id, timeframe,
        benchmark_instrument_id, account_id, params, enabled, published,
        source_publication_id, source_author_name_snapshot, source_title_snapshot
      ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,%s) RETURNING id
    """, (current_user_id(), payload["name"], strategy_id, strategy_version_id,
           payload.get("strategy", "user_script"),
           payload.get("mode", "backtest"), instrument_id, payload.get("timeframe", "5m"), benchmark_id,
           payload.get("accountId"), Jsonb(payload.get("paramsJson", {})), payload.get("enabled", True),
           payload.get("sourcePublicationId"), payload.get("sourceAuthorName"),
           payload.get("sourceTitle")))
  else:
    cur.execute("""
      UPDATE strategy_config SET name=%s, strategy_version_id=%s, strategy_key=%s, mode=%s,
        instrument_id=%s, timeframe=%s, benchmark_instrument_id=%s, account_id=%s, params=%s,
        enabled=%s, published=true, revision=revision + 1, updated_at=now()
      WHERE id=%s AND owner_user_id=%s AND strategy_id=%s RETURNING id
    """, (payload["name"], strategy_version_id, payload.get("strategy", "user_script"),
           payload.get("mode", "backtest"), instrument_id, payload.get("timeframe", "5m"), benchmark_id,
           payload.get("accountId"), Jsonb(payload.get("paramsJson", {})), payload.get("enabled", True),
           config_id, current_user_id(), strategy_id))
  row = cur.fetchone()
  if not row:
    raise KeyError(config_id)
  return _uuid(row["id"]) or ""


def create_strategy_config(payload: dict[str, Any]) -> dict[str, Any]:
  raise ConflictError("请使用完整策略保存接口创建策略，不能单独绑定脚本")


def update_strategy_config(config_id: str, payload: dict[str, Any]) -> dict[str, Any]:
  raise ConflictError("请使用完整策略保存接口修改策略，不能单独改绑脚本")


def _get_strategy_config_with_cursor(
  cur: Any,
  config_id: str,
  for_update: bool = False,
) -> dict[str, Any]:
  suffix = "AND c.id=%s FOR UPDATE OF c" if for_update else "AND c.id=%s"
  cur.execute(_config_query(suffix), (current_user_id(), config_id))
  row = cur.fetchone()
  if not row:
    raise KeyError(config_id)
  return _normalize_config(row)


def get_strategy_config_in_transaction(
  cur: Any,
  config_id: str,
) -> dict[str, Any]:
  """在调用方事务中读取当前用户的完整策略。"""
  return _get_strategy_config_with_cursor(cur, config_id)


def create_published_strategy_in_transaction(
  cur: Any,
  script_payload: dict[str, Any],
  config_payload: dict[str, Any],
) -> dict[str, Any]:
  """在调用方事务中创建并发布一套独立完整策略。"""
  resolved_name = _resolve_unique_strategy_name(
    cur, str(config_payload["name"]), None,
  )
  resolved_script = {**script_payload, "name": resolved_name}
  resolved_config = {**config_payload, "name": resolved_name}
  params_json = dict(resolved_config.get("paramsJson") or {})
  params_json["scriptName"] = resolved_name
  resolved_config["paramsJson"] = params_json
  script_id, strategy_version_id, _ = _write_strategy_script(
    cur, None, resolved_script,
  )
  config_id = _write_config(
    cur, None, resolved_config, script_id, strategy_version_id,
  )
  config = _get_strategy_config_with_cursor(cur, config_id)
  _publish_strategy_config_with_cursor(cur, config)
  return config



def save_complete_strategy(
  script_payload: dict[str, Any],
  config_payload: dict[str, Any],
  script_id: str | None,
  config_id: str | None,
  expected_revision: int | None = None,
) -> dict[str, dict[str, Any]]:
  """原子保存一对一完整策略，并自动创建精确的发布版本。"""
  with _db() as conn:
    with conn.cursor() as cur:
      owned_script_id: str | None = None
      if config_id is not None:
        cur.execute("""
          SELECT strategy_id, revision FROM strategy_config
          WHERE id=%s AND owner_user_id=%s FOR UPDATE
        """, (config_id, current_user_id()))
        current = cur.fetchone()
        if not current:
          raise KeyError(config_id)
        owned_script_id = _uuid(current["strategy_id"])
        if script_id and script_id != owned_script_id:
          raise ConflictError("策略脚本归属不一致，禁止将方案改绑到其他脚本")
        if expected_revision is None or int(current["revision"]) != expected_revision:
          raise ConflictError("该策略已在其他会话中更新，请刷新后重试")
      resolved_name = _resolve_unique_strategy_name(
        cur, str(config_payload["name"]), config_id,
      )
      resolved_script = {**script_payload, "name": resolved_name}
      saved_script_id, strategy_version_id, _ = _write_strategy_script(
        cur, owned_script_id, resolved_script,
      )
      config = {**config_payload, "name": resolved_name, "scriptId": saved_script_id}
      saved_config_id = _write_config(
        cur, config_id, config, saved_script_id, strategy_version_id,
      )
      saved_config = _get_strategy_config_with_cursor(cur, saved_config_id)
      _publish_strategy_config_with_cursor(cur, saved_config)
  return {
    "script": get_strategy_script(saved_script_id),
    "config": get_strategy_config(saved_config_id),
  }


def _version_fingerprint(snapshot: dict[str, Any]) -> str:
  return hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _publish_strategy_config_with_cursor(
  cur: Any,
  config: dict[str, Any],
) -> tuple[str, bool]:
  config_id = str(config["id"])
  strategy_version_id = str(config["strategyVersionId"])
  snapshot = _config_snapshot(config)
  cur.execute("""
    SELECT id, snapshot FROM strategy_config_version
    WHERE config_id=%s ORDER BY version_no DESC LIMIT 1
  """, (config_id,))
  previous = cur.fetchone()
  if previous and _version_fingerprint(previous["snapshot"]) == _version_fingerprint(snapshot):
    cur.execute("UPDATE strategy_config SET published=true WHERE id=%s", (config_id,))
    return _uuid(previous["id"]) or "", False
  cur.execute("""
    SELECT COALESCE(MAX(version_no), 0) + 1 AS value
    FROM strategy_config_version WHERE config_id=%s
  """, (config_id,))
  version_no = int(cur.fetchone()["value"])
  cur.execute("""
    INSERT INTO strategy_config_version (
      config_id, version_no, strategy_version_id, snapshot
    ) VALUES (%s, %s, %s, %s) RETURNING id
  """, (config_id, version_no, strategy_version_id, Jsonb(snapshot)))
  version_id = _uuid(cur.fetchone()["id"]) or ""
  cur.execute("UPDATE strategy_config SET published=true WHERE id=%s", (config_id,))
  return version_id, True


def publish_strategy_config(config_id: str) -> tuple[dict[str, Any], bool]:
  with _db() as conn:
    with conn.cursor() as cur:
      config = _get_strategy_config_with_cursor(cur, config_id, for_update=True)
      version_id, created = _publish_strategy_config_with_cursor(cur, config)
  return get_strategy_config_version(version_id), created


def get_strategy_config(config_id: str) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      return _get_strategy_config_with_cursor(cur, config_id)


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
      cur.execute("""
        UPDATE strategy_config
        SET archived_at=now(), published=false, revision=revision + 1, updated_at=now()
        WHERE id=%s AND owner_user_id=%s AND archived_at IS NULL
        RETURNING strategy_id
      """, (config_id, current_user_id()))
      row = cur.fetchone()
      if not row:
        return False
      cur.execute("""
        UPDATE user_strategy SET status='archived', updated_at=now()
        WHERE id=%s AND owner_user_id=%s
      """, (row["strategy_id"], current_user_id()))
      return True


def _with_script_code_hash(payload: dict[str, Any]) -> dict[str, Any]:
  normalized = dict(payload)
  params = dict(normalized.get("paramsJson") or {})
  script_id = normalized.get("scriptId")
  if script_id:
    version_id = normalized.get("strategyVersionId") or params.get("strategyVersionId")
    script = (
      get_strategy_script_version(str(version_id), str(script_id))
      if version_id
      else get_strategy_script(str(script_id))
    )
    code_hash = hashlib.sha256(script["code"].encode("utf-8")).hexdigest()
    params["scriptCodeHash"] = code_hash
    params["scriptSnapshot"] = {
      "id": script["id"],
      "versionId": script.get("versionId"),
      "name": script["name"],
      "code": script["code"],
      "entryType": script["entryType"],
      "signalRequired": bool(script.get("signalRequired", False)),
      "runtimeVersion": SCRIPT_RUNTIME_VERSION,
      "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
    }
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
  """, (owner_user_id, display_no, payload.get("strategyVersionId"), db_status,
         Jsonb(payload), Jsonb(payload.get("marketDataSnapshot", {})), db_status,
         config_id, payload.get("configVersionId"), payload.get("accountId"), instrument_id, benchmark_id,
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
  routed_payload = {
    **payload,
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
  }
  run_id, display_no = _create_backtest_run_row(cur, routed_payload, "queued", owner_user_id)
  cur.execute("UPDATE backtest_run SET fill_model=%s WHERE id=%s AND owner_user_id=%s", (
    fill_model, run_id, owner_user_id,
  ))
  cur.execute(f"""
    INSERT INTO {job_table} (
      run_id, owner_user_id, payload, runtime_version, capability_digest
    ) VALUES (%s, %s, %s, %s, %s)
  """, (
    run_id, owner_user_id, Jsonb(routed_payload),
    SCRIPT_RUNTIME_VERSION, RUNTIME_CAPABILITY_DIGEST,
  ))
  _save_research_signal_snapshot(
    cur, run_id, owner_user_id, routed_payload.get("researchSignalSnapshot"),
  )
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
  try:
    storage_key = shared_backtest_artifacts.storage_key_for_path(path)
  except shared_backtest_artifacts.SharedArtifactError:
    # 兼容测试和旧调用方传入的显式快照路径；正常运行仍由受控根目录生成。
    storage_key = f"research_signals/{path.name}"
  cur.execute(
    """INSERT INTO artifact_file
      (id, owner_user_id, run_id, artifact_type, storage_key, original_filename, content_type, byte_size, content_sha256)
      VALUES (gen_random_uuid(), %s, %s, 'research_signal', %s, %s, 'application/x-ndjson', %s, %s)
      ON CONFLICT (storage_key) DO UPDATE
      SET storage_key=EXCLUDED.storage_key
      WHERE artifact_file.artifact_type=EXCLUDED.artifact_type
        AND artifact_file.original_filename=EXCLUDED.original_filename
        AND artifact_file.content_type=EXCLUDED.content_type
        AND artifact_file.byte_size=EXCLUDED.byte_size
        AND artifact_file.content_sha256=EXCLUDED.content_sha256
      RETURNING id""",
    (owner_user_id, run_id, storage_key, path.name, path.stat().st_size, content_sha256),
  )
  artifact_row = cur.fetchone()
  if artifact_row is None:
    raise DatabaseError("研究信号冻结文件与已有存储记录冲突")
  artifact_id = artifact_row["id"]
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
      job_kind = "l2" if payload.get("fillModel") == "l2_quote" else "bar"
      if (
        settings.BACKTEST_RUNNER_AVAILABILITY_REQUIRED
        and not _has_healthy_backtest_runner(cur, job_kind)
      ):
        raise RunnerUnavailableError("回测执行服务暂不可用，请稍后重试")
      cur.execute("""
        SELECT count(*) AS value
        FROM api_idempotency_record record
        JOIN backtest_run run ON run.id=record.run_id
        WHERE record.api_key_id=%s AND run.status IN ('queued', 'running', 'cancelling')
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


def _finalize_expired_backtest_jobs(cur: Any) -> None:
  """回收过期槽位，并将已失去执行器的取消任务收敛到终态。"""
  for table in ("bar_backtest_job", "l2_backtest_job"):
    cur.execute(f"""
      WITH expired_jobs AS (
        UPDATE {table}
        SET status='cancelled', lease_expires_at=NULL, finished_at=now(), updated_at=now()
        WHERE status='cancelling' AND lease_expires_at < now()
        RETURNING run_id
      )
      UPDATE backtest_run AS run
      SET status='cancelled', error_message=NULL, finished_at=now()
      FROM expired_jobs
      WHERE run.id=expired_jobs.run_id AND run.status='cancelling'
    """)
  cur.execute("DELETE FROM backtest_execution_slot WHERE lease_expires_at < now()")


def _next_backtest_candidate(
  cur: Any,
  job_kind: str | None = None,
  runtime_version: str = SCRIPT_RUNTIME_VERSION,
  capability_digest: str = RUNTIME_CAPABILITY_DIGEST,
) -> dict[str, Any] | None:
  """在全局锁内按用户当前占用数、提交时间选择下一条任务。"""
  cur.execute("""
    SELECT candidate.*,
      (SELECT count(*) FROM backtest_execution_slot slot
       WHERE slot.owner_user_id=candidate.owner_user_id) AS owner_active_count
    FROM (
      SELECT 'bar' AS job_kind, run_id, owner_user_id, created_at
      FROM bar_backtest_job
      WHERE (status='queued' OR (status='running' AND lease_expires_at < now()))
        AND NOT cancel_requested
        AND runtime_version=%s AND capability_digest=%s
      UNION ALL
      SELECT 'l2' AS job_kind, run_id, owner_user_id, created_at
      FROM l2_backtest_job
      WHERE (status='queued' OR (status='running' AND lease_expires_at < now()))
        AND NOT cancel_requested
        AND runtime_version=%s AND capability_digest=%s
    ) AS candidate
    WHERE (%s::text IS NULL OR candidate.job_kind=%s::text)
    ORDER BY owner_active_count, candidate.created_at, candidate.run_id
    LIMIT 1
  """, (
    runtime_version, capability_digest, runtime_version, capability_digest,
    job_kind, job_kind,
  ))
  return cur.fetchone()


def _scheduler_capacity(cur: Any) -> tuple[int, int]:
  cur.execute("""
    SELECT count(*) AS active_count,
      (SELECT override_limit FROM backtest_scheduler_capacity
       WHERE singleton AND override_expires_at > now()) AS override_limit
    FROM backtest_execution_slot
  """)
  row = cur.fetchone() or {}
  return int(row.get("active_count", 0)), int(row["override_limit"]) if row.get("override_limit") else 0


def _record_backtest_runner_heartbeat(
  cur: Any,
  worker_id: str,
  job_kind: str,
  status: str,
  current_run_id: str | None = None,
  error_code: str | None = None,
  runtime_version: str = SCRIPT_RUNTIME_VERSION,
  capability_digest: str = RUNTIME_CAPABILITY_DIGEST,
) -> None:
  """限频写入 Runner 心跳，不保存异常正文或敏感诊断。"""
  cur.execute("""
    INSERT INTO backtest_runner_health (
      worker_id, job_kind, status, current_run_id, last_error_code,
      runtime_version, capability_digest
    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (worker_id) DO UPDATE SET
      job_kind=EXCLUDED.job_kind, status=EXCLUDED.status,
      current_run_id=EXCLUDED.current_run_id, last_error_code=EXCLUDED.last_error_code,
      runtime_version=EXCLUDED.runtime_version,
      capability_digest=EXCLUDED.capability_digest,
      heartbeat_at=now()
    WHERE backtest_runner_health.heartbeat_at
        < now() - make_interval(secs => %s)
      OR backtest_runner_health.status IS DISTINCT FROM EXCLUDED.status
      OR backtest_runner_health.current_run_id IS DISTINCT FROM EXCLUDED.current_run_id
      OR backtest_runner_health.last_error_code IS DISTINCT FROM EXCLUDED.last_error_code
      OR backtest_runner_health.runtime_version IS DISTINCT FROM EXCLUDED.runtime_version
      OR backtest_runner_health.capability_digest IS DISTINCT FROM EXCLUDED.capability_digest
  """, (
    worker_id, job_kind, status, current_run_id, error_code,
    runtime_version, capability_digest,
    settings.BACKTEST_RUNNER_HEARTBEAT_WRITE_SECONDS,
  ))


def record_backtest_runner_heartbeat(
  worker_id: str,
  job_kind: str,
  status: str,
  current_run_id: str | None = None,
  error_code: str | None = None,
  runtime_version: str = SCRIPT_RUNTIME_VERSION,
  capability_digest: str = RUNTIME_CAPABILITY_DIGEST,
) -> None:
  if job_kind not in {"bar", "l2"} or status not in {"ready", "running", "error"}:
    raise ValueError("Runner 心跳类型或状态无效")
  with _db() as conn:
    with conn.cursor() as cur:
      _record_backtest_runner_heartbeat(
        cur, worker_id, job_kind, status, current_run_id, error_code,
        runtime_version, capability_digest,
      )


def has_healthy_backtest_runner(job_kind: str) -> bool:
  """判断指定类型是否至少有一个处于有效心跳期的执行器。"""
  if not settings.BACKTEST_RUNNER_AVAILABILITY_REQUIRED:
    return True
  with _db() as conn:
    with conn.cursor() as cur:
      return _has_healthy_backtest_runner(cur, job_kind)


def _has_healthy_backtest_runner(cur: Any, job_kind: str) -> bool:
  cur.execute("""
    SELECT EXISTS (
      SELECT 1 FROM backtest_runner_health
      WHERE job_kind=%s AND status IN ('ready', 'running')
        AND runtime_version=%s AND capability_digest=%s
        AND heartbeat_at >= now() - make_interval(secs => %s)
    ) AS available
  """, (
    job_kind, SCRIPT_RUNTIME_VERSION, RUNTIME_CAPABILITY_DIGEST,
    settings.BACKTEST_RUNNER_HEALTH_TTL_SECONDS,
  ))
  return bool(cur.fetchone()["available"])


def get_backtest_runner_health() -> dict[str, Any]:
  """返回健康接口使用的聚合信息，不暴露 worker 标识和内部错误。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        WITH kinds(job_kind) AS (VALUES ('bar'::text), ('l2'::text))
        SELECT kinds.job_kind,
          count(health.worker_id) FILTER (
            WHERE health.status IN ('ready', 'running')
              AND health.heartbeat_at >= now() - make_interval(secs => %s)
          ) AS healthy_count,
          count(health.worker_id) FILTER (
            WHERE health.status='error'
              AND health.heartbeat_at >= now() - make_interval(secs => %s)
          ) AS error_count,
          CASE WHEN kinds.job_kind='bar'
            THEN (SELECT count(*) FROM bar_backtest_job
                  WHERE status='queued' AND runtime_version=%s AND capability_digest=%s)
            ELSE (SELECT count(*) FROM l2_backtest_job
                  WHERE status='queued' AND runtime_version=%s AND capability_digest=%s)
          END AS queued_count
        FROM kinds
        LEFT JOIN backtest_runner_health health
          ON health.job_kind=kinds.job_kind
          AND health.runtime_version=%s AND health.capability_digest=%s
        GROUP BY kinds.job_kind
        ORDER BY kinds.job_kind
      """, (
        settings.BACKTEST_RUNNER_HEALTH_TTL_SECONDS,
        settings.BACKTEST_RUNNER_HEALTH_TTL_SECONDS,
        SCRIPT_RUNTIME_VERSION,
        RUNTIME_CAPABILITY_DIGEST,
        SCRIPT_RUNTIME_VERSION,
        RUNTIME_CAPABILITY_DIGEST,
        SCRIPT_RUNTIME_VERSION,
        RUNTIME_CAPABILITY_DIGEST,
      ))
      rows = cur.fetchall()
  runners = {
    str(row["job_kind"]): {
      "healthy": int(row["healthy_count"]),
      "errors": int(row["error_count"]),
      "queued": int(row["queued_count"]),
    }
    for row in rows
  }
  return {
    "status": (
      "ok"
      if not settings.BACKTEST_RUNNER_AVAILABILITY_REQUIRED
      or all(item["healthy"] > 0 for item in runners.values())
      else "degraded"
    ),
    "runners": runners,
  }


def _claim_backtest_job(
  cur: Any,
  job_kind: str,
  worker_id: str,
  lease_seconds: int,
  runtime_version: str = SCRIPT_RUNTIME_VERSION,
  capability_digest: str = RUNTIME_CAPABILITY_DIGEST,
) -> dict[str, Any] | None:
  """领取 Bar/L2 共用容量中的一个槽位。"""
  if job_kind not in {"bar", "l2"}:
    raise ValueError("回测任务类型无效")
  cur.execute("SELECT pg_advisory_xact_lock(%s)", (_BACKTEST_SCHEDULER_LOCK_KEY,))
  _finalize_expired_backtest_jobs(cur)
  candidate = _next_backtest_candidate(
    cur, runtime_version=runtime_version, capability_digest=capability_digest,
  )
  if not candidate:
    return None
  if candidate["job_kind"] != job_kind:
    logger.info(
      "全局最优先任务等待对应执行器: kind=%s reason=%s",
      candidate["job_kind"], f"{candidate['job_kind']}_executor_wait",
    )
    candidate = _next_backtest_candidate(
      cur, job_kind, runtime_version, capability_digest,
    )
    if not candidate:
      return None

  active_count, override_limit = _scheduler_capacity(cur)
  normal_limit = override_limit or settings.BACKTEST_BASE_CONCURRENCY
  owner_active_count = int(candidate.get("owner_active_count", 0))
  fairness_burst = (
    override_limit == 0
    and settings.BACKTEST_FAIRNESS_BURST_ENABLED
    and active_count < settings.BACKTEST_BURST_MAX_CONCURRENCY
    and owner_active_count < settings.BACKTEST_USER_GUARANTEED_CONCURRENCY
  )
  if active_count >= normal_limit and not fairness_burst:
    reason = "burst_limit_wait" if active_count >= settings.BACKTEST_BURST_MAX_CONCURRENCY else "capacity_wait"
    logger.info("回测任务等待统一槽位: kind=%s reason=%s", job_kind, reason)
    return None

  table = "bar_backtest_job" if job_kind == "bar" else "l2_backtest_job"
  cur.execute(f"""
    UPDATE {table}
    SET status='running', attempt_no=attempt_no + 1, attempt_token=gen_random_uuid(),
      lease_expires_at=now() + make_interval(secs => %s), worker_id=%s,
      started_at=COALESCE(started_at, now()), updated_at=now()
    WHERE run_id=%s AND NOT cancel_requested
      AND runtime_version=%s AND capability_digest=%s
      AND (status='queued' OR (status='running' AND lease_expires_at < now()))
    RETURNING run_id, owner_user_id, payload, attempt_no, attempt_token, lease_expires_at
  """, (
    lease_seconds, worker_id, candidate["run_id"], runtime_version, capability_digest,
  ))
  row = cur.fetchone()
  if not row:
    return None
  cur.execute("""
    INSERT INTO backtest_execution_slot (
      run_id, owner_user_id, job_kind, attempt_token, worker_id, is_burst, lease_expires_at
    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (run_id) DO UPDATE SET
      attempt_token=EXCLUDED.attempt_token, worker_id=EXCLUDED.worker_id,
      is_burst=EXCLUDED.is_burst, acquired_at=now(), lease_expires_at=EXCLUDED.lease_expires_at
  """, (
    row["run_id"], row["owner_user_id"], job_kind, row["attempt_token"], worker_id,
    active_count >= settings.BACKTEST_BASE_CONCURRENCY, row["lease_expires_at"],
  ))
  return row


def claim_l2_backtest_job(
  worker_id: str,
  lease_seconds: int,
  runtime_version: str = SCRIPT_RUNTIME_VERSION,
  capability_digest: str = RUNTIME_CAPABILITY_DIGEST,
) -> dict[str, Any] | None:
  """从统一容量中原子领取一个 L2 任务。"""
  if not worker_id.strip() or lease_seconds <= 0:
    raise ValueError("L2 runner 标识和租约时长必须有效")
  with _db() as conn:
    with conn.cursor() as cur:
      row = _claim_backtest_job(
        cur, "l2", worker_id, lease_seconds, runtime_version, capability_digest,
      )
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


def claim_bar_backtest_job(
  worker_id: str,
  lease_seconds: int,
  runtime_version: str = SCRIPT_RUNTIME_VERSION,
  capability_digest: str = RUNTIME_CAPABILITY_DIGEST,
) -> dict[str, Any] | None:
  """从统一容量中原子领取一个标准 Bar 回测任务。"""
  if not worker_id.strip() or lease_seconds <= 0:
    raise ValueError("Bar runner 标识和租约时长必须有效")
  with _db() as conn:
    with conn.cursor() as cur:
      row = _claim_backtest_job(
        cur, "bar", worker_id, lease_seconds, runtime_version, capability_digest,
      )
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


def _finalize_expired_bar_backtest_cancellations(cur: Any) -> None:
  """兼容旧诊断调用，实际收尾已统一覆盖 Bar 与 L2。"""
  _finalize_expired_backtest_jobs(cur)


def heartbeat_l2_backtest_job(
  run_id: str, attempt_token: str, lease_seconds: int, worker_id: str | None = None,
) -> bool:
  """仅持有当前 attempt_token 的 runner 可以续租。"""
  with _db() as conn:
    with conn.cursor() as cur:
      return _heartbeat_backtest_job(
        cur, "l2_backtest_job", run_id, attempt_token, lease_seconds, worker_id,
      )


def heartbeat_bar_backtest_job(
  run_id: str, attempt_token: str, lease_seconds: int, worker_id: str | None = None,
) -> bool:
  """仅持有当前租约的 Bar runner 可以续租。"""
  with _db() as conn:
    with conn.cursor() as cur:
      return _heartbeat_backtest_job(
        cur, "bar_backtest_job", run_id, attempt_token, lease_seconds, worker_id,
      )


def _heartbeat_backtest_job(
  cur: Any,
  table: str,
  run_id: str,
  attempt_token: str,
  lease_seconds: int,
  worker_id: str | None = None,
) -> bool:
  cur.execute(f"""
    WITH renewed_job AS (
      UPDATE {table}
      SET lease_expires_at=now() + make_interval(secs => %s), updated_at=now()
      WHERE run_id=%s AND attempt_token=%s AND status='running' AND NOT cancel_requested
      RETURNING run_id, attempt_token, lease_expires_at
    )
    UPDATE backtest_execution_slot AS slot
    SET lease_expires_at=renewed_job.lease_expires_at
    FROM renewed_job
    WHERE slot.run_id=renewed_job.run_id AND slot.attempt_token=renewed_job.attempt_token
    RETURNING slot.run_id
  """, (lease_seconds, run_id, attempt_token))
  renewed = cur.fetchone() is not None
  if renewed and worker_id:
    _record_backtest_runner_heartbeat(
      cur, worker_id, "bar" if table == "bar_backtest_job" else "l2", "running", run_id,
    )
  return renewed


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
      effective_status = _finish_backtest_job(
        cur, "l2_backtest_job", run_id, attempt_token, status, summary, result,
      )
      if effective_status is None:
        return False
      if effective_status == "failed" and system_error and owner_user_id:
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
      effective_status = _finish_backtest_job(
        cur, "bar_backtest_job", run_id, attempt_token, status, summary, result,
      )
      if effective_status is None:
        return False
      _enqueue_research_signal_result(cur, run_id, effective_status, summary)
      if effective_status == "failed" and system_error and owner_user_id:
        _insert_system_error(cur, run_id, owner_user_id, system_error)
      return True


def _finish_backtest_job(
  cur: Any,
  table: str,
  run_id: str,
  attempt_token: str,
  requested_status: str,
  summary: dict[str, Any],
  result: dict[str, Any] | None,
) -> str | None:
  """以数据库中的取消状态优先，防止迟到成功结果覆盖取消。"""
  cur.execute(f"""
    UPDATE {table}
    SET status=CASE WHEN cancel_requested OR status='cancelling' THEN 'cancelled' ELSE %s END,
      error_message=CASE WHEN cancel_requested OR status='cancelling' THEN NULL ELSE %s END,
      lease_expires_at=NULL, finished_at=now(), updated_at=now()
    WHERE run_id=%s AND attempt_token=%s AND status IN ('running', 'cancelling')
    RETURNING status
  """, (
    requested_status, summary.get("error") if requested_status != "succeeded" else None,
    run_id, attempt_token,
  ))
  row = cur.fetchone()
  if row is None:
    return None
  effective_status = str(row["status"])
  effective_result = result if effective_status == "succeeded" else None
  effective_error = summary.get("error") if effective_status == "failed" else None
  cur.execute("""
    UPDATE backtest_run
    SET status=%s, result_summary=%s, result_json=%s, error_message=%s, finished_at=now()
    WHERE id=%s AND status IN ('running', 'cancelling')
  """, (
    effective_status, Jsonb(summary), Jsonb(effective_result) if effective_result else None,
    effective_error, run_id,
  ))
  cur.execute(
    "DELETE FROM backtest_execution_slot WHERE run_id=%s AND attempt_token=%s",
    (run_id, attempt_token),
  )
  return effective_status


def _enqueue_research_signal_result(cur: Any, run_id: str, status: str, summary: dict[str, Any]) -> None:
  """仅为存在研究信号快照的回测写入结果回传队列。"""
  if not settings.RESEARCH_RESULT_REPORT_ENABLED:
    return
  cur.execute(
    """INSERT INTO research_signal_result_outbox (run_id, payload)
      SELECT snapshot.run_id, jsonb_build_object(
        'backtestInstanceId', %s::text,
        'backtestRunId', snapshot.run_id::text,
        'signalDatasetId', snapshot.dataset_id::text,
        'signalDatasetVersion', snapshot.dataset_version,
        'snapshotHash', snapshot.snapshot_hash,
        'status', %s::text,
        'performanceSummary', %s::jsonb
      )
      FROM research_signal_snapshot AS snapshot
      WHERE snapshot.run_id = %s
      ON CONFLICT (run_id) DO NOTHING""",
    (settings.BACKTEST_INSTANCE_ID, status, Jsonb(summary), run_id),
  )


def request_l2_backtest_cancel(run_id: str) -> bool:
  """兼容旧调用：仅在该运行属于 L2 队列时请求取消。"""
  return _request_backtest_cancel(run_id, "l2_backtest_job") is not None


def request_bar_backtest_cancel(run_id: str) -> bool:
  """兼容旧调用：仅在该运行属于 Bar 队列时请求取消。"""
  return _request_backtest_cancel(run_id, "bar_backtest_job") is not None


def request_backtest_cancel(run_id: str) -> str | None:
  """幂等取消任意回测，返回数据库真实状态。"""
  status = _request_backtest_cancel(run_id, "l2_backtest_job")
  return status if status is not None else _request_backtest_cancel(run_id, "bar_backtest_job")


def _request_backtest_cancel(run_id: str, table: str) -> str | None:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(f"""
        UPDATE {table}
        SET cancel_requested=true,
          status=CASE WHEN status='queued' THEN 'cancelled' WHEN status='running' THEN 'cancelling' ELSE status END,
          finished_at=CASE WHEN status='queued' THEN now() ELSE finished_at END,
          updated_at=now()
        WHERE run_id=%s
        RETURNING status
      """, (run_id,))
      row = cur.fetchone()
      if row is None:
        return None
      status = str(row["status"])
      cur.execute("""
        UPDATE backtest_run
        SET status=%s, finished_at=CASE WHEN %s='cancelled' THEN COALESCE(finished_at, now()) ELSE finished_at END
        WHERE id=%s AND status IN ('queued', 'running', 'cancelling')
      """, (status, status, run_id))
      return status


def get_backtest_scheduler_capacity() -> dict[str, Any]:
  """返回管理员可见的统一容量与当前占用。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT
          CASE WHEN capacity.override_expires_at > now() THEN capacity.override_limit END AS override_limit,
          CASE WHEN capacity.override_expires_at > now() THEN capacity.override_expires_at END AS override_expires_at,
          count(slot.run_id) AS active_count,
          count(slot.run_id) FILTER (WHERE slot.job_kind='bar') AS bar_active_count,
          count(slot.run_id) FILTER (WHERE slot.job_kind='l2') AS l2_active_count
        FROM backtest_scheduler_capacity capacity
        LEFT JOIN backtest_execution_slot slot ON slot.lease_expires_at >= now()
        WHERE capacity.singleton
        GROUP BY capacity.override_limit, capacity.override_expires_at
      """)
      row = cur.fetchone() or {}
      override_limit = int(row["override_limit"]) if row.get("override_limit") else None
      return {
        "baseLimit": settings.BACKTEST_BASE_CONCURRENCY,
        "burstLimit": settings.BACKTEST_BURST_MAX_CONCURRENCY,
        "effectiveLimit": override_limit or settings.BACKTEST_BASE_CONCURRENCY,
        "overrideLimit": override_limit,
        "overrideExpiresAt": row.get("override_expires_at"),
        "activeCount": int(row.get("active_count", 0)),
        "barActiveCount": int(row.get("bar_active_count", 0)),
        "l2ActiveCount": int(row.get("l2_active_count", 0)),
      }


def set_backtest_scheduler_capacity(limit: int, ttl_seconds: int, reason: str) -> dict[str, Any]:
  """设置有时限的人工容量；到期只阻止新领取，不终止在途任务。"""
  if not 8 <= limit <= settings.BACKTEST_BURST_MAX_CONCURRENCY:
    raise ValueError(f"临时容量必须在 8 至 {settings.BACKTEST_BURST_MAX_CONCURRENCY} 之间")
  if not 60 <= ttl_seconds <= settings.BACKTEST_CAPACITY_OVERRIDE_MAX_TTL_SECONDS:
    raise ValueError("临时容量有效期超出部署允许范围")
  if not reason.strip():
    raise ValueError("临时扩容必须填写原因")
  actor_user_id = current_user_id()
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT pg_advisory_xact_lock(%s)", (_BACKTEST_SCHEDULER_LOCK_KEY,))
      cur.execute("SELECT override_limit FROM backtest_scheduler_capacity WHERE singleton FOR UPDATE")
      row = cur.fetchone() or {}
      snapshot = _scheduler_audit_snapshot(cur)
      cur.execute("""
        UPDATE backtest_scheduler_capacity
        SET override_limit=%s, override_expires_at=now() + make_interval(secs => %s),
          updated_by_user_id=%s, update_reason=%s, updated_at=now()
        WHERE singleton
        RETURNING override_expires_at
      """, (limit, ttl_seconds, actor_user_id, reason.strip()))
      expires_at = cur.fetchone()["override_expires_at"]
      cur.execute("""
        INSERT INTO backtest_scheduler_capacity_audit (
          actor_user_id, old_limit, new_limit, expires_at, reason, scheduler_snapshot
        ) VALUES (%s, %s, %s, %s, %s, %s)
      """, (
        actor_user_id, row.get("override_limit"), limit, expires_at, reason.strip(), Jsonb(snapshot),
      ))
  return get_backtest_scheduler_capacity()


def clear_backtest_scheduler_capacity(reason: str) -> dict[str, Any]:
  """提前结束人工扩容并记录审计。"""
  actor_user_id = current_user_id()
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT pg_advisory_xact_lock(%s)", (_BACKTEST_SCHEDULER_LOCK_KEY,))
      cur.execute("SELECT override_limit FROM backtest_scheduler_capacity WHERE singleton FOR UPDATE")
      row = cur.fetchone() or {}
      snapshot = _scheduler_audit_snapshot(cur)
      cur.execute("""
        UPDATE backtest_scheduler_capacity
        SET override_limit=NULL, override_expires_at=NULL, updated_by_user_id=%s,
          update_reason=%s, updated_at=now()
        WHERE singleton
      """, (actor_user_id, reason.strip()))
      cur.execute("""
        INSERT INTO backtest_scheduler_capacity_audit (
          actor_user_id, old_limit, new_limit, expires_at, reason, scheduler_snapshot
        ) VALUES (%s, %s, NULL, NULL, %s, %s)
      """, (actor_user_id, row.get("override_limit"), reason.strip(), Jsonb(snapshot)))
  return get_backtest_scheduler_capacity()


def _scheduler_audit_snapshot(cur: Any) -> dict[str, int]:
  """记录数据库可验证的扩容现场；主机资源由发布监控系统留存。"""
  cur.execute("""
    SELECT
      (SELECT count(*) FROM backtest_execution_slot WHERE lease_expires_at >= now()) AS active_count,
      (SELECT count(*) FROM bar_backtest_job WHERE status='queued')
        + (SELECT count(*) FROM l2_backtest_job WHERE status='queued') AS queued_count
  """)
  row = cur.fetchone() or {}
  return {"activeCount": int(row.get("active_count", 0)), "queuedCount": int(row.get("queued_count", 0))}


def _run_query(where: str = "") -> str:
  return f"""
    SELECT r.*, s.id AS strategy_id, c.name AS config_name,
      i.symbol || '.' || i.exchange AS symbol,
      b.symbol || '.' || b.exchange AS benchmark_symbol,
      COALESCE(bar_job.cancel_requested, l2_job.cancel_requested, false) AS cancel_requested,
      publication.id AS publication_id
    FROM backtest_run r
    LEFT JOIN strategy_config c ON c.id=r.config_id
    LEFT JOIN user_strategy_version sv ON sv.id=r.strategy_version_id
    LEFT JOIN user_strategy s ON s.id=sv.strategy_id
    LEFT JOIN market_instrument i ON i.id=r.instrument_id
    LEFT JOIN market_instrument b ON b.id=r.benchmark_instrument_id
    LEFT JOIN bar_backtest_job bar_job ON bar_job.run_id=r.id
    LEFT JOIN l2_backtest_job l2_job ON l2_job.run_id=r.id
    LEFT JOIN backtest_publication publication ON publication.source_run_id=r.id
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


def _backtest_run_filter_clause(
  query: str,
  symbol: str | None,
  config_id: str | None,
  favorites_only: bool,
) -> tuple[str, tuple[Any, ...]]:
  clauses: list[str] = []
  params: list[Any] = []
  if favorites_only:
    clauses.append("r.favorited_at IS NOT NULL")
  if config_id:
    clauses.append("r.config_id=%s")
    params.append(config_id)
  if symbol:
    clauses.append(
      "COALESCE(i.symbol || '.' || i.exchange, r.requested_params->>'symbol')=%s"
    )
    params.append(symbol)
  normalized_query = query.strip()
  if normalized_query:
    query_pattern = f"%{normalized_query}%"
    is_display_number = (
      normalized_query.startswith("#") and normalized_query[1:].isdigit()
    )
    display_predicate = (
      "r.display_no=%s" if is_display_number else "r.display_no::text ILIKE %s"
    )
    display_value: Any = (
      int(normalized_query[1:]) if is_display_number else query_pattern
    )
    clauses.append(f"""
      (r.id::text ILIKE %s OR {display_predicate}
        OR COALESCE(i.symbol || '.' || i.exchange, r.requested_params->>'symbol', '') ILIKE %s
        OR COALESCE(r.requested_params->>'strategy', '') ILIKE %s
        OR COALESCE(c.name, '') ILIKE %s OR COALESCE(r.tags::text, '') ILIKE %s)
    """)
    params.extend([
      query_pattern,
      display_value,
      query_pattern,
      query_pattern,
      query_pattern,
      query_pattern,
    ])
  clause = "" if not clauses else " AND " + " AND ".join(clauses)
  return clause, tuple(params)


def list_backtest_run_page(
  limit: int,
  offset: int,
  query: str = "",
  symbol: str | None = None,
  config_id: str | None = None,
  favorites_only: bool = False,
) -> dict[str, Any]:
  """返回当前用户的历史回测分页及分页所需的索引信息。"""
  if limit < 1 or limit > 100 or offset < 0:
    raise ValueError("分页参数无效")
  user_id = current_user_id()
  filter_clause, filter_params = _backtest_run_filter_clause(
    query, symbol, config_id, favorites_only,
  )
  base_params = (user_id, *filter_params)
  order_clause = (
    "r.favorited_at DESC, r.id DESC"
    if favorites_only
    else "r.created_at DESC, r.id DESC"
  )
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(f"""
        SELECT count(*) AS value
        FROM backtest_run r
        LEFT JOIN strategy_config c ON c.id=r.config_id
        LEFT JOIN market_instrument i ON i.id=r.instrument_id
        WHERE r.owner_user_id=%s {filter_clause}
      """, base_params)
      total = int(cur.fetchone()["value"])
      cur.execute(
        _run_query(
          f"{filter_clause} ORDER BY {order_clause} LIMIT %s OFFSET %s"
        ),
        (*base_params, limit, offset),
      )
      runs = [_normalize_run(row) for row in cur.fetchall()]
      symbol_config_clause = " AND r.config_id=%s" if config_id else ""
      symbol_params = (user_id, config_id) if config_id else (user_id,)
      cur.execute(f"""
        SELECT DISTINCT COALESCE(
          i.symbol || '.' || i.exchange, r.requested_params->>'symbol'
        ) AS symbol
        FROM backtest_run r
        LEFT JOIN market_instrument i ON i.id=r.instrument_id
        WHERE r.owner_user_id=%s {symbol_config_clause}
        ORDER BY symbol
      """, symbol_params)
      symbols = [str(row["symbol"]) for row in cur.fetchall() if row.get("symbol")]
      return {
        "runs": runs,
        "total": total,
        "limit": limit,
        "offset": offset,
        "symbols": symbols,
      }


def get_backtest_run(run_id: str) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(_run_query("AND r.id=%s"), (current_user_id(), run_id))
      row = cur.fetchone()
      if not row:
        raise KeyError(run_id)
      return _normalize_run(row)


_SHARED_RESULT_KEYS = (
  "strategyCategory", "resultSchemaVersion", "strategyMetrics", "summary",
  "equity", "dailyHoldings", "cycles", "executionPlans", "benchmarkSymbol",
  "benchmarkEquity", "benchmarkReturn", "excessReturn", "trades",
  "skippedSignals", "experimentManifest", "dataSnapshot", "warnings",
)
_SHARED_DETAIL_RESULT_KEYS = (
  "strategyCategory", "resultSchemaVersion", "strategyMetrics", "summary",
  "benchmarkSymbol", "benchmarkReturn", "excessReturn", "experimentManifest",
  "warnings",
)
_SHARED_CONFIGURATION_KEYS = (
  "initialCapital", "commissionRate", "slippage", "feeProfile", "allowLong",
  "allowShort", "holdingPolicy", "overlaySettlementPolicy", "fillModel",
  "executionAlgorithm", "executionParameters", "baseQuantity", "baseCostPrice",
  "initialPositionEnabled", "t0Enabled", "warmupTradingDays", "strategyParams",
)
_SHARED_DETAIL_STRATEGY_KEYS = (
  "name", "description", "entryType", "code", "codeHash", "signalRequired",
  "runtimeVersion", "capabilityDigest",
)
_SHARED_DETAIL_CONFIGURATION_KEYS = (
  "strategyName", "strategy", "strategyCategory", "symbol", "timeframe",
  "benchmarkSymbol", "startDate", "endDate", "params", "runParams", "execution",
  *_SHARED_CONFIGURATION_KEYS,
)
_SHARED_DETAIL_ORIGIN_KEYS = ("publicationId", "authorName", "title")
_SHARED_DETAIL_MARKET_DATA_KEYS = (
  "provider", "symbol", "timeframe", "sourcePeriod", "derivation",
  "warmupTradingDays", "barCount", "lastImportedAt", "adjustment",
  "manifestHash", "hash",
)
_SHARED_DETAIL_RESULT_DATA_KEYS = (
  "startDate", "endDate", "actualStartDate", "actualEndDate", "barCount",
  "sourcePeriod", "aggregationVersion", "mappingVersion", "quality",
)
_SHARED_DETAIL_DEPENDENCY_KEYS = (
  "symbol", "sourceTimeframe", "timeframe", "startDate", "endDate",
  "rowCount", "snapshotHash", "signalOrder", "alignment",
)
_SHARED_DETAIL_MANIFEST_KEYS = (
  "experimentHash", "engineVersion", "strategyRuntimeVersion",
  "strategyRuntimeCapabilityDigest", "signalModel", "fillModel",
  "executionAlgorithm", "executionParameters", "randomSeed", "strategyCodeHash",
  "configHash", "dataHash", "barCount", "barRange", "costModel",
  "holdingPolicy", "overlaySettlementPolicy", "strategyCategory",
  "initialPositionEnabled", "baseQuantity", "baseCostPrice", "t0Enabled",
  "resultSchemaVersion", "executionEnvironment",
)
_SHARED_RISK_NOTICE = "历史回测结果不代表未来表现"


def _shared_mapping_projection(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
  return shared_backtest_snapshot.project_fields(value, keys)


def _shared_data_detail_projection(value: Any) -> dict[str, Any]:
  projected = shared_backtest_snapshot.project_data(value)
  market_data = projected["marketData"]
  raw_value = value if isinstance(value, dict) else {}
  raw_market_data = raw_value.get("marketData")
  if isinstance(raw_market_data, dict):
    dates = raw_market_data.get("formalDates")
    if isinstance(dates, list) and dates:
      market_data["formalDateRange"] = {
        "start": dates[0], "end": dates[-1], "count": len(dates),
      }
  return projected


def _shared_result_detail_projection(value: Any) -> dict[str, Any]:
  return shared_backtest_snapshot.project_result(value, detail=True)


def _shared_artifact_sources(
  cur: Any,
  run_id: str,
  snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
  """读取原任务附件，并补齐复现研究信号所需的不可变元数据。"""
  cur.execute("""
    SELECT artifact.*, signal.dataset_id, signal.dataset_version,
      signal.snapshot_hash, signal.manifest, signal.source_partitions
    FROM artifact_file artifact
    LEFT JOIN research_signal_snapshot signal
      ON signal.artifact_file_id=artifact.id AND signal.run_id=artifact.run_id
    WHERE artifact.run_id=%s
    ORDER BY artifact.created_at, artifact.id
  """, (run_id,))
  rows = list(cur.fetchall())
  dependencies = snapshot.get("data", {}).get("dependencies", [])
  research_summary = next((
    item.get("summary", {}) for item in dependencies
    if isinstance(item, dict) and item.get("type") == "research_signal"
  ), {})
  research_rows = [
    row for row in rows
    if row.get("artifact_type") == "research_signal" and row.get("dataset_id")
  ]
  if research_summary and len(research_rows) != 1:
    raise ValueError("历史任务的研究信号快照不完整，请重新运行后共享")
  sources: list[dict[str, Any]] = []
  for row in rows:
    metadata: dict[str, Any] = {}
    if row.get("dataset_id"):
      metadata = shared_backtest_snapshot.project_reproduction({
        **research_summary,
        "datasetId": _uuid(row["dataset_id"]),
        "datasetVersion": int(row["dataset_version"]),
        "snapshotHash": row["snapshot_hash"],
        "manifest": _json(row.get("manifest"), {}),
        "sourcePartitions": _json(row.get("source_partitions"), []),
      })
    sources.append({
      **row,
      "source_artifact_file_id": _uuid(row["id"]),
      "reproduction_metadata": metadata,
    })
  return sources


def _shared_artifact_metadata(row: dict[str, Any], include_download: bool = False) -> dict[str, Any]:
  artifact_id = _uuid(row["id"]) or ""
  result = {
    "id": artifact_id,
    "type": row["artifact_type"],
    "displayName": row.get("display_name") or row.get("original_filename") or "附件",
    "contentType": row.get("content_type"),
    "byteSize": int(row.get("byte_size") or 0),
    "contentSha256": row["content_sha256"],
    "snapshotSchemaVersion": int(row.get("snapshot_schema_version") or 1),
    "reproduction": shared_backtest_snapshot.project_reproduction(
      _json(row.get("reproduction_metadata"), {}),
    ),
  }
  if include_download:
    result["downloadUrl"] = (
      f"/api/shared-backtests/{_uuid(row['publication_id'])}/artifacts/{artifact_id}"
    )
  return result


def _insert_shared_artifacts(cur: Any, artifacts: list[dict[str, Any]], publication_id: str) -> None:
  for artifact in artifacts:
    cur.execute("""
      INSERT INTO backtest_publication_artifact (
        id, publication_id, artifact_type, display_name, content_type,
        byte_size, content_sha256, storage_key, source_artifact_file_id,
        snapshot_schema_version, reproduction_metadata
      ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
    """, (
      artifact["id"], publication_id, artifact["artifact_type"],
      artifact["original_filename"], artifact.get("content_type"),
      artifact["byte_size"], artifact["content_sha256"], artifact["storage_key"],
      artifact.get("source_artifact_file_id"),
      Jsonb(artifact.get("reproduction_metadata") or {}),
    ))


def _publication_artifact_rows(cur: Any, publication_id: str) -> list[dict[str, Any]]:
  cur.execute("""
    SELECT * FROM backtest_publication_artifact
    WHERE publication_id=%s ORDER BY artifact_type, id
  """, (publication_id,))
  return list(cur.fetchall())


def _shared_strategy_snapshot(run: dict[str, Any]) -> dict[str, Any]:
  return shared_backtest_snapshot.build_snapshot(run)["strategy"]


def _shared_configuration_snapshot(run: dict[str, Any]) -> dict[str, Any]:
  return shared_backtest_snapshot.build_snapshot(run)["configuration"]


def _build_shared_backtest_snapshot(run: dict[str, Any]) -> dict[str, Any]:
  """从保存证据构造版本 1 业务白名单快照。"""
  return shared_backtest_snapshot.build_snapshot(run)


def _shared_snapshot_digest(snapshot: dict[str, Any]) -> str:
  canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _publication_list_item(row: dict[str, Any]) -> dict[str, Any]:
  return {
    "id": _uuid(row["id"]),
    "title": row["title"],
    "authorName": row["author_name_snapshot"],
    "tags": _json(row.get("tags"), []),
    "symbol": row["symbol"],
    "timeframe": row["timeframe"],
    "strategyCategory": row["strategy_category"],
    "startDate": row["start_date"],
    "endDate": row["end_date"],
    "totalReturn": _to_float(row.get("total_return")),
    "maxDrawdownRate": _to_float(row.get("max_drawdown_rate")),
    "annualSharpe": _to_float(row.get("annual_sharpe")),
    "copyCount": int(row.get("copy_count") or 0),
    "firstPublishedAt": _format_time(row["first_published_at"]),
    "sourcePublicationId": _uuid(row.get("source_publication_id")),
    "sourceAuthorName": row.get("source_author_name_snapshot"),
    "sourceTitle": row.get("source_title_snapshot"),
  }


def _publication_detail(
  row: dict[str, Any],
  artifacts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
  version = int(row["snapshot_schema_version"])
  if version != 1:
    raise ValueError(f"暂不支持共享快照版本: {version}")
  snapshot = _json(row["snapshot_json"], {})
  if not isinstance(snapshot, dict):
    raise ValueError("共享快照格式无效")
  result = snapshot.get("result", {})
  if not isinstance(result, dict):
    result = {}
  configuration = _shared_mapping_projection(
    snapshot.get("configuration"), _SHARED_DETAIL_CONFIGURATION_KEYS,
  )
  for key in (
    "params", "runParams", "execution", "strategyParams",
    "executionParameters", "feeProfile",
  ):
    if key in configuration:
      configuration[key] = shared_backtest_snapshot.project_author_json(
        configuration[key],
      )
  return {
    **_publication_list_item(row),
    "description": row["description"],
    "snapshotSchemaVersion": version,
    "snapshotDigest": row["snapshot_digest"],
    "strategy": _shared_mapping_projection(
      snapshot.get("strategy"), _SHARED_DETAIL_STRATEGY_KEYS,
    ),
    "configuration": configuration,
    "data": _shared_data_detail_projection(snapshot.get("data")),
    "result": _shared_result_detail_projection(result),
    "artifacts": [
      _shared_artifact_metadata(artifact, include_download=True)
      for artifact in artifacts or []
    ],
    "origin": _shared_mapping_projection(
      snapshot.get("origin"), _SHARED_DETAIL_ORIGIN_KEYS,
    ),
    "riskNotice": _SHARED_RISK_NOTICE,
  }


def _copy_publication_artifacts(
  cur: Any,
  run_id: str,
  publication_id: str,
  snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
  sources = _shared_artifact_sources(cur, run_id, snapshot)
  artifacts = shared_backtest_artifacts.copy_artifacts(
    sources, f"shared_backtests/{publication_id}",
  )
  try:
    for artifact in artifacts:
      artifact["publication_id"] = publication_id
      artifact["display_name"] = artifact["original_filename"]
      artifact["snapshot_schema_version"] = 1
      if artifact["artifact_type"] != "research_signal":
        continue
      expected_hash = artifact.get("reproduction_metadata", {}).get("snapshotHash")
      actual_hash = shared_backtest_artifacts.research_signal_snapshot_hash(
        artifact["storage_key"],
      )
      if expected_hash != actual_hash:
        raise ValueError("历史任务的研究信号内容摘要不一致，无法共享")
    return artifacts
  except Exception:
    shared_backtest_artifacts.cleanup_artifacts(artifacts)
    raise


def _insert_publication_record(
  cur: Any,
  publication_id: str,
  user_id: str,
  run_id: str,
  title: str,
  description: str,
  tags: list[str],
  snapshot: dict[str, Any],
) -> dict[str, Any] | None:
  summary = snapshot["result"].get("summary", {})
  configuration = snapshot["configuration"]
  origin = snapshot.get("origin") or {}
  cur.execute("""
    INSERT INTO backtest_publication (
      id, owner_user_id, author_name_snapshot, source_run_id, title, description,
      tags, status, snapshot_schema_version, snapshot_json, snapshot_digest,
      symbol, timeframe, strategy_category, start_date, end_date,
      total_return, max_drawdown_rate, annual_sharpe,
      source_publication_id, source_author_name_snapshot, source_title_snapshot
    ) VALUES (
      %s, %s, %s, %s, %s, %s, %s, 'active', 1, %s, %s,
      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (source_run_id) WHERE source_run_id IS NOT NULL DO NOTHING
    RETURNING *
  """, (
    publication_id, user_id, get_current_user().display_name, run_id, title,
    description, Jsonb(tags), Jsonb(snapshot), _shared_snapshot_digest(snapshot),
    configuration["symbol"], configuration["timeframe"],
    configuration["strategyCategory"], configuration["startDate"],
    configuration["endDate"], summary.get("totalReturn"),
    summary.get("maxDrawdownRate"), summary.get("annualSharpe"),
    origin.get("publicationId"), origin.get("authorName"), origin.get("title"),
  ))
  return cur.fetchone()


def _publication_for_source(
  cur: Any,
  run_id: str,
  user_id: str | None = None,
) -> dict[str, Any] | None:
  owner_filter = " AND owner_user_id=%s" if user_id is not None else ""
  params = (run_id, user_id) if user_id is not None else (run_id,)
  cur.execute(
    f"SELECT * FROM backtest_publication WHERE source_run_id=%s{owner_filter}",
    params,
  )
  return cur.fetchone()


def create_shared_backtest_publication(
  run_id: str,
  title: str,
  description: str,
  tags: list[str],
) -> tuple[dict[str, Any], bool]:
  """幂等地将当前用户的一次完整成功回测冻结为共享回测。"""
  user_id = current_user_id()
  publication_id = str(uuid4())
  copied_artifacts: list[dict[str, Any]] = []
  try:
    with _db() as conn:
      with conn.cursor() as cur:
        existing = _publication_for_source(cur, run_id, user_id)
        if existing:
          artifacts = _publication_artifact_rows(cur, str(existing["id"]))
          return _publication_detail(existing, artifacts), False
        cur.execute(_run_query("AND r.id=%s FOR UPDATE OF r"), (user_id, run_id))
        row = cur.fetchone()
        if not row:
          raise KeyError(run_id)
        run = _normalize_run(row)
        snapshot = _build_shared_backtest_snapshot(run)
        copied_artifacts = _copy_publication_artifacts(
          cur, run_id, publication_id, snapshot,
        )
        snapshot["data"]["dependencies"] = [
          _shared_artifact_metadata(artifact) for artifact in copied_artifacts
        ]
        publication = _insert_publication_record(
          cur, publication_id, user_id, run_id, title, description, tags, snapshot,
        )
        if publication is None:
          shared_backtest_artifacts.cleanup_artifacts(copied_artifacts)
          copied_artifacts = []
          publication = _publication_for_source(cur, run_id)
          if publication is None:
            raise DatabaseError("共享回测创建失败")
          artifacts = _publication_artifact_rows(cur, str(publication["id"]))
          return _publication_detail(publication, artifacts), False
        _insert_shared_artifacts(cur, copied_artifacts, publication_id)
        return _publication_detail(publication, copied_artifacts), True
  except Exception:
    shared_backtest_artifacts.cleanup_artifacts(copied_artifacts)
    raise


def list_shared_backtests(limit: int = 20, offset: int = 0) -> dict[str, Any]:
  """返回当前正常登录用户可见的已上架共享回测。"""
  if limit < 1 or limit > 100 or offset < 0:
    raise ValueError("分页参数无效")
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("SELECT count(*) AS value FROM backtest_publication WHERE status='active'")
      total = int(cur.fetchone()["value"])
      cur.execute("""
        SELECT * FROM backtest_publication WHERE status='active'
        ORDER BY first_published_at DESC, id DESC LIMIT %s OFFSET %s
      """, (limit, offset))
      return {
        "items": [_publication_list_item(row) for row in cur.fetchall()],
        "total": total,
        "limit": limit,
        "offset": offset,
      }


def get_shared_backtest(publication_id: str) -> dict[str, Any]:
  """读取 active 共享回测的安全详情投影。"""
  viewer_id = current_user_id()
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT p.*, favorite.user_id IS NOT NULL AS is_favorited,
          copied.recipient_user_id IS NOT NULL AS has_copied
        FROM backtest_publication p
        LEFT JOIN backtest_publication_favorite favorite
          ON favorite.publication_id=p.id AND favorite.user_id=%s
        LEFT JOIN backtest_publication_copy copied
          ON copied.publication_id=p.id AND copied.recipient_user_id=%s
        WHERE p.id=%s AND p.status='active'
      """, (viewer_id, viewer_id, publication_id))
      row = cur.fetchone()
      if not row:
        raise KeyError(publication_id)
      return {
        **_publication_detail(row, _publication_artifact_rows(cur, publication_id)),
        "isFavorited": bool(row.get("is_favorited")),
        "hasCopied": bool(row.get("has_copied")),
      }


def get_shared_backtest_artifact(publication_id: str, artifact_id: str) -> dict[str, Any]:
  """读取当前仍已上架共享附件的内部定位信息。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT artifact.*
        FROM backtest_publication_artifact artifact
        JOIN backtest_publication publication ON publication.id=artifact.publication_id
        WHERE artifact.id=%s AND artifact.publication_id=%s
          AND publication.status='active'
      """, (artifact_id, publication_id))
      row = cur.fetchone()
      if not row:
        raise KeyError(artifact_id)
      return row


def list_imported_research_signal_datasets() -> list[dict[str, Any]]:
  """列出当前用户从共享回测导入的可运行研究信号数据集。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT dataset.*
        FROM imported_research_signal_dataset dataset
        WHERE dataset.owner_user_id=%s
        ORDER BY dataset.created_at DESC, dataset.id DESC
      """, (current_user_id(),))
      return [{
        "id": _uuid(row["id"]),
        "name": row["display_name"],
        "datasetKey": row["dataset_key"],
        "version": int(row["dataset_version"]),
        "timeframe": row["timeframe"],
        "recordCount": int(row["row_count"]),
        "imported": True,
      } for row in cur.fetchall()]


def get_imported_research_signal_dataset(dataset_id: str) -> dict[str, Any] | None:
  """返回当前用户拥有的导入数据集；不允许沿用原作者数据身份。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT dataset.*, artifact.storage_key, artifact.byte_size,
          artifact.content_sha256
        FROM imported_research_signal_dataset dataset
        JOIN artifact_file artifact ON artifact.id=dataset.artifact_file_id
        WHERE dataset.id=%s AND dataset.owner_user_id=%s
      """, (dataset_id, current_user_id()))
      row = cur.fetchone()
      return dict(row) if row else None


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


def list_strategy_runtime_error_stats(
  from_time: str | None = None,
  to_time: str | None = None,
  limit: int = 100,
) -> list[dict[str, Any]]:
  """按日聚合独立脱敏的平台运行时错误，不返回任务或用户信息。"""
  clauses: list[str] = []
  params: list[Any] = []
  for value, clause in (
    (from_time, "occurred_at >= %s::timestamptz"),
    (to_time, "occurred_at <= %s::timestamptz"),
  ):
    if value:
      clauses.append(clause)
      params.append(value)
  where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
  params.append(limit)
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(f"""
        SELECT occurred_at::date AS error_date, error_code, runtime_version,
          missing_name, missing_module, count(*) AS occurrence_count,
          max(occurred_at) AS last_occurred_at
        FROM strategy_runtime_error_stat
        {where}
        GROUP BY occurred_at::date, error_code, runtime_version, missing_name, missing_module
        ORDER BY last_occurred_at DESC
        LIMIT %s
      """, params)
      return [{
        "date": row["error_date"].isoformat(),
        "code": row["error_code"],
        "runtimeVersion": row["runtime_version"],
        "missingName": row["missing_name"],
        "missingModule": row["missing_module"],
        "count": int(row["occurrence_count"]),
        "lastOccurredAt": _format_time(row["last_occurred_at"]),
      } for row in cur.fetchall()]


def prune_strategy_runtime_error_stats() -> int:
  """在服务启动时按私有部署配置清理过期脱敏统计。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute(
        "DELETE FROM strategy_runtime_error_stat "
        "WHERE occurred_at < now() - (%s * interval '1 day')",
        (settings.STRATEGY_RUNTIME_ERROR_RETENTION_DAYS,),
      )
      return cur.rowcount


def update_backtest_run_meta(run_id: str, note: str, tags: list[str]) -> dict[str, Any]:
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("UPDATE backtest_run SET note=%s, tags=%s WHERE id=%s AND owner_user_id=%s", (note, Jsonb(tags), run_id, current_user_id()))
      if cur.rowcount == 0:
        raise KeyError(run_id)
  return get_backtest_run(run_id)


def set_backtest_run_favorite(run_id: str, favorited: bool) -> dict[str, Any]:
  """幂等设置当前用户历史回测的收藏状态。"""
  with _db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        UPDATE backtest_run
        SET favorited_at=CASE
          WHEN %s THEN COALESCE(favorited_at, now())
          ELSE NULL
        END
        WHERE id=%s AND owner_user_id=%s
        RETURNING favorited_at
      """, (favorited, run_id, current_user_id()))
      row = cur.fetchone()
      if row is None:
        raise KeyError(run_id)
      return {
        "runId": run_id,
        "isFavorited": row["favorited_at"] is not None,
        "favoritedAt": (
          _format_time(row["favorited_at"])
          if row.get("favorited_at") is not None
          else None
        ),
      }


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
  _insert_strategy_runtime_error_stat(cur, record)


def _insert_strategy_runtime_error_stat(cur: Any, record: dict[str, Any]) -> None:
  """只为平台运行时缺陷留存不可反查用户的最小统计。"""
  if record.get("code") not in {
    "runtime.contract_missing",
    "runtime.version_mismatch",
    "runtime.dependency_missing",
    "runtime.dependency_load_failed",
  }:
    return
  context = record.get("context") if isinstance(record.get("context"), dict) else {}
  runtime_version = str(context.get("runtimeVersion") or "legacy")[:64]
  missing_value = context.get("missingName")
  missing = missing_value if isinstance(missing_value, str) and is_standard_builtin_name(missing_value) else None
  supported_package = context.get("supportedPackage")
  module_value = context.get("missingModule")
  missing_module = _safe_supported_missing_module(module_value, supported_package)
  cur.execute("""
    INSERT INTO strategy_runtime_error_stat (
      error_code, runtime_version, missing_name, missing_module
    ) VALUES (%s, %s, %s, %s)
  """, (record["code"], runtime_version, missing, missing_module))


def _safe_supported_missing_module(
  module_value: Any,
  supported_package: Any,
) -> str | None:
  """仅保留平台能力清单可证明归属的短模块名。"""
  if (
    not isinstance(module_value, str)
    or len(module_value) > 128
    or not is_safe_module_name(module_value)
  ):
    return None
  top_level = module_value.split(".", 1)[0]
  if supported_package == "python-stdlib":
    return top_level if top_level in SUPPORTED_STANDARD_LIBRARY_MODULES else None
  if supported_package not in SUPPORTED_QUANT_PACKAGE_VERSIONS:
    return None
  promised_imports = {
    import_name
    for import_name, distribution in SUPPORTED_QUANT_IMPORTS.items()
    if distribution == supported_package
  }
  return top_level if top_level in promised_imports else None


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
