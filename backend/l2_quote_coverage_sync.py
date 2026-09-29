"""按标的预热 L2 十档日级可用性摘要。"""

from __future__ import annotations

import argparse

from backend.services import bar_data, l2_quote_availability


def _arguments() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="预热 L2 十档日级可用性摘要")
  parser.add_argument("--symbol", required=True)
  parser.add_argument("--start", required=True)
  parser.add_argument("--end", required=True)
  parser.add_argument("--timeframe", required=True)
  return parser.parse_args()


def main() -> None:
  arguments = _arguments()
  bars, snapshot = bar_data.load_normalized_bars_with_snapshot(
    arguments.symbol, arguments.timeframe, arguments.start, arguments.end,
  )
  trade_dates = sorted({bar.trade_date for bar in bars})
  result = l2_quote_availability.rebuild_quote_availability(arguments.symbol, trade_dates)
  print(
    f"L2 十档摘要预热完成：Bar 来源 {snapshot['provider']}，周期 {arguments.timeframe}，"
    f"检查 {result['checked']} 个实际 Bar 日，可用 {result['available']} 个",
  )


if __name__ == "__main__":
  main()
