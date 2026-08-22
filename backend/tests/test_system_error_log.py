import socket
from http.client import ResponseNotReady

from backend import settings
from backend.services.backtest_errors import BacktestErrorDetail, RemoteBacktestError, StrategyExecutionError
from backend.services.clickhouse_client import ClickHouseError
from backend.services.state_store import _public_system_error_context
from backend.services.system_error_log import system_error_log_record


def test_unresolved_clickhouse_host_is_safe_and_actionable(monkeypatch) -> None:
  monkeypatch.setattr(settings, "CLICKHOUSE_HTTP_URL", "http://quant-clickhouse:8123")
  error = ClickHouseError("ClickHouse 查询失败")
  error.__cause__ = socket.gaierror(-2, "Name or service not known")

  record = system_error_log_record(error, "l2_runner", "l2.bar_load")

  assert record is not None
  assert record.code == "clickhouse.host_unresolved"
  assert record.category == "clickhouse"
  assert record.retryable is False
  assert record.reason == "ClickHouse 主机名无法解析：quant-clickhouse"
  assert record.context["service"] == "clickhouse"
  assert record.context["host"] == "quant-clickhouse"
  assert "ClickHouse" in record.context["__internal_raw_message"]
  assert "ClickHouseError" in record.context["__internal_raw_traceback"]
  assert "Name or service not known" in record.context["__internal_raw_traceback"]


def test_clickhouse_connection_state_error_is_not_retryable() -> None:
  error = ClickHouseError("ClickHouse 查询失败: Request-sent")
  error.__cause__ = ResponseNotReady("Request-sent")

  record = system_error_log_record(error, "l2_runner", "l2.market_replay", "run-74")

  assert record is not None
  assert record.code == "clickhouse.connection_state_invalid"
  assert record.category == "clickhouse"
  assert record.retryable is False
  assert record.exception_type == "ResponseNotReady"
  assert record.raw_log_ref == "container=quant-backtest-platform-l2-runner-*; taskId=run-74; stage=l2.market_replay"


def test_unknown_system_error_is_not_marked_retryable() -> None:
  record = system_error_log_record(RuntimeError("unexpected"), "l2_runner", "l2.market_replay", "run-75")

  assert record is not None
  assert record.code == "internal.unexpected"
  assert record.category == "internal"
  assert record.retryable is False
  assert record.context["__internal_raw_message"] == "unexpected"


def test_internal_error_context_is_not_returned_by_api() -> None:
  context = _public_system_error_context({
    "service": "worker",
    "__internal_raw_message": "secret diagnostic",
    "__internal_raw_traceback": "full traceback",
  })

  assert context == {"service": "worker"}


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
    {
      "exceptionType": "ValueError",
      "reason": "平仓数量超过可用持仓",
      "trace": "engine.py:120:process_quote",
      "__internal_raw_message": "原始异常正文",
      "__internal_raw_traceback": "完整调用栈",
    },
  )

  record = system_error_log_record(error, "l2_runner", "l2.market_replay")

  assert record is not None
  assert record.code == "worker.execution_rejected"
  assert record.category == "worker"
  assert record.retryable is False
  assert record.reason == "平仓数量超过可用持仓"
  assert record.context == {
    "service": "strategy_worker",
    "symbol": "688126.SH",
    "eventTime": "2026-04-03 09:31:02",
    "trace": "engine.py:120:process_quote",
    "__internal_raw_message": "原始异常正文",
    "__internal_raw_traceback": "完整调用栈",
  }
