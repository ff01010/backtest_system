from __future__ import annotations

import time

import pytest

from backend.bar_strategy_session import BarStrategySession


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
    "runtimeVersion": "python3.12-runtime-v2",
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
