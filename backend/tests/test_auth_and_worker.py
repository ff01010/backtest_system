# -*- coding: utf-8 -*-
"""认证摘要和隔离策略执行的无数据库单元测试。"""

from backend.services.auth import hash_password, verify_password
from backend.worker_app import WorkerRunRequest, run


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
