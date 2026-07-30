# -*- coding: utf-8 -*-
"""后端到隔离策略容器的最小客户端。"""

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


WORKER_URL = os.getenv("TREND_GRID_STRATEGY_WORKER_URL", "http://strategy-worker:8001")


class StrategyWorkerError(RuntimeError):
  """策略执行容器不可用或拒绝任务。"""


def run_script(payload: dict[str, Any]) -> dict[str, Any]:
  """同步提交脚本快照和 K 线，返回纯 JSON 执行结果。"""
  request = Request(
    f"{WORKER_URL}/run",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
  )
  try:
    with urlopen(request, timeout=120) as response:
      return json.loads(response.read().decode("utf-8"))
  except HTTPError as error:
    detail = error.read().decode("utf-8", errors="replace")
    raise StrategyWorkerError(f"策略执行失败：{detail}") from error
  except URLError as error:
    raise StrategyWorkerError("策略执行容器不可用") from error
