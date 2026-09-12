"""从 ClickHouse 统一证券目录同步 PostgreSQL 搜索目录。"""

from __future__ import annotations

from typing import Any

from backend.services import clickhouse_client, state_store


_DATABASE = "xl_derived"
_TABLE = "stock_directory"
_EXCHANGE_MAP = {"XSHG": "SH", "XSHE": "SZ", "BJSE": "BJ"}
_BOARD_MAP = {
  "MainBoard": "main",
  "GEM": "chinext",
  "KSH": "star",
  "BJS": "beijing",
}


def sync_market_instruments() -> int:
  """读取统一目录中的活跃 A 股，并替换 PostgreSQL 搜索目录。"""
  rows = clickhouse_client.query_rows(f"""
    SELECT symbol, exchange, name, type, status, board_type,
           toString(listed_date) AS listed_date,
           toString(de_listed_date) AS de_listed_date,
           source
    FROM {_DATABASE}.{_TABLE} FINAL
    WHERE type = 'CS' AND status = 'Active'
    ORDER BY exchange, symbol
  """)
  instruments = [normalize_row(row) for row in rows]
  if not instruments:
    raise RuntimeError("ClickHouse 统一证券目录中没有活跃 A 股")
  return state_store.replace_market_instruments_from_directory(instruments)


def normalize_row(row: dict[str, Any]) -> dict[str, str | None]:
  raw_exchange = str(row["exchange"]).strip().upper()
  exchange = _mapped_value(_EXCHANGE_MAP, raw_exchange, "交易所")
  board = _mapped_value(_BOARD_MAP, str(row["board_type"]).strip(), "板块")
  symbol = normalize_symbol(row["symbol"], exchange, raw_exchange)
  display_name = str(row["name"]).strip()
  if not display_name:
    raise RuntimeError(f"统一证券目录缺少证券名称: {symbol}.{exchange}")
  source = str(row.get("source") or "").strip() or "xl_derived.stock_directory"
  return {
    "symbol": symbol,
    "exchange": exchange,
    "display_name": display_name,
    "instrument_type": "stock",
    "board": board,
    "list_date": normalize_date(row.get("listed_date")),
    "delist_date": normalize_date(row.get("de_listed_date")),
    "source": source,
  }


def normalize_symbol(value: Any, exchange: str, raw_exchange: str) -> str:
  text = str(value).strip().upper()
  if "." in text:
    code, suffix = text.rsplit(".", 1)
    if suffix not in {exchange, raw_exchange}:
      raise RuntimeError(f"统一证券目录代码与交易所不一致: {text}/{raw_exchange}")
  else:
    code = text
  if not code.isdigit():
    raise RuntimeError(f"统一证券目录代码无效: {text}")
  return code


def _mapped_value(mapping: dict[str, str], value: str, label: str) -> str:
  mapped = mapping.get(value)
  if mapped is None:
    raise RuntimeError(f"统一证券目录存在未支持的{label}: {value or '<empty>'}")
  return mapped


def normalize_date(value: Any) -> str | None:
  text = str(value or "").strip()
  return text if text and text != "1970-01-01" else None
