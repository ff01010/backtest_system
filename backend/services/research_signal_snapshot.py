# -*- coding: utf-8 -*-
"""Fetch, validate, align and freeze published research signals before a backtest runs."""

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
from zoneinfo import ZoneInfo


_SIGNAL_API_URL = os.getenv("RESEARCH_SIGNAL_API_URL", "").rstrip("/")
_SERVICE_TOKEN = os.getenv("RESEARCH_SIGNAL_SERVICE_TOKEN", "")
_CANONICAL_FIELDS = (
  "signalId", "instrument", "timeframe", "signalTime", "availableAt", "effectiveAt", "sequence",
  "predictedLabelId", "action", "modelVersionId", "sourceHash", "diagnostic",
)
# 兼容既有研究信号数据集；该顺序不代表当前可选回测周期。
_TIMEFRAME_ORDER = ("1m", "3m", "5m", "15m", "30m", "45m", "60m", "1d")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ResearchSignalSnapshotError(ValueError):
  """The published signal source cannot safely be used by a backtest."""


def list_published_signal_datasets(user_id: str) -> list[dict[str, Any]]:
  """Return only the fields needed by the backtest selector."""

  payload = _request_json("/internal/v1/signal-datasets", user_id)
  if not isinstance(payload, list):
    raise ResearchSignalSnapshotError("研究信号列表格式无效")
  items: list[dict[str, Any]] = []
  for dataset in payload:
    if not isinstance(dataset, dict) or dataset.get("status") != "published":
      continue
    manifest = _manifest(dataset)
    dataset_id = _value(dataset, "signal_dataset_id", "datasetId")
    if not isinstance(dataset_id, str) or not dataset_id:
      continue
    items.append({
      "id": dataset_id,
      "name": dataset.get("display_name") or dataset.get("displayName") or dataset.get("dataset_key") or dataset_id,
      "datasetKey": dataset.get("dataset_key") or dataset.get("datasetKey"),
      "version": manifest.get("version"),
      "timeframe": manifest.get("timeframe"),
      "recordCount": dataset.get("record_count") or dataset.get("recordCount") or manifest.get("rowCount") or 0,
    })
  return items


def freeze_signal_snapshot(
  dataset_id: str,
  user_id: str,
  symbol: str,
  start_date: str,
  end_date: str,
  timeframe: str,
  bar_times: list[str] | str | None = None,
  output_root: str | None = None,
) -> dict[str, Any]:
  """Freeze exact source rows and their effective Bar alignment for one run."""

  if isinstance(bar_times, str):
    output_root, bar_times = bar_times, None
  resolved_bar_times = bar_times if isinstance(bar_times, list) else []
  dataset = _request_json(f"/internal/v1/signal-datasets/{dataset_id}/manifest", user_id)
  if not isinstance(dataset, dict) or dataset.get("status") != "published":
    raise ResearchSignalSnapshotError("仅已发布信号数据集可以用于回测")
  manifest = _manifest(dataset)
  source_timeframe = manifest.get("timeframe", timeframe)
  if not isinstance(source_timeframe, str):
    raise ResearchSignalSnapshotError("信号 manifest 缺少周期")
  _validate_timeframe_alignment(source_timeframe, timeframe)
  rows = _request_signal_rows(dataset_id, user_id, symbol, start_date, end_date, source_timeframe)
  canonical_rows = _select_and_validate_rows(rows, symbol, start_date, end_date, source_timeframe)
  source_partitions = _select_source_partitions(manifest, canonical_rows)
  content_hash = _snapshot_hash(canonical_rows)
  alignment_plan = _build_alignment_plan(canonical_rows, resolved_bar_times)
  artifact_file = _write_snapshot(dataset_id, content_hash, canonical_rows, output_root)
  return {
    "datasetId": dataset_id,
    "manifest": manifest,
    "symbol": symbol,
    "sourceTimeframe": source_timeframe,
    "timeframe": timeframe,
    "startDate": start_date,
    "endDate": end_date,
    "rowCount": len(canonical_rows),
    "snapshotHash": content_hash,
    "artifactFile": artifact_file,
    "signalOrder": "instrument,effectiveAt,sequence,signalId",
    "sourcePartitions": source_partitions,
    "alignment": {
      "version": "effective-at-first-bar-v1",
      "sourceTimeframe": source_timeframe,
      "backtestTimeframe": timeframe,
      "alignedCount": len(alignment_plan),
      "unmatchedCount": len(canonical_rows) - len(alignment_plan),
    },
    "alignmentPlan": alignment_plan,
  }


def freeze_imported_signal_snapshot(
  dataset: dict[str, Any],
  symbol: str,
  start_date: str,
  end_date: str,
  timeframe: str,
  bar_times: list[str],
) -> dict[str, Any]:
  """从当前用户拥有的持久化附件重新冻结研究信号，不访问原作者服务授权。"""
  artifact_file = dataset.get("artifactFile")
  if not isinstance(artifact_file, str):
    raise ResearchSignalSnapshotError("导入的研究信号附件不可用")
  try:
    with open(artifact_file, "r", encoding="utf-8") as handle:
      rows = [json.loads(line) for line in handle if line.strip()]
  except (OSError, json.JSONDecodeError) as error:
    raise ResearchSignalSnapshotError("导入的研究信号附件不可读取") from error
  source_timeframe = str(dataset.get("timeframe") or timeframe)
  _validate_timeframe_alignment(source_timeframe, timeframe)
  canonical_rows = _select_and_validate_rows(
    rows, symbol, start_date, end_date, source_timeframe,
  )
  manifest = dataset.get("manifest")
  if not isinstance(manifest, dict):
    raise ResearchSignalSnapshotError("导入的研究信号 manifest 无效")
  content_hash = _snapshot_hash(canonical_rows)
  alignment_plan = _build_alignment_plan(canonical_rows, bar_times)
  return {
    "datasetId": str(dataset["id"]),
    "manifest": manifest,
    "symbol": symbol,
    "sourceTimeframe": source_timeframe,
    "timeframe": timeframe,
    "startDate": start_date,
    "endDate": end_date,
    "rowCount": len(canonical_rows),
    "snapshotHash": content_hash,
    "artifactFile": artifact_file,
    "signalOrder": "instrument,effectiveAt,sequence,signalId",
    "sourcePartitions": _select_source_partitions(manifest, canonical_rows),
    "alignment": {
      "version": "effective-at-first-bar-v1",
      "sourceTimeframe": source_timeframe,
      "backtestTimeframe": timeframe,
      "alignedCount": len(alignment_plan),
      "unmatchedCount": len(canonical_rows) - len(alignment_plan),
    },
    "alignmentPlan": alignment_plan,
  }


def align_frozen_signal_snapshot(
  snapshot: dict[str, Any],
  bar_times: list[str],
) -> dict[str, Any]:
  """在 Worker 已读取正式 Bar 后，为冻结信号生成确定性的执行对齐计划。"""
  artifact_file = snapshot.get("artifactFile")
  if not isinstance(artifact_file, str):
    raise ResearchSignalSnapshotError("研究信号冻结附件不可用")
  try:
    with open(artifact_file, "r", encoding="utf-8") as handle:
      rows = [json.loads(line) for line in handle if line.strip()]
  except (OSError, json.JSONDecodeError) as error:
    raise ResearchSignalSnapshotError("研究信号冻结附件不可读取") from error
  if not all(isinstance(row, dict) for row in rows):
    raise ResearchSignalSnapshotError("研究信号冻结附件格式无效")
  symbol = snapshot.get("symbol")
  start_date = snapshot.get("startDate")
  end_date = snapshot.get("endDate")
  source_timeframe = snapshot.get("sourceTimeframe")
  if not all(isinstance(value, str) and value for value in (
    symbol, start_date, end_date, source_timeframe,
  )):
    raise ResearchSignalSnapshotError("研究信号冻结快照元数据无效")
  canonical_rows = _select_and_validate_rows(
    rows, symbol, start_date, end_date, source_timeframe,
  )
  expected_hash = snapshot.get("snapshotHash")
  if isinstance(expected_hash, str) and _snapshot_hash(canonical_rows) != expected_hash:
    raise ResearchSignalSnapshotError("研究信号冻结附件校验失败")
  alignment_plan = _build_alignment_plan(canonical_rows, bar_times)
  aligned = dict(snapshot)
  aligned["alignmentPlan"] = alignment_plan
  aligned["alignment"] = {
    "version": "effective-at-first-bar-v1",
    "sourceTimeframe": snapshot.get("sourceTimeframe"),
    "backtestTimeframe": snapshot.get("timeframe"),
    "alignedCount": len(alignment_plan),
    "unmatchedCount": len(canonical_rows) - len(alignment_plan),
  }
  return aligned


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
        return {
          "rows": [json.loads(line) for line in body.splitlines() if line.strip()],
          "nextCursor": response.headers.get("X-Next-Cursor") or None,
        }
      return json.loads(body)
  except (HTTPError, URLError, json.JSONDecodeError) as error:
    raise ResearchSignalSnapshotError("无法读取已发布研究信号") from error


def _request_signal_rows(
  dataset_id: str,
  user_id: str,
  symbol: str,
  start_date: str,
  end_date: str,
  source_timeframe: str,
) -> list[Any]:
  cursor: str | None = None
  rows: list[Any] = []
  while True:
    query = urlencode({
      "instrument": symbol,
      "timeframe": source_timeframe,
      "start_date": start_date,
      "end_date": end_date,
      "limit": 1_000,
      **({"cursor": cursor} if cursor else {}),
    })
    page = _request_json(f"/internal/v1/signal-datasets/{dataset_id}/signals?{query}", user_id)
    if isinstance(page, list):
      return page
    if not isinstance(page, dict) or not isinstance(page.get("rows"), list):
      raise ResearchSignalSnapshotError("研究信号接口返回的记录格式无效")
    rows.extend(page["rows"])
    cursor = _value(page, "nextCursor", "next_cursor")
    if cursor is None:
      return rows
    if not isinstance(cursor, str) or not cursor.isdigit():
      raise ResearchSignalSnapshotError("研究信号分页游标无效")


def _manifest(dataset: dict[str, Any]) -> dict[str, Any]:
  nested = dataset.get("manifest")
  manifest = dict(nested) if isinstance(nested, dict) else dict(dataset)
  for key in ("version", "status"):
    if key not in manifest and key in dataset:
      manifest[key] = dataset[key]
  return manifest


def _validate_timeframe_alignment(source_timeframe: str, backtest_timeframe: str) -> None:
  try:
    source_rank = _TIMEFRAME_ORDER.index(source_timeframe)
    backtest_rank = _TIMEFRAME_ORDER.index(backtest_timeframe)
  except ValueError as error:
    raise ResearchSignalSnapshotError("信号或回测周期不受支持") from error
  if source_rank < backtest_rank:
    raise ResearchSignalSnapshotError("信号周期不能细于回测周期，请选择相同或更细的回测周期")


def _select_source_partitions(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  partitions = manifest.get("partitions")
  if isinstance(partitions, list):
    needed = {
      f"{row['timeframe']}/{row['instrument']}/{_parse_timestamp(row['effectiveAt']).strftime('%Y-%m')}"
      for row in rows
    }
    selected = [item for item in partitions if isinstance(item, dict) and item.get("partitionKey") in needed]
    if len(selected) == len(needed) and all(
      isinstance(item.get("contentHash"), str) and len(item["contentHash"]) == 64
      for item in selected
    ):
      return selected
  grouped: dict[str, list[dict[str, Any]]] = {}
  for row in rows:
    key = f"{row['timeframe']}/{row['instrument']}/{_parse_timestamp(row['effectiveAt']).strftime('%Y-%m')}"
    grouped.setdefault(key, []).append(row)
  return [{
    "partitionKey": key,
    "contentHash": _snapshot_hash(value),
    "rowCount": len(value),
    "hashSource": "snapshot-derived-v1",
  } for key, value in sorted(grouped.items())]


def _select_and_validate_rows(
  rows: list[Any],
  symbol: str,
  start_date: str,
  end_date: str,
  source_timeframe: str,
) -> list[dict[str, Any]]:
  start = date.fromisoformat(start_date)
  end = date.fromisoformat(end_date)
  selected = [_canonical_signal(row) for row in rows if isinstance(row, dict)]
  selected = [row for row in selected if row["instrument"] == symbol and start <= _event_date(row) <= end]
  if any(row["timeframe"] != source_timeframe for row in selected):
    raise ResearchSignalSnapshotError("信号周期与已发布 manifest 不一致")
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
    "modelVersionId": str(_value(row, "model_version_id", "modelVersionId") or ""),
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
  return values


def _build_alignment_plan(rows: list[dict[str, Any]], bar_times: list[str]) -> list[dict[str, str]]:
  parsed_bars = sorted((_parse_bar_time(value), value) for value in bar_times)
  transitions: dict[str, dict[str, str]] = {}
  cursor = 0
  for row in rows:
    effective_at = _parse_timestamp(row["effectiveAt"])
    while cursor < len(parsed_bars) and parsed_bars[cursor][0] < effective_at:
      cursor += 1
    if cursor >= len(parsed_bars):
      continue
    execution_bar_time = parsed_bars[cursor][1]
    # 同一根 Bar 只能向策略暴露一个当前信号；排序后的最后事件代表该 Bar 的最终可见状态。
    transitions[execution_bar_time] = {
      "signalId": row["signalId"],
      "action": row["action"],
      "effectiveAt": row["effectiveAt"],
      "executionBarTime": execution_bar_time,
    }
  return [transitions[raw_time] for _, raw_time in parsed_bars if raw_time in transitions]


def _parse_bar_time(value: str) -> datetime:
  if len(value) == 8 and value.isdigit():
    return datetime.strptime(value, "%Y%m%d").replace(tzinfo=_SHANGHAI)
  if len(value) in {12, 14} and value.isdigit():
    parsed = datetime.strptime(value, "%Y%m%d%H%M" if len(value) == 12 else "%Y%m%d%H%M%S")
    return parsed.replace(tzinfo=_SHANGHAI)
  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError as error:
    raise ResearchSignalSnapshotError("回测 Bar 时间格式无效") from error
  return parsed.replace(tzinfo=_SHANGHAI) if parsed.tzinfo is None else parsed


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
