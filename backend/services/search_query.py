# -*- coding: utf-8 -*-
"""将用户搜索输入解析为与 SQL 无关的不可变查询描述。"""

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import re
from typing import Literal
from uuid import UUID


BacktestSearchMode = Literal["empty", "display_no", "uuid", "symbol", "text"]


class SearchQueryError(ValueError):
  """搜索输入不足以形成安全查询。"""


class SearchCursorError(ValueError):
  """游标无效或不属于当前筛选条件。"""


@dataclass(frozen=True)
class BacktestRunSearchSpec:
  """历史回测搜索的规范化、不可变描述。"""

  mode: BacktestSearchMode
  normalized: str
  display_no: int | None = None
  run_id: str | None = None
  symbols: tuple[str, ...] = ()
  text_pattern: str | None = None


def escape_like_literal(value: str) -> str:
  """转义 LIKE 元字符，使用户输入始终按字面量匹配。"""
  return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_backtest_run_search(query: str) -> BacktestRunSearchSpec:
  """解析历史回测搜索输入，避免精确查询退化为模糊扫描。"""
  normalized = query.strip()
  if not normalized:
    return BacktestRunSearchSpec(mode="empty", normalized="")
  if normalized.startswith("#") and normalized[1:].isdigit():
    return BacktestRunSearchSpec(
      mode="display_no",
      normalized=str(int(normalized[1:])),
      display_no=int(normalized[1:]),
    )
  if re.fullmatch(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
    normalized,
  ):
    run_id = str(UUID(normalized))
    return BacktestRunSearchSpec(mode="uuid", normalized=run_id, run_id=run_id)
  symbol_match = re.fullmatch(r"(\d{6})(?:\.(SH|SZ|BJ))?", normalized.upper())
  if symbol_match:
    code, exchange = symbol_match.groups()
    symbols = (
      (f"{code}.{exchange}",)
      if exchange
      else tuple(f"{code}.{item}" for item in ("SH", "SZ", "BJ"))
    )
    return BacktestRunSearchSpec(
      mode="symbol",
      normalized=normalized.upper(),
      display_no=int(code),
      symbols=symbols,
    )
  if normalized.isdigit():
    return BacktestRunSearchSpec(
      mode="display_no",
      normalized=str(int(normalized)),
      display_no=int(normalized),
    )
  if len(normalized) < 2:
    raise SearchQueryError("普通文字搜索请至少输入两个字符")
  text = normalized.lower()
  return BacktestRunSearchSpec(
    mode="text",
    normalized=text,
    text_pattern=f"%{escape_like_literal(text)}%",
  )


def backtest_search_bindings(
  spec: BacktestRunSearchSpec,
  *,
  symbol: str | None,
  config_id: str | None,
  favorites_only: bool,
) -> dict[str, object]:
  """生成游标绑定，筛选变化时旧游标不能继续使用。"""
  return {
    "mode": spec.mode,
    "query": spec.normalized,
    "symbol": symbol or "",
    "configId": config_id or "",
    "favoritesOnly": favorites_only,
  }


def encode_backtest_cursor(
  order_value: str,
  run_id: str,
  bindings: dict[str, object],
  secret: str,
) -> str:
  """编码带签名的历史回测游标。"""
  payload = json.dumps({
    "bindings": bindings,
    "position": {"orderValue": order_value, "runId": run_id},
  }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
  signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
  return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")


def decode_backtest_cursor(
  value: str,
  bindings: dict[str, object],
  secret: str,
) -> tuple[str, str]:
  """校验游标签名、结构和当前筛选绑定。"""
  try:
    padded = value + "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    payload, signature = raw[:-32], raw[-32:]
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    decoded = json.loads(payload.decode("utf-8"))
  except Exception as error:
    raise SearchCursorError("分页游标无效") from error
  if not hmac.compare_digest(signature, expected):
    raise SearchCursorError("分页游标无效")
  if decoded.get("bindings") != bindings:
    raise SearchCursorError("分页游标与当前筛选条件不一致")
  position = decoded.get("position")
  if not isinstance(position, dict):
    raise SearchCursorError("分页游标无效")
  order_value = position.get("orderValue")
  run_id = position.get("runId")
  if not isinstance(order_value, str) or not isinstance(run_id, str):
    raise SearchCursorError("分页游标无效")
  try:
    UUID(run_id)
  except ValueError as error:
    raise SearchCursorError("分页游标无效") from error
  return order_value, run_id
