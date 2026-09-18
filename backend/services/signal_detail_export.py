"""信号明细 CSV 契约、资格判断与 ZIP 条目生成。"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
import math
import re
from typing import Any


CSV_HEADERS = (
  "signalTime",
  "executionTime",
  "status",
  "direction",
  "side",
  "action",
  "price",
  "priceSource",
  "quantity",
  "pnl",
  "researchSignalId",
  "researchSignalAction",
  "researchSignalEffectiveAt",
  "note",
)
EXCLUSION_REASONS = {
  "run_not_succeeded",
  "result_missing",
  "result_incompatible",
  "no_signal_details",
}
_CLOSED_TRADE_ACTION_PATTERN = re.compile(r"(CLOSE|COVER|STOP|FORCE)")
_OVERLAY_CLOSE_EFFECTS = {"partial_close", "close", "reverse"}
_MISSING = object()


class SignalDetailSerializationError(ValueError):
  """结果值无法按既有 CSV 契约序列化。"""


def _javascript_number_string(value: float | int) -> str:
  if isinstance(value, bool):
    return "true" if value else "false"
  if isinstance(value, int):
    if abs(value) <= 9_007_199_254_740_991:
      return str(value)
    try:
      return _javascript_number_string(float(value))
    except OverflowError:
      return "Infinity" if value > 0 else "-Infinity"
  if not math.isfinite(value):
    if math.isnan(value):
      return "NaN"
    return "Infinity" if value > 0 else "-Infinity"
  if value == 0:
    return "0"
  raw = repr(value).lower()
  absolute = abs(value)
  if 1e-6 <= absolute < 1e21 and "e" in raw:
    fixed = format(Decimal(raw), "f")
    return fixed.rstrip("0").rstrip(".") if "." in fixed else fixed
  if "e" not in raw:
    return raw[:-2] if raw.endswith(".0") else raw
  mantissa, exponent = raw.split("e", 1)
  sign = "+" if not exponent.startswith("-") else "-"
  digits = exponent.lstrip("+-0") or "0"
  return f"{mantissa}e{sign if sign == '+' else '-'}{digits}"


def _javascript_string(value: Any) -> str:
  if value is _MISSING:
    return "undefined"
  if value is None:
    return "null"
  if isinstance(value, bool):
    return "true" if value else "false"
  if isinstance(value, (int, float)):
    return _javascript_number_string(value)
  if isinstance(value, (dict, list)):
    return "[object Object]" if isinstance(value, dict) else ",".join(
      _javascript_string(item) for item in value
    )
  return str(value)


def format_signal_time(value: Any) -> str:
  if not value:
    return "-"
  text = _javascript_string(value)
  compact = re.sub(r"\D", "", text)
  if len(compact) >= 12:
    return (
      f"{compact[:4]}-{compact[4:6]}-{compact[6:8]} "
      f"{compact[8:10]}:{compact[10:12]}"
    )
  if len(compact) == 8:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
  return text


def _strategy_category(result: dict[str, Any], requested_params: dict[str, Any]) -> str:
  manifest = result.get("experimentManifest")
  manifest_category = manifest.get("strategyCategory") if isinstance(manifest, dict) else None
  for value in (
    result.get("strategyCategory"), manifest_category,
    requested_params.get("strategyCategory"),
  ):
    if value in {"core_position_overlay", "directional_holding"}:
      return str(value)
  return (
    "core_position_overlay"
    if int(requested_params.get("baseQuantity") or 0) > 0
    or bool(requested_params.get("t0Enabled"))
    else "directional_holding"
  )


def closed_loop_trade_pnl(trade: dict[str, Any], is_overlay: bool) -> Any:
  status = trade.get("status")
  if status and status != "filled":
    return ""
  if is_overlay:
    if trade.get("cycleEffect") not in _OVERLAY_CLOSE_EFFECTS:
      return ""
    pnl = trade.get("realizedOverlayPnl")
    return pnl if isinstance(pnl, (int, float)) and math.isfinite(pnl) else ""
  action = str(trade.get("action") or trade.get("direction") or "").upper()
  pnl = trade.get("pnl")
  if not _CLOSED_TRADE_ACTION_PATTERN.search(action) and pnl == 0:
    return ""
  return pnl if isinstance(pnl, (int, float)) and math.isfinite(pnl) else ""


def qualify_result(
  status: str,
  result_json: Any,
  requested_params: dict[str, Any],
  fill_model: str,
) -> tuple[dict[str, Any] | None, str | None]:
  """返回规范结果，或稳定排除原因。"""
  if status != "succeeded":
    return None, "run_not_succeeded"
  if result_json is None:
    return None, "result_missing"
  if not isinstance(result_json, dict):
    return None, "result_incompatible"
  try:
    result = result_json
    if fill_model == "l2_quote":
      from backend.l2.result_adapter import normalize_l2_result

      result = normalize_l2_result(result_json, requested_params)
  except Exception:
    return None, "result_incompatible"
  if not isinstance(result, dict) or not isinstance(result.get("trades"), list):
    return None, "result_incompatible"
  skipped = result.get("skippedSignals", [])
  if not isinstance(skipped, list):
    return None, "result_incompatible"
  if len(result["trades"]) + len(skipped) == 0:
    return None, "no_signal_details"
  return result, None


def signal_detail_rows(
  result: dict[str, Any],
  requested_params: dict[str, Any],
) -> list[dict[str, Any]]:
  trades = [*result.get("trades", []), *result.get("skippedSignals", [])]
  if any(not isinstance(trade, dict) for trade in trades):
    raise SignalDetailSerializationError("信号明细包含非对象记录")
  trades.sort(key=lambda trade: _javascript_string(
    trade.get("signalTime") or trade.get("time", _MISSING)
  ))
  is_overlay = _strategy_category(result, requested_params) == "core_position_overlay"
  return [{
    "signalTime": format_signal_time(trade.get("signalTime") or trade.get("time", _MISSING)),
    "executionTime": format_signal_time(
      trade.get("executionTime") or trade.get("time", _MISSING)
    ),
    "status": "已跳过" if trade.get("status") == "skipped" else "已成交",
    "direction": trade.get("direction", _MISSING),
    "side": trade.get("side", _MISSING),
    "action": trade.get("action") or trade.get("direction", _MISSING),
    "price": trade.get("price", _MISSING),
    "priceSource": trade.get("priceSource") or "成交模型价格",
    "quantity": trade.get("quantity", _MISSING),
    "pnl": closed_loop_trade_pnl(trade, is_overlay),
    "researchSignalId": trade.get("researchSignalId") or "",
    "researchSignalAction": trade.get("researchSignalAction") or "",
    "researchSignalEffectiveAt": trade.get("researchSignalEffectiveAt") or "",
    "note": trade.get("note", _MISSING),
  } for trade in trades]


def _escape_cell(value: Any) -> str:
  return f'"{_javascript_string(value).replace(chr(34), chr(34) * 2)}"'


def iter_csv_chunks(
  result: dict[str, Any],
  requested_params: dict[str, Any],
) -> Iterator[bytes]:
  yield ("\ufeff" + ",".join(CSV_HEADERS) + "\n").encode("utf-8")
  rows = signal_detail_rows(result, requested_params)
  for index, row in enumerate(rows):
    yield (
      ",".join(_escape_cell(row[header]) for header in CSV_HEADERS)
      + ("\n" if index < len(rows) - 1 else "")
    ).encode("utf-8")


def csv_bytes(result: dict[str, Any], requested_params: dict[str, Any]) -> bytes:
  return b"".join(iter_csv_chunks(result, requested_params))


def safe_csv_filename(symbol: str, strategy_name: str, display_no: int) -> str:
  safe_symbol = _safe_filename_component(symbol)
  safe_strategy_name = _safe_filename_component(strategy_name)
  if not safe_symbol:
    raise SignalDetailSerializationError("回测冻结快照缺少标的")
  if not safe_strategy_name:
    raise SignalDetailSerializationError("回测冻结快照缺少策略方案名称")
  return f"{safe_symbol}_{safe_strategy_name}_{display_no}.csv"


def _safe_filename_component(value: str) -> str:
  sanitized = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", value.strip())
  return sanitized.strip(" ._")
