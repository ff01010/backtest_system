"""回测失败目录：集中定义安全提示、终态与管理员可见性。"""

from __future__ import annotations

from dataclasses import dataclass
from http.client import ResponseNotReady
import socket
from urllib.error import HTTPError
from urllib.parse import urlparse

from backend import settings
from backend.backtesting.overlay import ResultReconciliationError
from backend.services.backtest_faults import BacktestFault, find_backtest_fault
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
  public_category: str = "system_error"
  operator_visible: bool = True
  terminal_status: str = "failed"
  http_status: int = 503


@dataclass(frozen=True)
class _FaultSpec:
  category: str
  public_category: str
  retryable: bool
  public_message: str
  public_hint: str
  admin_reason: str
  operator_visible: bool
  http_status: int
  context_fields: tuple[str, ...] = ()
  terminal_status: str = "failed"


_FAULT_SPECS: dict[str, _FaultSpec] = {
  "data.l2_quote_missing": _FaultSpec(
    "data", "data", False, "当前日期范围缺少 L2 十档快照。",
    "请调整日期范围，或在数据补齐后重新发起回测。",
    "实际回放交易日缺少 L2 连续竞价十档快照。", False, 404,
    ("symbol", "missingDates"),
  ),
  "data.l2_bar_missing": _FaultSpec(
    "data", "data", False, "所选范围没有可用的 L2 Bar 数据。",
    "请调整标的、日期或周期后重试。", "所选范围没有可用的 L2 Bar 数据。",
    False, 404, ("symbol", "timeframe"),
  ),
  "data.l2_quote_order_ambiguous": _FaultSpec(
    "data", "data", False, "L2 行情数据时间顺序异常，当前任务无法继续。",
    "系统已记录，请联系数据维护人员处理。",
    "L2 快照缺少稳定 sequence，且同一时间戳存在重复记录。", True, 422,
    ("symbol", "tradeDate"),
  ),
  "data.l2_bar_partial": _FaultSpec(
    "data", "data", False, "L2 Bar 数据不完整，当前任务无法继续。",
    "系统已记录，请联系数据维护人员处理。", "L2 聚合 Bar 未达到完整状态。",
    True, 422, ("symbol", "tradeDates"),
  ),
  "data.l2_bar_price_invalid": _FaultSpec(
    "data", "data", False, "L2 行情数据缺少有效价格，当前任务无法继续。",
    "系统已记录，请联系数据维护人员处理。", "L2 聚合 Bar 没有有效价格。",
    True, 422, ("symbol", "tradeDate"),
  ),
  "data.future_date": _FaultSpec(
    "data", "data", False, "回测区间包含尚未结束的交易日。",
    "请选择已经结束的历史交易日。", "请求包含尚未结束的交易日。", False, 422,
  ),
  "l2.config.mapping_missing": _FaultSpec(
    "l2_config", "system_error", False, "L2 数据源尚未完成配置。",
    "请联系管理员检查数据源配置。", "L2 数据源映射缺失。", True, 503, ("kind",),
  ),
  "l2.config.mapping_invalid": _FaultSpec(
    "l2_config", "system_error", False, "L2 数据源配置无效。",
    "请联系管理员检查数据源配置。", "L2 数据源映射结构无效。", True, 503, ("kind",),
  ),
  "l2.config.mapping_field_missing": _FaultSpec(
    "l2_config", "system_error", False, "L2 数据源配置缺少必要字段。",
    "请联系管理员检查数据源配置。", "L2 数据源映射缺少必要字段。", True, 503,
    ("field",),
  ),
  "l2.config.mapping_identifier_invalid": _FaultSpec(
    "l2_config", "system_error", False, "L2 数据源配置包含非法标识符。",
    "请联系管理员检查数据源配置。", "L2 数据源表名或列名不符合白名单格式。", True, 503,
  ),
  "l2.config.mapping_column_missing": _FaultSpec(
    "l2_config", "system_error", False, "L2 数据源配置与实际表结构不一致。",
    "请联系管理员检查数据源配置。", "L2 映射列不存在于 ClickHouse 表。", True, 503,
  ),
  "l2.config.event_mapping_invalid": _FaultSpec(
    "l2_config", "system_error", False, "L2 逐笔数据源配置无效。",
    "请联系管理员检查数据源配置。", "L2 逐笔事件映射缺少必要字段。", True, 503,
  ),
  "l2.config.source_kind_invalid": _FaultSpec(
    "l2_config", "system_error", False, "L2 数据源类型配置无效。",
    "请联系管理员检查数据源配置。", "内部请求了未知 L2 数据源类型。", True, 503,
    ("kind",),
  ),
  "cache.redis.unavailable": _FaultSpec(
    "cache", "system_error", True, "行情缓存暂时不可用。", "请稍后重试。",
    "Redis 行情缓存连接失败。", True, 503,
  ),
  "cache.redis.payload_invalid": _FaultSpec(
    "cache", "system_error", False, "行情缓存返回了无效数据。", "请联系管理员处理。",
    "Redis 行情缓存载荷无效。", True, 503,
  ),
  "cache.redis.command_failed": _FaultSpec(
    "cache", "system_error", True, "行情缓存请求失败。", "请稍后重试。",
    "Redis 返回命令错误。", True, 503,
  ),
  "cache.redis.protocol_invalid": _FaultSpec(
    "cache", "system_error", False, "行情缓存响应异常。", "请联系管理员处理。",
    "Redis 协议响应无效。", True, 503,
  ),
  "cache.redis.connection_closed": _FaultSpec(
    "cache", "system_error", True, "行情缓存连接中断。", "请稍后重试。",
    "Redis 在响应完成前关闭连接。", True, 503,
  ),
  "task.snapshot_missing": _FaultSpec(
    "task", "system_error", False, "回测任务缺少必要的策略快照。",
    "无需修改策略，请提供任务编号联系管理员。", "回测任务缺少已保存的策略脚本快照。",
    True, 503,
  ),
  "worker.protocol.invalid_bar_count": _FaultSpec(
    "worker", "system_error", False, "回测执行进程返回了无效结果。",
    "无需修改策略，请提供任务编号联系管理员。", "策略会话返回的 Bar 处理数量无效。",
    True, 503,
  ),
  "worker.protocol.missing_quote_boundary": _FaultSpec(
    "worker", "system_error", False, "回测执行进程缺少必要的时间边界。",
    "无需修改策略，请提供任务编号联系管理员。", "策略会话未返回待成交订单的时间边界。",
    True, 503,
  ),
  "task.inactive": _FaultSpec(
    "task", "system_error", False, "回测任务已取消或当前执行已失效。",
    "可在确认任务状态后重新运行。", "任务取消或当前执行租约已经失效。",
    False, 409, terminal_status="cancelled",
  ),
  "backtest.timeframe_unsupported": _FaultSpec(
    "request", "system_error", False, "当前回测引擎不支持所选周期。",
    "请选择页面提供的可用周期。", "回测请求包含不受支持的周期。", False, 422,
    ("timeframe",),
  ),
  "backtest.date_range_invalid": _FaultSpec(
    "request", "system_error", False, "回测结束日期不能早于开始日期。",
    "请检查回测日期范围。", "回测请求的日期范围无效。", False, 422,
  ),
  "backtest.date_format_invalid": _FaultSpec(
    "request", "system_error", False, "回测日期格式无效。",
    "请使用 YYYY-MM-DD 格式。", "回测请求的日期格式无效。", False, 422,
  ),
  "l2.query.invalid_value": _FaultSpec(
    "internal", "system_error", False, "L2 数据查询参数异常。",
    "请提供任务编号联系管理员。", "内部 L2 查询参数未通过安全校验。", True, 503,
  ),
  "l2.unclassified": _FaultSpec(
    "internal", "system_error", False, "L2 回测服务发生内部错误。",
    "请提供任务编号联系管理员。", "存在尚未归类的 L2DataError。", True, 503,
  ),
}


def classify_system_error(error: Exception) -> SystemErrorClassification:
  """将异常映射为稳定语义；结构化外层错误优先于底层技术原因。"""
  fault = find_backtest_fault(error)
  if fault is not None:
    return _classify_fault(fault)
  cause = root_cause(error)
  host = _clickhouse_host()
  if isinstance(cause, ResultReconciliationError):
    context = {"tradeDate": cause.trade_date} if cause.trade_date else {}
    return SystemErrorClassification(
      "result.overlay_reconciliation_failed", "result", False,
      "平台结果生成失败。", "无需修改策略，请提供任务编号联系管理员。",
      "增强收益与账户收益无法对账", context,
    )
  if isinstance(cause, socket.gaierror):
    return _clickhouse(
      "host_unresolved", False, "数据源地址不可用。", "请联系管理员检查数据源配置。",
      f"ClickHouse 主机名无法解析：{host or '未配置主机'}", host,
    )
  if isinstance(cause, ConnectionRefusedError):
    return _clickhouse(
      "connection_refused", True, "数据源暂时无法连接。", "请稍后重试。",
      f"ClickHouse 拒绝连接：{host or '未配置主机'}", host,
    )
  if isinstance(cause, ResponseNotReady):
    return _clickhouse(
      "connection_state_invalid", False, "回测服务内部连接状态异常。",
      "请联系管理员处理后再重试。", "ClickHouse HTTP 连接存在未消费响应，无法发起后续查询。", host,
    )
  if isinstance(cause, TimeoutError):
    return _clickhouse(
      "timeout", True, "数据源请求超时。", "请稍后重试。", "ClickHouse 请求超时。", host,
    )
  if isinstance(cause, HTTPError):
    return _clickhouse(
      "query_failed", cause.code >= 500, "数据源查询失败。",
      "请稍后重试；如持续失败请联系管理员。", "ClickHouse 查询返回异常响应。", host,
      {"status": str(cause.code)},
    )
  if isinstance(error, ClickHouseError):
    return _clickhouse(
      "query_failed", False, "数据源查询失败。", "请联系管理员检查数据源状态。",
      "ClickHouse 查询执行失败。", host,
    )
  if isinstance(cause, FileNotFoundError):
    return SystemErrorClassification(
      "data.unavailable", "data", False, "所选区间没有可用行情数据。",
      "请检查标的、日期和周期后重试。", "回测所需的数据文件不存在或不可读取。", {},
      public_category="data", operator_visible=False, http_status=404,
    )
  return SystemErrorClassification(
    "internal.unexpected", "internal", False, "回测服务发生内部错误。",
    "请联系管理员处理后再重试。", "发生未预期内部错误，需要维护人员处理。", {},
  )


def root_cause(error: Exception) -> Exception:
  current = error
  while current.__cause__ is not None:
    current = current.__cause__
  return current


def _classify_fault(fault: BacktestFault) -> SystemErrorClassification:
  spec = _FAULT_SPECS.get(fault.code)
  if spec is None:
    return SystemErrorClassification(
      "internal.unexpected", "internal", False, "回测服务发生内部错误。",
      "请联系管理员处理后再重试。", "出现未登记的回测错误码。",
      {"faultCode": fault.code[:128]},
    )
  context = {
    key: fault.context[key]
    for key in spec.context_fields
    if fault.context.get(key)
  }
  return SystemErrorClassification(
    code=fault.code,
    category=spec.category,
    retryable=spec.retryable,
    public_message=spec.public_message,
    public_hint=spec.public_hint,
    admin_reason=spec.admin_reason,
    context=context,
    public_category=spec.public_category,
    operator_visible=spec.operator_visible,
    terminal_status=spec.terminal_status,
    http_status=spec.http_status,
  )


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
