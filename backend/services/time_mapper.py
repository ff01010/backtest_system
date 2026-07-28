# -*- coding: utf-8 -*-
"""信号时间 → 结束时间制 bar datetime 映射。"""


def ceil_to_5min(signal_time: str) -> str:
  """将 HH:MM:SS 信号时间映射到所属 5 分钟 bar 的结束时间。

  09:50:01 → 095500
  11:05:21 → 111000
  11:09:59 → 111000
  13:00:04 → 130500

  返回: HHMMSS 格式字符串
  """
  parts = signal_time.split(":")
  h = int(parts[0])
  m = int(parts[1])
  s = int(parts[2]) if len(parts) > 2 else 0

  total_seconds = h * 3600 + m * 60 + s
  morning_start = 9 * 3600 + 30 * 60
  morning_end = 11 * 3600 + 30 * 60
  afternoon_start = 13 * 3600
  afternoon_end = 15 * 3600

  in_morning = morning_start <= total_seconds <= morning_end
  in_afternoon = afternoon_start <= total_seconds <= afternoon_end
  if not in_morning and not in_afternoon:
    return ""

  # 交易时段开盘时刻属于第一根 bar；其他精确边界保持不变。
  if total_seconds in (morning_start, afternoon_start):
    total_seconds += 5 * 60
  elif s > 0 or m % 5 != 0:
    total_seconds = ((total_seconds + 5 * 60 - 1) // (5 * 60)) * (5 * 60)

  mapped_h, remainder = divmod(total_seconds, 3600)
  mapped_m = remainder // 60
  return f"{mapped_h:02d}{mapped_m:02d}00"


def map_signal_to_bar(signal_time: str, target_date: str) -> str | None:
  """将信号时间映射到 bar 的 datetime 字符串

  signal_time: "11:05:21"
  target_date: "20260622"

  返回: "20260622110500" 或 None（非交易时段）
  """
  bar_key = ceil_to_5min(signal_time)
  if not bar_key:
    return None
  return f"{target_date}{bar_key}"
