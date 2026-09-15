# -*- coding: utf-8 -*-
"""共享回测附件的受控文件复制与摘要校验。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
from uuid import uuid4


_MAX_ARTIFACT_COUNT = 20
_MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_MAX_TOTAL_BYTES = 500 * 1024 * 1024


class SharedArtifactError(ValueError):
  """共享附件缺失、损坏或超出受控范围。"""


def artifact_root() -> Path:
  return Path(os.getenv("TREND_GRID_OUTPUT_ROOT", "./runtime/artifacts")).resolve()


def safe_filename(value: object) -> str:
  name = Path(str(value or "attachment.bin").replace("\\", "/")).name.strip()
  return name[:255] or "attachment.bin"


def resolve_storage_key(storage_key: str) -> Path:
  root = artifact_root()
  target = (root / storage_key).resolve()
  try:
    target.relative_to(root)
  except ValueError as error:
    raise SharedArtifactError("共享附件存储位置无效") from error
  return target


def storage_key_for_path(path: Path) -> str:
  target = path.resolve()
  try:
    return target.relative_to(artifact_root()).as_posix()
  except ValueError as error:
    raise SharedArtifactError("附件文件不在受控存储目录中") from error


def verified_artifact_path(
  storage_key: str,
  expected_sha256: str,
  expected_size: int,
) -> Path:
  target = resolve_storage_key(storage_key)
  if not target.is_file() or target.stat().st_size != expected_size:
    raise SharedArtifactError("共享附件不存在或大小不一致")
  with target.open("rb") as handle:
    digest = hashlib.file_digest(handle, "sha256").hexdigest()
  if digest != expected_sha256:
    raise SharedArtifactError("共享附件内容摘要校验失败")
  return target


def research_signal_snapshot_hash(storage_key: str) -> str:
  """计算与研究信号冻结服务一致的规范化行摘要。"""
  try:
    with resolve_storage_key(storage_key).open("r", encoding="utf-8") as handle:
      rows = [json.loads(line) for line in handle if line.strip()]
  except (OSError, json.JSONDecodeError) as error:
    raise SharedArtifactError("研究信号附件内容无效") from error
  if not all(isinstance(row, dict) for row in rows):
    raise SharedArtifactError("研究信号附件内容无效")
  encoded = "\n".join(
    json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for row in rows
  )
  return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def copy_artifacts(
  sources: list[dict[str, Any]],
  namespace: str,
) -> list[dict[str, Any]]:
  """复制并校验一组附件；失败时删除本次已经生成的文件。"""
  if len(sources) > _MAX_ARTIFACT_COUNT:
    raise SharedArtifactError("共享附件数量超过限制")
  total_size = sum(int(source.get("byte_size") or 0) for source in sources)
  if total_size > _MAX_TOTAL_BYTES:
    raise SharedArtifactError("共享附件总大小超过限制")
  copied: list[dict[str, Any]] = []
  try:
    for source in sources:
      copied.append(_copy_artifact(source, namespace))
    return copied
  except Exception:
    cleanup_artifacts(copied)
    raise


def _copy_artifact(source: dict[str, Any], namespace: str) -> dict[str, Any]:
  byte_size = int(source.get("byte_size") or 0)
  if byte_size < 0 or byte_size > _MAX_ARTIFACT_BYTES:
    raise SharedArtifactError("单个共享附件大小超过限制")
  expected_digest = str(source.get("content_sha256") or "")
  source_path = verified_artifact_path(
    str(source.get("storage_key") or ""), expected_digest, byte_size,
  )
  artifact_id = str(uuid4())
  filename = safe_filename(source.get("original_filename"))
  storage_key = f"{namespace.strip('/')}/{artifact_id}-{filename}"
  target = resolve_storage_key(storage_key)
  target.parent.mkdir(parents=True, exist_ok=True)
  temporary_name: str | None = None
  try:
    with source_path.open("rb") as source_handle:
      with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as temporary:
        shutil.copyfileobj(source_handle, temporary)
        temporary_name = temporary.name
    temporary_path = Path(temporary_name)
    with temporary_path.open("rb") as handle:
      copied_digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if temporary_path.stat().st_size != byte_size or copied_digest != expected_digest:
      raise SharedArtifactError("共享附件复制后的内容摘要不一致")
    os.replace(temporary_path, target)
    temporary_name = None
  finally:
    if temporary_name:
      Path(temporary_name).unlink(missing_ok=True)
  return {
    **source,
    "id": artifact_id,
    "storage_key": storage_key,
    "original_filename": filename,
    "byte_size": byte_size,
    "content_sha256": expected_digest,
  }


def cleanup_artifacts(artifacts: list[dict[str, Any]]) -> None:
  """只清理本次计划中明确列出的文件。"""
  for artifact in artifacts:
    storage_key = artifact.get("storage_key")
    if not isinstance(storage_key, str):
      continue
    try:
      resolve_storage_key(storage_key).unlink(missing_ok=True)
    except (OSError, SharedArtifactError):
      continue
