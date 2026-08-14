"""交易日格式的唯一解析与边界转换入口。"""

from __future__ import annotations

from datetime import date


class TradingDateError(ValueError):
  """交易日格式或范围无效。"""


def parse_trading_date(value: str | date) -> date:
  """兼容 ISO 与旧紧凑格式输入，返回日期对象。"""
  if isinstance(value, date):
    return value
  if not isinstance(value, str):
    raise TradingDateError("交易日必须为 YYYY-MM-DD 或 YYYYMMDD")
  normalized = value.strip()
  try:
    if len(normalized) == 8 and normalized.isdigit():
      return date.fromisoformat(f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}")
    return date.fromisoformat(normalized)
  except ValueError as error:
    raise TradingDateError("交易日必须为 YYYY-MM-DD 或 YYYYMMDD") from error


def format_api_date(value: str | date) -> str:
  """返回服务接口唯一使用的 ISO 交易日字符串。"""
  return parse_trading_date(value).isoformat()


def format_legacy_date(value: str | date) -> str:
  """仅供旧文件名、CSV 或旧外部引擎使用紧凑交易日。"""
  return parse_trading_date(value).strftime("%Y%m%d")


def normalize_date_range(start_date: str | date, end_date: str | date) -> tuple[str, str]:
  """解析并校验日期区间，返回 ISO 格式起止日期。"""
  start = parse_trading_date(start_date)
  end = parse_trading_date(end_date)
  if start > end:
    raise TradingDateError("开始日期不能晚于结束日期")
  return start.isoformat(), end.isoformat()
