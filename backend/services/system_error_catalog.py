"""系统错误目录：集中定义安全前端提示与管理员诊断。"""

from __future__ import annotations

from dataclasses import dataclass
from http.client import ResponseNotReady
import socket
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse

from backend import settings
from backend.backtesting.overlay import ResultReconciliationError
from backend.l2.providers import L2DataCoverageError, L2DataError
from backend.services.clickhouse_client import ClickHouseError


@dataclass(frozen=True)
class SystemErrorClassification:
  code: str
  category: str
  retryable: bool
  public_message: str
  public_hint: str
  admin_reason: str
  context: dict[str, str]


def classify_system_error(error: Exception) -> SystemErrorClassification:
  """将底层异常映射为稳定错误编码，避免前端依赖异常文本。"""
  cause = root_cause(error)
  host = _clickhouse_host()
  if isinstance(cause, ResultReconciliationError):
    context = {"tradeDate": cause.trade_date} if cause.trade_date else {}
    return SystemErrorClassification(
      "result.overlay_reconciliation_failed", "result", False,
      "平台结果生成失败。",
      "无需修改策略，请提供任务编号联系管理员。",
      "增强收益与账户收益无法对账",
      context,
    )
  if isinstance(cause, L2DataCoverageError):
    missing_dates = ", ".join(cause.missing_dates)
    return SystemErrorClassification(
      "l2.quote_coverage_missing", "data", False,
      "当前日期范围缺少 L2 十档快照。",
      "请在数据补齐后重新发起回测。",
      str(cause),
      {"symbol": cause.symbol, "missingDates": missing_dates},
    )
  if isinstance(cause, socket.gaierror):
    return _clickhouse("host_unresolved", False, "数据源地址不可用。", "请联系管理员检查数据源配置。", f"ClickHouse 主机名无法解析：{host or '未配置主机'}", host)
  if isinstance(cause, L2DataError):
    return SystemErrorClassification(
      "data.unavailable", "data", False,
      "当前日期范围的回测数据不完整。",
      "请调整日期范围或在数据补齐后重新发起回测。",
      str(cause),
      {},
    )

  if isinstance(cause, ConnectionRefusedError):
    return _clickhouse("connection_refused", True, "数据源暂时无法连接。", "请稍后重试。", f"ClickHouse 拒绝连接：{host or '未配置主机'}", host)
  if isinstance(cause, ResponseNotReady):
    return _clickhouse("connection_state_invalid", False, "回测服务内部连接状态异常。", "请联系管理员处理后再重试。", "ClickHouse HTTP 连接存在未消费响应，无法发起后续查询。", host)
  if isinstance(cause, TimeoutError):
    return _clickhouse("timeout", True, "数据源请求超时。", "请稍后重试。", "ClickHouse 请求超时。", host)
  if isinstance(cause, HTTPError):
    retryable = cause.code >= 500
    return _clickhouse("query_failed", retryable, "数据源查询失败。", "请稍后重试；如持续失败请联系管理员。", "ClickHouse 查询返回异常响应。", host, {"status": str(cause.code)})
  if isinstance(error, ClickHouseError):
    return _clickhouse("query_failed", False, "数据源查询失败。", "请联系管理员检查数据源状态。", "ClickHouse 查询执行失败。", host)
  if isinstance(error, FileNotFoundError):
    return SystemErrorClassification("data.unavailable", "data", False, "所选区间没有可用行情数据。", "请检查标的、日期和周期后重试。", "回测所需的数据文件不存在或不可读取。", {})
  return SystemErrorClassification("internal.unexpected", "internal", False, "回测服务发生内部错误。", "请联系管理员处理后再重试。", "发生未预期内部错误，需要维护人员处理。", {})


def root_cause(error: Exception) -> Exception:
  current = error
  while current.__cause__ is not None:
    current = current.__cause__
  return current


def _clickhouse(
  suffix: str,
  retryable: bool,
  public_message: str,
  public_hint: str,
  admin_reason: str,
  host: str,
  extra_context: dict[str, str] | None = None,
) -> SystemErrorClassification:
  return SystemErrorClassification(
    code=f"clickhouse.{suffix}",
    category="clickhouse",
    retryable=retryable,
    public_message=public_message,
    public_hint=public_hint,
    admin_reason=admin_reason,
    context={"service": "clickhouse", "host": host, **(extra_context or {})},
  )


def _clickhouse_host() -> str:
  parsed = urlparse(settings.CLICKHOUSE_HTTP_URL)
  return parsed.hostname or ""
