"""部署时初始化并同步 A 股交易日历。"""

from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.services.trade_calendar import sync_trade_calendar


def _arguments() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description='同步 A 股交易日历')
  parser.add_argument('--start', default='2010-01-01')
  parser.add_argument('--end', default=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat())
  return parser.parse_args()


def main() -> None:
  arguments = _arguments()
  result = sync_trade_calendar(arguments.start, arguments.end)
  print(f"交易日历同步完成：{result['startDate']} 至 {result['endDate']}，开市日 {result['openDateCount']} 个")


if __name__ == '__main__':
  main()
