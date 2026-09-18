# -*- coding: utf-8 -*-
"""Git 发布公告生成器的关键回归测试。"""

from pathlib import Path
import subprocess

import pytest

from backend import release_manifest


def _git(repo: Path, *args: str) -> str:
  result = subprocess.run(
    ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    encoding="utf-8",
  )
  return result.stdout.strip()


def _commit(repo: Path, subject: str, body: str = "") -> str:
  marker = repo / "marker.txt"
  previous = marker.read_text(encoding="utf-8") if marker.exists() else ""
  marker.write_text(f"{previous}{subject}\n", encoding="utf-8")
  _git(repo, "add", "marker.txt")
  command = ["commit", "-m", subject]
  if body:
    command.extend(["-m", body])
  _git(repo, *command)
  return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, str]:
  repo = tmp_path / "repo"
  repo.mkdir()
  _git(repo, "init")
  _git(repo, "config", "user.name", "Release Test")
  _git(repo, "config", "user.email", "release@example.invalid")
  base = _commit(repo, "chore: 初始化")
  return repo, base


def test_generate_manifest_uses_user_facing_commits_and_trailers(
  tmp_path: Path,
) -> None:
  repo, base = _repository(tmp_path)
  _commit(repo, "feat: 支持北交所股票回测")
  _commit(repo, "fix: 支持北交所股票回测")
  _commit(repo, "fix: 修复保存错误提示")
  _commit(repo, "docs: 更新内部说明")
  _commit(
    repo,
    "refactor: 调整内部发布模块",
    "Release-Note: 优化版本更新提醒\nRelease-Level: important",
  )
  _commit(repo, "fix: 不应公开的内部修复", "Release-Note: skip")

  manifest = release_manifest.generate_manifest(
    repo, base, "HEAD", client_compatibility_version=1,
  )

  assert manifest["title"] == "功能与体验更新"
  assert manifest["highlights"] == [
    "支持北交所股票回测",
    "优化版本更新提醒",
    "修复保存错误提示",
  ]
  assert manifest["level"] == "important"
  assert manifest["notifyUsers"] is True


def test_generate_manifest_creates_silent_release_without_public_changes(
  tmp_path: Path,
) -> None:
  repo, base = _repository(tmp_path)
  _commit(repo, "test: 补充内部回归测试")

  manifest = release_manifest.generate_manifest(
    repo, base, "HEAD", client_compatibility_version=1,
  )

  assert manifest["highlights"] == []
  assert manifest["level"] == "normal"
  assert manifest["notifyUsers"] is False


def test_mandatory_release_requires_compatibility_version_increment(
  tmp_path: Path,
) -> None:
  repo, base = _repository(tmp_path)
  _commit(
    repo,
    "feat!: 调整客户端契约",
    "Release-Note: 客户端需要刷新后继续使用\nRelease-Level: mandatory",
  )

  with pytest.raises(ValueError, match="客户端兼容版本"):
    release_manifest.generate_manifest(
      repo,
      base,
      "HEAD",
      client_compatibility_version=1,
      previous_client_compatibility_version=1,
    )
