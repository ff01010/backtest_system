"""回测对外错误必须可定位且不泄露内部异常。"""

from http.client import ResponseNotReady

from backend.backtesting.overlay import ResultReconciliationError
from backend.services.backtest_errors import RemoteBacktestError, StrategyExecutionError, error_summary
from backend.services.clickhouse_client import ClickHouseError


def test_unknown_error_is_sanitized() -> None:
  summary = error_summary(RuntimeError("postgresql://secret@internal-host:5432/query"), "run-42")

  assert summary["error"] == "回测服务发生内部错误。"
  assert summary["errorDetail"] == {
    "category": "system_error",
    "code": "internal.unexpected",
    "message": "回测服务发生内部错误。",
    "hint": "请联系管理员处理后再重试。",
    "callback": None,
    "eventTime": None,
    "symbol": None,
    "retryable": False,
    "taskId": "run-42",
    "missingName": None,
    "strategyLine": None,
    "runtimeVersion": None,
    "requestedModule": None,
    "missingModule": None,
    "supportedPackage": None,
  }
  assert "internal-host" not in str(summary)


def test_overlay_reconciliation_error_has_safe_actionable_summary() -> None:
  summary = error_summary(
    ResultReconciliationError("增强收益与账户收益无法对账: internal values"),
    "run-result",
  )

  assert summary["error"] == "平台结果生成失败。"
  assert summary["errorDetail"]["category"] == "system_error"
  assert summary["errorDetail"]["code"] == "result.overlay_reconciliation_failed"
  assert summary["errorDetail"]["hint"] == "无需修改策略，请提供任务编号联系管理员。"
  assert summary["errorDetail"]["retryable"] is False
  assert summary["errorDetail"]["taskId"] == "run-result"
  assert "internal values" not in str(summary)


def test_clickhouse_connection_state_error_is_safe_and_not_retryable() -> None:
  error = ClickHouseError("ClickHouse 查询失败: Request-sent")
  error.__cause__ = ResponseNotReady("Request-sent")

  summary = error_summary(error, "run-74")

  assert summary["errorDetail"]["code"] == "clickhouse.connection_state_invalid"
  assert summary["errorDetail"]["message"] == "回测服务内部连接状态异常。"
  assert summary["errorDetail"]["retryable"] is False
  assert "Request-sent" not in str(summary)


def test_strategy_error_is_safe_and_locatable() -> None:
  error = StrategyExecutionError.from_exception(
    NameError("private_symbol"), "on_bar", "2026-06-01 09:35:00", "688981.SH",
  )
  summary = error_summary(error, "run-7")

  assert summary["errorDetail"]["category"] == "strategy_error"
  assert summary["errorDetail"]["callback"] == "on_bar"
  assert summary["errorDetail"]["eventTime"] == "2026-06-01 09:35:00"
  assert summary["errorDetail"]["symbol"] == "688981.SH"
  assert "private_symbol" not in str(summary)


def test_remote_error_keeps_worker_diagnostic_outside_public_summary() -> None:
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

  assert error.diagnostic["reason"] == "平仓数量超过可用持仓"
  assert "平仓数量超过可用持仓" not in str(error_summary(error, "run-8"))
