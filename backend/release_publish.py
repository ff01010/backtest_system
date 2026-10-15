# -*- coding: utf-8 -*-
"""可信部署环境使用的发布状态管理命令。"""

import argparse
import json
import re
import sys
from typing import Any

from backend.release_manifest import (
  canonical_manifest_bytes,
  read_manifest,
  validate_manifest,
)
from backend.services import state_store


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

  experiment_epoch = commands.add_parser("activate-experiment-epoch")
  experiment_epoch.add_argument("--expected-epoch", required=True)
  experiment_epoch.add_argument("--target-epoch", required=True)
  experiment_epoch.add_argument("--build-id", required=True)
  experiment_epoch.add_argument("--actor", required=True)
  experiment_epoch.add_argument("--detail-json", default="{}")
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
  if args.command == "activate-experiment-epoch":
    build_id = validate_build_id(args.build_id)
    try:
      detail = json.loads(args.detail_json)
    except json.JSONDecodeError as error:
      raise ValueError("detail-json 必须是 JSON 对象") from error
    if not isinstance(detail, dict):
      raise ValueError("detail-json 必须是 JSON 对象")
    activated = state_store.activate_experiment_contract_epoch(
      args.expected_epoch,
      args.target_epoch,
      build_id,
      args.actor,
      detail,
    )
    return {"contractEpoch": activated, "buildId": build_id}
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
