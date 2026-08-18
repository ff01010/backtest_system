# -*- coding: utf-8 -*-
"""后端到隔离策略容器的最小客户端。"""

import json
import os
from typing import Any
from http.client import HTTPConnection, HTTPResponse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from backend.services.backtest_errors import RemoteBacktestError


WORKER_URL = os.getenv("TREND_GRID_STRATEGY_WORKER_URL", "http://strategy-worker:8001")


class StrategyWorkerError(RuntimeError):
  """策略执行容器不可用或拒绝任务。"""


def run_script(payload: dict[str, Any]) -> dict[str, Any]:
  """同步提交脚本快照和 K 线，返回纯 JSON 执行结果。"""
  return _request("/run", "POST", payload, timeout=120)


def create_l2_session(payload: dict[str, Any]) -> str:
  """创建隔离 L2 脚本会话并返回其内部标识。"""
  result = _request("/l2/sessions", "POST", payload, timeout=30)
  session_id = result.get("sessionId")
  if not isinstance(session_id, str) or not session_id:
    raise StrategyWorkerError("策略执行容器未返回 L2 会话标识")
  return session_id


def begin_l2_day(session_id: str, payload: dict[str, Any]) -> None:
  """声明即将流入的单个交易日。"""
  _request(f"/l2/sessions/{session_id}/days", "POST", payload, timeout=30)


def process_l2_chunk(session_id: str, payload: dict[str, Any], timeout: int = 90) -> None:
  """发送一个已排序且有界的 L2 数据块。"""
  _request(f"/l2/sessions/{session_id}/chunks", "POST", payload, timeout=timeout)


def stream_l2_day(session_id: str, chunks: Any, timeout: int = 300) -> dict[str, Any]:
  """以单个分块传输请求发送一个交易日，避免频繁 HTTP 往返。"""
  parsed = urlsplit(WORKER_URL)
  if parsed.scheme != "http" or not parsed.hostname:
    raise StrategyWorkerError("策略执行服务地址无效")
  connection = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
  path = f"{parsed.path.rstrip('/')}/l2/sessions/{session_id}/stream"
  try:
    connection.putrequest("POST", path)
    connection.putheader("Content-Type", "application/x-ndjson")
    connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    for chunk in chunks:
      payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
      connection.send(f"{len(payload):X}\r\n".encode("ascii"))
      connection.send(payload)
      connection.send(b"\r\n")
    connection.send(b"0\r\n\r\n")
    response = connection.getresponse()
    return _stream_response(response)
  except (OSError, ValueError) as error:
    raise StrategyWorkerError("策略执行容器流式传输失败") from error
  finally:
    connection.close()


def process_l2_bars(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
  """处理连续 Bar，遇到待成交订单时由 worker 提前返回。"""
  return _request(f"/l2/sessions/{session_id}/bars", "POST", payload, timeout=90)


def process_l2_quote(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
  """处理一条按需读取的盘口快照并返回订单需求状态。"""
  return _request(f"/l2/sessions/{session_id}/quotes", "POST", payload, timeout=30)


def finish_l2_day(session_id: str, trade_date: str) -> None:
  """确认交易日数据流完整。"""
  _request(f"/l2/sessions/{session_id}/days/{trade_date}/finish", "POST", None, timeout=30)


def finish_l2_session(session_id: str) -> dict[str, Any]:
  """关闭会话并取得受限规模的回测结果。"""
  return _request(f"/l2/sessions/{session_id}/finish", "POST", None, timeout=60)


def discard_l2_session(session_id: str) -> None:
  """取消或异常时释放 worker 内存态会话。"""
  try:
    _request(f"/l2/sessions/{session_id}", "DELETE", None, timeout=15)
  except StrategyWorkerError:
    return


def _request(path: str, method: str, payload: dict[str, Any] | None, timeout: int) -> dict[str, Any]:
  body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
  request = Request(
    f"{WORKER_URL}{path}", data=body, headers={"Content-Type": "application/json"}, method=method,
  )
  try:
    with urlopen(request, timeout=timeout) as response:
      return json.loads(response.read().decode("utf-8"))
  except HTTPError as error:
    body = error.read().decode("utf-8", errors="replace")
    try:
      response = json.loads(body)
      detail = response.get("detail") if isinstance(response, dict) else None
      error_detail = detail.get("errorDetail") if isinstance(detail, dict) else None
      if isinstance(error_detail, dict):
        diagnostic = detail.get("diagnostic")
        diagnostic = {
          key: value for key, value in diagnostic.items()
          if isinstance(key, str) and isinstance(value, str)
        } if isinstance(diagnostic, dict) else None
        raise RemoteBacktestError.from_dict(error_detail, diagnostic) from error
    except json.JSONDecodeError:
      pass
    raise StrategyWorkerError("策略执行服务返回异常") from error
  except URLError as error:
    raise StrategyWorkerError("策略执行容器不可用") from error


def _stream_response(response: HTTPResponse) -> dict[str, Any]:
  body = response.read().decode("utf-8", errors="replace")
  if response.status >= 400:
    raise StrategyWorkerError("策略执行服务返回异常")
  try:
    payload = json.loads(body)
  except json.JSONDecodeError as error:
    raise StrategyWorkerError("策略执行服务返回无效响应") from error
  if not isinstance(payload, dict):
    raise StrategyWorkerError("策略执行服务返回无效响应")
  return payload
