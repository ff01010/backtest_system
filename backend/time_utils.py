"""系统管理时间的统一显示格式。"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _as_aware_utc(value: datetime) -> datetime:
  """兼容历史无时区值；系统管理时间的旧值一律按 UTC 解释。"""
  return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def format_shanghai_datetime(value: datetime | None) -> str:
  """将数据库时间转换为页面使用的中国标准时间。"""
  if value is None:
    return ""
  return _as_aware_utc(value).astimezone(SHANGHAI_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def format_shanghai_iso_datetime(value: datetime) -> str:
  """将系统生成时间返回为携带 UTC+8 偏移的 ISO 字符串。"""
  return _as_aware_utc(value).astimezone(SHANGHAI_TIMEZONE).isoformat(timespec="seconds")


def format_epoch_as_shanghai(timestamp: float) -> str:
  """将文件时间戳转换为页面使用的中国标准时间。"""
  return format_shanghai_datetime(datetime.fromtimestamp(timestamp, tz=timezone.utc))


def now_shanghai_iso_datetime() -> str:
  """返回当前中国标准时间，供展示型元数据使用。"""
  return datetime.now(SHANGHAI_TIMEZONE).isoformat(timespec="seconds")
