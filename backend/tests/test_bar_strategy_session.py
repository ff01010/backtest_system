from __future__ import annotations

import time

import pytest

from backend.bar_strategy_session import BarStrategySession
from backend.services.backtest_errors import RemoteBacktestError
from backend.strategies.runtime_contract import RUNTIME_CAPABILITY_DIGEST, SCRIPT_RUNTIME_VERSION


def _payload(code: str) -> dict[str, object]:
  return {
    "scriptId": "session-test",
    "code": code,
    "params": {},
    "bars": [
      {
        "time": "20260901093500", "symbol": "688981.SH", "open": 10,
        "high": 10, "low": 10, "close": 10, "volume": 1000,
      },
    ],
    "initialCapital": 100000,
    "commissionRate": 0,
    "feeProfile": {},
    "slippage": 0,
    "holdingPolicy": "intraday",
    "baseQuantity": 0,
    "baseCostPrice": 0,
    "t0Enabled": False,
    "warmupBarCount": 0,
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
  }


def test_bar_strategy_session_returns_isolated_result() -> None:
  result = BarStrategySession().run_script(_payload("""
def on_bar(bar, state):
  state['seen'] = state.get('seen', 0) + 1
  return []
"""))

  assert result["trades"] == []
  assert result["skippedSignals"] == []


def test_bar_strategy_session_supports_standard_tuple_builtin() -> None:
  result = BarStrategySession().run_script(_payload("""
def on_bar(bar, state):
  state['items'] = tuple(enumerate(range(2)))
  return []
"""))

  assert len(result["portfolioSnapshots"]) == 1


def test_bar_strategy_session_supports_talib_with_bounded_numeric_threads() -> None:
  payload = _payload("""
import os
import numpy as np
import talib

NUMERIC_THREAD_NAMES = (
  'OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
)


def on_bar(bar, state):
  values = np.asarray([1.0, 2.0, 3.0], dtype=float)
  sma = talib.SMA(values, timeperiod=3)[-1]
  threads_are_bounded = all(os.environ.get(name) == '1' for name in NUMERIC_THREAD_NAMES)
  if bar['time'] == '20260901093500' and np.isfinite(sma) and threads_are_bounded:
    return [{'action': 'buy_open', 'quantity': int(np.int64(200)), 'reason': 'TA-Lib SMA'}]
  return []
""")
  payload["bars"].append({
    "time": "20260901094000", "symbol": "688981.SH", "open": 10,
    "high": 10, "low": 10, "close": 10, "volume": 1000,
  })

  result = BarStrategySession().run_script(payload)

  assert len(result["trades"]) == 1
  assert result["trades"][0]["quantity"] == 200


def test_bar_strategy_session_keeps_raw_script_exception_for_administrator() -> None:
  payload = _payload("def on_bar(bar, state):\n  return missing_variable\n")
  payload["scriptId"] = "diagnostic-script"

  with pytest.raises(RemoteBacktestError) as raised:
    BarStrategySession().run_script(payload)

  diagnostic = raised.value.diagnostic
  assert diagnostic["exceptionType"] == "NameError"
  assert "missing_variable" in diagnostic["reason"]
  assert "missing_variable" in diagnostic["__internal_raw_message"]
  assert "strategy_script_diagnostic-script.py" in diagnostic["__internal_raw_traceback"]


def test_bar_strategy_session_applies_slippage_to_trade_price() -> None:
  payload = _payload(
    "def on_bar(bar, state):\n  return [{'action': 'buy_open', 'quantity': 100}]\n",
  )
  payload["bars"].append({
    "time": "20260901094000", "symbol": "688981.SH", "open": 11,
    "high": 11, "low": 11, "close": 11, "volume": 1000,
  })
  payload["slippage"] = 0.001
  payload["holdingPolicy"] = "overnight"

  result = BarStrategySession().run_script(payload)

  assert round(result["trades"][0]["price"], 2) == 11.01
  assert result["trades"][0]["priceSource"] == "下一根 Bar 开盘价 + 固定滑点 10.00 bp"


def test_bar_strategy_session_terminates_blocked_strategy_on_cancel() -> None:
  checks = 0

  def abort_check() -> None:
    nonlocal checks
    checks += 1
    if checks >= 3:
      raise ValueError("Bar 任务租约丢失或已取消")

  started = time.monotonic()
  with pytest.raises(ValueError, match="已取消"):
    BarStrategySession(abort_check).run_script(_payload("""
def on_bar(bar, state):
  while True:
    pass
"""))

  assert time.monotonic() - started < 5
