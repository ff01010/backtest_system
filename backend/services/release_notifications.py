# -*- coding: utf-8 -*-
"""应用发布状态、公告回执和历史记录的业务边界。"""

import base64
import binascii
from datetime import datetime
import hashlib
import json
from typing import Any

from backend import settings
from backend.services import state_store
from backend.time_utils import format_shanghai_iso_datetime


class ReleaseStateUnavailable(RuntimeError):
  """部署状态尚未初始化或与运行构建不一致。"""


def _release_payload(row: dict[str, Any]) -> dict[str, Any] | None:
  release_id = row.get("current_notice_id") or row.get("id")
  if release_id is None or row.get("status") != "published":
    return None
  return {
    "id": str(release_id),
    "releaseNo": int(row["release_no"]),
    "version": row["version"],
    "title": row["title"],
    "summary": row.get("summary") or "",
    "highlights": list(row.get("highlights") or []),
    "level": row["level"],
    "publishedAt": format_shanghai_iso_datetime(row["published_at"]),
  }


def get_release_status(client_build_id: str, client_version: int) -> tuple[dict[str, Any], str]:
  """构造当前用户的发布状态和个性化 ETag。"""
  row = state_store.get_release_status_record()
  if row is None:
    raise ReleaseStateUnavailable("部署状态尚未初始化")
  server_build_id = str(row["current_build_id"]).strip()
  if settings.APPLICATION_BUILD_ID != "dev" and server_build_id != settings.APPLICATION_BUILD_ID:
    raise ReleaseStateUnavailable("运行构建与部署状态不一致")
  release = _release_payload(row)
  client_outdated = client_build_id != "dev" and client_build_id != server_build_id
  required_version = int(row["required_client_version"])
  upgrade_required = client_build_id != "dev" and client_version < required_version
  acknowledged = bool(row.get("acknowledged")) if release else False
  unread = release is not None and not acknowledged
  activation_kind = row["activation_kind"]
  if upgrade_required:
    presentation_mode = "mandatory_gate"
  elif activation_kind == "rollback" and client_outdated:
    presentation_mode = "rollback_mismatch"
  elif release is not None:
    presentation_mode = "release_notice"
  elif client_outdated:
    presentation_mode = "version_mismatch"
  else:
    presentation_mode = "none"
  payload = {
    "serverBuildId": server_build_id,
    "requiredClientCompatibilityVersion": required_version,
    "clientOutdated": client_outdated,
    "activationKind": activation_kind,
    "presentationMode": presentation_mode,
    "acknowledged": acknowledged,
    "unread": unread,
    "upgradeRequired": upgrade_required,
    "release": release,
  }
  etag_source = {
    "buildId": server_build_id,
    "requiredVersion": required_version,
    "activationKind": activation_kind,
    "noticeId": str(row.get("current_notice_id") or ""),
    "manifestDigest": str(row.get("manifest_digest") or "").strip(),
    "status": row.get("status") or "",
    "acknowledged": acknowledged,
  }
  encoded = json.dumps(etag_source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  return payload, f'"{hashlib.sha256(encoded.encode("utf-8")).hexdigest()}"'


def acknowledge_release(release_id: str, client_build_id: str) -> dict[str, Any]:
  """写入当前用户回执；公告已失效时报告冲突。"""
  if not state_store.acknowledge_release(release_id, client_build_id):
    raise state_store.ConflictError("release.notice_stale")
  return {"acknowledged": True, "releaseId": release_id}


def _encode_cursor(published_at: datetime, release_no: int) -> str:
  value = json.dumps(
    [published_at.isoformat(), release_no], ensure_ascii=True, separators=(",", ":"),
  )
  return base64.urlsafe_b64encode(value.encode("ascii")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[datetime, int] | None:
  if not cursor:
    return None
  try:
    padded = cursor + "=" * (-len(cursor) % 4)
    value = json.loads(base64.urlsafe_b64decode(padded).decode("ascii"))
    if not isinstance(value, list) or len(value) != 2:
      raise ValueError("更新记录游标结构无效")
    published_at = datetime.fromisoformat(value[0])
    release_no = int(value[1])
    if published_at.tzinfo is None or release_no < 1:
      raise ValueError("更新记录游标内容无效")
    return published_at, release_no
  except (binascii.Error, TypeError, ValueError, UnicodeDecodeError) as error:
    raise ValueError("更新记录游标无效") from error


def list_published_releases(limit: int, cursor: str | None) -> dict[str, Any]:
  """读取用户可见发布历史。"""
  rows = state_store.list_release_records(limit + 1, _decode_cursor(cursor))
  has_more = len(rows) > limit
  visible = rows[:limit]
  items = []
  for row in visible:
    shaped = dict(row)
    shaped["current_notice_id"] = shaped.pop("id")
    shaped["status"] = "published"
    items.append(_release_payload(shaped))
  next_cursor = _encode_cursor(
    visible[-1]["published_at"], int(visible[-1]["release_no"]),
  ) if has_more and visible else None
  return {"items": items, "nextCursor": next_cursor}
