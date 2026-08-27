"""L2 十档日级可用性摘要：预检优先读取 PostgreSQL 摘要，未命中时单次回填。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.l2.config import has_profile, load_profile, load_temporary_quote_profile
from backend.l2.models import SourceMappingProfile
from backend.l2.providers import ClickHouseL2Adapter
from backend.services import state_store


def available_quote_dates(symbol: str, trade_dates: list[str]) -> list[str]:
  """返回已有有效十档快照的交易日，并将首次查询结果持久化为摘要。"""
  dates = sorted(set(trade_dates))
  if not dates:
    return []
  profile = load_profile("quote") if has_profile("quote") else load_temporary_quote_profile()
  source_key = _source_key(profile)
  adapter = ClickHouseL2Adapter(profile)
  try:
    cached = state_store.list_l2_quote_daily_availability(source_key, symbol, dates)
  except state_store.DatabaseError:
    return sorted(adapter.quote_daily_availability(symbol, dates))
  uncached_dates = [item for item in dates if item not in cached]
  if uncached_dates:
    measured = adapter.quote_daily_availability(symbol, uncached_dates)
    records = [
      {
        "tradeDate": item,
        "status": "complete" if item in measured else "missing",
        "snapshotCount": int(measured.get(item, {}).get("snapshotCount") or 0),
        "firstQuoteTime": measured.get(item, {}).get("firstQuoteTime") or "",
        "lastQuoteTime": measured.get(item, {}).get("lastQuoteTime") or "",
      }
      for item in uncached_dates
    ]
    try:
      state_store.upsert_l2_quote_daily_availability(source_key, symbol, records)
    except state_store.DatabaseError:
      pass
    cached = {**cached, **{item["tradeDate"]: item for item in records}}
  return [item for item in dates if cached[item]["status"] == "complete"]


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
