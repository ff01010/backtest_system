# -*- coding: utf-8 -*-
"""数据路径和 PostgreSQL 连接配置。"""

import os

# K 线数据根目录。各周期位于 {KLINE_ROOT}/{timeframe}/{symbol}/ 下。
KLINE_ROOT = os.getenv("TREND_GRID_KLINE_ROOT", "./runtime/kline")

# 信号数据根目录。
SIGNAL_ROOT = os.getenv("TREND_GRID_SIGNAL_ROOT", "./runtime/signals")

# 支持的 K 线周期：部分周期由 1m/5m 拟合生成。
TIMEFRAMES = ["1m", "2m", "3m", "4m", "5m", "10m", "15m", "30m", "45m", "60m", "1d"]

# 回测引擎已验证的运行周期。图表和数据管理可继续使用 TIMEFRAMES 中的其它周期。
BACKTEST_TIMEFRAMES = ("1m", "5m")
L2_BACKTEST_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "45m", "60m", "1d")

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
L2_TEMP_BAR_AGGREGATION_VERSION = os.getenv("TREND_GRID_L2_TEMP_BAR_AGGREGATION_VERSION", "tick_quote_session_v4")
L2_TEMP_MAPPING_VERSION = os.getenv("TREND_GRID_L2_TEMP_MAPPING_VERSION", "tick_quote_depth_v3")
L2_JOB_LEASE_SECONDS = int(os.getenv("TREND_GRID_L2_JOB_LEASE_SECONDS", "90"))
L2_RUNNER_POLL_SECONDS = float(os.getenv("TREND_GRID_L2_RUNNER_POLL_SECONDS", "2"))
L2_PAGE_SIZE = int(os.getenv("TREND_GRID_L2_PAGE_SIZE", "2000"))
L2_QUOTE_PREFETCH_SIZE = int(os.getenv("TREND_GRID_L2_QUOTE_PREFETCH_SIZE", "16"))
L2_WORKER_SESSION_LIMIT = int(os.getenv("TREND_GRID_L2_WORKER_SESSION_LIMIT", "2"))

# 当前部署尚未配置 HTTPS，默认关闭 Secure 以保证同源 HTTP 登录可用。
AUTH_COOKIE_SECURE = os.getenv("TREND_GRID_AUTH_COOKIE_SECURE", "false").lower() == "true"


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
