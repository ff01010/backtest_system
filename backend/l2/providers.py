"""受控 ClickHouse L2 数据源适配器与低频 Bar 缓存。"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Iterator, Protocol, Sequence

from backend.l2.models import CoverageSnapshot, NormalizedBar, QuoteLevel, QuoteSnapshot, SourceMappingProfile, TradeEvent
from backend.services import clickhouse_client
from backend.time_utils import now_shanghai_iso_datetime

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*$")


class L2DataError(RuntimeError):
  """L2 数据契约、覆盖范围或缓存出现错误。"""


class L2BarProvider(Protocol):
  """已物化 L2 Bar 的只读协议。"""

  def load_bars(self, symbol: str, period: str, start_date: str, end_date: str) -> list[NormalizedBar]:
    """读取指定范围内已经完成且带版本的 Bar。"""


class L2QuoteProvider(Protocol):
  """十档快照的只读协议。"""

  def iter_quotes(self, symbol: str, trade_date: str, page_size: int) -> Iterator[list[QuoteSnapshot]]:
    """以有界页读取单日快照。"""

  def next_quote_after(
    self, symbol: str, trade_date: str, after_time: str, before_time: str | None = None,
  ) -> QuoteSnapshot | None:
    """返回严格晚于时间游标的下一条连续竞价快照。"""

  def quotes_after(
    self, symbol: str, trade_date: str, after_time: str, before_time: str | None, limit: int,
  ) -> list[QuoteSnapshot]:
    """返回严格晚于时间游标的有限条连续竞价快照。"""


class L2EventProvider(Protocol):
  """逐笔成交的只读协议。"""

  def iter_trades(self, symbol: str, trade_date: str, page_size: int) -> Iterator[list[TradeEvent]]:
    """以有界页读取单日逐笔成交。"""


class RedisJsonCache:
  """无需额外依赖的最小 Redis JSON 缓存客户端。"""

  def __init__(self, host: str, port: int, timeout_seconds: float = 2.0) -> None:
    self._host = host
    self._port = port
    self._timeout = timeout_seconds

  def get(self, key: str) -> dict[str, Any] | None:
    response = self._command("GET", key)
    if response is None:
      return None
    if not isinstance(response, bytes):
      raise L2DataError("Redis 返回了无效缓存值")
    return json.loads(response.decode("utf-8"))

  def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    self._command("SET", key, payload, "EX", str(ttl_seconds))

  def _command(self, *parts: str) -> bytes | str | int | None:
    packet = b"*" + str(len(parts)).encode() + b"\r\n" + b"".join(
      b"$" + str(len(item.encode("utf-8"))).encode() + b"\r\n" + item.encode("utf-8") + b"\r\n"
      for item in parts
    )
    try:
      with socket.create_connection((self._host, self._port), self._timeout) as connection:
        connection.sendall(packet)
        return self._read_response(connection)
    except OSError as error:
      raise L2DataError(f"Redis 缓存不可用: {error}") from error

  def _read_response(self, connection: socket.socket) -> bytes | str | int | None:
    marker = connection.recv(1)
    line = self._read_line(connection)
    if marker == b"+":
      return line.decode("utf-8")
    if marker == b":":
      return int(line)
    if marker == b"-":
      raise L2DataError(f"Redis 返回错误: {line.decode('utf-8')}")
    if marker != b"$":
      raise L2DataError("Redis 协议响应无效")
    length = int(line)
    if length < 0:
      return None
    data = self._read_exact(connection, length)
    self._read_exact(connection, 2)
    return data

  @staticmethod
  def _read_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
      char = connection.recv(1)
      if not char:
        raise L2DataError("Redis 连接在响应完成前关闭")
      if char == b"\r":
        if connection.recv(1) != b"\n":
          raise L2DataError("Redis 行结束符无效")
        return b"".join(chunks)
      chunks.append(char)

  @staticmethod
  def _read_exact(connection: socket.socket, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
      chunk = connection.recv(remaining)
      if not chunk:
        raise L2DataError("Redis 连接在响应完成前关闭")
      chunks.append(chunk)
      remaining -= len(chunk)
    return b"".join(chunks)


class ClickHouseL2Adapter:
  """先校验部署映射，再以固定别名输出标准化 L2 数据。"""

  def __init__(self, profile: SourceMappingProfile) -> None:
    self._profile = profile
    self._validated = False
    self._validated_quote_days: set[tuple[str, str]] = set()

  def load_bars(self, symbol: str, period: str, start_date: str, end_date: str) -> list[NormalizedBar]:
    self._validate_profile({"symbol", "trade_date", "period", "start_time", "end_time", "open", "high", "low", "close", "volume", "amount", "quality", "source_version"})
    rows = clickhouse_client.query_rows(self._select("symbol, trade_date, period, start_time, end_time, open, high, low, close, volume, amount, quality, source_version, snapshot_count, max_gap_ms, valid_price_count, actual_duration_seconds", symbol, start_date, end_date, "period"))
    bars = [self._bar_from_row(row) for row in rows if str(row["period"]) == period]
    if not bars:
      raise L2DataError("L2 Bar 数据为空或不存在所选周期")
    return bars

  def iter_quotes(self, symbol: str, trade_date: str, page_size: int) -> Iterator[list[QuoteSnapshot]]:
    fields = self._quote_fields(symbol, trade_date)
    self._ensure_unique_quote_times(symbol, trade_date)
    derived_sequence = 0
    for rows in self._pages(fields, symbol, trade_date, page_size, continuous_session=True):
      quotes: list[QuoteSnapshot] = []
      for row in rows:
        derived_sequence += 1
        quotes.append(self._quote_from_row(row, derived_sequence))
      yield quotes

  def next_quote_after(
    self, symbol: str, trade_date: str, after_time: str, before_time: str | None = None,
  ) -> QuoteSnapshot | None:
    """按时间游标读取一条快照，兼容单条读取调用方。"""
    quotes = self.quotes_after(symbol, trade_date, after_time, before_time, 1)
    return quotes[0] if quotes else None

  def quotes_after(
    self, symbol: str, trade_date: str, after_time: str, before_time: str | None, limit: int,
  ) -> list[QuoteSnapshot]:
    """按时间游标小批量读取快照，避免每条快照各发一次远程请求。"""
    if limit <= 0:
      raise ValueError("L2 快照预取数量必须大于 0")
    fields = self._quote_fields(symbol, trade_date)
    self._ensure_unique_quote_times(symbol, trade_date)
    time_column = self._profile.columns["time"]
    conditions = [
      f"{self._profile.columns['symbol']}={self._literal(symbol)}",
      f"{self._profile.columns['trade_date']}={self._literal(trade_date)}",
      f"{time_column}>{self._timestamp_literal(after_time)}",
      self._continuous_session_filter().removeprefix(" AND "),
    ]
    if before_time is not None:
      conditions.append(f"{time_column}<{self._timestamp_literal(before_time)}")
    order = f"{time_column}, {self._profile.columns['sequence']}" if "sequence" in self._profile.columns else time_column
    query = f"SELECT {self._aliases(fields)} FROM {self._profile.table_name} WHERE {' AND '.join(conditions)} ORDER BY {order} LIMIT {limit}"
    rows = clickhouse_client.query_rows_with_retry(query)
    return [self._quote_from_row(row, index) for index, row in enumerate(rows, start=1)]

  def last_quote_before(self, symbol: str, trade_date: str, before_time: str) -> QuoteSnapshot | None:
    """读取收盘边界前最后一条连续竞价快照。"""
    fields = self._quote_fields(symbol, trade_date)
    self._ensure_unique_quote_times(symbol, trade_date)
    time_column = self._profile.columns["time"]
    conditions = [
      f"{self._profile.columns['symbol']}={self._literal(symbol)}",
      f"{self._profile.columns['trade_date']}={self._literal(trade_date)}",
      f"{time_column}<{self._timestamp_literal(before_time)}",
      self._continuous_session_filter().removeprefix(" AND "),
    ]
    order = f"{time_column} DESC, {self._profile.columns['sequence']} DESC" if "sequence" in self._profile.columns else f"{time_column} DESC"
    query = f"SELECT {self._aliases(fields)} FROM {self._profile.table_name} WHERE {' AND '.join(conditions)} ORDER BY {order} LIMIT 1"
    rows = clickhouse_client.query_rows_with_retry(query)
    return self._quote_from_row(rows[0], 0) if rows else None

  def iter_trades(self, symbol: str, trade_date: str, page_size: int) -> Iterator[list[TradeEvent]]:
    required = {"symbol", "trade_date", "time", "sequence", "trade_id", "price", "volume", "side", "event_type"}
    self._validate_profile(required)
    if not self._profile.trade_value:
      raise L2DataError("逐笔事件映射必须配置 tradeValue")
    fields = ", ".join(sorted(required | {"source_version"}))
    for rows in self._pages(fields, symbol, trade_date, page_size, trade_only=True, continuous_session=True):
      yield [self._trade_from_row(row) for row in rows]

  def count_rows(self, symbol: str, trade_date: str, trade_only: bool = False) -> int:
    """返回单日适配器记录数，用于在流式前校验分块数量。"""
    required = {"symbol", "trade_date", "time"}
    if trade_only:
      required.add("event_type")
    self._validate_profile(required)
    event_filter = self._trade_filter() if trade_only else ""
    session_filter = self._continuous_session_filter()
    query = (
      f"SELECT count() AS total FROM {self._profile.table_name} "
      f"WHERE {self._profile.columns['symbol']}={self._literal(symbol)} "
      f"AND {self._profile.columns['trade_date']}={self._literal(trade_date)}{event_filter}{session_filter}"
    )
    rows = clickhouse_client.query_rows(query)
    return int(rows[0]["total"])

  def coverage(self, symbol: str, start_date: str, end_date: str) -> CoverageSnapshot:
    self._validate_profile({"symbol", "trade_date", "source_version"})
    source_version = self._profile.columns.get("source_version")
    version_sql = f"any({source_version})" if source_version else self._literal(self._profile.source_version or "")
    query = (
      f"SELECT min({self._profile.columns['trade_date']}) AS first_date, "
      f"max({self._profile.columns['trade_date']}) AS last_date, {version_sql} AS source_version "
      f"FROM {self._profile.table_name} WHERE {self._profile.columns['symbol']}={self._literal(symbol)} "
      f"AND {self._profile.columns['trade_date']} BETWEEN {self._literal(start_date)} AND {self._literal(end_date)}"
    )
    row = clickhouse_client.query_rows(query)[0]
    complete = str(row.get("first_date") or "") <= start_date and str(row.get("last_date") or "") >= end_date
    return CoverageSnapshot(self._profile.source_name, symbol, start_date, end_date, self._string_or_none(row.get("source_version")), complete)

  def _pages(self, fields: str, symbol: str, trade_date: str, page_size: int, trade_only: bool = False, continuous_session: bool = False) -> Iterator[list[dict[str, Any]]]:
    if page_size <= 0:
      raise ValueError("L2 读取页大小必须大于 0")
    offset = 0
    while True:
      query = f"{self._select(fields, symbol, trade_date, trade_date, None, trade_only, continuous_session)} LIMIT {page_size} OFFSET {offset}"
      rows = clickhouse_client.query_rows_with_retry(query)
      if not rows:
        return
      yield rows
      if len(rows) < page_size:
        return
      offset += len(rows)

  def _quote_fields(self, symbol: str, trade_date: str) -> str:
    required = {"symbol", "trade_date", "time", "last_price"}
    required.update({f"bid_price_{index}" for index in range(1, 11)})
    required.update({f"bid_volume_{index}" for index in range(1, 11)})
    required.update({f"ask_price_{index}" for index in range(1, 11)})
    required.update({f"ask_volume_{index}" for index in range(1, 11)})
    self._validate_profile(required)
    return ", ".join(sorted(required | {"source_version", "sequence"}))

  def _select(self, fields: str, symbol: str, start_date: str, end_date: str, order_field: str | None, trade_only: bool = False, continuous_session: bool = False) -> str:
    aliases = self._aliases(fields)
    has_sequence = "sequence" in self._profile.columns
    order = "trade_date, time, sequence" if order_field is None and has_sequence else "trade_date, time"
    if order_field is not None:
      order = "trade_date, start_time"
    event_filter = self._trade_filter() if trade_only else ""
    session_filter = self._continuous_session_filter() if continuous_session else ""
    symbol_column = self._profile.columns["symbol"]
    trade_date_column = self._profile.columns["trade_date"]
    return f"SELECT {aliases} FROM {self._profile.table_name} WHERE {symbol_column}={self._literal(symbol)} AND {trade_date_column} BETWEEN {self._literal(start_date)} AND {self._literal(end_date)}{event_filter}{session_filter} ORDER BY {order}"

  def _aliases(self, fields: str) -> str:
    requested = [item.strip() for item in fields.split(",")]
    expressions: list[str] = []
    for name in requested:
      column = self._profile.columns.get(name)
      if column:
        expressions.append(f"{column} AS {name}")
      elif name == "source_version":
        expressions.append(f"{self._literal(self._profile.source_version or '')} AS source_version")
      elif name == "sequence":
        expressions.append("0 AS sequence")
      elif name in {"snapshot_count", "max_gap_ms", "valid_price_count", "actual_duration_seconds"}:
        expressions.append(f"0 AS {name}")
      else:
        raise L2DataError(f"数据源映射缺少字段: {name}")
    return ", ".join(expressions)

  def _validate_profile(self, required: set[str]) -> None:
    if not _TABLE.fullmatch(self._profile.table_name):
      raise L2DataError("数据源表名不符合白名单格式")
    mapped = {self._profile.columns[name] for name in required if name in self._profile.columns}
    if any(not _IDENTIFIER.fullmatch(item) for item in mapped):
      raise L2DataError("数据源列名不符合白名单格式")
    missing = required - self._profile.columns.keys() - {"source_version", "snapshot_count", "max_gap_ms", "valid_price_count", "actual_duration_seconds"}
    if missing:
      raise L2DataError(f"数据源映射缺少字段: {', '.join(sorted(missing))}")
    if self._validated:
      return
    rows = clickhouse_client.query_rows(f"DESCRIBE TABLE {self._profile.table_name}")
    actual = {str(item.get("name")) for item in rows}
    if not mapped <= actual:
      raise L2DataError("数据源映射列不存在于 ClickHouse 表")
    self._validated = True

  def _trade_filter(self) -> str:
    column = self._profile.columns.get("event_type")
    if not column or not self._profile.trade_value:
      raise L2DataError("逐笔事件映射必须配置 event_type 和 tradeValue")
    return f" AND {column}={self._literal(self._profile.trade_value)}"

  def _continuous_session_filter(self) -> str:
    time_column = self._profile.columns["time"]
    date_column = self._profile.columns["trade_date"]
    morning_start = f"toDateTime(concat(toString({date_column}), ' 09:30:00'))"
    morning_end = f"toDateTime(concat(toString({date_column}), ' 11:30:00'))"
    afternoon_start = f"toDateTime(concat(toString({date_column}), ' 13:00:00'))"
    afternoon_end = f"toDateTime(concat(toString({date_column}), ' 14:57:00'))"
    return (
      f" AND (({time_column}>={morning_start} AND {time_column}<{morning_end}) "
      f"OR ({time_column}>={afternoon_start} AND {time_column}<{afternoon_end}))"
    )

  def _bar_from_row(self, row: dict[str, Any]) -> NormalizedBar:
    return NormalizedBar(
      symbol=str(row["symbol"]), trade_date=str(row["trade_date"]), period=str(row["period"]),
      start_time=str(row["start_time"]), end_time=str(row["end_time"]), open=self._price(row["open"], "open"),
      high=self._price(row["high"], "high"), low=self._price(row["low"], "low"), close=self._price(row["close"], "close"),
      volume=float(row["volume"]) * self._profile.volume_scale, amount=float(row["amount"]) * self._profile.amount_scale,
      quality=str(row["quality"]), source_version=str(row["source_version"]), snapshot_count=int(row["snapshot_count"]),
      max_gap_ms=int(row["max_gap_ms"]), valid_price_count=int(row["valid_price_count"]), actual_duration_seconds=int(row["actual_duration_seconds"]),
    )

  def _quote_from_row(self, row: dict[str, Any], derived_sequence: int) -> QuoteSnapshot:
    last_price = self._price(row["last_price"], "last_price")
    quote_price_scale = self._temporary_quote_price_scale(row, last_price)
    bids = self._quote_levels(row, "bid", quote_price_scale)
    asks = self._quote_levels(row, "ask", quote_price_scale)
    sequence = int(row["sequence"]) if "sequence" in self._profile.columns else derived_sequence
    return QuoteSnapshot(str(row["symbol"]), str(row["trade_date"]), str(row["time"]), sequence, bids, asks, last_price, str(row.get("source_version") or ""))

  def _quote_levels(
    self, row: dict[str, Any], side: str, price_scale: float | None = None,
  ) -> tuple[QuoteLevel, ...]:
    levels: list[QuoteLevel] = []
    for index in range(1, 11):
      value = row[f"{side}_price_{index}"]
      price = self._price(value, f"{side}_price_{index}") if price_scale is None else float(value) * price_scale
      volume = self._volume(row[f"{side}_volume_{index}"])
      if price == 0:
        continue
      levels.append(QuoteLevel(price, volume))
    return tuple(levels)

  def _temporary_quote_price_scale(self, row: dict[str, Any], last_price: float) -> float | None:
    """兼容临时十档表历史分区中元与万分之一元并存的情况。"""
    if self._profile.table_name != "quant.tick_quote" or last_price <= 0:
      return None
    raw_prices = [
      float(row[f"{side}_price_{index}"])
      for side in ("bid", "ask")
      for index in range(1, 11)
      if float(row[f"{side}_price_{index}"]) > 0
    ]
    ratios = [price / last_price for price in raw_prices]
    direct_count = sum(0.5 <= ratio <= 1.5 for ratio in ratios)
    scaled_count = sum(5_000 <= ratio <= 15_000 for ratio in ratios)
    if direct_count and not scaled_count:
      return 1.0
    if scaled_count and not direct_count:
      return 0.0001
    return None

  def _validate_unique_quote_times(self, symbol: str, trade_date: str) -> None:
    query = (
      f"SELECT count() AS total, uniqExact({self._profile.columns['time']}) AS unique_total "
      f"FROM {self._profile.table_name} WHERE {self._profile.columns['symbol']}={self._literal(symbol)} "
      f"AND {self._profile.columns['trade_date']}={self._literal(trade_date)}{self._continuous_session_filter()}"
    )
    row = clickhouse_client.query_rows(query)[0]
    if int(row["total"]) != int(row["unique_total"]):
      raise L2DataError("快照缺少稳定 sequence，且同一时间戳存在重复记录")

  def _ensure_unique_quote_times(self, symbol: str, trade_date: str) -> None:
    if "sequence" in self._profile.columns:
      return
    key = (symbol, trade_date)
    if key not in self._validated_quote_days:
      self._validate_unique_quote_times(symbol, trade_date)
      self._validated_quote_days.add(key)

  def _trade_from_row(self, row: dict[str, Any]) -> TradeEvent:
    return TradeEvent(str(row["symbol"]), str(row["trade_date"]), str(row["time"]), int(row["sequence"]), str(row["trade_id"]), self._price(row["price"], "price"), self._volume(row["volume"]), str(row["side"]), str(row.get("source_version") or ""))

  def _price(self, value: Any, field_name: str) -> float:
    return float(value) * self._profile.price_scales.get(field_name, self._profile.price_scale)

  def _volume(self, value: Any) -> int:
    return int(float(value) * self._profile.volume_scale)

  @staticmethod
  def _literal(value: str) -> str:
    if value == "":
      return "''"
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
      raise L2DataError("L2 查询参数格式无效")
    return f"'{value}'"

  @classmethod
  def _timestamp_literal(cls, value: str) -> str:
    normalized = value.replace(" ", "T")
    return cls._literal(normalized)

  @staticmethod
  def _string_or_none(value: Any) -> str | None:
    return str(value) if value not in {None, ""} else None


class VersionedBarCache:
  """以聚合与映射版本隔离的单日派生 Bar 缓存。"""

  def __init__(self, cache: RedisJsonCache | None, ttl_seconds: int) -> None:
    self._cache = cache
    self._ttl = ttl_seconds

  def get(self, aggregation_version: str | None, mapping_version: str | None, symbol: str, trade_date: str, period: str) -> list[NormalizedBar] | None:
    if self._cache is None or not aggregation_version or not mapping_version:
      return None
    try:
      value = self._cache.get(self._key(aggregation_version, mapping_version, symbol, trade_date, period))
    except L2DataError:
      return None
    if value is None:
      return None
    return [NormalizedBar(**item) for item in value.get("bars", [])]

  def set(
    self,
    aggregation_version: str | None,
    mapping_version: str | None,
    symbol: str,
    trade_date: str,
    period: str,
    bars: Sequence[NormalizedBar],
    metadata: dict[str, Any] | None = None,
  ) -> None:
    if self._cache is None or not aggregation_version or not mapping_version:
      return
    try:
      self._cache.set(
        self._key(aggregation_version, mapping_version, symbol, trade_date, period),
        {"bars": [asdict(item) for item in bars], "metadata": metadata or {}},
        self._ttl,
      )
    except L2DataError:
      return

  @staticmethod
  def _key(aggregation_version: str, mapping_version: str, symbol: str, trade_date: str, period: str) -> str:
    return f"l2bar:v2:{aggregation_version}:{mapping_version}:{symbol}:{trade_date}:{period}"

  @classmethod
  def key(cls, aggregation_version: str, mapping_version: str, symbol: str, trade_date: str, period: str) -> str:
    """返回缓存键，供任务快照记录审计信息。"""
    return cls._key(aggregation_version, mapping_version, symbol, trade_date, period)


class TemporaryQuoteBarProvider:
  """在 ClickHouse 按交易时段聚合 3 秒快照，并缓存历史交易日结果。"""

  _PERIOD_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "45m": 2700, "60m": 3600, "1d": 14_400}

  def __init__(
    self,
    profile: SourceMappingProfile,
    cache: VersionedBarCache,
    aggregation_version: str,
    mapping_version: str,
  ) -> None:
    self._profile = profile
    self._cache = cache
    self._aggregation_version = aggregation_version
    self._mapping_version = mapping_version
    self._cache_status: dict[str, str] = {}
    self._cache_keys: dict[str, str] = {}
    self._quality_by_day: dict[str, str] = {}
    self._validated = False

  def load_bars(self, symbol: str, period: str, start_date: str, end_date: str) -> list[NormalizedBar]:
    if period not in self._PERIOD_SECONDS:
      raise L2DataError(f"临时 L2 聚合不支持周期: {period}")
    start = self._parse_date(start_date)
    end = self._parse_date(end_date)
    if end < start:
      raise L2DataError("L2 回测结束日期不能早于开始日期")
    if end >= date.today():
      raise L2DataError("临时 L2 聚合只支持已结束的历史交易日")
    self._validate_profile()
    bars: list[NormalizedBar] = []
    for trade_date in self._trading_days(symbol, start.isoformat(), end.isoformat()):
      bars.extend(self._load_day(symbol, trade_date, period))
    if not bars:
      raise L2DataError("临时 L2 聚合未找到所选范围内的快照数据")
    partial = [bar.trade_date for bar in bars if bar.quality != "complete"]
    if partial:
      raise L2DataError(f"临时 L2 Bar 数据不完整: {', '.join(sorted(set(partial)))}")
    return bars

  def coverage(self, symbol: str, start_date: str, end_date: str) -> CoverageSnapshot:
    return CoverageSnapshot(
      source_name=self._profile.source_name,
      symbol=symbol,
      start_date=start_date,
      end_date=end_date,
      source_version=f"{self._aggregation_version}:{self._mapping_version}",
      complete=True,
      warnings=tuple(f"{day}:{status}" for day, status in sorted(self._cache_status.items())),
    )

  def cache_snapshot(self) -> dict[str, Any]:
    """返回持久化到任务结果的派生 Bar 审计信息。"""
    return {
      "provider": "temporary_tick_quote_aggregation",
      "inputTable": self._profile.table_name,
      "aggregationVersion": self._aggregation_version,
      "mappingVersion": self._mapping_version,
      "days": [
        {"tradeDate": day, "cacheStatus": self._cache_status[day], "cacheKey": self._cache_keys[day], "quality": self._quality_by_day.get(day, "unknown")}
        for day in sorted(self._cache_status)
      ],
    }

  def _load_day(self, symbol: str, trade_date: str, period: str) -> list[NormalizedBar]:
    key = self._cache.key(self._aggregation_version, self._mapping_version, symbol, trade_date, period)
    self._cache_keys[trade_date] = key
    cached = self._cache.get(self._aggregation_version, self._mapping_version, symbol, trade_date, period)
    if cached is not None:
      self._cache_status[trade_date] = "hit"
      self._quality_by_day[trade_date] = _summarize_quality(cached)
      return cached
    bars = self._aggregate_day(symbol, trade_date, period)
    quality = _summarize_quality(bars)
    self._quality_by_day[trade_date] = quality
    if quality == "complete":
      self._cache.set(
        self._aggregation_version,
        self._mapping_version,
        symbol,
        trade_date,
        period,
        bars,
        {
          "inputTable": self._profile.table_name,
          "aggregationVersion": self._aggregation_version,
          "mappingVersion": self._mapping_version,
          "generatedAt": now_shanghai_iso_datetime(),
          "sourcePolicy": "closed_day_append_only",
        },
      )
      self._cache_status[trade_date] = "miss"
    else:
      self._cache_status[trade_date] = "miss_unusable"
    return bars

  def _aggregate_day(self, symbol: str, trade_date: str, period: str) -> list[NormalizedBar]:
    seconds = self._PERIOD_SECONDS[period]
    rows = clickhouse_client.query_rows(self._aggregation_query(symbol, trade_date, period, seconds))
    bars: list[NormalizedBar] = []
    for row in rows:
      if int(row["valid_price_count"]) == 0:
        raise L2DataError(f"临时 L2 Bar 无有效价格: {trade_date}")
      bars.append(NormalizedBar(
        symbol=str(row["symbol"]), trade_date=str(row["trade_date"]), period=period,
        start_time=str(row["start_time"]), end_time=str(row["end_time"]),
        open=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]),
        volume=float(row["volume"]) * self._profile.volume_scale,
        amount=float(row["amount"]) * self._profile.amount_scale,
        quality=str(row["quality"]),
        source_version=f"{self._aggregation_version}:{self._mapping_version}",
        snapshot_count=int(row["snapshot_count"]), max_gap_ms=int(row["max_gap_ms"]),
        valid_price_count=int(row["valid_price_count"]), actual_duration_seconds=int(row["actual_duration_seconds"]),
      ))
    return bars

  def _aggregation_query(self, symbol: str, trade_date: str, period: str, seconds: int) -> str:
    column = self._profile.columns
    morning_start = "toDateTime(concat(toString(trade_date), ' 09:30:00'))"
    morning_end = "toDateTime(concat(toString(trade_date), ' 11:30:00'))"
    afternoon_start = "toDateTime(concat(toString(trade_date), ' 13:00:00'))"
    afternoon_end = "toDateTime(concat(toString(trade_date), ' 14:57:00'))"
    is_daily = period == "1d"
    bucket = morning_start if is_daily else f"addSeconds(session_start, intDiv(dateDiff('second', session_start, event_time), {seconds}) * {seconds})"
    bar_end = afternoon_end if is_daily else f"least(addSeconds(bucket_start, {seconds}), max(session_end))"
    duration = "14220" if is_daily else "dateDiff('second', bucket_start, bar_end)"
    source_where = (
      f"{column['symbol']}={ClickHouseL2Adapter._literal(symbol)} AND "
      f"{column['trade_date']}={ClickHouseL2Adapter._literal(trade_date)}"
    )
    return f"""
WITH source AS (
  SELECT {column['symbol']} AS symbol, {column['trade_date']} AS trade_date,
    {column['time']} AS event_time, {column['last_price']} AS last_price,
    {column['volume']} AS volume, {column['amount']} AS amount,
    multiIf(
      {column['time']} >= {morning_start} AND {column['time']} < {morning_end}, {morning_start},
      {column['time']} >= {afternoon_start} AND {column['time']} < {afternoon_end}, {afternoon_start},
      NULL
    ) AS session_start,
    multiIf(
      {column['time']} >= {morning_start} AND {column['time']} < {morning_end}, {morning_end},
      {column['time']} >= {afternoon_start} AND {column['time']} < {afternoon_end}, {afternoon_end},
      NULL
    ) AS session_end
  FROM {self._profile.table_name}
  WHERE {source_where}
), session_rows AS (
  SELECT * FROM source WHERE session_start IS NOT NULL
), measured AS (
  SELECT *, {bucket} AS bucket_start,
    lagInFrame(event_time, 1, event_time) OVER (
      PARTITION BY symbol, trade_date, session_start ORDER BY event_time
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS previous_time
  FROM session_rows
), bars AS (
  SELECT symbol, trade_date, bucket_start, {bar_end} AS bar_end,
    argMinIf(last_price, event_time, last_price > 0) AS open,
    maxIf(last_price, last_price > 0) AS high,
    minIf(last_price, last_price > 0) AS low,
    argMaxIf(last_price, event_time, last_price > 0) AS close,
    sumIf(volume, volume >= 0) AS aggregated_volume,
    sumIf(amount, amount >= 0) AS aggregated_amount,
    count() AS snapshot_count,
    max(dateDiff('millisecond', previous_time, event_time)) AS max_gap_ms,
    countIf(last_price > 0) AS valid_price_count,
    countIf(volume < 0 OR amount < 0) AS invalid_increment_count
  FROM measured
  GROUP BY symbol, trade_date, bucket_start
)
SELECT symbol, toString(trade_date) AS trade_date,
  formatDateTime(bucket_start, '%F %T') AS start_time,
  formatDateTime(bar_end, '%F %T') AS end_time,
  open, high, low, close, aggregated_volume AS volume, aggregated_amount AS amount, snapshot_count, max_gap_ms, valid_price_count,
  {duration} AS actual_duration_seconds,
  if(valid_price_count = snapshot_count AND invalid_increment_count = 0, 'complete', 'partial') AS quality
FROM bars
ORDER BY trade_date, bucket_start
"""

  def _trading_days(self, symbol: str, start_date: str, end_date: str) -> list[str]:
    column = self._profile.columns
    query = (
      f"SELECT DISTINCT toString({column['trade_date']}) AS trade_date FROM {self._profile.table_name} "
      f"WHERE {column['symbol']}={ClickHouseL2Adapter._literal(symbol)} "
      f"AND {column['trade_date']} BETWEEN {ClickHouseL2Adapter._literal(start_date)} "
      f"AND {ClickHouseL2Adapter._literal(end_date)} ORDER BY trade_date"
    )
    return [str(row["trade_date"]) for row in clickhouse_client.query_rows(query)]

  def _validate_profile(self) -> None:
    if self._validated:
      return
    required = {"symbol", "trade_date", "time", "last_price", "volume", "amount"}
    if not _TABLE.fullmatch(self._profile.table_name) or not required <= self._profile.columns.keys():
      raise L2DataError("临时 L2 快照映射不完整")
    mapped = {self._profile.columns[item] for item in required}
    if any(not _IDENTIFIER.fullmatch(item) for item in mapped):
      raise L2DataError("临时 L2 快照映射包含非法列名")
    actual = {str(row.get("name")) for row in clickhouse_client.query_rows(f"DESCRIBE TABLE {self._profile.table_name}")}
    if not mapped <= actual:
      raise L2DataError("临时 L2 快照映射列不存在于 ClickHouse 表")
    self._validated = True

  @staticmethod
  def _parse_date(value: str) -> date:
    try:
      return date.fromisoformat(value)
    except ValueError as error:
      raise L2DataError("L2 日期必须为 YYYY-MM-DD 或 YYYYMMDD") from error


def _summarize_quality(bars: Sequence[NormalizedBar]) -> str:
  """将单日所有 Bar 的质量压缩为可审计状态。"""
  if not bars:
    return "empty"
  if all(bar.quality == "complete" for bar in bars):
    return "complete"
  return "partial"
