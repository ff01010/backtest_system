"""通过正式 Bar 策略子进程回归最近的普通用户策略，不输出或落盘源码。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from backend.bar_strategy_session import BarStrategySession
from backend.l2.models import NormalizedBar
from backend.l2.strategy_session import L2StrategySession
from backend.services import state_store
from backend.strategies.runtime_contract import RUNTIME_CAPABILITY_DIGEST, SCRIPT_RUNTIME_VERSION


def _recent_samples() -> list[dict[str, Any]]:
  with state_store._db() as conn:
    with conn.cursor() as cur:
      cur.execute("""
        SELECT r.script_snapshot->>'code' AS code,
          COALESCE(r.requested_params->'strategyParams', r.params_snapshot->'runParams', '{}'::jsonb) AS params,
          COALESCE(r.requested_params->>'symbol', '688981.SH') AS symbol,
          COALESCE((r.requested_params->>'initialCapital')::double precision, 100000000) AS initial_capital,
          COALESCE((r.requested_params->>'commissionRate')::double precision, 0) AS commission_rate,
          COALESCE(r.requested_params->>'holdingPolicy', 'overnight') AS holding_policy,
          COALESCE(r.requested_params->>'strategyCategory', 'directional_holding') AS strategy_category,
          COALESCE(r.requested_params->>'overlaySettlementPolicy', 'intraday_restore') AS settlement_policy,
          COALESCE((r.requested_params->>'baseQuantity')::integer, 0) AS base_quantity,
          COALESCE((r.requested_params->>'baseCostPrice')::double precision, 0) AS base_cost_price,
          COALESCE((r.requested_params->>'initialPositionEnabled')::boolean, false) AS initial_position_enabled,
          COALESCE((r.requested_params->>'t0Enabled')::boolean, false) AS t0_enabled
        FROM backtest_run r
        JOIN app_user u ON u.id=r.owner_user_id
        WHERE u.role='user' AND r.status='succeeded'
          AND r.script_snapshot->>'code' IS NOT NULL
        ORDER BY r.created_at DESC
        LIMIT 2
      """)
      return [dict(row) for row in cur.fetchall()]


def _bars(symbol: str, count: int = 260) -> list[dict[str, Any]]:
  start = datetime(2025, 1, 2, 9, 30)
  return [{
    "time": (start + timedelta(days=index)).strftime("%Y%m%d%H%M%S"),
    "symbol": symbol,
    "open": 10.0,
    "high": 10.1,
    "low": 9.9,
    "close": 10.0,
    "volume": 100_000.0,
  } for index in range(count)]


def _payload(sample: dict[str, Any], index: int) -> dict[str, Any]:
  symbol = str(sample["symbol"])
  return {
    "scriptId": f"ordinary-user-sample-{index}",
    "code": str(sample["code"]),
    "params": dict(sample["params"]),
    "bars": _bars(symbol),
    "initialCapital": float(sample["initial_capital"]),
    "commissionRate": float(sample["commission_rate"]),
    "holdingPolicy": str(sample["holding_policy"]),
    "strategyCategory": str(sample["strategy_category"]),
    "overlaySettlementPolicy": str(sample["settlement_policy"]),
    "baseQuantity": int(sample["base_quantity"]),
    "baseCostPrice": float(sample["base_cost_price"]),
    "initialPositionEnabled": bool(sample["initial_position_enabled"]),
    "t0Enabled": bool(sample["t0_enabled"]),
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
  }


def _run_bar_session(
  session: BarStrategySession,
  payload: dict[str, Any],
) -> dict[str, Any]:
  control = dict(payload)
  rows = control.pop("bars")
  control["symbol"] = rows[0]["symbol"]
  frame = pd.DataFrame([
    {key: value for key, value in row.items() if key != "symbol"}
    for row in rows
  ])
  return session.run_script(control, frame)


def _verify_bar_runtime() -> None:
  result = _run_bar_session(BarStrategySession(), {
    "scriptId": "bar-runtime-smoke",
    "code": "def on_bar(bar, state):\n  state['items'] = tuple(enumerate(range(2)))\n  return []\n",
    "params": {},
    "bars": _bars("688981.SH", 2),
    "initialCapital": 100_000,
    "commissionRate": 0,
    "feeProfile": {},
    "slippage": 0,
    "holdingPolicy": "overnight",
    "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
  })
  if len(result["portfolioSnapshots"]) != 2:
    raise ValueError("Bar 子进程未返回完整结果")


def _verify_l2_runtime() -> None:
  session = L2StrategySession({
    "scriptId": "l2-runtime-smoke",
    "code": "def on_bar(bar, state):\n  state['items'] = tuple(enumerate(range(2)))\n  return []\n",
    "params": {}, "initialCapital": 100_000, "commissionRate": 0,
    "feeProfile": {}, "holdingPolicy": "overnight", "symbol": "688981.SH",
    "quoteMode": "order_driven", "runtimeVersion": SCRIPT_RUNTIME_VERSION,
    "capabilityDigest": RUNTIME_CAPABILITY_DIGEST,
    "instrumentRules": {},
  })
  try:
    session.begin_day("2025-01-02")
    session.process_bars([NormalizedBar(
      symbol="688981.SH", trade_date="2025-01-02", period="1m",
      start_time="2025-01-02 09:30:00", end_time="2025-01-02 09:31:00",
      open=10, high=10.1, low=9.9, close=10, volume=100_000, amount=1_000_000,
      quality="complete", source_version="runtime-smoke",
    )])
    session.finish_day("2025-01-02")
    if session.result()["fills"]:
      raise ValueError("L2 运行时冒烟产生了意外成交")
  finally:
    session.close()


def main() -> None:
  _verify_bar_runtime()
  print("bar-runtime=passed")
  _verify_l2_runtime()
  print("l2-runtime=passed")
  samples = _recent_samples()
  succeeded = 0
  for index, sample in enumerate(samples, start=1):
    try:
      result = _run_bar_session(BarStrategySession(), _payload(sample, index))
      if not isinstance(result.get("portfolioSnapshots"), list):
        raise ValueError("Bar 策略子进程未返回最终账户快照")
    except Exception as error:
      print(f"sample-{index}=failed:{type(error).__name__}")
    else:
      succeeded += 1
      print(f"sample-{index}=passed")
  print(f"summary={succeeded}/{len(samples)}")
  raise SystemExit(0 if len(samples) == 2 and succeeded == 2 else 1)


if __name__ == "__main__":
  main()
