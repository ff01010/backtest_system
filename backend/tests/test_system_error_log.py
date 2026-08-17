import socket

from backend import settings
from backend.services.backtest_errors import BacktestErrorDetail, StrategyExecutionError
from backend.services.clickhouse_client import ClickHouseError
from backend.services.system_error_log import system_error_log_record


def test_unresolved_clickhouse_host_is_safe_and_actionable(monkeypatch) -> None:
  monkeypatch.setattr(settings, "CLICKHOUSE_HTTP_URL", "http://quant-clickhouse:8123")
  error = ClickHouseError("ClickHouse 查询失败")
  error.__cause__ = socket.gaierror(-2, "Name or service not known")

  record = system_error_log_record(error, "l2_runner", "l2.bar_load")

  assert record is not None
  assert record.code == "clickhouse.host_unresolved"
  assert record.reason == "ClickHouse 主机名无法解析：quant-clickhouse"
  assert record.context == {"service": "clickhouse", "host": "quant-clickhouse"}


def test_strategy_error_is_not_written_to_system_error_log() -> None:
  error = StrategyExecutionError(BacktestErrorDetail(
    "strategy_error", "strategy.runtime_error", "策略脚本运行时发生异常。",
  ))

  assert system_error_log_record(error, "l2_runner", "l2.market_replay") is None
