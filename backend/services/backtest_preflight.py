"""兼容旧客户端的非阻断回测预检接口。"""

from __future__ import annotations

from typing import Any

from backend import settings


def preview(
  symbol: str,
  start_date: str,
  end_date: str,
  fill_model: str,
  timeframe: str | None = None,
  warmup_trading_days: int = 0,
) -> dict[str, Any]:
  """兼容旧客户端；不再执行运行前行情完整性查询。"""
  items = [{
    "timeframe": item,
    "available": True,
    "missingDataTypes": [],
    "missingDates": [],
    "barDates": [],
    "availableWarmupDays": 0,
    "sourcePeriod": None,
    "derivation": None,
    "reason": "回测将在执行时读取行情",
    "message": "回测将在执行时读取行情",
  } for item in settings.BAR_RUN_TIMEFRAMES]
  return {
    "symbol": symbol, "startDate": start_date, "endDate": end_date, "fillModel": fill_model,
    "timeframes": items, "missingDates": [], "calendarDays": [],
  }


def require_available(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  fill_model: str,
  warmup_trading_days: int = 0,
) -> dict[str, Any]:
  result = preview(
    symbol, start_date, end_date, fill_model, timeframe, warmup_trading_days,
  )
  selected = next(
    (item for item in result["timeframes"] if item["timeframe"] == timeframe),
    result["timeframes"][0],
  )
  return {"preflight": selected, "barSnapshot": {}}
