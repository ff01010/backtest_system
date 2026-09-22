"""回测保存与提交共用的数据预检。"""

from __future__ import annotations

from typing import Any
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from backend import settings
from backend.l2.providers import L2DataError
from backend.services import bar_data, l2_quote_availability, market_data, state_store


class PreflightError(ValueError):
  """回测依赖的数据未完整覆盖当前策略文件。"""

  def __init__(self, result: dict[str, Any]) -> None:
    self.result = result
    super().__init__(str(result.get("message") or "当前日期范围数据不完整。"))


def preview(
  symbol: str,
  start_date: str,
  end_date: str,
  fill_model: str,
  timeframe: str | None = None,
  warmup_trading_days: int = 0,
) -> dict[str, Any]:
  trade_dates, calendar_days, calendar_available = _calendar_days(start_date, end_date)
  future_dates = [item["date"] for item in calendar_days if item["status"] == "future"]
  quote_dates = _available_quote_dates(symbol, trade_dates) if fill_model == "l2_quote" else None
  items = [
    _preview_timeframe(
      symbol, item, start_date, end_date, trade_dates, fill_model, calendar_available,
      future_dates, quote_dates, warmup_trading_days,
    )
    for item in settings.BAR_RUN_TIMEFRAMES
  ]
  selected = next((item for item in items if item["timeframe"] == timeframe), items[0])
  missing_dates = set(selected["missingDates"])
  for item in calendar_days:
    if item["status"] == "future" or not item["tradingDay"]:
      continue
    if item["date"] in missing_dates:
      item["status"] = "incomplete"
      item["missingDataTypes"] = selected["missingDataTypes"]
    elif calendar_available:
      item["status"] = "available"
  return {
    "symbol": symbol, "startDate": start_date, "endDate": end_date, "fillModel": fill_model,
    "timeframes": items, "missingDates": selected["missingDates"], "calendarDays": calendar_days,
  }


def require_available(
  symbol: str,
  timeframe: str,
  start_date: str,
  end_date: str,
  fill_model: str,
  warmup_trading_days: int = 0,
) -> dict[str, Any]:
  trade_dates, calendar_days, calendar_available = _calendar_days(start_date, end_date)
  future_dates = [item["date"] for item in calendar_days if item["status"] == "future"]
  quote_dates = _available_quote_dates(symbol, trade_dates) if fill_model == "l2_quote" else None
  result = _preview_timeframe(
    symbol, timeframe, start_date, end_date, trade_dates, fill_model, calendar_available,
    future_dates, quote_dates, warmup_trading_days,
  )
  if not result["available"]:
    raise PreflightError(result)
  try:
    if fill_model == "l2_quote":
      bars, bar_snapshot = bar_data.load_normalized_bars_with_snapshot(symbol, timeframe, start_date, end_date)
      bar_dates = sorted({bar.trade_date for bar in bars})
      if not bar_dates:
        raise PreflightError(_failure(symbol, timeframe, start_date, end_date, "data.bar_missing"))
      _require_quotes(trade_dates, quote_dates or set())
    else:
      source = market_data.resolve_backtest_data(
        symbol, timeframe, start_date, end_date,
        trade_dates if calendar_available else None, warmup_trading_days,
      )
      bar_snapshot = {"provider": "qmt_kline", **market_data.snapshot_for_resolution(symbol, source)}
  except PreflightError:
    raise
  except ValueError as error:
    raise PreflightError(_failure(symbol, timeframe, start_date, end_date, "data.bar_missing")) from error
  except L2DataError as error:
    if error.code != "data.l2_bar_missing":
      raise
    raise PreflightError(_failure(symbol, timeframe, start_date, end_date, "data.bar_missing")) from error
  return {"preflight": result, "barSnapshot": bar_snapshot}


def _preview_timeframe(
  symbol: str, timeframe: str, start_date: str, end_date: str, trade_dates: list[str], fill_model: str,
  calendar_available: bool, future_dates: list[str], quote_dates: set[str] | None, warmup_trading_days: int,
) -> dict[str, Any]:
  bar_dates: list[str] = []
  dates: list[str] = []
  codes: list[str] = []
  if fill_model == "l2_quote" and not calendar_available:
    codes.append("data.calendar_unavailable")
  elif fill_model == "l2_quote" and not trade_dates:
    codes.append("data.no_trading_days")
  elif fill_model == "l2_quote":
    bar_dates = sorted(quote_dates or set())
    dates = sorted(set(trade_dates) - set(bar_dates))
    if dates:
      codes.extend(["data.bar_missing", "data.l2_quote_missing"])
  else:
    data = market_data.preview_backtest_data(
      symbol, timeframe, start_date, end_date, trade_dates if calendar_available else None, warmup_trading_days,
    )
    resolution = data.get("resolution")
    bar_dates = list(resolution.formal_dates) if resolution is not None else []
    dates = list(data["missingDates"])
    codes.extend(data["missingDataTypes"])
  if future_dates:
    dates.extend(future_dates)
    codes.append("data.future_date")
  return {
    "timeframe": timeframe, "available": not codes, "missingDataTypes": codes,
    "missingDates": sorted(set(dates)), "barDates": bar_dates,
    "availableWarmupDays": data.get("availableWarmupDays", 0) if fill_model != "l2_quote" else 0,
    "sourcePeriod": resolution.source_period if fill_model != "l2_quote" and resolution is not None else None,
    "derivation": resolution.derivation if fill_model != "l2_quote" and resolution is not None else None,
    "reason": data["reason"] if fill_model != "l2_quote" else _message(codes),
    "message": data["reason"] if fill_model != "l2_quote" else _message(codes),
  }


def _available_bar_dates(symbol: str, timeframe: str, trade_dates: list[str]) -> list[str]:
  return bar_data.derived_bar_available_dates(symbol, timeframe, trade_dates)


def _available_quote_dates(symbol: str, trade_dates: list[str]) -> set[str]:
  return set(l2_quote_availability.available_quote_dates(symbol, trade_dates))


def _calendar_days(start_date: str, end_date: str) -> tuple[list[str], list[dict[str, Any]], bool]:
  start = date.fromisoformat(start_date)
  end = date.fromisoformat(end_date)
  all_dates = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
  today = _shanghai_today()
  future_dates = {
    value.isoformat() for value in all_dates if value.weekday() < 5 and value > today
  }
  try:
    calendar_dates = set(state_store.list_open_trade_dates(start_date, end_date))
    if not state_store.is_trade_calendar_range_covered(start_date, end_date):
      return [], _unknown_calendar_days(all_dates, future_dates), False
  except state_store.DatabaseError:
    return [], _unknown_calendar_days(all_dates, future_dates), False
  trade_dates = sorted(value for value in calendar_dates if value not in future_dates)
  calendar_days = []
  for value in all_dates:
    text = value.isoformat()
    if text in future_dates:
      status = "future"
      trading_day = True
    elif text in calendar_dates:
      status = "available"
      trading_day = True
    else:
      status = "non_trading"
      trading_day = False
    calendar_days.append({
      "date": text, "tradingDay": trading_day, "status": status, "missingDataTypes": [],
    })
  return trade_dates, calendar_days, True


def _unknown_calendar_days(all_dates: list[date], future_dates: set[str]) -> list[dict[str, Any]]:
  return [{
    "date": value.isoformat(),
    "tradingDay": value.weekday() < 5,
    "status": "future" if value.isoformat() in future_dates else "unknown",
    "missingDataTypes": [],
  } for value in all_dates]


def _shanghai_today() -> date:
  return datetime.now(ZoneInfo("Asia/Shanghai")).date()



def _require_quotes(trade_dates: list[str], quote_dates: set[str]) -> None:
  missing_dates = sorted(set(trade_dates) - quote_dates)
  if missing_dates:
    result = _failure("", "", "", "", "data.l2_quote_missing")
    result["missingDates"] = missing_dates
    raise PreflightError(result)


def _failure(symbol: str, timeframe: str, start_date: str, end_date: str, code: str) -> dict[str, Any]:
  return {"symbol": symbol, "timeframe": timeframe, "startDate": start_date, "endDate": end_date, "available": False, "missingDataTypes": [code], "missingDates": [start_date] if start_date else [], "message": _message([code])}


def _message(codes: list[str]) -> str:
  if "data.future_date" in codes:
    return "回测区间包含未来交易日，无法保存完整策略。"
  if "data.calendar_unavailable" in codes:
    return "交易日历暂不可用，无法确认数据完整性。"
  if "data.no_trading_days" in codes:
    return "回测区间内没有有效交易日。"
  names = {"data.bar_missing": "Bar 数据", "data.l2_quote_missing": "L2 十档快照"}
  values = [names[code] for code in codes if code in names]
  return f"当前日期范围缺少{'、'.join(values)}，无法保存完整策略。" if values else "当前日期范围数据不完整。"
