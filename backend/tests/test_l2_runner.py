"""L2 异步任务 Runner 测试。"""

from typing import Any

import pytest

from backend import l2_runner
from backend.backtesting.overlay import ResultReconciliationError
from backend.l2.models import NormalizedBar
from backend.l2.providers import L2DataCoverageError, L2DataError


def test_l2_runner_persists_actual_worker_for_result_error(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  job = {
    "runId": "run-result",
    "ownerUserId": "owner-1",
    "attemptToken": "attempt-1",
    "payload": {
      "symbol": "688981.SH",
      "timeframe": "1m",
      "startDate": "2026-01-05",
      "endDate": "2026-01-05",
    },
  }
  finished: dict[str, Any] = {}

  monkeypatch.setattr(l2_runner.state_store, "claim_l2_backtest_job", lambda *_: job)
  monkeypatch.setattr(l2_runner._JobLeaseKeeper, "start", lambda _: None)
  monkeypatch.setattr(l2_runner._JobLeaseKeeper, "stop", lambda _: None)
  monkeypatch.setattr(
    l2_runner.bar_data,
    "load_normalized_bars_with_snapshot",
    lambda *_: (_ for _ in ()).throw(
      ResultReconciliationError("增强收益与账户收益无法对账"),
    ),
  )
  monkeypatch.setattr(
    l2_runner.state_store,
    "finish_l2_backtest_job",
    lambda *args, **kwargs: finished.update({"args": args, "kwargs": kwargs}) or True,
  )

  assert l2_runner.L2Runner("l2-runner-a:4321").run_once() is True
  assert finished["args"][2] == "failed"
  assert finished["args"][3]["errorDetail"]["code"] == (
    "result.overlay_reconciliation_failed"
  )
  assert finished["kwargs"]["system_error"]["context"]["workerId"] == (
    "l2-runner-a:4321"
  )
  assert finished["kwargs"]["system_error"]["raw_log_ref"] == (
    "container=l2-runner-a; workerId=l2-runner-a:4321; "
    "taskId=run-result; stage=l2.bar_load"
  )


def test_l2_runner_persists_operator_visible_data_error(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  job = {
    "runId": "run-data",
    "ownerUserId": "owner-1",
    "attemptToken": "attempt-1",
    "payload": {
      "symbol": "688449.SH",
      "timeframe": "1m",
      "startDate": "2025-05-29",
      "endDate": "2025-05-29",
    },
  }
  finished: dict[str, Any] = {}
  error = L2DataError(
    "快照缺少稳定 sequence，且同一时间戳存在重复记录",
    code="data.l2_quote_order_ambiguous",
    context={"symbol": "688449.SH", "tradeDate": "2025-05-29"},
  )
  monkeypatch.setattr(l2_runner.state_store, "claim_l2_backtest_job", lambda *_: job)
  monkeypatch.setattr(l2_runner._JobLeaseKeeper, "start", lambda _: None)
  monkeypatch.setattr(l2_runner._JobLeaseKeeper, "stop", lambda _: None)
  monkeypatch.setattr(
    l2_runner.bar_data,
    "load_normalized_bars_with_snapshot",
    lambda *_: (_ for _ in ()).throw(error),
  )
  monkeypatch.setattr(
    l2_runner.state_store,
    "finish_l2_backtest_job",
    lambda *args, **kwargs: finished.update({"args": args, "kwargs": kwargs}) or True,
  )

  assert l2_runner.L2Runner("l2-runner-a:4321").run_once() is True
  assert finished["args"][2] == "failed"
  assert finished["args"][3]["errorDetail"]["category"] == "data"
  assert finished["args"][3]["errorDetail"]["code"] == "data.l2_quote_order_ambiguous"
  assert finished["kwargs"]["system_error"]["category"] == "data"
  context = finished["kwargs"]["system_error"]["context"]
  assert context["symbol"] == "688449.SH"
  assert context["tradeDate"] == "2025-05-29"
  assert context["workerId"] == "l2-runner-a:4321"
  assert context["__internal_raw_message"] == "快照缺少稳定 sequence，且同一时间戳存在重复记录"
  assert "L2DataError" in context["__internal_raw_traceback"]


def test_l2_runner_rejects_missing_quote_summary_before_strategy_session(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  job = {
    "runId": "run-missing-quote",
    "ownerUserId": "owner-1",
    "attemptToken": "attempt-1",
    "payload": {
      "symbol": "688981.SH",
      "timeframe": "1m",
      "startDate": "2026-01-05",
      "endDate": "2026-01-05",
    },
  }
  bar = NormalizedBar(
    symbol="688981.SH",
    trade_date="2026-01-05",
    period="1m",
    start_time="2026-01-05 09:29:00",
    end_time="2026-01-05 09:30:00",
    open=10.0,
    high=10.2,
    low=9.9,
    close=10.1,
    volume=100.0,
    amount=0.0,
    quality="complete",
    source_version="test",
  )
  finished: dict[str, Any] = {}
  monkeypatch.setattr(l2_runner.state_store, "claim_l2_backtest_job", lambda *_: job)
  monkeypatch.setattr(l2_runner._JobLeaseKeeper, "start", lambda _: None)
  monkeypatch.setattr(l2_runner._JobLeaseKeeper, "stop", lambda _: None)
  monkeypatch.setattr(l2_runner, "has_profile", lambda *_: False)
  monkeypatch.setattr(l2_runner, "load_temporary_quote_profile", lambda: object())
  monkeypatch.setattr(l2_runner, "ClickHouseL2Adapter", lambda _: object())
  monkeypatch.setattr(
    l2_runner.bar_data,
    "load_normalized_bars_with_snapshot",
    lambda *_: ([bar], {"provider": "qmt_kline"}),
  )
  monkeypatch.setattr(
    l2_runner.l2_quote_availability,
    "require_summarized_quote_dates",
    lambda *_: (_ for _ in ()).throw(
      L2DataCoverageError("688981.SH", ["2026-01-05"]),
    ),
  )
  monkeypatch.setattr(
    l2_runner.state_store,
    "finish_l2_backtest_job",
    lambda *args, **kwargs: finished.update({"args": args, "kwargs": kwargs}) or True,
  )

  assert l2_runner.L2Runner("l2-runner-a:4321").run_once() is True
  assert finished["args"][2] == "failed"
  assert finished["args"][3]["error"] == "行情数据异常，请联系管理员"
  assert finished["args"][3]["errorDetail"]["code"] == "data.l2_quote_missing"
  assert finished["kwargs"]["system_error"]["raw_log_ref"].endswith(
    "taskId=run-missing-quote; stage=l2.quote_coverage"
  )
