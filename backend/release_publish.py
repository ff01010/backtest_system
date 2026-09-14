# -*- coding: utf-8 -*-
"""可信部署环境使用的发布状态管理命令。"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from backend.services import state_store


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
  if not isinstance(manifest["clientCompatibilityVersion"], int) or manifest["clientCompatibilityVersion"] < 1:
    raise ValueError("clientCompatibilityVersion 必须为正整数")
  if not isinstance(manifest["title"], str) or not 1 <= len(manifest["title"]) <= 128:
    raise ValueError("title 长度必须为 1 至 128")
  if not isinstance(manifest["summary"], str) or len(manifest["summary"]) > 1000:
    raise ValueError("summary 长度不能超过 1000")
  highlights = manifest["highlights"]
  if not isinstance(highlights, list) or len(highlights) > 20:
    raise ValueError("highlights 必须为不超过 20 条的数组")
  if any(not isinstance(item, str) or not 1 <= len(item) <= 300 for item in highlights):
    raise ValueError("每条 highlight 长度必须为 1 至 300")
  if manifest["level"] not in {"normal", "important", "mandatory"}:
    raise ValueError("level 无效")
  if not isinstance(manifest["notifyUsers"], bool):
    raise ValueError("notifyUsers 必须为布尔值")
  if manifest["level"] == "mandatory" and not manifest["notifyUsers"]:
    raise ValueError("mandatory 发布必须通知用户")
  return manifest


def read_manifest(path: str) -> tuple[dict[str, Any], str]:
  """读取并计算发布清单摘要。"""
  manifest = validate_manifest(json.loads(Path(path).read_text(encoding="utf-8")))
  digest = hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()
  return manifest, digest


def validate_build_id(build_id: str) -> str:
  if not re.fullmatch(r"[0-9a-f]{40}", build_id):
    raise ValueError("buildId 必须为 40 位小写 Git SHA")
  return build_id


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="管理应用发布状态")
  commands = parser.add_subparsers(dest="command", required=True)

  bootstrap = commands.add_parser("bootstrap")
  bootstrap.add_argument("--build-id", required=True)
  bootstrap.add_argument("--client-version", required=True, type=int)
  bootstrap.add_argument("--deployment-id", required=True)

  activate = commands.add_parser("activate")
  activate.add_argument("--manifest", required=True)
  activate.add_argument("--build-id", required=True)
  activate.add_argument("--deployment-id", required=True)
  activate.add_argument("--expected-generation", required=True, type=int)

  silent = commands.add_parser("activate-silent")
  silent.add_argument("--build-id", required=True)
  silent.add_argument("--client-version", required=True, type=int)
  silent.add_argument("--deployment-id", required=True)
  silent.add_argument("--expected-generation", required=True, type=int)
  silent_notice = silent.add_mutually_exclusive_group(required=True)
  silent_notice.add_argument("--keep-notice", action="store_true")
  silent_notice.add_argument("--no-notice", action="store_true")

  rollback = commands.add_parser("rollback")
  rollback.add_argument("--build-id", required=True)
  rollback.add_argument("--client-version", required=True, type=int)
  rollback.add_argument("--deployment-id", required=True)
  rollback.add_argument("--expected-generation", required=True, type=int)
  rollback_notice = rollback.add_mutually_exclusive_group(required=True)
  rollback_notice.add_argument("--release-id")
  rollback_notice.add_argument("--no-notice", action="store_true")

  withdraw = commands.add_parser("withdraw-notice")
  withdraw.add_argument("--release-id", required=True)
  withdraw.add_argument("--deployment-id", required=True)
  withdraw.add_argument("--expected-generation", required=True, type=int)
  return parser


def run_command(args: argparse.Namespace) -> dict[str, Any]:
  """执行已经解析的发布管理命令。"""
  if args.command == "bootstrap":
    return state_store.bootstrap_deployment(
      validate_build_id(args.build_id), args.client_version, args.deployment_id,
    )
  if args.command == "activate":
    manifest, digest = read_manifest(args.manifest)
    build_id = validate_build_id(args.build_id)
    if not manifest["notifyUsers"]:
      return state_store.activate_deployment_without_notice(
        build_id, manifest["clientCompatibilityVersion"], args.deployment_id,
        args.expected_generation, "silent", True,
      )
    return state_store.activate_release_record(
      manifest, build_id, digest, args.deployment_id, args.expected_generation,
    )
  if args.command == "activate-silent":
    return state_store.activate_deployment_without_notice(
      validate_build_id(args.build_id), args.client_version, args.deployment_id,
      args.expected_generation, "silent", args.keep_notice,
    )
  if args.command == "rollback":
    return state_store.rollback_deployment(
      validate_build_id(args.build_id), args.client_version, args.deployment_id,
      args.expected_generation, args.release_id,
    )
  return state_store.withdraw_release_record(
    args.release_id, args.deployment_id, args.expected_generation,
  )


def main() -> int:
  try:
    result = run_command(_parser().parse_args())
  except (ValueError, state_store.DatabaseError) as error:
    print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
    return 1
  print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
