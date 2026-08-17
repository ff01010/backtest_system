# -*- coding: utf-8 -*-
"""认证摘要和隔离策略执行的无数据库单元测试。"""

import io
import json
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException

from backend.services import strategy_worker
from backend.services.auth import hash_password, verify_password
from backend.services.backtest_errors import RemoteBacktestError
from backend.worker_app import WorkerL2QuoteRequest, WorkerRunRequest, process_l2_quote, run


def test_password_hash_round_trip() -> None:
  encoded = hash_password("correct-horse-battery-staple")

  assert verify_password("correct-horse-battery-staple", encoded) is True
  assert verify_password("wrong-password", encoded) is False


def test_worker_runs_script_without_database_access() -> None:
  result = run(WorkerRunRequest(
    scriptId="test-script",
    code="def on_bar(bar, state):\n  return []\n",
    bars=[{
      "time": "202601010930", "symbol": "000001.SZ", "open": 10,
      "high": 10, "low": 10, "close": 10, "volume": 100,
    }],
    initialCapital=100_000,
    commissionRate=0.0003,
  ))

  assert result["trades"] == []
  assert len(result["portfolioSnapshots"]) == 1


def test_worker_applies_slippage_to_script_trade_price() -> None:
  result = run(WorkerRunRequest(
    scriptId="slippage-script",
    code="def on_bar(bar, state):\n  return [{'action': 'buy_open', 'quantity': 100}]\n",
    bars=[{
      "time": "202601010930", "symbol": "000001.SZ", "open": 10,
      "high": 10, "low": 10, "close": 10, "volume": 100,
    }],
    initialCapital=100_000,
    commissionRate=0,
    slippage=0.001,
    holdingPolicy="overnight",
  ))

  assert round(result["trades"][0]["price"], 2) == 10.01
  assert result["trades"][0]["priceSource"] == "当前 Bar 收盘价 + 固定滑点 10.00 bp"


def test_l2_quote_error_has_diagnostic_and_quote_context(monkeypatch) -> None:
  class FailingSession:
    def process_quote(self, payload: WorkerL2QuoteRequest) -> dict[str, object]:
      raise ValueError("平仓数量超过可用持仓")

  monkeypatch.setattr("backend.worker_app._session", lambda session_id: FailingSession())
  payload = WorkerL2QuoteRequest(
    tradeDate="2026-04-03",
    quote={"symbol": "688126.SH", "time": "2026-04-03 09:31:02"},
  )

  with pytest.raises(HTTPException) as raised:
    process_l2_quote("session-1", payload)

  detail = raised.value.detail
  assert detail["diagnostic"]["reason"] == "平仓数量超过可用持仓"
  assert detail["errorDetail"]["symbol"] == "688126.SH"
  assert detail["errorDetail"]["eventTime"] == "2026-04-03 09:31:02"


def test_strategy_worker_keeps_structured_quote_diagnostic(monkeypatch) -> None:
  response = json.dumps({"detail": {
    "errorDetail": {
      "category": "system_error",
      "code": "system.internal",
      "message": "回测执行异常，请稍后重试。",
      "symbol": "688126.SH",
      "eventTime": "2026-04-03 09:31:02",
    },
    "diagnostic": {
      "exceptionType": "ValueError",
      "reason": "平仓数量超过可用持仓",
    },
  }}).encode("utf-8")
  error = HTTPError("http://worker/quotes", 422, "unprocessable", None, io.BytesIO(response))

  def raise_http_error(*args: object, **kwargs: object) -> object:
    raise error

  monkeypatch.setattr(strategy_worker, "urlopen", raise_http_error)

  with pytest.raises(RemoteBacktestError) as raised:
    strategy_worker.process_l2_quote("session-1", {"tradeDate": "2026-04-03", "quote": {}})

  assert raised.value.diagnostic["reason"] == "平仓数量超过可用持仓"
  assert raised.value.detail.event_time == "2026-04-03 09:31:02"
