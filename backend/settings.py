# -*- coding: utf-8 -*-
"""数据路径和 PostgreSQL 连接配置。"""

import os
import re


def _read_bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
  raw = os.getenv(name, str(default))
  try:
    value = int(raw)
  except ValueError as error:
    raise ValueError(f"{name} 必须是整数") from error
  if not minimum <= value <= maximum:
    raise ValueError(f"{name} 必须在 {minimum} 至 {maximum} 之间")
  return value


def _read_bool(name: str, default: bool) -> bool:
  raw = os.getenv(name, str(default)).strip().lower()
  if raw not in {"true", "false"}:
    raise ValueError(f"{name} 必须是 true 或 false")
  return raw == "true"


# K 线数据根目录。各周期位于 {KLINE_ROOT}/{timeframe}/{symbol}/ 下。
KLINE_ROOT = os.getenv("TREND_GRID_KLINE_ROOT", "./runtime/kline")

# 信号数据根目录。
SIGNAL_ROOT = os.getenv("TREND_GRID_SIGNAL_ROOT", "./runtime/signals")

# 支持的 K 线周期：部分周期由 1m/5m 拟合生成。
TIMEFRAMES = ["1m", "2m", "3m", "4m", "5m", "10m", "15m", "30m", "45m", "60m", "1d"]

# 两种回测模式共用统一 Bar 周期；L2 模式额外校验十档快照和必要的逐笔成交。
BAR_RUN_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "45m", "60m", "1d")
BACKTEST_TIMEFRAMES = BAR_RUN_TIMEFRAMES
L2_BACKTEST_TIMEFRAMES = BAR_RUN_TIMEFRAMES

# 信号文件命名模板
SIGNAL_FILENAME = "signal_{strategy}_{date}.csv"
SIGNAL_FILENAME_LEGACY = "signal_{date}.csv"

STRATEGIES = ["trend_grid", "trend_grid_both"]

# PostgreSQL 状态库。数据库、角色和表由 deploy 中的迁移创建。
DATABASE_URL = os.getenv("TREND_GRID_DATABASE_URL", "")
DEFAULT_USER_ID = os.getenv(
  "TREND_GRID_DEFAULT_USER_ID",
  "00000000-0000-0000-0000-000000000001",
)

# QMT 原始历史行情存储在服务器共享 ClickHouse 中。
CLICKHOUSE_HTTP_URL = os.getenv("TREND_GRID_CLICKHOUSE_HTTP_URL", "")
CLICKHOUSE_USER = os.getenv("TREND_GRID_CLICKHOUSE_USER", "default")
CLICKHOUSE_PASSWORD = os.getenv("TREND_GRID_CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_TIMEOUT_SECONDS = int(os.getenv("TREND_GRID_CLICKHOUSE_TIMEOUT_SECONDS", "30"))

# L2 服务仅使用部署端提供的映射配置，用户请求不能指定表或列。
L2_BAR_MAPPING_JSON = os.getenv("TREND_GRID_L2_BAR_MAPPING_JSON", "")
L2_QUOTE_MAPPING_JSON = os.getenv("TREND_GRID_L2_QUOTE_MAPPING_JSON", "")
L2_EVENT_MAPPING_JSON = os.getenv("TREND_GRID_L2_EVENT_MAPPING_JSON", "")
L2_REDIS_HOST = os.getenv("TREND_GRID_L2_REDIS_HOST", "redis")
L2_REDIS_PORT = int(os.getenv("TREND_GRID_L2_REDIS_PORT", "6379"))
L2_BAR_CACHE_TTL_SECONDS = int(os.getenv("TREND_GRID_L2_BAR_CACHE_TTL_SECONDS", "2592000"))
L2_TEMP_BAR_AGGREGATION_VERSION = os.getenv("TREND_GRID_L2_TEMP_BAR_AGGREGATION_VERSION", "tick_quote_session_v5")
L2_TEMP_MAPPING_VERSION = os.getenv("TREND_GRID_L2_TEMP_MAPPING_VERSION", "tick_quote_depth_v3")
L2_JOB_LEASE_SECONDS = int(os.getenv("TREND_GRID_L2_JOB_LEASE_SECONDS", "90"))
L2_RUNNER_POLL_SECONDS = float(os.getenv("TREND_GRID_L2_RUNNER_POLL_SECONDS", "2"))
L2_PAGE_SIZE = int(os.getenv("TREND_GRID_L2_PAGE_SIZE", "2000"))
L2_QUOTE_PREFETCH_SIZE = _read_bounded_int("TREND_GRID_L2_QUOTE_PREFETCH_SIZE", 128, 16, 512)
L2_WORKER_SESSION_LIMIT = int(os.getenv("TREND_GRID_L2_WORKER_SESSION_LIMIT", "2"))

# Bar 与 L2 共用的全局调度容量。公平扩容只为尚未获得槽位的排队用户服务。
BACKTEST_BASE_CONCURRENCY = _read_bounded_int("TREND_GRID_BACKTEST_BASE_CONCURRENCY", 8, 1, 12)
BACKTEST_BURST_MAX_CONCURRENCY = _read_bounded_int(
  "TREND_GRID_BACKTEST_BURST_MAX_CONCURRENCY", 12, BACKTEST_BASE_CONCURRENCY, 12,
)
BACKTEST_USER_GUARANTEED_CONCURRENCY = _read_bounded_int(
  "TREND_GRID_BACKTEST_USER_GUARANTEED_CONCURRENCY", 1, 1, BACKTEST_BASE_CONCURRENCY,
)
BACKTEST_FAIRNESS_BURST_ENABLED = _read_bool("TREND_GRID_BACKTEST_FAIRNESS_BURST_ENABLED", True)
BACKTEST_CANCEL_POLL_SECONDS = float(os.getenv("TREND_GRID_BACKTEST_CANCEL_POLL_SECONDS", "1"))
if not 0.2 <= BACKTEST_CANCEL_POLL_SECONDS <= 10:
  raise ValueError("TREND_GRID_BACKTEST_CANCEL_POLL_SECONDS 必须在 0.2 至 10 之间")
BACKTEST_CANCEL_GRACE_SECONDS = _read_bounded_int(
  "TREND_GRID_BACKTEST_CANCEL_GRACE_SECONDS", 5, 1, 30,
)
BACKTEST_CAPACITY_OVERRIDE_MAX_TTL_SECONDS = _read_bounded_int(
  "TREND_GRID_BACKTEST_CAPACITY_OVERRIDE_MAX_TTL_SECONDS", 3600, 60, 86400,
)
BACKTEST_RUNNER_HEALTH_TTL_SECONDS = _read_bounded_int(
  "TREND_GRID_BACKTEST_RUNNER_HEALTH_TTL_SECONDS", 30, 5, 300,
)
BACKTEST_RUNNER_HEARTBEAT_WRITE_SECONDS = _read_bounded_int(
  "TREND_GRID_BACKTEST_RUNNER_HEARTBEAT_WRITE_SECONDS", 10, 1, 60,
)
BACKTEST_RUNNER_ERROR_BACKOFF_MAX_SECONDS = _read_bounded_int(
  "TREND_GRID_BACKTEST_RUNNER_ERROR_BACKOFF_MAX_SECONDS", 30, 2, 300,
)
BACKTEST_RUNNER_AVAILABILITY_REQUIRED = _read_bool(
  "TREND_GRID_BACKTEST_RUNNER_AVAILABILITY_REQUIRED", True,
)
STRATEGY_RUNTIME_ERROR_RETENTION_DAYS = _read_bounded_int(
  "TREND_GRID_STRATEGY_RUNTIME_ERROR_RETENTION_DAYS", 180, 30, 3650,
)

# 全局默认 A 股费用。仅由部署环境配置，当前不向普通用户开放编辑。
DEFAULT_COMMISSION_RATE = float(os.getenv("TREND_GRID_DEFAULT_COMMISSION_RATE", "0.00008"))
DEFAULT_TRANSFER_FEE_RATE = float(os.getenv("TREND_GRID_DEFAULT_TRANSFER_FEE_RATE", "0.00001"))
DEFAULT_STAMP_DUTY_RATE = float(os.getenv("TREND_GRID_DEFAULT_STAMP_DUTY_RATE", "0.0005"))
DEFAULT_MINIMUM_COMMISSION = float(os.getenv("TREND_GRID_DEFAULT_MINIMUM_COMMISSION", "5"))


def default_fee_profile() -> dict[str, float]:
  """返回每个任务写入快照的默认 A 股费用。"""
  return {
    "commissionRate": DEFAULT_COMMISSION_RATE,
    "transferFeeRate": DEFAULT_TRANSFER_FEE_RATE,
    "stampDutyRate": DEFAULT_STAMP_DUTY_RATE,
    "minimumCommission": DEFAULT_MINIMUM_COMMISSION,
  }

# 当前部署尚未配置 HTTPS，默认关闭 Secure 以保证同源 HTTP 登录可用。
AUTH_COOKIE_SECURE = os.getenv("TREND_GRID_AUTH_COOKIE_SECURE", "false").lower() == "true"

# 研究服务兑换一次性授权码时使用的后端间凭据，不能为空才允许兑换。
RESEARCH_AUTH_SERVICE_TOKEN = os.getenv("TREND_GRID_RESEARCH_AUTH_SERVICE_TOKEN", "")
RESEARCH_AUTH_CODE_LIFETIME_SECONDS = _read_bounded_int(
  "TREND_GRID_RESEARCH_AUTH_CODE_LIFETIME_SECONDS", 60, 30, 300,
)
RESEARCH_CENTER_CALLBACK_URL = os.getenv("TREND_GRID_RESEARCH_CENTER_CALLBACK_URL", "")

# 区分本机与服务器回测来源；本机默认不把调试结果回传研究中心。
BACKTEST_INSTANCE_ID = os.getenv("BACKTEST_INSTANCE_ID", "local").strip()
RESEARCH_RESULT_REPORT_ENABLED = _read_bool("RESEARCH_RESULT_REPORT_ENABLED", False)
RESEARCH_SIGNAL_API_URL = os.getenv("RESEARCH_SIGNAL_API_URL", "").rstrip("/")
RESEARCH_SIGNAL_SERVICE_TOKEN = os.getenv("RESEARCH_SIGNAL_SERVICE_TOKEN", "")


def validate_research_integration_configuration() -> None:
  """拒绝不明确的实例标识，以及缺少回传目标的启用配置。"""
  if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", BACKTEST_INSTANCE_ID):
    raise ValueError("BACKTEST_INSTANCE_ID 必须是小写字母开头的 1 至 32 位标识")
  if RESEARCH_RESULT_REPORT_ENABLED and (
    not RESEARCH_SIGNAL_API_URL or not RESEARCH_SIGNAL_SERVICE_TOKEN
  ):
    raise ValueError("启用研究结果回传时必须配置研究中心地址和服务令牌")

# 对外回测 API 默认关闭。启用后必须配置私有 pepper，避免仅用数据库摘要保护 API Key。
PUBLIC_API_ENABLED = os.getenv("TREND_GRID_PUBLIC_API_ENABLED", "false").lower() == "true"
API_KEY_PEPPER = os.getenv("TREND_GRID_API_KEY_PEPPER", "")
PUBLIC_API_MAX_INFLIGHT = _read_bounded_int("TREND_GRID_PUBLIC_API_MAX_INFLIGHT", 2, 1, 20)
PUBLIC_API_DAILY_SUBMIT_LIMIT = _read_bounded_int("TREND_GRID_PUBLIC_API_DAILY_SUBMIT_LIMIT", 100, 1, 10_000)


def validate_public_api_configuration() -> None:
  """开放 API 启用时，拒绝在缺少密钥摘要 pepper 的环境中启动。"""
  if PUBLIC_API_ENABLED and len(API_KEY_PEPPER) < 32:
    raise ValueError("启用开放 API 时必须设置长度不少于 32 的 TREND_GRID_API_KEY_PEPPER")


def resolve_kline_path(symbol: str, timeframe: str, date: str | None = None) -> str | None:
  """根据标的和周期解析 K 线文件路径, 不存在返回 None

  symbol: "688981.SH"
  timeframe: "5m"
  返回: 完整 CSV 路径 或 None
  """
  code, market = symbol.split(".")
  code_market = f"{code}_{market}"
  path = os.path.join(KLINE_ROOT, timeframe, code_market, f"{code_market}_{timeframe}.csv")
  return path if os.path.exists(path) else None


def resolve_signal_path(date: str, symbol: str, strategy: str = "trend_grid") -> str:
  """根据日期和标的解析信号文件路径

  date: "20260622"
  symbol: "688981.SH"
  strategy: "trend_grid" | "trend_grid_both"
  查找顺序: bar/{code_market}_bar_{date}.csv → tick/{code_market}_tick_{date}.csv
  """
  code_market = symbol.replace(".", "_")

  # bar 子目录
  bar_path = os.path.join(SIGNAL_ROOT, "bar", f"{code_market}_bar_{date}.csv")
  if os.path.exists(bar_path):
    return bar_path

  # tick 子目录
  tick_path = os.path.join(SIGNAL_ROOT, "tick", f"{code_market}_tick_{date}.csv")
  if os.path.exists(tick_path):
    return tick_path

  # 兜底：返回 bar 路径（调用方用 os.path.exists 判断是否存在）
  return bar_path


def validate_symbol(symbol: str) -> bool:
  """校验标的格式: 6位数字.SH、.SZ 或 .BJ。"""
  import re
  return bool(re.match(r"^\d{6}\.(SH|SZ|BJ)$", symbol))


def validate_date(date: str) -> bool:
  """校验日期格式: YYYYMMDD"""
  import re
  return bool(re.match(r"^\d{8}$", date))


def validate_timeframe(tf: str) -> bool:
  """校验周期是否在白名单中"""
  return tf in TIMEFRAMES


def validate_backtest_timeframe(tf: str) -> bool:
  """校验周期是否被当前回测引擎支持。"""
  return tf in BACKTEST_TIMEFRAMES


def validate_l2_backtest_timeframe(tf: str) -> bool:
  """校验 L2 回测支持的预聚合周期。"""
  return tf in L2_BACKTEST_TIMEFRAMES
