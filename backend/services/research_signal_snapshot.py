# -*- coding: utf-8 -*-
"""Fetch, validate and freeze published research signals before a backtest runs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_SIGNAL_API_URL = os.getenv("RESEARCH_SIGNAL_API_URL", "").rstrip("/")
_SERVICE_TOKEN = os.getenv("RESEARCH_SIGNAL_SERVICE_TOKEN", "")
_CANONICAL_FIELDS = (
  "signalId", "instrument", "timeframe", "signalTime", "availableAt", "effectiveAt", "sequence",
  "predictedLabelId", "action", "modelVersionId", "sourceHash", "diagnostic",
)


class ResearchSignalSnapshotError(ValueError):
  """The published signal source cannot safely be used by a backtest."""


def freeze_signal_snapshot(
  dataset_id: str,
  user_id: str,
  symbol: str,
  start_date: str,
  end_date: str,
  timeframe: str,
  output_root: str | None = None,
) -> dict[str, Any]:
  """Freeze the exact signal rows that can affect one backtest run."""

  manifest = _request_json(f"/internal/v1/signal-datasets/{dataset_id}/manifest", user_id)
  if not isinstance(manifest, dict) or manifest.get("status") != "published":
    raise ResearchSignalSnapshotError("仅已发布信号数据集可以用于回测")
  rows = _request_signal_rows(dataset_id, user_id, symbol, start_date, end_date, timeframe)
  canonical_rows = _select_and_validate_rows(rows, symbol, start_date, end_date, timeframe)
  source_partitions = _select_source_partitions(manifest, canonical_rows)
  content_hash = _snapshot_hash(canonical_rows)
  artifact_file = _write_snapshot(dataset_id, content_hash, canonical_rows, output_root)
  return {
    "datasetId": dataset_id,
    "manifest": manifest,
    "symbol": symbol,
    "timeframe": timeframe,
    "startDate": start_date,
    "endDate": end_date,
    "rowCount": len(canonical_rows),
    "snapshotHash": content_hash,
    "artifactFile": artifact_file,
    "signalOrder": "instrument,effectiveAt,sequence,signalId",
    "sourcePartitions": source_partitions,
  }


def _request_json(path: str, user_id: str) -> Any:
  if not _SIGNAL_API_URL or not _SERVICE_TOKEN:
    raise ResearchSignalSnapshotError("研究信号服务尚未配置")
  request = Request(
    f"{_SIGNAL_API_URL}{path}",
    headers={"X-Service-Token": _SERVICE_TOKEN, "X-User-Id": user_id},
    method="GET",
  )
  try:
    with urlopen(request, timeout=30) as response:
      body = response.read().decode("utf-8")
      if response.headers.get_content_type() == "application/x-ndjson":
        return {"rows": [json.loads(line) for line in body.splitlines() if line.strip()], "nextCursor": response.headers.get("X-Next-Cursor") or None}
      return json.loads(body)
  except (HTTPError, URLError, json.JSONDecodeError) as error:
    raise ResearchSignalSnapshotError("无法读取已发布研究信号") from error


def _request_signal_rows(
  dataset_id: str, user_id: str, symbol: str, start_date: str, end_date: str, timeframe: str,
) -> list[Any]:
  cursor: str | None = None
  rows: list[Any] = []
  while True:
    query = urlencode({
      "instrument": symbol, "timeframe": timeframe, "start_date": start_date, "end_date": end_date,
      "limit": 1_000, **({"cursor": cursor} if cursor else {}),
    })
    page = _request_json(f"/internal/v1/signal-datasets/{dataset_id}/signals?{query}", user_id)
    if isinstance(page, list):
      return page
    if not isinstance(page, dict) or not isinstance(page.get("rows"), list):
      raise ResearchSignalSnapshotError("研究信号接口返回的记录格式无效")
    rows.extend(page["rows"])
    cursor = page.get("nextCursor")
    if not cursor:
      return rows
    if not isinstance(cursor, str):
      raise ResearchSignalSnapshotError("研究信号分页游标无效")


def _select_source_partitions(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  partitions = manifest.get("partitions")
  if not isinstance(partitions, list):
    raise ResearchSignalSnapshotError("信号 manifest 缺少逻辑分区哈希")
  needed = {f"{row['timeframe']}/{row['instrument']}/{_parse_timestamp(row['effectiveAt']).strftime('%Y-%m')}" for row in rows}
  selected = [item for item in partitions if isinstance(item, dict) and item.get("partitionKey") in needed]
  if len(selected) != len(needed) or any(not isinstance(item.get("contentHash"), str) or len(item["contentHash"]) != 64 for item in selected):
    raise ResearchSignalSnapshotError("信号 manifest 分区哈希不完整")
  return selected


def _select_and_validate_rows(
  rows: list[Any], symbol: str, start_date: str, end_date: str, timeframe: str,
) -> list[dict[str, Any]]:
  start = date.fromisoformat(start_date)
  end = date.fromisoformat(end_date)
  selected = [_canonical_signal(row) for row in rows if isinstance(row, dict)]
  selected = [row for row in selected if row["instrument"] == symbol and start <= _event_date(row) <= end]
  if any(row["timeframe"] != timeframe for row in selected):
    raise ResearchSignalSnapshotError("信号周期与回测周期不一致")
  selected.sort(key=lambda row: (row["instrument"], row["effectiveAt"], row["sequence"], row["signalId"]))
  _validate_signal_order(selected)
  return selected


def _canonical_signal(row: dict[str, Any]) -> dict[str, Any]:
  values = {
    "signalId": _value(row, "signal_id", "signalId"),
    "instrument": _value(row, "instrument"),
    "timeframe": _value(row, "timeframe"),
    "signalTime": _timestamp(_value(row, "signal_time", "signalTime")),
    "availableAt": _timestamp(_value(row, "available_at", "availableAt")),
    "effectiveAt": _timestamp(_value(row, "effective_at", "effectiveAt")),
    "sequence": _value(row, "sequence"),
    "predictedLabelId": _value(row, "predicted_label_id", "predictedLabelId"),
    "action": _value(row, "action"),
    "modelVersionId": _value(row, "model_version_id", "modelVersionId"),
    "sourceHash": _value(row, "source_hash", "sourceHash"),
    "diagnostic": _value(row, "diagnostic") or {},
  }
  if not all(isinstance(values[field], str) and values[field] for field in _CANONICAL_FIELDS if field not in {"sequence", "diagnostic"}):
    raise ResearchSignalSnapshotError("信号缺少必填字段")
  if not isinstance(values["sequence"], int) or values["sequence"] < 1:
    raise ResearchSignalSnapshotError("信号 sequence 必须为正整数")
  if values["action"] not in {"long", "short", "flat"}:
    raise ResearchSignalSnapshotError("信号 action 无效")
  if not isinstance(values["diagnostic"], dict):
    raise ResearchSignalSnapshotError("信号 diagnostic 必须为对象")
  if _parse_timestamp(values["availableAt"]) > _parse_timestamp(values["effectiveAt"]):
    raise ResearchSignalSnapshotError("信号 availableAt 晚于 effectiveAt")
  if _parse_timestamp(values["effectiveAt"]).second or _parse_timestamp(values["effectiveAt"]).microsecond:
    raise ResearchSignalSnapshotError("首期日线回测仅接受分钟边界生效的信号")
  return values


def _value(row: dict[str, Any], *names: str) -> Any:
  for name in names:
    if name in row:
      return row[name]
  return None


def _timestamp(value: Any) -> str:
  if not isinstance(value, str):
    raise ResearchSignalSnapshotError("信号时间字段无效")
  return _parse_timestamp(value).isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError as error:
    raise ResearchSignalSnapshotError("信号时间必须是带时区的 ISO 时间戳") from error
  if parsed.tzinfo is None:
    raise ResearchSignalSnapshotError("信号时间必须包含时区")
  return parsed


def _event_date(row: dict[str, Any]) -> date:
  return _parse_timestamp(row["effectiveAt"]).date()


def _validate_signal_order(rows: list[dict[str, Any]]) -> None:
  signal_ids: set[str] = set()
  event_keys: set[tuple[str, str, int]] = set()
  for row in rows:
    event_key = (row["instrument"], row["effectiveAt"], row["sequence"])
    if row["signalId"] in signal_ids or event_key in event_keys:
      raise ResearchSignalSnapshotError("信号标识或同一时点顺序发生冲突")
    signal_ids.add(row["signalId"])
    event_keys.add(event_key)


def _snapshot_hash(rows: list[dict[str, Any]]) -> str:
  encoded = "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for row in rows)
  return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _write_snapshot(dataset_id: str, snapshot_hash: str, rows: list[dict[str, Any]], output_root: str | None) -> str:
  root = Path(output_root or os.getenv("TREND_GRID_OUTPUT_ROOT", "./runtime/artifacts"))
  target = root / "research_signals" / f"{dataset_id}_{snapshot_hash}.ndjson"
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.exists():
    return str(target)
  content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)
  with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as temporary:
    temporary.write(content)
    temporary_name = temporary.name
  os.replace(temporary_name, target)
  return str(target)
