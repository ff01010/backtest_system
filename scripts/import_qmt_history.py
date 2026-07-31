"""将本地导出的 QMT 原始 CSV 批量导入 ClickHouse。"""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


DATABASE = "raw_historical_data"
REQUIRED_COLUMNS = (
  "qmt_index", "time", "open", "high", "low", "close", "volume", "amount",
  "settelementPrice", "openInterest", "preClose", "suspendFlag",
)
EXPECTED_ROWS_BY_PERIOD = {"1m": 241, "5m": 48, "60m": 4, "1d": 1}


class ImportError(RuntimeError):
  """QMT 原始数据文件或 ClickHouse 写入失败。"""


@dataclass(frozen=True)
class ImportConfig:
  """导入所需的连接和目录配置。"""

  input_root: Path
  clickhouse_url: str
  clickhouse_user: str
  clickhouse_password: str
  symbols: frozenset[str]
  dry_run: bool


@dataclass(frozen=True)
class QmtFile:
  """符合 QMT 原始导出目录约定的单个 CSV 文件。"""

  path: Path
  relative_path: str
  symbol: str
  period: str
  sha256: str


@dataclass(frozen=True)
class FileData:
  """已校验的单个 QMT 文件内容与统计信息。"""

  rows: list[dict[str, Any]]
  coverage_rows: list[dict[str, Any]]
  start_time: int
  end_time: int
  invalid_count: int


class ClickHouseClient:
  """仅使用 ClickHouse HTTP JSONEachRow 接口的最小客户端。"""

  def __init__(self, url: str, user: str, password: str) -> None:
    self._url = url.rstrip("/")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

  def insert(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
    batch: list[dict[str, Any]] = []
    for row in rows:
      batch.append(row)
      if len(batch) == 5_000:
        self._insert_batch(table, batch)
        batch = []
    if batch:
      self._insert_batch(table, batch)

  def _insert_batch(self, table: str, rows: list[dict[str, Any]]) -> None:
    query = f"INSERT INTO {DATABASE}.{table} FORMAT JSONEachRow"
    body = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows).encode("utf-8")
    request = Request(self._query_url(query), data=body, headers=self._headers, method="POST")
    try:
      with urlopen(request, timeout=60) as response:
        response.read()
    except OSError as error:
      raise ImportError(f"ClickHouse 写入 {table} 失败: {error}") from error

  def _query_url(self, query: str) -> str:
    return f"{self._url}/?database={DATABASE}&query={quote(query)}"


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="导入 QMT 原始历史 CSV 到 ClickHouse")
  parser.add_argument("--input-root", required=True, help="qmt_raw 目录的服务器路径")
  parser.add_argument("--clickhouse-url", default=os.getenv("QMT_CLICKHOUSE_HTTP_URL", "http://127.0.0.1:8123"))
  parser.add_argument("--clickhouse-user", default=os.getenv("QMT_CLICKHOUSE_USER", "default"))
  parser.add_argument("--symbols", default="", help="仅导入指定标的，使用英文逗号分隔")
  parser.add_argument("--dry-run", action="store_true", help="只校验文件，不写入 ClickHouse")
  return parser.parse_args()


def build_config(args: argparse.Namespace) -> ImportConfig:
  password = os.getenv("QMT_CLICKHOUSE_PASSWORD", "")
  if not args.dry_run and not password:
    raise ImportError("缺少 QMT_CLICKHOUSE_PASSWORD 环境变量")
  root = Path(args.input_root).resolve()
  if not root.is_dir():
    raise ImportError(f"导入目录不存在: {root}")
  symbols = frozenset(item.strip() for item in args.symbols.split(",") if item.strip())
  return ImportConfig(root, args.clickhouse_url, args.clickhouse_user, password, symbols, args.dry_run)


def find_qmt_files(root: Path, symbols: frozenset[str]) -> list[QmtFile]:
  files: list[QmtFile] = []
  for path in sorted(root.rglob("*_raw.csv")):
    relative = path.relative_to(root)
    if len(relative.parts) != 3:
      raise ImportError(f"文件目录不符合 period/代码/文件.csv 约定: {path}")
    period, symbol_dir, _ = relative.parts
    symbol = _parse_symbol(symbol_dir)
    if symbols and symbol not in symbols:
      continue
    files.append(QmtFile(path, relative.as_posix(), symbol, period, _sha256(path)))
  if not files:
    raise ImportError(f"未找到 *_raw.csv 文件: {root}")
  return files


def load_file_data(file: QmtFile, batch_id: UUID) -> FileData:
  rows: list[dict[str, Any]] = []
  invalid_count = 0
  with file.path.open("r", encoding="utf-8", newline="") as handle:
    reader = csv.DictReader(handle)
    _validate_columns(reader.fieldnames, file.path)
    for raw in reader:
      try:
        rows.append(_normalize_row(raw, file, batch_id))
      except (KeyError, TypeError, ValueError):
        invalid_count += 1
  if not rows:
    raise ImportError(f"文件没有可导入的有效行: {file.path}")
  coverage_rows = _build_coverage_rows(rows, file.period, batch_id)
  times = [int(row["time"]) for row in rows]
  return FileData(rows, coverage_rows, min(times), max(times), invalid_count)


def _validate_columns(columns: list[str] | None, path: Path) -> None:
  missing = [item for item in REQUIRED_COLUMNS if columns is None or item not in columns]
  if missing:
    raise ImportError(f"QMT 原始文件缺少字段 {', '.join(missing)}: {path}")


def _normalize_row(raw: dict[str, str], file: QmtFile, batch_id: UUID) -> dict[str, Any]:
  qmt_index = raw["qmt_index"].strip()
  if len(qmt_index) < 8 or not qmt_index[:8].isdigit():
    raise ValueError("qmt_index 格式无效")
  return {
    "symbol": file.symbol,
    "period": file.period,
    "qmt_index": qmt_index,
    "time": int(raw["time"]),
    "open": float(raw["open"]),
    "high": float(raw["high"]),
    "low": float(raw["low"]),
    "close": float(raw["close"]),
    "volume": int(raw["volume"]),
    "amount": float(raw["amount"]),
    "settelementPrice": float(raw["settelementPrice"]),
    "openInterest": int(raw["openInterest"]),
    "preClose": float(raw["preClose"]),
    "suspendFlag": int(raw["suspendFlag"]),
    "import_batch_id": str(batch_id),
  }


def _build_coverage_rows(rows: list[dict[str, Any]], period: str, batch_id: UUID) -> list[dict[str, Any]]:
  grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
  for row in rows:
    key = (str(row["symbol"]), str(row["qmt_index"])[:8])
    grouped.setdefault(key, []).append(row)
  expected = EXPECTED_ROWS_BY_PERIOD.get(period)
  return [_coverage_row(symbol, date_text, period, values, expected, batch_id) for (symbol, date_text), values in grouped.items()]


def _coverage_row(symbol: str, date_text: str, period: str, rows: list[dict[str, Any]], expected: int | None, batch_id: UUID) -> dict[str, Any]:
  times = [int(row["time"]) for row in rows]
  observed = len(rows)
  complete = expected is None or observed == expected
  note = "" if complete else f"预期 {expected} 根，实际 {observed} 根"
  return {
    "symbol": symbol,
    "period": period,
    "trade_date": datetime.strptime(date_text, "%Y%m%d").date().isoformat(),
    "first_bar_time": min(times),
    "last_bar_time": max(times),
    "expected_row_count": expected or observed,
    "observed_row_count": observed,
    "valid_row_count": observed,
    "status": "complete" if complete else "partial",
    "quality_note": note,
    "import_batch_id": str(batch_id),
    "record_version": time.time_ns(),
  }


def import_file(client: ClickHouseClient, file: QmtFile, dry_run: bool) -> FileData:
  batch_id = uuid4()
  data = load_file_data(file, batch_id)
  if dry_run:
    return data
  created_at = _timestamp()
  client.insert("qmt_import_batch", [_batch_row(file, batch_id, data, "started", "", created_at)])
  try:
    client.insert("qmt_kline", data.rows)
    client.insert("qmt_kline_coverage", data.coverage_rows)
  except ImportError as error:
    client.insert("qmt_import_batch", [_batch_row(file, batch_id, data, "failed", str(error), created_at)])
    raise
  client.insert("qmt_import_batch", [_batch_row(file, batch_id, data, "complete", "", created_at)])
  return data


def _batch_row(file: QmtFile, batch_id: UUID, data: FileData, status: str, reason: str, created_at: str) -> dict[str, Any]:
  return {
    "batch_id": str(batch_id), "source_file": file.relative_path, "source_file_sha256": file.sha256,
    "symbol": file.symbol, "period": file.period, "data_start_time": data.start_time,
    "data_end_time": data.end_time, "source_row_count": len(data.rows) + data.invalid_count,
    "imported_row_count": len(data.rows) if status == "complete" else 0,
    "invalid_row_count": data.invalid_count, "status": status, "failure_reason": reason,
    "qmt_client_version": "xtquant", "created_at": created_at, "updated_at": _timestamp(),
    "record_version": time.time_ns(),
  }


def _parse_symbol(value: str) -> str:
  parts = value.split("_")
  if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in {"SH", "SZ", "BJ"}:
    raise ImportError(f"目录中的标的代码无效: {value}")
  return f"{parts[0]}.{parts[1]}"


def _sha256(path: Path) -> str:
  digest = hashlib.sha256()
  with path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()


def _timestamp() -> str:
  return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def main() -> None:
  config = build_config(parse_args())
  client = ClickHouseClient(config.clickhouse_url, config.clickhouse_user, config.clickhouse_password)
  files = find_qmt_files(config.input_root, config.symbols)
  imported = 0
  rows = 0
  for file in files:
    data = import_file(client, file, config.dry_run)
    imported += 1
    rows += len(data.rows)
    print(f"{'校验' if config.dry_run else '导入'}完成: {file.relative_path}，{len(data.rows)} 行")
  print(f"{'校验' if config.dry_run else '导入'}汇总: {imported} 个文件，{rows} 行")


if __name__ == "__main__":
  main()
