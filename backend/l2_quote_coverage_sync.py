"""按标的预热 L2 十档日级可用性摘要。"""

from __future__ import annotations

import argparse

from backend.services import l2_quote_availability, state_store


def _arguments() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="预热 L2 十档日级可用性摘要")
  parser.add_argument("--symbol", required=True)
  parser.add_argument("--start", required=True)
  parser.add_argument("--end", required=True)
  return parser.parse_args()


def main() -> None:
  arguments = _arguments()
  trade_dates = state_store.list_open_trade_dates(arguments.start, arguments.end)
  result = l2_quote_availability.rebuild_quote_availability(arguments.symbol, trade_dates)
  print(f"L2 十档摘要预热完成：检查 {result['checked']} 个交易日，可用 {result['available']} 个")


if __name__ == "__main__":
  main()
