import socket

from backend import settings
from backend.services.backtest_errors import BacktestErrorDetail, RemoteBacktestError, StrategyExecutionError
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


def test_remote_worker_diagnostic_is_saved_for_administrator() -> None:
  error = RemoteBacktestError.from_dict(
    {
      "category": "system_error",
      "code": "system.internal",
      "message": "回测执行异常，请稍后重试。",
      "eventTime": "2026-04-03 09:31:02",
      "symbol": "688126.SH",
    },
    {"exceptionType": "ValueError", "reason": "平仓数量超过可用持仓"},
  )

  record = system_error_log_record(error, "l2_runner", "l2.market_replay")

  assert record is not None
  assert record.code == "worker.execution_rejected"
  assert record.reason == "平仓数量超过可用持仓"
  assert record.context == {
    "service": "strategy_worker",
    "symbol": "688126.SH",
    "eventTime": "2026-04-03 09:31:02",
  }
