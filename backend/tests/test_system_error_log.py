import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import ResponseNotReady
from pathlib import Path
from typing import Any, Iterator

import pytest

from backend import settings
from backend.backtesting.overlay import ResultReconciliationError
from backend.services import state_store
from backend.services.backtest_errors import BacktestErrorDetail, RemoteBacktestError, StrategyExecutionError
from backend.services.clickhouse_client import ClickHouseError
from backend.services.state_store import _insert_strategy_runtime_error_stat, _public_system_error_context
from backend.services.system_error_log import system_error_log_record


_RESULT_RAW_LOG_REF = (
  "container=l2-runner-a; workerId=l2-runner-a:4321; "
  "taskId=run-result; stage=l2.result_finalize"
)


class _SystemErrorCursor:
  def __init__(self) -> None:
    self.commands: list[tuple[str, object]] = []
    self._next_row: dict[str, Any] | None = None
    self._inserted: tuple[Any, ...] | None = None

  def __enter__(self) -> "_SystemErrorCursor":
    return self

  def __exit__(self, *args: object) -> None:
    return None

  def execute(self, query: str, params: object = None) -> None:
    self.commands.append((query, params))
    if "UPDATE l2_backtest_job" in query:
      self._next_row = {"status": "failed"}
    elif "INSERT INTO system_error_log" in query:
      self._inserted = tuple(params)  # type: ignore[arg-type]

  def fetchone(self) -> dict[str, Any] | None:
    row = self._next_row
    self._next_row = None
    return row

  def fetchall(self) -> list[dict[str, Any]]:
    if self._inserted is None:
      return []
    values = self._inserted
    return [{
      "id": 1,
      "run_id": values[0],
      "display_no": 42,
      "owner_user_id": values[1],
      "username": "tester",
      "display_name": "测试用户",
      "symbol": "688981.SH",
      "timeframe": "1m",
      "source": values[2],
      "stage": values[3],
      "error_code": values[4],
      "reason": values[5],
      "exception_type": values[6],
      "context_json": values[7].obj,
      "category": values[8],
      "retryable": values[9],
      "raw_log_ref": values[10],
      "occurred_at": datetime(2026, 1, 5, tzinfo=timezone.utc),
    }]


class _SystemErrorConnection:
  def __init__(self, cursor: _SystemErrorCursor) -> None:
    self._cursor = cursor

  def cursor(self) -> _SystemErrorCursor:
    return self._cursor


@contextmanager
def _system_error_db(cursor: _SystemErrorCursor) -> Iterator[_SystemErrorConnection]:
  yield _SystemErrorConnection(cursor)


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


def test_overlay_reconciliation_error_records_actual_l2_runner() -> None:
  record = system_error_log_record(
    ResultReconciliationError("增强收益与账户收益无法对账", "2026-01-05"),
    "l2_runner",
    "l2.result_finalize",
    "run-result",
    worker_id="l2-runner-a:4321",
  )

  assert record is not None
  assert record.code == "result.overlay_reconciliation_failed"
  assert record.category == "result"
  assert record.retryable is False
  assert record.reason == "增强收益与账户收益无法对账"
  assert record.exception_type == "ResultReconciliationError"
  assert record.context["workerId"] == "l2-runner-a:4321"
  assert record.context["tradeDate"] == "2026-01-05"
  assert record.raw_log_ref == _RESULT_RAW_LOG_REF


def test_administrator_can_find_result_error_by_task_id(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  record = system_error_log_record(
    ResultReconciliationError("增强收益与账户收益无法对账", "2026-01-05"),
    "l2_runner", "l2.result_finalize", "run-result",
    worker_id="l2-runner-a:4321",
  )
  assert record is not None
  cursor = _SystemErrorCursor()
  monkeypatch.setattr(state_store, "_db", lambda: _system_error_db(cursor))

  saved = state_store.finish_l2_backtest_job(
    "run-result", "attempt-1", "failed", {"error": "平台结果生成失败。"},
    system_error=record.to_dict(), owner_user_id="owner-1",
  )

  records = state_store.list_system_error_logs(run_id="run-result")

  query, params = cursor.commands[-1]
  assert saved is True
  assert "e.run_id=%s" in query
  assert params == ["run-result", 100]
  assert records[0]["reason"] == "增强收益与账户收益无法对账"
  assert records[0]["context"] == {
    "workerId": "l2-runner-a:4321",
    "tradeDate": "2026-01-05",
  }
  assert records[0]["rawLogRef"] == _RESULT_RAW_LOG_REF


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


def test_remote_strategy_diagnostic_is_saved_for_administrator() -> None:
  error = RemoteBacktestError.from_dict(
    {
      "category": "strategy_error",
      "code": "strategy.runtime_error",
      "message": "策略引用了未定义的变量或函数。",
    },
    {
      "exceptionType": "NameError",
      "reason": "name '__build_class__' is not defined",
      "__internal_raw_message": "name '__build_class__' is not defined",
      "__internal_raw_traceback": "strategy_script_1.py:1",
    },
  )

  record = system_error_log_record(error, "bar_runner", "bar.backtest", "run-1")

  assert record is not None
  assert record.code == "worker.execution_rejected"
  assert record.reason == "name '__build_class__' is not defined"
  assert record.exception_type == "NameError"
  assert record.context["__internal_raw_message"] == "name '__build_class__' is not defined"


def test_remote_runtime_error_preserves_platform_code_for_aggregate() -> None:
  error = RemoteBacktestError.from_dict(
    {
      "category": "system_error",
      "code": "runtime.contract_missing",
      "message": "回测因平台运行环境兼容问题未完成。",
      "missingName": "tuple",
      "runtimeVersion": "python3.12-runtime-v2",
    },
    {
      "exceptionType": "NameError",
      "reason": "name 'tuple' is not defined",
      "missingName": "tuple",
      "runtimeVersion": "python3.12-runtime-v2",
    },
  )

  record = system_error_log_record(error, "l2_runner", "l2.market_replay", "run-runtime")

  assert record is not None
  assert record.code == "runtime.contract_missing"
  assert record.category == "runtime"
  assert record.context["missingName"] == "tuple"
  assert record.context["runtimeVersion"] == "python3.12-runtime-v2"


class _RecordingCursor:
  def __init__(self) -> None:
    self.calls: list[tuple[str, tuple[Any, ...]]] = []

  def execute(self, query: str, params: tuple[Any, ...]) -> None:
    self.calls.append((query, params))


def test_runtime_error_stat_keeps_only_minimal_platform_fields() -> None:
  cursor = _RecordingCursor()
  _insert_strategy_runtime_error_stat(cursor, {
    "code": "runtime.contract_missing",
    "context": {
      "runtimeVersion": "python3.12-runtime-v2",
      "missingName": "tuple",
      "runId": "run-secret",
      "sourceCode": "private strategy",
    },
  })

  assert len(cursor.calls) == 1
  assert cursor.calls[0][1] == (
    "runtime.contract_missing", "python3.12-runtime-v2", "tuple", None,
  )
  assert "run-secret" not in str(cursor.calls)
  assert "private strategy" not in str(cursor.calls)


def test_user_defined_name_is_not_written_to_runtime_error_stat() -> None:
  cursor = _RecordingCursor()
  _insert_strategy_runtime_error_stat(cursor, {
    "code": "strategy.undefined_name",
    "context": {"missingName": "private_signal"},
  })

  assert cursor.calls == []


def test_dependency_stat_keeps_only_module_from_supported_package() -> None:
  cursor = _RecordingCursor()
  _insert_strategy_runtime_error_stat(cursor, {
    "code": "runtime.dependency_missing",
    "context": {
      "runtimeVersion": "python3.12-runtime-v3",
      "supportedPackage": "TA-Lib",
      "missingModule": "talib._ta_lib",
    },
  })

  assert cursor.calls[0][1] == (
    "runtime.dependency_missing", "python3.12-runtime-v3", None, "talib",
  )


def test_dependency_stat_normalizes_user_controlled_submodule_suffix() -> None:
  cursor = _RecordingCursor()
  _insert_strategy_runtime_error_stat(cursor, {
    "code": "runtime.dependency_missing",
    "context": {
      "runtimeVersion": "python3.12-runtime-v3",
      "supportedPackage": "TA-Lib",
      "missingModule": "talib.user_controlled",
    },
  })

  assert cursor.calls[0][1] == (
    "runtime.dependency_missing", "python3.12-runtime-v3", None, "talib",
  )


@pytest.mark.parametrize(
  "module_name",
  ["backend.settings", f"talib.{('x' * 128)}"],
)
def test_dependency_stat_rejects_unrelated_or_oversized_module_name(
  module_name: str,
) -> None:
  cursor = _RecordingCursor()
  _insert_strategy_runtime_error_stat(cursor, {
    "code": "runtime.dependency_missing",
    "context": {
      "runtimeVersion": "python3.12-runtime-v3",
      "supportedPackage": "TA-Lib",
      "missingModule": module_name,
    },
  })

  assert cursor.calls[0][1] == (
    "runtime.dependency_missing", "python3.12-runtime-v3", None, None,
  )


def test_runtime_error_stat_migration_has_no_business_identity_foreign_key() -> None:
  migration = (
    Path(__file__).resolve().parents[2]
    / "db" / "migrations" / "025_strategy_runtime_error_stats.sql"
  ).read_text(encoding="utf-8")

  assert "REFERENCES backtest_run" not in migration
  assert "owner_user_id" not in migration
  assert "run_id" not in migration
  assert "source_code" not in migration
