"""回测系统错误的安全分类；原始堆栈只保留在容器日志中。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import socket
from typing import Any
from urllib.parse import urlparse

from backend import settings
from backend.services.backtest_errors import error_detail


@dataclass(frozen=True)
class SystemErrorLogRecord:
  """可供管理员查看、但不包含秘密信息的系统错误记录。"""

  source: str
  stage: str
  code: str
  reason: str
  exception_type: str
  context: dict[str, str]

  def to_dict(self) -> dict[str, Any]:
    return asdict(self)


def system_error_log_record(error: Exception, source: str, stage: str) -> SystemErrorLogRecord | None:
  """仅将系统错误转换为管理员日志；策略错误继续由原有页面展示。"""
  if error_detail(error).category != "system_error":
    return None

  cause = _root_cause(error)
  if isinstance(cause, socket.gaierror):
    host = _clickhouse_host()
    return SystemErrorLogRecord(
      source, stage, "clickhouse.host_unresolved",
      f"ClickHouse 主机名无法解析：{host or '未配置主机'}",
      type(cause).__name__, {"service": "clickhouse", "host": host},
    )
  if isinstance(cause, ConnectionRefusedError):
    host = _clickhouse_host()
    return SystemErrorLogRecord(
      source, stage, "clickhouse.connection_refused",
      f"ClickHouse 拒绝连接：{host or '未配置主机'}",
      type(cause).__name__, {"service": "clickhouse", "host": host},
    )
  if isinstance(cause, TimeoutError):
    host = _clickhouse_host()
    return SystemErrorLogRecord(
      source, stage, "system.timeout",
      "系统组件请求超时。",
      type(cause).__name__, {"host": host},
    )
  if isinstance(error, FileNotFoundError):
    return SystemErrorLogRecord(
      source, stage, "data.unavailable",
      "回测所需的数据文件不存在或不可读取。",
      type(error).__name__, {},
    )
  return SystemErrorLogRecord(
    source, stage, "system.unclassified",
    "未分类系统异常；请使用任务编号在容器日志中查看原始堆栈。",
    type(cause).__name__, {},
  )


def _root_cause(error: Exception) -> Exception:
  current = error
  while current.__cause__ is not None:
    current = current.__cause__
  return current


def _clickhouse_host() -> str:
  parsed = urlparse(settings.CLICKHOUSE_HTTP_URL)
  return parsed.hostname or ""
