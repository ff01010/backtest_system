"""从 ClickHouse 的 QMT 目录缓存同步 PostgreSQL 搜索目录。"""

from __future__ import annotations

from typing import Any

from backend.services import clickhouse_client, state_store


_DATABASE = "raw_historical_data"
_TABLE = "qmt_instrument_directory"


def sync_market_instruments() -> int:
  """读取完整 QMT 快照，并替换 PostgreSQL 中的有效股票目录。"""
  rows = clickhouse_client.query_rows(f"""
    SELECT symbol, exchange, display_name, instrument_type, board,
           toString(list_date) AS list_date, toString(delist_date) AS delist_date
    FROM {_DATABASE}.{_TABLE} FINAL
    ORDER BY exchange, symbol
  """)
  instruments = [normalize_row(row) for row in rows]
  if not instruments:
    raise RuntimeError("ClickHouse QMT 证券目录为空")
  return state_store.replace_market_instruments_from_qmt(instruments)


def normalize_row(row: dict[str, Any]) -> dict[str, str | None]:
  return {
    "symbol": str(row["symbol"]),
    "exchange": str(row["exchange"]),
    "display_name": str(row["display_name"]),
    "instrument_type": str(row["instrument_type"]),
    "board": str(row["board"]),
    "list_date": normalize_date(row.get("list_date")),
    "delist_date": normalize_date(row.get("delist_date")),
  }


def normalize_date(value: Any) -> str | None:
  text = str(value or "").strip()
  return text if text and text != "1970-01-01" else None
