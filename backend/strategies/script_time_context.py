"""自定义策略可见的 Bar 时间上下文适配。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class ScriptTimeContextError(ValueError):
  """Bar 时间不能转换为稳定的脚本上下文。"""


@dataclass(frozen=True)
class ScriptBarTimeContext:
  """自定义脚本使用的标准化 Bar 时间字段。"""

  time: str
  datetime: str
  trade_date: str
  hhmm: int | None


_FORMATS: tuple[tuple[str, bool, int], ...] = (
  ("%Y%m%d%H%M%S", True, 14),
  ("%Y%m%d%H%M", True, 12),
  ("%Y-%m-%d %H:%M:%S", True, 19),
  ("%Y-%m-%d %H:%M", True, 16),
  ("%Y%m%d", False, 8),
  ("%Y-%m-%d", False, 10),
)
_SUPPORTED_FORMATS = "YYYYMMDDHHMMSS、YYYYMMDDHHMM、YYYY-MM-DD HH:MM:SS[.fff]、YYYY-MM-DD HH:MM、YYYYMMDD、YYYY-MM-DD"


def normalize_script_bar_time(raw_time: str) -> ScriptBarTimeContext:
  """将任意已支持的 Bar 完成时刻转换为稳定的脚本字段。"""
  if not isinstance(raw_time, str) or not raw_time.strip():
    raise ScriptTimeContextError(f"Bar 时间无效: {raw_time!r}；支持格式: {_SUPPORTED_FORMATS}")
  value = raw_time.strip()
  if "." in value:
    base, fraction = value.rsplit(".", 1)
    if base.count(":") == 2 and fraction.isdigit() and 1 <= len(fraction) <= 6:
      try:
        parsed = datetime.strptime(f"{base}.{fraction.ljust(6, '0')}", "%Y-%m-%d %H:%M:%S.%f")
      except ValueError:
        pass
      else:
        return ScriptBarTimeContext(
          time=parsed.strftime("%Y%m%d%H%M%S"),
          datetime=parsed.strftime("%Y-%m-%d %H:%M:%S"),
          trade_date=parsed.strftime("%Y-%m-%d"),
          hhmm=int(parsed.strftime("%H%M")),
        )
  for format_value, is_intraday, length in _FORMATS:
    if len(value) != length:
      continue
    try:
      parsed = datetime.strptime(value, format_value)
    except ValueError:
      continue
    if not is_intraday:
      return ScriptBarTimeContext(
        time=parsed.strftime("%Y%m%d"),
        datetime=parsed.strftime("%Y-%m-%d"),
        trade_date=parsed.strftime("%Y-%m-%d"),
        hhmm=None,
      )
    return ScriptBarTimeContext(
      time=parsed.strftime("%Y%m%d%H%M%S"),
      datetime=parsed.strftime("%Y-%m-%d %H:%M:%S"),
      trade_date=parsed.strftime("%Y-%m-%d"),
      hhmm=int(parsed.strftime("%H%M")),
    )
  raise ScriptTimeContextError(f"Bar 时间无效: {raw_time!r}；支持格式: {_SUPPORTED_FORMATS}")
