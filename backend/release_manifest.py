# -*- coding: utf-8 -*-
"""发布清单校验与基于 Git 提交的公告草稿生成。"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Sequence


_COMMIT_PATTERN = re.compile(
  r"^(?P<type>feat|fix|docs|test|refactor|chore|build|ci|perf)"
  r"(?:\([^)]*\))?!?:\s*(?P<title>.+)$",
)
_LEVEL_PRIORITY = {"normal": 0, "important": 1, "mandatory": 2}
_MAX_HIGHLIGHTS = 20


@dataclass(frozen=True)
class _Commit:
  subject: str
  body: str


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
  """返回跨环境稳定的 manifest 规范字节。"""
  return json.dumps(
    manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
  """校验用户公告清单，不允许部署时猜测缺失字段。"""
  required = {
    "schemaVersion", "version", "clientCompatibilityVersion", "title",
    "summary", "highlights", "level", "notifyUsers",
  }
  if set(manifest) != required:
    raise ValueError("发布清单字段不完整或包含未知字段")
  if manifest["schemaVersion"] != 1:
    raise ValueError("schemaVersion 只能为 1")
  if not isinstance(manifest["version"], str) or not 1 <= len(manifest["version"]) <= 32:
    raise ValueError("version 长度必须为 1 至 32")
  client_version = manifest["clientCompatibilityVersion"]
  if not isinstance(client_version, int) or client_version < 1:
    raise ValueError("clientCompatibilityVersion 必须为正整数")
  if not isinstance(manifest["title"], str) or not 1 <= len(manifest["title"]) <= 128:
    raise ValueError("title 长度必须为 1 至 128")
  if not isinstance(manifest["summary"], str) or len(manifest["summary"]) > 1000:
    raise ValueError("summary 长度不能超过 1000")
  highlights = manifest["highlights"]
  if not isinstance(highlights, list) or len(highlights) > _MAX_HIGHLIGHTS:
    raise ValueError("highlights 必须为不超过 20 条的数组")
  if any(not isinstance(item, str) or not 1 <= len(item) <= 300 for item in highlights):
    raise ValueError("每条 highlight 长度必须为 1 至 300")
  if manifest["level"] not in _LEVEL_PRIORITY:
    raise ValueError("level 无效")
  if not isinstance(manifest["notifyUsers"], bool):
    raise ValueError("notifyUsers 必须为布尔值")
  if manifest["level"] == "mandatory" and not manifest["notifyUsers"]:
    raise ValueError("mandatory 发布必须通知用户")
  return manifest


def read_manifest(path: str | Path) -> tuple[dict[str, Any], str]:
  """读取并计算发布清单摘要。"""
  manifest = validate_manifest(
    json.loads(Path(path).read_text(encoding="utf-8")),
  )
  digest = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
  return manifest, digest


def _run_git(repository: Path, arguments: Sequence[str]) -> str:
  try:
    result = subprocess.run(
      ["git", *arguments], cwd=repository, check=True, capture_output=True,
      text=True, encoding="utf-8", errors="replace",
    )
  except subprocess.CalledProcessError as error:
    detail = (error.stderr or error.stdout).strip()
    raise ValueError(f"Git 命令执行失败：{detail}") from error
  return result.stdout


def _read_commits(repository: Path, base: str, target: str) -> list[_Commit]:
  _run_git(repository, ["rev-parse", "--verify", f"{base}^{{commit}}"])
  _run_git(repository, ["rev-parse", "--verify", f"{target}^{{commit}}"])
  output = _run_git(
    repository,
    [
      "log", "--reverse", "--no-merges", "--format=%s%x1f%b%x1e",
      f"{base}..{target}",
    ],
  )
  commits: list[_Commit] = []
  for record in output.split("\x1e"):
    record = record.strip("\r\n")
    if not record:
      continue
    subject, separator, body = record.partition("\x1f")
    if not separator:
      raise ValueError("Git 提交记录格式无效")
    commits.append(_Commit(subject.strip(), body.strip()))
  return commits


def _read_trailer(body: str, name: str) -> str | None:
  pattern = re.compile(rf"^{re.escape(name)}:\s*(.+?)\s*$", re.MULTILINE)
  matches = pattern.findall(body)
  return matches[-1].strip() if matches else None


def _append_unique(items: list[str], value: str) -> None:
  normalized = value.strip()
  if normalized and normalized not in items:
    items.append(normalized)


def _summarize_commits(commits: Sequence[_Commit]) -> tuple[list[str], list[str], str]:
  features: list[str] = []
  fixes: list[str] = []
  level = "normal"
  for commit in commits:
    note = _read_trailer(commit.body, "Release-Note")
    if note and note.lower() == "skip":
      continue
    match = _COMMIT_PATTERN.match(commit.subject)
    commit_type = match.group("type") if match else ""
    if note:
      public_text = note
    elif match and commit_type in {"feat", "fix"}:
      public_text = match.group("title").strip()
    else:
      continue
    release_level = _read_trailer(commit.body, "Release-Level") or "normal"
    if release_level not in _LEVEL_PRIORITY:
      raise ValueError(f"无效 Release-Level：{release_level}")
    if _LEVEL_PRIORITY[release_level] > _LEVEL_PRIORITY[level]:
      level = release_level
    if public_text in features or public_text in fixes:
      continue
    target = fixes if commit_type == "fix" else features
    _append_unique(target, public_text)
  return features, fixes, level


def _release_copy(features: Sequence[str], fixes: Sequence[str]) -> tuple[str, str]:
  if features and fixes:
    return (
      "功能与体验更新",
      f"本次更新包含 {len(features)} 项功能改进和 {len(fixes)} 项问题修复。",
    )
  if features:
    return "功能更新", f"本次更新包含 {len(features)} 项功能改进。"
  if fixes:
    return "问题修复", f"本次更新包含 {len(fixes)} 项问题修复。"
  return "内部维护更新", "本次发布仅包含内部维护调整，不展示用户公告。"


def _target_version(repository: Path, target: str) -> str:
  timestamp = _run_git(repository, ["show", "-s", "--format=%cI", target]).strip()
  try:
    committed_at = datetime.fromisoformat(timestamp)
  except ValueError as error:
    raise ValueError("无法读取目标提交日期") from error
  return committed_at.strftime("%Y.%m.%d")


def generate_manifest(
  repository: Path,
  base: str,
  target: str,
  *,
  client_compatibility_version: int,
  previous_client_compatibility_version: int | None = None,
  version: str | None = None,
) -> dict[str, Any]:
  """从指定 Git 提交范围生成可审核的发布清单。"""
  commits = _read_commits(repository, base, target)
  features, fixes, level = _summarize_commits(commits)
  if level == "mandatory":
    if previous_client_compatibility_version is None:
      raise ValueError("mandatory 发布必须提供上一客户端兼容版本")
    if client_compatibility_version <= previous_client_compatibility_version:
      raise ValueError("mandatory 发布必须递增客户端兼容版本")
  title, summary = _release_copy(features, fixes)
  highlights = [*features, *fixes][:_MAX_HIGHLIGHTS]
  return validate_manifest({
    "schemaVersion": 1,
    "version": version or _target_version(repository, target),
    "clientCompatibilityVersion": client_compatibility_version,
    "title": title,
    "summary": summary,
    "highlights": highlights,
    "level": level,
    "notifyUsers": bool(highlights),
  })


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
  """原子写入已经校验的发布清单。"""
  validate_manifest(manifest)
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_suffix(f"{path.suffix}.tmp")
  temporary.write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
  )
  temporary.replace(path)
