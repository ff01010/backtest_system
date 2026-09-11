# -*- coding: utf-8 -*-
"""认证摘要和隔离策略执行的无数据库单元测试。"""

import io
import json
from contextlib import contextmanager
from urllib.error import HTTPError

import pytest
from fastapi import HTTPException

from backend.services import auth, strategy_worker
from backend.services.auth import hash_password, verify_password
from backend.services.backtest_errors import RemoteBacktestError
from backend.worker_app import WorkerL2QuoteRequest, WorkerRunRequest, process_l2_quote, run


def test_password_hash_round_trip() -> None:
  encoded = hash_password("correct-horse-battery-staple")

  assert verify_password("correct-horse-battery-staple", encoded) is True
  assert verify_password("wrong-password", encoded) is False


def test_delete_user_rejects_current_login() -> None:
  with pytest.raises(auth.UserManagementError, match="当前登录账号"):
    auth.delete_user("user-1", "user-1")


def test_list_users_clamps_page_to_available_range(monkeypatch: pytest.MonkeyPatch) -> None:
  class Cursor:
    def __init__(self) -> None:
      self.params: tuple[int, int] | None = None
      self.last_sql = ""

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, sql: str, params: tuple[int, int] | None = None) -> None:
      self.last_sql = sql
      self.params = params

    def fetchone(self) -> dict[str, int]:
      return {"value": 21}

    def fetchall(self) -> list[dict[str, object]]:
      return []

  class Connection:
    def __init__(self) -> None:
      self.cursor_instance = Cursor()

    def __enter__(self) -> "Connection":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def cursor(self) -> Cursor:
      return self.cursor_instance

  @contextmanager
  def system_db():
    yield Connection()

  monkeypatch.setattr(auth, "_system_db", system_db)

  result = auth.list_users(8, 20)

  assert result == {"users": [], "page": 2, "pageSize": 20, "total": 21}


@pytest.mark.parametrize(("has_history", "expected_action"), [(False, "deleted"), (True, "disabled")])
def test_delete_user_preserves_backtest_history(monkeypatch: pytest.MonkeyPatch, has_history: bool, expected_action: str) -> None:
  class Cursor:
    def __init__(self) -> None:
      self.calls: list[str] = []
      self.last_sql = ""

    def __enter__(self) -> "Cursor":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def execute(self, sql: str, _params: object = None) -> None:
      self.last_sql = sql
      self.calls.append(sql)

    def fetchone(self) -> dict[str, object]:
      if "SELECT id, role, status" in self.last_sql:
        return {"id": "user-1", "role": "user", "status": "active"}
      return {"value": has_history}

  class Connection:
    def __init__(self) -> None:
      self.cursor_instance = Cursor()

    def __enter__(self) -> "Connection":
      return self

    def __exit__(self, *args: object) -> None:
      return None

    def cursor(self) -> Cursor:
      return self.cursor_instance

  connection = Connection()

  @contextmanager
  def system_db():
    yield connection

  monkeypatch.setattr(auth, "_system_db", system_db)

  result = auth.delete_user("user-1", "admin-1")

  assert result["action"] == expected_action
  statements = "\n".join(connection.cursor_instance.calls)
  assert ("DELETE FROM app_user" in statements) is (not has_history)
  assert ("status='disabled'" in statements) is has_history


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


def test_worker_runs_talib_strategy() -> None:
  result = run(WorkerRunRequest(
    scriptId="talib-script",
    code="""
import numpy as np
import talib


def on_bar(bar, state):
  values = np.asarray([1.0, 2.0, 3.0], dtype=float)
  if not state.get('submitted') and np.isfinite(talib.SMA(values, timeperiod=3)[-1]):
    state['submitted'] = True
    return [{'action': 'buy_open', 'quantity': int(np.int64(100)), 'reason': 'TA-Lib SMA'}]
  return []
""",
    bars=[{
      "time": "202601010930", "symbol": "000001.SZ", "open": 10,
      "high": 10, "low": 10, "close": 10, "volume": 100,
    }, {
      "time": "202601010931", "symbol": "000001.SZ", "open": 10,
      "high": 10, "low": 10, "close": 10, "volume": 100,
    }],
    initialCapital=100_000,
    commissionRate=0,
    holdingPolicy="overnight",
    fillModel="bar_next_open",
  ))

  assert len(result["trades"]) == 1
  assert result["trades"][0]["quantity"] == 100


def test_worker_keeps_raw_script_exception_for_administrator() -> None:
  request = WorkerRunRequest(
    scriptId="diagnostic-script",
    code="def on_bar(bar, state):\n  return missing_variable\n",
    bars=[{
      "time": "202601010930", "symbol": "000001.SZ", "open": 10,
      "high": 10, "low": 10, "close": 10, "volume": 100,
    }],
    initialCapital=100_000,
    commissionRate=0.0003,
  )

  with pytest.raises(HTTPException) as raised:
    run(request)

  diagnostic = raised.value.detail["diagnostic"]
  assert diagnostic["exceptionType"] == "NameError"
  assert "missing_variable" in diagnostic["reason"]
  assert "missing_variable" in diagnostic["__internal_raw_message"]
  assert "strategy_script_diagnostic-script.py" in diagnostic["__internal_raw_traceback"]


def test_worker_applies_slippage_to_script_trade_price() -> None:
  result = run(WorkerRunRequest(
    scriptId="slippage-script",
    code="def on_bar(bar, state):\n  return [{'action': 'buy_open', 'quantity': 100}]\n",
    bars=[{
      "time": "202601010930", "symbol": "000001.SZ", "open": 10,
      "high": 10, "low": 10, "close": 10, "volume": 100,
    }, {
      "time": "202601010931", "symbol": "000001.SZ", "open": 11,
      "high": 11, "low": 11, "close": 11, "volume": 100,
    }],
    initialCapital=100_000,
    commissionRate=0,
    slippage=0.001,
    holdingPolicy="overnight",
    fillModel="bar_next_open",
  ))

  assert round(result["trades"][0]["price"], 2) == 11.01
  assert result["trades"][0]["priceSource"] == "下一根 Bar 开盘价 + 固定滑点 10.00 bp"


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
