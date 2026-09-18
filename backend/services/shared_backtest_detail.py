# -*- coding: utf-8 -*-
"""共享回测快照的版本化详情投影。"""

from typing import Any, Literal

from backend.services import shared_backtest_snapshot, state_store


SharedResultSection = Literal[
  "holdings", "trades", "signals", "execution-plans", "cycles",
]

_SECTION_KEYS: dict[SharedResultSection, str] = {
  "holdings": "dailyHoldings",
  "trades": "trades",
  "signals": "skippedSignals",
  "execution-plans": "executionPlans",
  "cycles": "cycles",
}
_ITEM_FIELDS: dict[SharedResultSection, tuple[str, ...]] = {
  "holdings": (
    "date", "close", "longQty", "shortQty", "cash", "longMarketValue",
    "shortMarketValue", "marketValue", "totalEquity", "dailyPnl",
    "targetBaseQuantity", "actualQuantity", "overlayQuantity",
    "sellableQuantity", "dailyAccountPnl", "dailyBaseBenchmarkPnl",
    "dailyOverlayPnl", "dailyRealizedOverlayPnl", "unrealizedOverlayPnl",
  ),
  "trades": (
    "time", "signalTime", "executionTime", "parentOrderId", "childIndex",
    "executionAlgorithm", "priceSource", "fundingLimited", "depthQuantity",
    "affordableQuantity", "remainingQuantity", "availableCashBefore",
    "action", "status", "direction", "side", "anchor", "fillId",
    "positionKey", "inventorySource", "cycleId", "cycleEffect",
    "overlayQuantityBefore", "overlayQuantityAfter", "accountRealizedPnl",
    "realizedOverlayPnl", "totalFee", "attributionSegments", "price",
    "quantity", "pnl", "note", "strategyModule", "researchSignalId",
    "researchSignalAction", "researchSignalEffectiveAt",
  ),
  "signals": (
    "time", "signalTime", "executionTime", "parentOrderId", "action",
    "status", "direction", "side", "anchor", "price", "quantity", "pnl",
    "note", "strategyModule", "researchSignalId", "researchSignalAction",
    "researchSignalEffectiveAt",
  ),
  "execution-plans": (
    "parentOrderId", "algorithm", "signalTime", "action", "requestedQuantity",
    "filledQuantity", "cancelledQuantity", "rejectedQuantity",
    "remainingQuantity", "status", "reason", "plannedChildCount",
  ),
  "cycles": (
    "cycleId", "symbol", "direction", "openedAt", "closedAt",
    "openedQuantity", "closedQuantity", "remainingQuantity", "realizedPnl",
    "unrealizedPnl", "netPnl", "fillIds", "status",
  ),
}
_EQUITY_FIELDS = ("date", "pnl", "value")
_BENCHMARK_FIELDS = ("date", "close", "value", "returnRate")


def _load_snapshot(publication_id: str) -> dict[str, Any]:
  """只读取 active 发布快照，未知版本必须显式失败。"""
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT snapshot_schema_version, snapshot_json
        FROM backtest_publication
        WHERE id=%s AND status='active'
      """, (publication_id,))
      row = cur.fetchone()
  if not row:
    raise KeyError(publication_id)
  version = int(row["snapshot_schema_version"])
  if version != 1:
    raise ValueError(f"暂不支持共享快照版本: {version}")
  snapshot = state_store._json(row["snapshot_json"], {})
  if not isinstance(snapshot, dict):
    raise ValueError("共享快照格式无效")
  return snapshot


def _project_record(value: Any, fields: tuple[str, ...]) -> dict[str, Any] | None:
  if not isinstance(value, dict):
    return None
  projected = shared_backtest_snapshot.project_fields(value, fields)
  if "attributionSegments" in projected:
    projected["attributionSegments"] = _project_records(
      projected["attributionSegments"], shared_backtest_snapshot.ATTRIBUTION_FIELDS,
    )
  return projected


def _project_records(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
  if not isinstance(value, list):
    return []
  return [
    record for item in value
    if (record := _project_record(item, fields)) is not None
  ]


def get_shared_backtest_chart(publication_id: str) -> dict[str, Any]:
  """读取收益与基准曲线专用投影，避免详情首屏携带大数组。"""
  snapshot = _load_snapshot(publication_id)
  result = snapshot.get("result")
  if not isinstance(result, dict):
    result = {}
  return {
    "equity": _project_records(result.get("equity"), _EQUITY_FIELDS),
    "benchmarkSymbol": result.get("benchmarkSymbol"),
    "benchmarkEquity": _project_records(
      result.get("benchmarkEquity"), _BENCHMARK_FIELDS,
    ),
  }


def get_shared_backtest_result_page(
  publication_id: str,
  section: SharedResultSection,
  cursor: int,
  limit: int,
) -> dict[str, Any]:
  """按固定业务字段分页读取共享结果明细。"""
  if section not in _SECTION_KEYS:
    raise ValueError("不支持的共享结果类型")
  if cursor < 0 or limit < 1 or limit > 200:
    raise ValueError("分页参数无效")
  snapshot = _load_snapshot(publication_id)
  result = snapshot.get("result")
  if not isinstance(result, dict):
    result = {}
  items = _project_records(result.get(_SECTION_KEYS[section]), _ITEM_FIELDS[section])
  page = items[cursor:cursor + limit]
  next_cursor = cursor + len(page)
  return {
    "items": page,
    "nextCursor": str(next_cursor) if next_cursor < len(items) else None,
    "total": len(items),
  }
