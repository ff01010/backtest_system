"""`canonical_json_v1` 规范化与摘要实现。"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
import unicodedata
from typing import Any


CANONICALIZATION_VERSION = "canonical_json_v1"


def canonical_decimal(value: Decimal) -> str:
  if not value.is_finite():
    raise ValueError("Decimal 必须是有限值")
  if value == 0:
    return "0"
  normalized = format(value, "f")
  if "." in normalized:
    normalized = normalized.rstrip("0").rstrip(".")
  return normalized


def canonical_value(value: Any) -> Any:
  if isinstance(value, str):
    return unicodedata.normalize("NFC", value)
  if value is None or isinstance(value, (bool, int)):
    return value
  if isinstance(value, Decimal):
    return canonical_decimal(value)
  if isinstance(value, float):
    if not math.isfinite(value):
      raise ValueError("浮点数必须是有限值")
    raise ValueError("身份字段禁止使用二进制 float")
  if isinstance(value, datetime):
    utc_value = value.astimezone(timezone.utc)
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
  if isinstance(value, date):
    return value.isoformat()
  if isinstance(value, dict):
    return {
      canonical_value(str(key)): canonical_value(item)
      for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
    }
  if isinstance(value, (list, tuple)):
    return [canonical_value(item) for item in value]
  if isinstance(value, (set, frozenset)):
    items = [canonical_value(item) for item in value]
    return sorted(items, key=canonical_json)
  raise TypeError(f"canonical_json_v1 不支持类型: {type(value).__name__}")


def canonical_json(value: Any) -> str:
  return json.dumps(
    canonical_value(value),
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
  )


def digest(value: Any) -> str:
  return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def with_version(value: dict[str, Any]) -> dict[str, Any]:
  return {"canonicalizationVersion": CANONICALIZATION_VERSION, **value}
