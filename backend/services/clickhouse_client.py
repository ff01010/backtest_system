"""量化回测 ClickHouse HTTP 客户端。"""

from __future__ import annotations

import base64
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from backend import settings


class ClickHouseError(RuntimeError):
  """ClickHouse 连接或查询失败。"""


def query_rows(query: str) -> list[dict[str, Any]]:
  """以 JSON 格式执行只读查询并返回行字典。"""
  url, headers = _connection()
  request = Request(f"{url}/?query={quote(f'{query} FORMAT JSON')}", headers=headers)
  try:
    with urlopen(request, timeout=settings.CLICKHOUSE_TIMEOUT_SECONDS) as response:
      payload = json.loads(response.read().decode("utf-8"))
  except (HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
    raise ClickHouseError(f"ClickHouse 查询失败: {error}") from error
  data = payload.get("data") if isinstance(payload, dict) else None
  if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
    raise ClickHouseError("ClickHouse 响应缺少 data 行数组")
  return data


def _connection() -> tuple[str, dict[str, str]]:
  if not settings.CLICKHOUSE_HTTP_URL:
    raise ClickHouseError("未设置 TREND_GRID_CLICKHOUSE_HTTP_URL")
  if not settings.CLICKHOUSE_PASSWORD:
    raise ClickHouseError("未设置 TREND_GRID_CLICKHOUSE_PASSWORD")
  credential = f"{settings.CLICKHOUSE_USER}:{settings.CLICKHOUSE_PASSWORD}".encode("utf-8")
  token = base64.b64encode(credential).decode("ascii")
  return settings.CLICKHOUSE_HTTP_URL.rstrip("/"), {"Authorization": f"Basic {token}"}
