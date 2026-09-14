"""标准 Bar 异步任务 Runner 测试。"""

from typing import Any

import pytest

from backend import bar_runner
from backend.services.backtest_errors import RemoteBacktestError


def test_bar_runner_claim_failure_reports_error_without_false_ready(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  class StopLoop(Exception):
    pass

  heartbeats: list[tuple[Any, ...]] = []
  monkeypatch.setattr(
    bar_runner.state_store,
    "claim_bar_backtest_job",
    lambda *_: (_ for _ in ()).throw(bar_runner.state_store.DatabaseError("claim failed")),
  )
  monkeypatch.setattr(
    bar_runner.state_store,
    "record_backtest_runner_heartbeat",
    lambda *args: heartbeats.append(args),
  )
  monkeypatch.setattr(bar_runner.time, "sleep", lambda _: (_ for _ in ()).throw(StopLoop()))

  with pytest.raises(StopLoop):
    bar_runner.BarRunner("test-worker").run_forever()

  assert heartbeats == [(
    "test-worker", "bar", "error", None, "DatabaseError",
    bar_runner.SCRIPT_RUNTIME_VERSION, bar_runner.RUNTIME_CAPABILITY_DIGEST,
  )]


def test_bar_runner_finishes_queued_job(monkeypatch: pytest.MonkeyPatch) -> None:
  job = {
    "runId": "run-1",
    "ownerUserId": "owner-1",
    "attemptToken": "attempt-1",
    "payload": {"fillModel": "bar_next_open"},
  }
  finished: dict[str, Any] = {}
  monkeypatch.setattr(bar_runner.state_store, "claim_bar_backtest_job", lambda *_: job)
  monkeypatch.setattr(bar_runner.backtest_runner, "run_backtest", lambda _: {
    "summary": {"tradeCount": 3}, "warnings": ["示例"], "outputDir": "", "trades": [],
  })
  monkeypatch.setattr(bar_runner.state_store, "heartbeat_bar_backtest_job", lambda *_: True)
  monkeypatch.setattr(bar_runner.research_result_outbox, "dispatch_pending", lambda: 0)
  monkeypatch.setattr(
    bar_runner.state_store,
    "finish_bar_backtest_job",
    lambda *args, **kwargs: finished.update({"args": args, "kwargs": kwargs}) or True,
  )

  assert bar_runner.BarRunner("test-worker").run_once() is True
  assert finished["args"][2] == "succeeded"
  assert finished["args"][3]["fillModel"] == "bar_next_open"
  assert finished["args"][3]["warningCount"] == 1


def test_bar_runner_persists_remote_strategy_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
  job = {
    "runId": "run-2",
    "ownerUserId": "owner-2",
    "attemptToken": "attempt-2",
    "payload": {"fillModel": "bar_next_open"},
  }
  error = RemoteBacktestError.from_dict(
    {
      "category": "strategy_error",
      "code": "strategy.runtime_error",
      "message": "策略引用了未定义的变量或函数。",
    },
    {"exceptionType": "NameError", "reason": "missing_variable"},
  )
  finished: dict[str, Any] = {}

  def fail_backtest(_: dict[str, Any]) -> dict[str, Any]:
    raise error

  monkeypatch.setattr(bar_runner.state_store, "claim_bar_backtest_job", lambda *_: job)
  monkeypatch.setattr(bar_runner.backtest_runner, "run_backtest", fail_backtest)
  monkeypatch.setattr(bar_runner.research_result_outbox, "dispatch_pending", lambda: 0)
  monkeypatch.setattr(
    bar_runner.state_store,
    "finish_bar_backtest_job",
    lambda *args, **kwargs: finished.update({"args": args, "kwargs": kwargs}) or True,
  )

  assert bar_runner.BarRunner("test-worker").run_once() is True
  assert finished["kwargs"]["system_error"]["reason"] == "missing_variable"
  assert finished["kwargs"]["system_error"]["exception_type"] == "NameError"


def test_research_result_outbox_failure_does_not_escape(monkeypatch: pytest.MonkeyPatch) -> None:
  def raise_dispatch_error() -> int:
    raise RuntimeError("研究服务暂不可用")

  monkeypatch.setattr(bar_runner.research_result_outbox, "dispatch_pending", raise_dispatch_error)

  bar_runner._dispatch_research_result_outbox()


def test_worker_summary_uses_cash_and_base_cost_as_initial_equity() -> None:
  summary = bar_runner.backtest_runner._build_worker_summary(
    1_000_000,
    100_000,
    20.0,
    True,
    [],
    [{"date": "20260730", "close": 21.0, "totalEquity": 3_120_000.0, "dailyPnl": 120_000.0}],
  )

  assert summary["initialEquity"] == 3_000_000.0
  assert summary["totalPnl"] == 120_000.0
  assert summary["baseHoldingPnl"] == 100_000.0
  assert summary["t0ExcessPnl"] == 20_000.0
  assert summary["orderCount"] == 0
  assert summary["fillCount"] == 0

def test_bar_runner_marks_job_cancelled_when_lease_keeper_detects_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
  job = {
    "runId": "run-cancelled",
    "ownerUserId": "owner-1",
    "attemptToken": "attempt-1",
    "payload": {"fillModel": "bar_next_open"},
  }
  finished: dict[str, Any] = {}
  monkeypatch.setattr(bar_runner.state_store, "claim_bar_backtest_job", lambda *_: job)
  monkeypatch.setattr(bar_runner.backtest_runner, "run_backtest", lambda _: {
    "summary": {"tradeCount": 1}, "warnings": [], "outputDir": "", "trades": [],
  })
  monkeypatch.setattr(bar_runner._BarJobLeaseKeeper, "start", lambda _: None)
  monkeypatch.setattr(bar_runner._BarJobLeaseKeeper, "stop", lambda _: None)
  monkeypatch.setattr(
    bar_runner._BarJobLeaseKeeper,
    "ensure_active",
    lambda _: (_ for _ in ()).throw(ValueError("Bar 任务租约丢失或已取消")),
  )
  monkeypatch.setattr(bar_runner.research_result_outbox, "dispatch_pending", lambda: 0)
  monkeypatch.setattr(
    bar_runner.state_store,
    "finish_bar_backtest_job",
    lambda *args, **kwargs: finished.update({"args": args, "kwargs": kwargs}) or True,
  )

  assert bar_runner.BarRunner("test-worker").run_once() is True
  assert finished["args"][2] == "cancelled"
