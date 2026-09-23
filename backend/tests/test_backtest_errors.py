"""回测对外错误必须可定位且不泄露内部异常。"""

import ast
from http.client import ResponseNotReady
from pathlib import Path

from backend.backtesting.overlay import ResultReconciliationError
from backend.l2.providers import L2DataError
from backend.services.backtest_errors import (
  RemoteBacktestError,
  StrategyExecutionError,
  error_summary,
  error_terminal_status,
  normalize_public_error_summary,
)
from backend.services.backtest_faults import BacktestFault
from backend.services.clickhouse_client import ClickHouseError
from backend.services.system_error_catalog import classify_system_error


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
    "tradeDate": None,
    "retryable": False,
    "taskId": "run-42",
    "missingName": None,
    "strategyLine": None,
    "runtimeVersion": None,
    "requestedModule": None,
    "missingModule": None,
    "supportedPackage": None,
  }


def test_historical_data_error_summary_uses_current_public_message() -> None:
  summary = normalize_public_error_summary({
    "error": "688981.SH 5m 缺少 2026-07-30",
    "errorDetail": {
      "category": "data",
      "code": "data.bar_missing",
      "message": "688981.SH 5m 缺少 2026-07-30",
      "hint": "请补齐 qmt_kline_5m_raw",
      "taskId": "run-old",
      "missingDates": ["2026-07-30"],
      "table": "qmt_kline_5m_raw",
      "rawError": "query timed out",
    },
  })

  assert summary["error"] == "行情数据异常，请联系管理员"
  assert summary["errorDetail"] == {
    "category": "data",
    "code": "data.bar_missing",
    "message": "行情数据异常，请联系管理员",
    "hint": None,
    "retryable": False,
    "taskId": "run-old",
  }


def test_legacy_error_only_summary_does_not_expose_raw_text() -> None:
  summary = normalize_public_error_summary({
    "error": "clickhouse://user:secret@internal/qmt_kline_5m_raw timed out",
  })

  assert summary == {"error": "回测执行异常，请联系管理员"}
  assert "internal-host" not in str(summary)


def test_historical_l2_source_error_uses_one_public_data_message() -> None:
  summary = normalize_public_error_summary({
    "error": "L2 数据源配置与实际表结构不一致。",
    "errorDetail": {
      "category": "system_error",
      "code": "l2.config.mapping_column_missing",
      "message": "L2 数据源配置与实际表结构不一致。",
      "hint": "请联系管理员检查数据源配置。",
    },
  })

  assert summary["error"] == "行情数据异常，请联系管理员"
  assert summary["errorDetail"]["category"] == "data"
  assert summary["errorDetail"]["message"] == "行情数据异常，请联系管理员"
  assert summary["errorDetail"]["hint"] is None


def test_historical_data_error_without_code_gets_safe_stable_code() -> None:
  summary = normalize_public_error_summary({
    "error": "internal source detail",
    "errorDetail": {
      "category": "data",
      "message": "internal source detail",
    },
  })

  assert summary["errorDetail"]["code"] == "data.legacy_error"
  assert summary["errorDetail"]["message"] == "行情数据异常，请联系管理员"


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
  assert summary["errorDetail"]["category"] == "data"
  assert summary["errorDetail"]["message"] == "行情数据异常，请联系管理员"
  assert summary["errorDetail"]["hint"] is None
  assert summary["errorDetail"]["retryable"] is False
  assert "Request-sent" not in str(summary)


def test_bar_load_failure_uses_one_public_data_message() -> None:
  source = RuntimeError("qmt_history_dev.qmt_kline_5m_raw timed out")
  error = BacktestFault(
    "data.bar_load_failed",
    "标准 Bar 行情读取失败",
    {"symbol": "688981.SH", "timeframe": "5m"},
  )
  error.__cause__ = source

  summary = error_summary(error, "run-data")

  assert summary["error"] == "行情数据异常，请联系管理员"
  assert summary["errorDetail"]["category"] == "data"
  assert summary["errorDetail"]["code"] == "data.bar_load_failed"
  assert summary["errorDetail"]["message"] == "行情数据异常，请联系管理员"
  assert summary["errorDetail"]["hint"] is None
  assert summary["errorDetail"]["symbol"] is None
  assert summary["errorDetail"]["tradeDate"] is None
  assert "qmt_kline_5m_raw" not in str(summary)


def test_remote_data_failure_is_normalized_before_display() -> None:
  error = RemoteBacktestError.from_dict({
    "category": "data",
    "code": "data.l2_bar_missing",
    "message": "所选范围没有可用的 L2 Bar 数据。",
    "hint": "请调整日期范围。",
  })

  detail = error_summary(error, "run-l2-data")["errorDetail"]

  assert detail["message"] == "行情数据异常，请联系管理员"
  assert detail["hint"] is None


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


def test_unclassified_l2_error_fails_closed_as_system_error() -> None:
  summary = error_summary(L2DataError("尚未归类的 L2 错误"), "run-l2")

  assert summary["errorDetail"]["category"] == "system_error"
  assert summary["errorDetail"]["code"] == "l2.unclassified"


def test_task_inactive_uses_typed_terminal_status() -> None:
  error = BacktestFault("task.inactive", "任务租约丢失或已取消")

  assert error_terminal_status(error) == "cancelled"


def test_every_production_l2_error_declares_a_stable_code() -> None:
  root = Path(__file__).resolve().parents[2]
  paths = [
    root / "backend" / "l2" / "providers.py",
    root / "backend" / "l2" / "config.py",
    root / "backend" / "l2_runner.py",
  ]
  missing: list[str] = []
  codes: set[str] = set()
  for path in paths:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
      if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        continue
      if node.func.id != "L2DataError":
        continue
      code_keywords = [keyword for keyword in node.keywords if keyword.arg == "code"]
      if not code_keywords:
        missing.append(f"{path.name}:{node.lineno}")
      elif isinstance(code_keywords[0].value, ast.Constant):
        codes.add(str(code_keywords[0].value.value))

  assert missing == []
  assert {
    classify_system_error(L2DataError("test", code=code)).code
    for code in codes
  } == codes
