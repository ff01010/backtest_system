"""读取并校验部署提供的 L2 数据源映射。"""

from __future__ import annotations

import json

from backend import settings
from backend.l2.models import SourceMappingProfile
from backend.l2.providers import L2DataError


def load_profile(kind: str) -> SourceMappingProfile:
  """返回指定源的受控映射；缺少配置时明确拒绝运行。"""
  raw = _raw_mapping(kind)
  if not raw:
    raise L2DataError(f"未配置 L2 {kind} 数据源映射")
  try:
    value = json.loads(raw)
  except json.JSONDecodeError as error:
    raise L2DataError(f"L2 {kind} 数据源映射不是有效 JSON") from error
  if not isinstance(value, dict) or not isinstance(value.get("columns"), dict):
    raise L2DataError(f"L2 {kind} 数据源映射缺少 columns")
  return SourceMappingProfile(
    source_name=str(value.get("sourceName", kind)),
    table_name=str(value.get("tableName", "")),
    columns={str(key): str(item) for key, item in value["columns"].items()},
    source_version=str(value["sourceVersion"]) if value.get("sourceVersion") else None,
    price_scale=float(value.get("priceScale", 1)),
    price_scales={str(key): float(item) for key, item in dict(value.get("priceScales") or {}).items()},
    volume_scale=float(value.get("volumeScale", 1)),
    amount_scale=float(value.get("amountScale", 1)),
    time_semantics=str(value.get("timeSemantics", "event_time")),
    quality_column=str(value["qualityColumn"]) if value.get("qualityColumn") else None,
    trade_value=str(value["tradeValue"]) if value.get("tradeValue") else None,
  )


def has_profile(kind: str) -> bool:
  """判断部署是否为指定数据类型配置了正式映射。"""
  return bool(_raw_mapping(kind).strip())


def load_temporary_quote_profile() -> SourceMappingProfile:
  """返回已核验的临时 3 秒 L2 快照表映射。"""
  return SourceMappingProfile(
    source_name="quant.tick_quote",
    table_name="quant.tick_quote",
    columns={
      "symbol": "symbol",
      "trade_date": "trade_date",
      "time": "datetime",
      "last_price": "last_price",
      "volume": "volume",
      "amount": "amount",
      "cum_amount": "cum_amount",
      **{f"bid_price_{index}": f"bid_price_{index}" for index in range(1, 11)},
      **{f"bid_volume_{index}": f"bid_vol_{index}" for index in range(1, 11)},
      **{f"ask_price_{index}": f"ask_price_{index}" for index in range(1, 11)},
      **{f"ask_volume_{index}": f"ask_vol_{index}" for index in range(1, 11)},
    },
    source_version=settings.L2_TEMP_MAPPING_VERSION,
    price_scales={
      **{f"bid_price_{index}": 0.0001 for index in range(1, 11)},
      **{f"ask_price_{index}": 0.0001 for index in range(1, 11)},
    },
  )


def load_temporary_event_profile() -> SourceMappingProfile:
  """返回已核验的临时逐笔成交表映射。"""
  return SourceMappingProfile(
    source_name="quant.tick_data",
    table_name="quant.tick_data",
    columns={
      "symbol": "symbol",
      "trade_date": "trade_date",
      "time": "datetime",
      "sequence": "trade_id",
      "trade_id": "trade_id",
      "price": "price",
      "volume": "volume",
      "side": "side",
      "event_type": "type",
    },
    source_version=settings.L2_TEMP_MAPPING_VERSION,
    trade_value="trade",
  )


def _raw_mapping(kind: str) -> str:
  values = {
    "bar": settings.L2_BAR_MAPPING_JSON,
    "quote": settings.L2_QUOTE_MAPPING_JSON,
    "event": settings.L2_EVENT_MAPPING_JSON,
  }
  if kind not in values:
    raise L2DataError(f"未知 L2 数据源类型: {kind}")
  return values[kind]
