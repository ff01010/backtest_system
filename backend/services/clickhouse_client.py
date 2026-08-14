"""量化回测 ClickHouse HTTP 客户端。"""

from __future__ import annotations

import base64
import gzip
import json
import threading
import time
from typing import Any
from http.client import HTTPConnection, HTTPException, HTTPSConnection, HTTPResponse, RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit

from backend import settings


class ClickHouseError(RuntimeError):
  """ClickHouse 连接或查询失败。"""


_thread_connections = threading.local()


def query_rows(query: str) -> list[dict[str, Any]]:
  """以复用 HTTP 连接和 gzip JSON 格式执行只读查询。"""
  url, headers = _connection()
  for attempt in range(2):
    try:
      return _read_rows(url, headers, query)
    except (RemoteDisconnected, ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as error:
      _discard_connection()
      if attempt == 0:
        continue
      raise ClickHouseError(f"ClickHouse 查询失败: {error}") from error
    except (HTTPError, HTTPException, URLError, OSError, TimeoutError, gzip.BadGzipFile, json.JSONDecodeError) as error:
      _discard_connection()
      raise ClickHouseError(f"ClickHouse 查询失败: {error}") from error
  raise RuntimeError("ClickHouse 连接恢复流程意外结束")


def _read_rows(url: str, headers: dict[str, str], query: str) -> list[dict[str, Any]]:
  response = _request(url, headers, query)
  body = response.read()
  if response.getheader("Content-Encoding", "").lower() == "gzip":
    body = gzip.decompress(body)
  payload = json.loads(body.decode("utf-8"))
  data = payload.get("data") if isinstance(payload, dict) else None
  if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
    raise ClickHouseError("ClickHouse 响应缺少 data 行数组")
  return data


def _request(url: str, headers: dict[str, str], query: str) -> HTTPResponse:
  parsed = urlsplit(url)
  if parsed.scheme not in {"http", "https"} or not parsed.hostname:
    raise ClickHouseError("ClickHouse 地址必须为 HTTP 或 HTTPS URL")
  connection = _get_connection(parsed.scheme, parsed.hostname, parsed.port)
  path = f"{parsed.path.rstrip('/')}/?enable_http_compression=1&query={quote(f'{query} FORMAT JSON')}"
  connection.request("GET", path, headers={**headers, "Accept-Encoding": "gzip"})
  response = connection.getresponse()
  if response.status >= 400:
    response.read()
    raise HTTPError(url, response.status, response.reason, response.headers, None)
  return response


def _get_connection(scheme: str, host: str, port: int | None) -> HTTPConnection:
  key = (scheme, host, port)
  cached = getattr(_thread_connections, "connection", None)
  if cached is not None and cached[0] == key:
    return cached[1]
  _discard_connection()
  connection = _new_connection(scheme, host, port)
  _thread_connections.connection = (key, connection)
  return connection


def _new_connection(scheme: str, host: str, port: int | None) -> HTTPConnection:
  connection_type = HTTPSConnection if scheme == "https" else HTTPConnection
  return connection_type(host, port, timeout=settings.CLICKHOUSE_TIMEOUT_SECONDS)


def _discard_connection() -> None:
  cached = getattr(_thread_connections, "connection", None)
  if cached is not None:
    cached[1].close()
    delattr(_thread_connections, "connection")


def query_rows_with_retry(query: str, max_attempts: int = 2, initial_delay_seconds: float = 0.25) -> list[dict[str, Any]]:
  """对临时网络错误进行有限重试，避免 L2 长任务因单次超时失败。"""
  if max_attempts <= 0:
    raise ValueError("ClickHouse 查询最大尝试次数必须大于 0")
  for attempt in range(max_attempts):
    try:
      return query_rows(query)
    except ClickHouseError as error:
      if not _is_retryable(error) or attempt == max_attempts - 1:
        raise
      time.sleep(initial_delay_seconds * (attempt + 1))
  raise RuntimeError("ClickHouse 重试流程意外结束")


def _is_retryable(error: ClickHouseError) -> bool:
  cause = error.__cause__
  return isinstance(cause, (URLError, OSError, TimeoutError)) and not isinstance(cause, HTTPError)


def _connection() -> tuple[str, dict[str, str]]:
  if not settings.CLICKHOUSE_HTTP_URL:
    raise ClickHouseError("未设置 TREND_GRID_CLICKHOUSE_HTTP_URL")
  if not settings.CLICKHOUSE_PASSWORD:
    raise ClickHouseError("未设置 TREND_GRID_CLICKHOUSE_PASSWORD")
  credential = f"{settings.CLICKHOUSE_USER}:{settings.CLICKHOUSE_PASSWORD}".encode("utf-8")
  token = base64.b64encode(credential).decode("ascii")
  return settings.CLICKHOUSE_HTTP_URL.rstrip("/"), {"Authorization": f"Basic {token}"}
