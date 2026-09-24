"""按任务 UUID 输出受控、脱敏且有界的诊断时间线。"""

from __future__ import annotations

import argparse
import json
from typing import Sequence
from uuid import UUID

from backend import settings
from backend.services import state_store, task_diagnostics


def _uuid(value: str) -> str:
  try:
    return str(UUID(value))
  except ValueError as error:
    raise argparse.ArgumentTypeError("必须提供有效 UUID") from error


def main(argv: Sequence[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("run_id", type=_uuid, help="回测任务 UUID")
  parser.add_argument("--attempt-token", type=_uuid, help="只查询指定 Attempt UUID")
  args = parser.parse_args(argv)
  settings.validate_observability_configuration()
  state_store.ensure_schema()
  try:
    diagnostics = task_diagnostics.get_admin_run_diagnostics(
      args.run_id,
      args.attempt_token,
    )
  except KeyError:
    parser.error("未找到该回测任务")
  print(json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":")))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
