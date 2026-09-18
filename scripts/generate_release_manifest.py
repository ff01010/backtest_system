# -*- coding: utf-8 -*-
"""根据 Git 提交生成待审核的发布公告清单。"""

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 允许从 scripts 目录直接执行，同时复用后端发布清单模块。
sys.path.insert(0, str(PROJECT_ROOT))

from backend import release_manifest  # noqa: E402


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="根据 Git 提交生成发布公告草稿")
  parser.add_argument("--base", required=True, help="公告范围的起始提交或分支")
  parser.add_argument("--target", default="HEAD", help="公告范围的目标提交")
  parser.add_argument(
    "--output",
    type=Path,
    default=PROJECT_ROOT / "deploy" / "release-manifest.json",
    help="发布清单输出路径",
  )
  parser.add_argument("--version", help="展示版本号；默认使用目标提交日期")
  parser.add_argument(
    "--client-compatibility-version",
    type=int,
    help="客户端兼容版本；默认沿用现有清单",
  )
  return parser


def _existing_client_version(path: Path) -> int:
  if not path.exists():
    return 1
  content = json.loads(path.read_text(encoding="utf-8"))
  value = content.get("clientCompatibilityVersion", 1)
  if not isinstance(value, int) or value < 1:
    raise ValueError("现有发布清单的客户端兼容版本无效")
  return value


def main(arguments: Sequence[str] | None = None) -> int:
  args = _parser().parse_args(arguments)
  output = args.output.resolve()
  try:
    previous_version = _existing_client_version(output)
    client_version = (
      args.client_compatibility_version
      if args.client_compatibility_version is not None
      else previous_version
    )
    manifest = release_manifest.generate_manifest(
      PROJECT_ROOT,
      args.base,
      args.target,
      client_compatibility_version=client_version,
      previous_client_compatibility_version=previous_version,
      version=args.version,
    )
    release_manifest.write_manifest(output, manifest)
  except (OSError, ValueError, json.JSONDecodeError) as error:
    print(f"公告生成失败：{error}", file=sys.stderr)
    return 1
  print(f"公告草稿已生成：{output}")
  print(json.dumps(manifest, ensure_ascii=False, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
