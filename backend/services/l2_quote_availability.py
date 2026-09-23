"""L2 十档日级可用性摘要：在线请求只读取 PostgreSQL 摘要。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.l2.config import has_profile, load_profile, load_temporary_quote_profile
from backend.l2.models import SourceMappingProfile
from backend.l2.providers import ClickHouseL2Adapter, L2DataCoverageError
from backend.services import state_store


def available_quote_dates(symbol: str, trade_dates: list[str]) -> list[str]:
  """返回摘要中已标记完整的十档快照交易日，不查询或回填原始行情。"""
  dates = sorted(set(trade_dates))
  if not dates:
    return []
  profile = load_profile("quote") if has_profile("quote") else load_temporary_quote_profile()
  source_key = _source_key(profile)
  try:
    cached = state_store.list_l2_quote_daily_availability(source_key, symbol, dates)
  except state_store.DatabaseError:
    return []
  return [
    item for item in dates
    if item in cached and cached[item].get("status") == "complete"
  ]


def status(symbol: str, timeframe: str) -> dict[str, Any]:
  """返回已有 L2 日级摘要声明的可回测范围，不查询或回填原始行情。"""
  profile = load_profile("quote") if has_profile("quote") else load_temporary_quote_profile()
  summary = state_store.l2_quote_availability_range(_source_key(profile), symbol)
  first_date = summary["firstDate"]
  last_date = summary["lastDate"]
  return {
    "symbol": symbol,
    "timeframe": timeframe,
    "path": "postgresql://l2_quote_daily_availability",
    "exists": bool(first_date and last_date),
    "firstDate": first_date,
    "lastDate": last_date,
    "barCount": summary["snapshotCount"],
    "fileSize": 0,
    "mtime": summary["checkedAt"] or None,
    "duplicateCount": 0,
    "nonTradingCount": 0,
    "expectedBarsPerDay": None,
    "gapDates": [],
    "preview": [],
  }


def require_summarized_quote_dates(symbol: str, trade_dates: list[str]) -> None:
  """Worker 执行期只信任日级摘要，不触发原始 Quote 扫描或回填。"""
  dates = sorted(set(trade_dates))
  if not dates:
    return
  profile = load_profile("quote") if has_profile("quote") else load_temporary_quote_profile()
  try:
    cached = state_store.list_l2_quote_daily_availability(
      _source_key(profile), symbol, dates,
    )
  except state_store.DatabaseError as error:
    raise L2DataCoverageError(symbol, dates) from error
  missing_dates = [
    item for item in dates
    if item not in cached or cached[item].get("status") != "complete"
  ]
  if missing_dates:
    raise L2DataCoverageError(symbol, missing_dates)


def rebuild_quote_availability(symbol: str, trade_dates: list[str]) -> dict[str, int]:
  """强制重建指定标的、日期范围的摘要，供数据运维任务调用。"""
  dates = sorted(set(trade_dates))
  if not dates:
    return {"checked": 0, "available": 0}
  profile = load_profile("quote") if has_profile("quote") else load_temporary_quote_profile()
  measured = ClickHouseL2Adapter(profile).quote_daily_availability(symbol, dates)
  records = [
    {
      "tradeDate": item,
      "status": "complete" if item in measured else "missing",
      "snapshotCount": int(measured.get(item, {}).get("snapshotCount") or 0),
      "firstQuoteTime": measured.get(item, {}).get("firstQuoteTime") or "",
      "lastQuoteTime": measured.get(item, {}).get("lastQuoteTime") or "",
    }
    for item in dates
  ]
  state_store.upsert_l2_quote_daily_availability(_source_key(profile), symbol, records)
  return {"checked": len(records), "available": len(measured)}


def _source_key(profile: SourceMappingProfile) -> str:
  identity: dict[str, Any] = {
    "sourceName": profile.source_name,
    "tableName": profile.table_name,
    "columns": profile.columns,
    "sourceVersion": profile.source_version,
  }
  serialized = json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
  return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
