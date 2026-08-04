"""从 Windows QMT 导出证券目录，并刷新 ClickHouse 目录缓存。"""

from __future__ import annotations

import argparse
import base64
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


DATABASE = "raw_historical_data"
TABLE = "qmt_instrument_directory"
SECTOR = "沪深A股"


@dataclass(frozen=True)
class Instrument:
  symbol: str
  exchange: str
  display_name: str
  instrument_type: str
  board: str
  list_date: str | None
  delist_date: str | None


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="从 QMT 导出证券目录并刷新 ClickHouse 缓存")
  parser.add_argument("--env-file", default="deploy/.env", help="ClickHouse 配置文件")
  parser.add_argument("--cache-path", default="storage/qmt/instrument-directory.json", help="本地目录缓存路径")
  parser.add_argument("--skip-clickhouse", action="store_true", help="仅生成本地缓存")
  return parser.parse_args()


def load_environment(path: Path) -> dict[str, str]:
  if not path.exists():
    raise RuntimeError(f"缺少环境文件: {path}")
  values: dict[str, str] = {}
  for line in path.read_text(encoding="utf-8").splitlines():
    if "=" not in line or line.lstrip().startswith("#"):
      continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip()
  return values


def qmt_instruments() -> list[Instrument]:
  from xtquant import xtdata

  xtdata.enable_hello = False
  xtdata.connect()
  instruments: list[Instrument] = []
  for symbol in xtdata.get_stock_list_in_sector(SECTOR):
    instrument = normalize_instrument(symbol, xtdata.get_instrument_detail(symbol))
    if instrument is not None:
      instruments.append(instrument)
  return sorted(instruments, key=lambda item: (item.exchange, item.symbol))


def normalize_instrument(symbol: str, detail: Any) -> Instrument | None:
  if not isinstance(detail, dict) or "." not in symbol:
    return None
  code, exchange = symbol.rsplit(".", 1)
  exchange = exchange.upper()
  if exchange not in {"SH", "SZ", "BJ"} or not code.isdigit():
    return None
  name = str(detail.get("InstrumentName") or code).strip()
  return Instrument(
    symbol=code,
    exchange=exchange,
    display_name=name,
    instrument_type="stock",
    board=board_for(code, exchange),
    list_date=valid_date(detail.get("OpenDate")),
    delist_date=valid_date(detail.get("ExpireDate")),
  )


def board_for(code: str, exchange: str) -> str:
  if exchange == "BJ":
    return "beijing"
  if code.startswith("688"):
    return "star"
  if code.startswith(("300", "301")):
    return "chinext"
  return "main"


def valid_date(value: Any) -> str | None:
  text = str(value or "").strip()
  if len(text) != 8 or not text.isdigit() or text == "99999999":
    return None
  return f"{text[:4]}-{text[4:6]}-{text[6:]}"


def write_cache(path: Path, instruments: list[Instrument], cached_at: str) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  payload = {
    "source": "qmt",
    "cachedAt": cached_at,
    "count": len(instruments),
    "instruments": [asdict(item) for item in instruments],
  }
  path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def refresh_clickhouse(environment: dict[str, str], instruments: list[Instrument], cached_at: str) -> None:
  url = environment.get("TREND_GRID_CLICKHOUSE_HTTP_URL", "").rstrip("/")
  user = environment.get("TREND_GRID_CLICKHOUSE_USER", "default")
  password = environment.get("TREND_GRID_CLICKHOUSE_PASSWORD", "")
  if not url or not password:
    raise RuntimeError("缺少 ClickHouse 连接配置")
  headers = authorization_headers(user, password)
  execute_clickhouse(url, headers, create_table_sql())
  execute_clickhouse(url, headers, f"TRUNCATE TABLE {DATABASE}.{TABLE}")
  insert_clickhouse(url, headers, instruments, cached_at)


def authorization_headers(user: str, password: str) -> dict[str, str]:
  token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
  return {"Authorization": f"Basic {token}"}


def execute_clickhouse(url: str, headers: dict[str, str], statement: str) -> None:
  request = Request(url, data=statement.encode("utf-8"), headers=headers, method="POST")
  with urlopen(request, timeout=60) as response:
    response.read()


def insert_clickhouse(url: str, headers: dict[str, str], instruments: list[Instrument], cached_at: str) -> None:
  rows = [asdict(item) | {"source": "qmt", "cached_at": cached_at} for item in instruments]
  body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")
  query = quote(f"INSERT INTO {DATABASE}.{TABLE} FORMAT JSONEachRow")
  request = Request(f"{url}/?query={query}", data=body, headers=headers, method="POST")
  with urlopen(request, timeout=120) as response:
    response.read()


def create_table_sql() -> str:
  return f"""
    CREATE TABLE IF NOT EXISTS {DATABASE}.{TABLE} (
      symbol String,
      exchange LowCardinality(String),
      display_name String,
      instrument_type LowCardinality(String),
      board LowCardinality(String),
      list_date Nullable(Date),
      delist_date Nullable(Date),
      source LowCardinality(String),
      cached_at DateTime
    ) ENGINE = ReplacingMergeTree(cached_at)
    ORDER BY (exchange, symbol)
  """


def main() -> None:
  args = parse_args()
  cached_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  instruments = qmt_instruments()
  if not instruments:
    raise RuntimeError("QMT 未返回可用证券目录")
  cache_path = Path(args.cache_path)
  write_cache(cache_path, instruments, cached_at)
  if not args.skip_clickhouse:
    refresh_clickhouse(load_environment(Path(args.env_file)), instruments, cached_at)
  print(f"QMT 证券目录已缓存: {len(instruments)} 条，文件: {cache_path}")
  if not args.skip_clickhouse:
    print(f"ClickHouse 目录表已刷新: {DATABASE}.{TABLE}")


if __name__ == "__main__":
  main()
