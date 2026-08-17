"""回测对外错误必须可定位且不泄露内部异常。"""

from backend.services.backtest_errors import RemoteBacktestError, StrategyExecutionError, error_summary


def test_unknown_error_is_sanitized() -> None:
  summary = error_summary(RuntimeError("postgresql://secret@internal-host:5432/query"), "run-42")

  assert summary["error"] == "回测执行异常，请稍后重试。"
  assert summary["errorDetail"] == {
    "category": "system_error",
    "code": "system.internal",
    "message": "回测执行异常，请稍后重试。",
    "hint": "如持续出现，请提供任务编号给维护人员。",
    "callback": None,
    "eventTime": None,
    "symbol": None,
    "retryable": True,
    "taskId": "run-42",
  }
  assert "internal-host" not in str(summary)


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
